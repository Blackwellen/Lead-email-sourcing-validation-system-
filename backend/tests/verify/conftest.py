import pytest

from app.config import Settings
from app.verify.smtp import SmtpProbe

from .smtpserver import ScenarioServer

OWNED = "owned-domain.co.uk"


@pytest.fixture
def smtp_server():
    made: list[ScenarioServer] = []

    def make(with_tls: bool = False) -> ScenarioServer:
        s = ScenarioServer(with_tls=with_tls)
        made.append(s)
        return s

    yield make
    for s in made:
        s.stop()


@pytest.fixture
def probe_settings() -> Settings:
    return Settings(smtp_diagnostics_enabled=True, smtp_authorised_domains=OWNED, smtp_mail_from=f"verify@{OWNED}", smtp_helo_host=f"probe.{OWNED}")


@pytest.fixture
def make_probe(probe_settings):
    sleeps: list[float] = []

    async def fake_sleep(x: float) -> None:
        sleeps.append(x)

    def make(server: ScenarioServer, **kw) -> SmtpProbe:
        kw.setdefault("connect_timeout", 2.0)
        kw.setdefault("command_timeout", 1.0)
        kw.setdefault("settings", probe_settings)
        p = SmtpProbe(target_override=lambda h: ("127.0.0.1", server.port), sleep=fake_sleep, **kw)
        p.sleeps = sleeps  # type: ignore[attr-defined]
        return p

    return make
