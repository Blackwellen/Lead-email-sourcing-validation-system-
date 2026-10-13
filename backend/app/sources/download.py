"""Streamed, resumable, validator-aware file download (Range + ETag/Last-Modified), disk-space sanity checks."""
from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..config import get_settings
from ..crawler.safe_http import DnsFailure, SSRFBlocked, safe_client
from .base import AdapterError


class DownloadError(AdapterError):
    pass


@dataclass
class DownloadResult:
    path: Path
    downloaded_bytes: int
    resumed: bool
    skipped_unchanged: bool
    etag: str | None
    last_modified: str | None
    size: int


def _user_agent() -> str:
    s = get_settings()
    ua = s.user_agent
    if s.contact_email:
        ua = ua.replace("set LEAD_CONTACT_EMAIL", s.contact_email)
    return ua


def make_client(transport: httpx.AsyncBaseTransport | None = None, *, timeout: float = 60.0) -> httpx.AsyncClient:
    """SSRF-safe client (every redirect hop validated, connections pinned to validated public addresses).
    ``transport`` is the test seam (``httpx.MockTransport``)."""
    return safe_client(transport=transport, timeout=timeout, headers={"User-Agent": _user_agent()})


async def download_file(
    url: str,
    dest: Path,
    *,
    client: httpx.AsyncClient | None = None,
    max_bytes: int = 20 * 1024**3,
    min_free_factor: float = 1.15,
    chunk_size: int = 1024 * 256,
    resume: bool = True,
    on_progress: Callable[[int, int | None], None] | None = None,
) -> DownloadResult:
    """Download ``url`` to ``dest`` without ever holding the body in RAM.

    * A ``dest.part`` file plus ``dest.meta.json`` (validators) enables resume via ``Range`` + ``If-Range``.
    * If ``dest`` already exists and the server's ETag/Last-Modified are unchanged, nothing is downloaded.
    * Refuses when Content-Length exceeds ``max_bytes`` or free disk < size * ``min_free_factor``.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    meta_path = dest.with_name(dest.name + ".meta.json")
    meta: dict = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            meta = {}
    own = client is None
    cli = client or make_client()
    try:
        return await _download(cli, url, dest, part, meta_path, meta, max_bytes, min_free_factor, chunk_size, resume, on_progress)
    except (SSRFBlocked, DnsFailure) as e:
        raise DownloadError(f"refusing to download {url.split('?')[0]}: {e}") from None
    finally:
        if own:
            await cli.aclose()


async def _download(cli: httpx.AsyncClient, url: str, dest: Path, part: Path, meta_path: Path, meta: dict, max_bytes: int,
                    min_free_factor: float, chunk_size: int, resume: bool, on_progress: Callable[[int, int | None], None] | None) -> DownloadResult:
    head = await cli.head(url)
    if head.status_code in (405, 501):
        head = None  # server dislikes HEAD; fall through to GET
    elif head.status_code >= 400:
        raise DownloadError(f"HEAD {url} -> HTTP {head.status_code}")
    etag = head.headers.get("etag") if head is not None else None
    lm = head.headers.get("last-modified") if head is not None else None
    total = int(head.headers["content-length"]) if head is not None and "content-length" in head.headers else None

    if dest.exists() and meta and ((etag and meta.get("etag") == etag) or (lm and not etag and meta.get("last_modified") == lm)):
        return DownloadResult(dest, 0, False, True, etag, lm, dest.stat().st_size)

    if total is not None:
        if total > max_bytes:
            raise DownloadError(f"refusing {total} byte download (max {max_bytes})")
        free = shutil.disk_usage(dest.parent).free
        have = part.stat().st_size if part.exists() else 0
        if free < (total - have) * min_free_factor:
            raise DownloadError(f"insufficient disk space: need ~{total - have}, free {free}")

    headers: dict[str, str] = {}
    offset = 0
    resumed = False
    if resume and part.exists() and meta.get("partial_for") == (etag or lm or "") and part.stat().st_size > 0:
        offset = part.stat().st_size
        if total is not None and offset > total:
            offset = 0
        if offset:
            headers["Range"] = f"bytes={offset}-"
            if etag or lm:
                headers["If-Range"] = etag or lm or ""
            resumed = True
    elif part.exists():
        part.unlink()

    meta_path.write_text(json.dumps({"partial_for": etag or lm or "", "url": url}))
    async with cli.stream("GET", url, headers=headers) as resp:
        if resp.status_code == 416 and total is not None and offset == total:
            pass  # already complete
        elif resp.status_code == 206 and resumed:
            pass
        elif resp.status_code == 200:
            if resumed:  # server ignored Range / validator changed -> restart cleanly
                offset = 0
                resumed = False
            if "content-length" in resp.headers and int(resp.headers["content-length"]) > max_bytes:
                raise DownloadError("content-length exceeds max_bytes")
        else:
            raise DownloadError(f"GET {url} -> HTTP {resp.status_code}")
        mode = "ab" if (resumed and offset) else "wb"
        got = 0
        if resp.status_code != 416:
            with open(part, mode) as fh:
                async for chunk in resp.aiter_bytes(chunk_size):
                    got += len(chunk)
                    if offset + got > max_bytes:
                        raise DownloadError("download exceeded max_bytes")
                    fh.write(chunk)
                    if on_progress:
                        on_progress(offset + got, total)
    size = part.stat().st_size
    if total is not None and size != total:
        raise DownloadError(f"incomplete download: {size} of {total} bytes (resume possible)")
    os.replace(part, dest)
    meta_path.write_text(json.dumps({"etag": etag, "last_modified": lm, "url": url, "size": size}))
    return DownloadResult(dest, got, resumed, False, etag, lm, size)
