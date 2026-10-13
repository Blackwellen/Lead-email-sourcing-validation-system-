"""Safe upload helpers: bounded reads and magic-byte sniffing."""
from __future__ import annotations

import io
import zipfile

from fastapi import UploadFile

from ..security import ApiError

CHUNK = 64 * 1024


async def read_limited(file: UploadFile, max_bytes: int) -> bytes:
    buf = bytearray()
    while True:
        chunk = await file.read(CHUNK)
        if not chunk:
            break
        buf += chunk
        if len(buf) > max_bytes:
            raise ApiError(413, "payload_too_large", f"The file is larger than the {max_bytes // (1024 * 1024) or 1} MB limit.")
    if not buf:
        raise ApiError(422, "empty_file", "The uploaded file is empty.")
    return bytes(buf)


BAD_MAGIC = (b"MZ", b"\x7fELF", b"%PDF", b"\x1f\x8b", b"Rar!", b"7z\xbc\xaf", b"\xca\xfe\xba\xbe", b"#!", b"<?php", b"<html", b"<!DOC", b"<script", b"\xd0\xcf\x11\xe0")


def sniff(data: bytes) -> str:
    """Return 'xlsx' | 'text' or raise 422 for anything else (executables, archives, HTML, legacy OLE, binaries)."""
    head = data[:8]
    if head[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = z.namelist()
                if len(names) > 200 or "[Content_Types].xml" not in names or not any(n.startswith("xl/") for n in names):
                    raise ApiError(422, "unsupported_file", "That archive is not a valid Excel (.xlsx) workbook.")
                if any(n.lower().endswith(("vbaproject.bin", ".exe", ".dll", ".js", ".sh")) for n in names):
                    raise ApiError(422, "unsupported_file", "Workbooks with macros or embedded programs are not accepted.")
                if sum(i.file_size for i in z.infolist()) > 300 * 1024 * 1024:
                    raise ApiError(422, "unsupported_file", "The workbook expands to an unreasonable size.")
        except zipfile.BadZipFile:
            raise ApiError(422, "unsupported_file", "That file is not a valid Excel (.xlsx) workbook.") from None
        return "xlsx"
    if any(data.lstrip()[:12].lower().startswith(m.lower()) for m in BAD_MAGIC) or any(data.startswith(m) for m in BAD_MAGIC):
        raise ApiError(422, "unsupported_file", "Only CSV, JSON or Excel (.xlsx) files are accepted; this file looks like something else.")
    sample = data[:65536]
    if b"\x00" in sample:
        raise ApiError(422, "unsupported_file", "This file contains binary data and is not a text CSV/JSON file.")
    try:
        sample.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        if e.start < len(sample) - 4:
            raise ApiError(422, "unsupported_file", "The file is not valid UTF-8 text. Save it as UTF-8 CSV and try again.") from None
    return "text"
