"""Local controllable SMTP server (aiosmtpd) that records every verb it receives."""
from __future__ import annotations

import asyncio
import datetime
import socket
import ssl
import tempfile
from collections import Counter
from collections.abc import Callable

from aiosmtpd.controller import Controller
from aiosmtpd.smtp import SMTP


def _self_signed_context() -> ssl.SSLContext:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
    d = tempfile.mkdtemp()
    with open(f"{d}/c.pem", "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(f"{d}/k.pem", "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(f"{d}/c.pem", f"{d}/k.pem")
    return ctx


class Scenario:
    """Behaviour knobs + recorded observations."""

    def __init__(self) -> None:
        self.verbs: list[str] = []
        self.connections = 0
        self.rcpt_attempts: Counter[str] = Counter()
        self.rcpt_rule: Callable[[str, int], str] = lambda addr, n: "250 2.1.5 OK"
        self.mail_reply: str | None = None
        self.rcpt_delay = 0.0
        self.rcpt_disconnect = False
        self.starttls: str | None = None  # None | 'fail' | 'ok'
        self.rcpt_addresses: list[str] = []

    def reset(self) -> None:
        self.verbs.clear()
        self.connections = 0
        self.rcpt_attempts.clear()
        self.rcpt_addresses.clear()


class _Handler:
    def __init__(self, sc: Scenario) -> None:
        self.sc = sc

    async def handle_MAIL(self, server, session, envelope, address, mail_options):
        if self.sc.mail_reply:
            return self.sc.mail_reply
        envelope.mail_from = address
        return "250 2.1.0 OK"

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        sc = self.sc
        sc.rcpt_attempts[address] += 1
        sc.rcpt_addresses.append(address)
        if sc.rcpt_disconnect:
            server.transport.close()
            return "250 OK"
        if sc.rcpt_delay:
            await asyncio.sleep(sc.rcpt_delay)
        reply = sc.rcpt_rule(address, sc.rcpt_attempts[address])
        if reply.startswith("250"):
            envelope.rcpt_tos.append(address)
        return reply

    async def handle_DATA(self, server, session, envelope):  # must never be reached
        self.sc.verbs.append("DATA-HANDLED")
        return "250 OK"


class _RecordingSMTP(SMTP):
    def __init__(self, handler, scenario: Scenario, **kw):
        super().__init__(handler, **kw)
        self.sc = scenario
        wrapped = {}
        for verb, fn in self._smtp_methods.items():
            wrapped[verb] = self._wrap(verb, fn)
        self._smtp_methods = wrapped

    def _wrap(self, verb, fn):
        async def inner(arg):
            self.sc.verbs.append(verb)
            if verb == "STARTTLS" and self.sc.starttls == "fail":
                await self.push("220 2.0.0 Ready to start TLS")
                self.transport.close()
                return None
            return await fn(arg)
        return inner

    def connection_made(self, transport):
        self.sc.connections += 1
        super().connection_made(transport)


class _Ctl(Controller):
    def __init__(self, scenario: Scenario, tls_ctx, **kw):
        self.scenario = scenario
        self.tls_ctx = tls_ctx
        super().__init__(_Handler(scenario), **kw)

    def factory(self):
        return _RecordingSMTP(self.handler, self.scenario, tls_context=self.tls_ctx, enable_SMTPUTF8=True, timeout=5)


class ScenarioServer:
    def __init__(self, with_tls: bool = False) -> None:
        self.scenario = Scenario()
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.ctl = _Ctl(self.scenario, _self_signed_context() if with_tls else None, hostname="127.0.0.1", port=self.port)
        self.ctl.start()
        self.scenario.reset()  # the controller's readiness probe made a connection

    def stop(self) -> None:
        self.ctl.stop()

    @property
    def verbs(self) -> list[str]:
        return self.scenario.verbs

    @property
    def connections(self) -> int:
        return self.scenario.connections


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p
