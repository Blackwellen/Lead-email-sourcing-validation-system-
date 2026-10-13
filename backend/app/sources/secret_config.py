"""Adapter configuration with secrets sealed at rest.

Secret-looking keys (api keys, tokens, passwords ...) are stored as ``enc:v1:<fernet token>`` using the application's Fernet
helper (``app.security``); everything else is stored as given. API responses show a mask, never the value; the worker opens the
sealed values when it instantiates the adapter (:func:`open_config`)."""
from __future__ import annotations

import re
from typing import Any

from ..security import decrypt_secret, encrypt_secret

# Secret-looking configuration keys: api keys in all spellings (api_key, apikey, subscription_key, x_api_key ...), any
# *_key / key, secrets, tokens, passwords (password, passwd, pwd, pass, passphrase), credentials, auth / authorization /
# bearer values, GUIDs (ABN Lookup), subscription ids/keys, signatures, cookies and session ids. ``authority_codes`` /
# ``authorised`` are deliberately NOT matched.
SECRET_KEY_RE = re.compile(
    r"(api_?key|apikey|(^|_)keys?($|_)|secret|token|passw|passphrase|(^|_)pwd($|_)|(^|_)pass($|_)|credential|"
    r"(^|_)auth($|_)|authori[sz]ation|bearer|guid|subscription|signature|(^|_)sig($|_)|cookie|session_?id|private|access_?key)",
    re.I,
)
PREFIX = "enc:v1:"
MASK = "********"


def is_sealed(v: Any) -> bool:
    return isinstance(v, str) and v.startswith(PREFIX)


def is_secret_key(k: Any) -> bool:
    return bool(SECRET_KEY_RE.search(str(k)))


def seal_value(v: str) -> str:
    return v if is_sealed(v) else PREFIX + encrypt_secret(v)


def open_value(v: Any) -> Any:
    if is_sealed(v):
        return decrypt_secret(v[len(PREFIX):])  # None when the key changed: the adapter then reports 'not configured'
    return v


def open_config(cfg: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (cfg or {}).items():
        out[k] = open_config(v) if isinstance(v, dict) else open_value(v)
    return out


def mask_config(cfg: dict[str, Any] | None) -> dict[str, Any]:
    def m(k: str, v: Any) -> Any:
        if isinstance(v, dict):
            return {kk: m(kk, vv) for kk, vv in v.items()}
        return MASK if (is_sealed(v) or SECRET_KEY_RE.search(k)) and v not in (None, "", False) else v

    return {k: m(k, v) for k, v in (cfg or {}).items()}


def secret_keys(cfg: dict[str, Any] | None) -> list[str]:
    return sorted(k for k, v in (cfg or {}).items() if is_sealed(v) or (SECRET_KEY_RE.search(k) and v not in (None, "", False)))
