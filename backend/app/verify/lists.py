"""V05 -- disposable / free-provider / role-account classification.

Data lives in ``app/data``. The disposable list is only a *seed*; call :func:`refresh_from_url`
(operator action, never at import) to refresh it from the open-source ``disposable-email-domains``
project. The role dictionary is operator-configurable via :func:`set_role_dictionary`.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DISPOSABLE_FILE = DATA_DIR / "disposable_domains.txt"
FREE_FILE = DATA_DIR / "free_providers.txt"
ROLE_FILE = DATA_DIR / "role_accounts.txt"
DEFAULT_REFRESH_URL = (
    "https://raw.githubusercontent.com/disposable-email-domains/disposable-email-domains/main/"
    "disposable_email_blocklist.conf"
)

_role_override: dict[str, str] | None = None


def _read_domains(path: Path) -> frozenset[str]:
    out: set[str] = set()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip().lower()
            if line:
                out.add(line)
    except FileNotFoundError:
        pass
    return frozenset(out)


@lru_cache(maxsize=1)
def disposable_domains() -> frozenset[str]:
    return _read_domains(DISPOSABLE_FILE)


@lru_cache(maxsize=1)
def free_providers() -> frozenset[str]:
    return _read_domains(FREE_FILE)


def _parents(domain: str) -> list[str]:
    d = domain.lower().strip().rstrip(".")
    parts = d.split(".")
    return [".".join(parts[i:]) for i in range(len(parts) - 1)]


def is_disposable(domain: str) -> bool:
    """True when ``domain`` or a parent domain is on the disposable list."""
    s = disposable_domains()
    return any(p in s for p in _parents(domain))


def is_free_provider(domain: str) -> bool:
    """True when the domain is a consumer mailbox provider (gmail, outlook, btinternet ...)."""
    d = domain.lower().strip().rstrip(".")
    return d in free_providers()


def _norm_local(local: str) -> str:
    return re.sub(r"[.\-_+]", "", local.lower().split("+", 1)[0])


@lru_cache(maxsize=1)
def _file_roles() -> dict[str, str]:
    mapping: dict[str, str] = {}
    try:
        for line in ROLE_FILE.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            rtype, _, names = line.partition("\t")
            for n in names.split(","):
                n = _norm_local(n.strip())
                if n:
                    mapping.setdefault(n, rtype.strip())
    except FileNotFoundError:
        pass
    return mapping


def set_role_dictionary(mapping: dict[str, str] | None) -> None:
    """Replace the role dictionary (``{local_part: role_type}``); ``None`` restores the file default."""
    global _role_override
    _role_override = None if mapping is None else {_norm_local(k): v for k, v in mapping.items()}


def classify_role(local: str) -> str | None:
    """Return the role type for a role-style local part (``info`` -> ``generic``) or None."""
    key = _norm_local(local)
    table = _role_override if _role_override is not None else _file_roles()
    return table.get(key)


def clear_caches() -> None:
    disposable_domains.cache_clear()
    free_providers.cache_clear()
    _file_roles.cache_clear()


def parse_blocklist(text: str) -> list[str]:
    """Parse a one-domain-per-line blocklist, ignoring comments and junk."""
    out = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip().lower()
        if line and re.fullmatch(r"[a-z0-9._-]+\.[a-z0-9-]{2,}", line):
            out.append(line)
    return out


async def refresh_from_url(url: str = DEFAULT_REFRESH_URL, *, client=None, timeout: float = 20.0, path: Path | None = None) -> int:
    """Download an updated disposable-domain list and merge it into the data file.

    Returns the number of domains in the merged file. Merges with the existing seed so a bad/empty
    download never shrinks the list. Operator-triggered only.
    """

    own = client is None
    if client is None:
        from ..crawler.safe_http import safe_client

        client = safe_client(timeout=timeout)
    try:
        r = await client.get(url)
        r.raise_for_status()
        fresh = parse_blocklist(r.text)
    finally:
        if own:
            await client.aclose()
    if len(fresh) < 50:
        raise ValueError(f"refusing to update: downloaded list looks truncated ({len(fresh)} domains)")
    target = path or DISPOSABLE_FILE
    merged = sorted(set(fresh) | set(_read_domains(target)))
    header = (
        "# Disposable e-mail domains. Seed merged with the open-source `disposable-email-domains` list\n"
        f"# (source: {url}). Refresh with app.verify.lists.refresh_from_url().\n"
    )
    tmp = target.with_suffix(".tmp")
    tmp.write_text(header + "\n".join(merged) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    clear_caches()
    return len(merged)
