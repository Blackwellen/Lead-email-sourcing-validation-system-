"""Streaming helpers shared by the discovery adapters (no adapter registers anything here).

* :func:`iter_json_array` -- incremental parser for a huge JSON array (top-level, or the value of one key such as
  GLEIF's ``{"records": [...]}``); memory is bounded by the largest single element, never by the file.
* :func:`open_member` / :func:`list_members` -- read a member of a ``.zip`` (or a plain / ``.gz`` file) as a binary
  stream without extracting to disk.
* :func:`ensure_local_file` -- config ``path`` / ``url`` resolution with the shared resumable downloader.
* :func:`gb_company_number` -- Companies House number normalisation so OCDS/charity/LEI records join up with
  the ``companies_house_bulk`` identity (``GB:cn:<8 chars>``).
"""
from __future__ import annotations

import codecs
import contextlib
import gzip
import json
import re
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO

import httpx

from ..config import get_settings
from .base import NotConfigured
from .download import download_file


def iter_json_array(fh: BinaryIO, *, key: str | None = None, chunk_size: int = 1 << 20) -> Iterator[Any]:
    """Yield the elements of a JSON array read from ``fh`` (binary) incrementally.

    ``key`` selects ``{"<key>": [ ... ]}`` documents.  Elements may be any JSON value but are expected to be
    objects.  Raises ``ValueError`` on truncated / malformed input.
    """
    dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
    jd = json.JSONDecoder()
    buf = ""
    eof = False

    def fill() -> bool:
        nonlocal buf, eof
        if eof:
            return False
        data = fh.read(chunk_size)
        if not data:
            eof = True
            buf += dec.decode(b"", final=True)
            return False
        buf += dec.decode(data)
        return True

    # -- locate the array start
    if key is None:
        while True:
            buf = buf.lstrip("﻿ \t\r\n")
            if buf:
                break
            if not fill():
                return
        if buf[0] != "[":
            raise ValueError("expected a top-level JSON array")
        pos = 1
    else:
        pat = re.compile(r'"' + re.escape(key) + r'"\s*:\s*\[')
        while True:
            m = pat.search(buf)
            if m:
                pos = m.end()
                break
            if len(buf) > 2 * chunk_size:  # keep only a tail that could still hold the start of the pattern
                buf = buf[-(len(key) + 64):]
            if not fill():
                m = pat.search(buf)
                if m:
                    pos = m.end()
                    break
                raise ValueError(f"key {key!r} with an array value not found")
    # -- elements
    while True:
        # skip whitespace / commas
        while True:
            while pos < len(buf) and buf[pos] in " \t\r\n,":
                pos += 1
            if pos < len(buf):
                break
            buf, pos = "", 0
            if not fill():
                raise ValueError("unexpected end of JSON array")
        if buf[pos] == "]":
            return
        while True:
            try:
                obj, end = jd.raw_decode(buf, pos)
                break
            except json.JSONDecodeError as exc:
                if not fill():
                    raise ValueError(f"malformed JSON element near offset {pos}: {exc}") from exc
        yield obj
        pos = end
        if pos > chunk_size:  # compact
            buf, pos = buf[pos:], 0


# --------------------------------------------------------------------------- archive members
def list_members(path: Path, suffixes: tuple[str, ...]) -> list[str]:
    """Sorted member names of a zip matching ``suffixes`` (case-insensitive); a plain file yields its own name."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            return sorted(i.filename for i in zf.infolist() if not i.is_dir() and i.filename.lower().endswith(suffixes))
    return [path.name]


@contextlib.contextmanager
def open_member(path: Path, member: str | None = None, suffixes: tuple[str, ...] = ()):
    """Context manager yielding ``(binary_stream, member_name)`` for zip members, ``.gz`` or plain files."""
    if zipfile.is_zipfile(path):
        zf = zipfile.ZipFile(path)
        try:
            names = [i.filename for i in zf.infolist() if not i.is_dir()]
            if member:
                names = [n for n in names if n == member]
            elif suffixes:
                names = sorted(n for n in names if n.lower().endswith(suffixes))
            if not names:
                raise NotConfigured(f"no matching member in {path}")
            with zf.open(names[0]) as fh:
                yield fh, names[0]
        finally:
            zf.close()
        return
    if path.suffix.lower() == ".gz":
        with gzip.open(path, "rb") as fh:
            yield fh, path.name[:-3]
        return
    with open(path, "rb") as fh:
        yield fh, path.name


async def ensure_local_file(
    config: dict[str, Any], adapter_id: str, *, client: httpx.AsyncClient | None = None, path_key: str = "path",
    url_key: str = "url", max_bytes: int = 20 * 1024**3,
) -> Path:
    """Config ``path`` (must exist) or ``url`` (downloaded with resume + validators into the data dir)."""
    p = config.get(path_key)
    if p:
        path = Path(p)
        if not path.exists():
            raise NotConfigured(f"configured {path_key} does not exist: {path}")
        return path
    url = config.get(url_key)
    if not url:
        raise NotConfigured(f"set config '{path_key}' (local file) or '{url_key}' (download URL)")
    dest = Path(config.get("data_dir") or get_settings().data_dir) / adapter_id / (url.rsplit("/", 1)[-1].split("?")[0] or "download")
    res = await download_file(url, dest, client=client or config.get("http_client"), max_bytes=int(config.get("max_bytes", max_bytes)))
    return res.path


_GB_NUM = re.compile(r"^(?:[A-Z]{1,2}\d{5,6}|\d{1,8})$")


def gb_company_number(raw: str | None) -> str | None:
    """Normalise a Companies House number: digits are zero-padded to 8 (``6`` -> ``00000006``), prefixes kept."""
    if not raw:
        return None
    s = re.sub(r"\s+", "", str(raw)).upper()
    if not _GB_NUM.match(s):
        return None
    return s.zfill(8) if s.isdigit() else s


def parse_dt(s: str | None):
    """Lenient ISO-ish date/datetime -> aware UTC datetime (or None)."""
    from datetime import UTC, datetime

    if not s:
        return None
    t = str(s).strip()
    if not t:
        return None
    t = t.replace("Z", "+00:00")
    t = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", t) if re.search(r"[+-]\d{4}$", t) and "T" in t else t
    for cand in (t, t.split(".")[0] if "." in t and "T" in t and "+" not in t else t):
        try:
            d = datetime.fromisoformat(cand)
            return d if d.tzinfo else d.replace(tzinfo=UTC)
        except ValueError:
            continue
    # '2026-08-31+02:00' (TED) and 'YYYYMMDD'
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", t)
    if m:
        try:
            return datetime.fromisoformat(m.group(1)).replace(tzinfo=UTC)
        except ValueError:
            return None
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", t)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=UTC)
        except ValueError:
            return None
    return None
