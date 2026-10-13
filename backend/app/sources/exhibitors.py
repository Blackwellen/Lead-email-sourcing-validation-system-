"""Exhibitor / sponsor / speaker list import (DISC-05) -- permitted datasets only, no HTML scraping.

``exhibitor_list_import`` reads an operator-provided file (CSV / XLSX / JSON array, optionally zipped) or a configured
JSON / CSV feed URL and turns every row into

* a :class:`BusinessRecord` (name, optional website as a CANDIDATE domain, never confirmed), and
* a participation signal: ``exhibitor`` or ``sponsor`` (``event`` for speakers) with ``detail`` = event name / date /
  stand / event URL / role, ``source_url`` = the URL of the published list (``list_url``), so the evidence is retained.

Permission: a local file is the operator's own data (``permission_basis`` optional but recorded).  A feed ``url`` requires
``permission_basis`` (``"open_licence: ..."``, ``"own_feed: ..."`` or ``"written: ..."``) -- default deny -- and a robots.txt
check before the download.  Scraping exhibitor HTML pages of restricted sites is out of scope by design.

Accepted column names (case/space/punctuation-insensitive): company | company_name | exhibitor | organisation | name;
website | web | company_website | domain; stand | booth | stand_number | stand_no; event | event_name | show;
event_date | date | dates; event_url | show_url | list_url; role | type | participation (exhibitor|sponsor|speaker);
country | country_code; city | town; postcode | postal_code | zip.  ``config['columns']`` overrides: ``{"company": "Firm"}``.
"""
from __future__ import annotations

import csv
import io
import re
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._stream import iter_json_array, open_member
from .base import (
    AdapterHealth,
    AdapterInfo,
    BusinessRecord,
    Checkpoint,
    DiscoveryQuery,
    NotConfigured,
    NotPermitted,
    RatePolicy,
    SourceAdapter,
    register,
)
from .download import download_file
from .geo_filters import RecordFilter, normalise_postcode
from .industry_directories import DirectoryFetcher
from .persist import normalise_name
from .signals_util import make_signal

ADAPTER_ID = "exhibitor_list_import"
ROLES = ("exhibitor", "sponsor", "speaker")
_ALIASES: dict[str, tuple[str, ...]] = {
    "company": ("company", "company_name", "exhibitor", "exhibitor_name", "organisation", "organization", "name", "firm", "sponsor"),
    "website": ("website", "web", "company_website", "domain", "web_address", "site", "company_url"),
    "stand": ("stand", "booth", "stand_number", "stand_no", "stand_id", "hall_stand"),
    "event_name": ("event", "event_name", "show", "show_name", "exhibition", "conference"),
    "event_date": ("event_date", "date", "dates", "event_dates", "start_date"),
    "event_url": ("event_url", "show_url", "list_url", "event_link", "source_url"),
    "role": ("role", "type", "participation", "category", "level"),
    "country": ("country", "country_code", "iso_country"),
    "city": ("city", "town"),
    "postcode": ("postcode", "postal_code", "zip", "zip_code", "post_code"),
}
_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y", "%Y/%m/%d", "%d.%m.%Y")


def _norm_key(k: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(k).strip().lower()).strip("_")


def parse_event_date(s: str | None) -> datetime | None:
    """First date in free text ('2026-03-10', '10/03/2026', '10 March 2026', '2026-03-10 to 2026-03-12')."""
    if not s:
        return None
    t = str(s).strip()
    cands = [t, re.split(r"\s+(?:to|-|–|—)\s+", t)[0], t[:10]]
    for c in cands:
        c = c.strip()
        for f in _DATE_FORMATS:
            try:
                return datetime.strptime(c, f).replace(tzinfo=UTC)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(c.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            continue
    return None


def normalise_role(v: str | None) -> str:
    t = (v or "").strip().lower()
    if not t:
        return "exhibitor"
    if "sponsor" in t or t in ("partner", "gold", "silver", "bronze", "headline", "principal"):
        return "sponsor"
    if "speak" in t or "presenter" in t or "panel" in t:
        return "speaker"
    return "exhibitor"


def _column_map(header: list[str], overrides: dict[str, str] | None) -> dict[str, str]:
    """canonical field -> actual column name."""
    norm = {_norm_key(h): h for h in header}
    out: dict[str, str] = {}
    for field_, aliases in _ALIASES.items():
        ov = (overrides or {}).get(field_)
        if ov and ov in header:
            out[field_] = ov
            continue
        for a in aliases:
            if a in norm:
                out[field_] = norm[a]
                break
    return out


def _iter_dict_rows(path: Path, fmt: str, cfg: dict[str, Any], skip: int) -> Iterator[tuple[int, dict[str, str]]]:
    if fmt == "xlsx":
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            ws = wb[cfg["sheet"]] if cfg.get("sheet") else wb.worksheets[0]
            it = ws.iter_rows(values_only=True)
            header = [str(h).strip() if h is not None else "" for h in (next(it, None) or [])]
            for n, row in enumerate(it):
                if n < skip or row is None:
                    continue
                yield n, {header[i]: ("" if v is None else str(v).strip()) for i, v in enumerate(row) if i < len(header) and header[i]}
        finally:
            wb.close()
        return
    with open_member(path, cfg.get("member"), (".csv", ".json", ".txt")) as (fh, name):
        if fmt == "json" or name.lower().endswith(".json"):
            for n, obj in enumerate(iter_json_array(fh, key=cfg.get("record_path"))):
                if n < skip or not isinstance(obj, dict):
                    continue
                yield n, {str(k): ("" if v is None else str(v)) for k, v in obj.items() if not isinstance(v, dict | list)}
            return
        rd = csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig", errors="replace", newline=""), delimiter=cfg.get("delimiter", ","))
        for n, row in enumerate(rd):
            if n < skip:
                continue
            yield n, {k: (v or "").strip() for k, v in row.items() if k}


def detect_fmt(path: Path, fmt: str | None) -> str:
    if fmt and fmt != "auto":
        return fmt.lower()
    s = path.name.lower()
    return "xlsx" if s.endswith((".xlsx", ".xlsm")) else "json" if s.endswith(".json") else "csv"


@register
class ExhibitorListImport(SourceAdapter):
    info = AdapterInfo(
        adapter_id=ADAPTER_ID,
        name="Exhibitor / sponsor list import (operator file or permitted feed)",
        country_coverage=("*",),
        source_type="manual_import",
        licence="Per list -- the operator is responsible for having the right to use the list",
        source_url="",  # no fixed source: operator-provided lists
        access_requirements=("Config: 'path' (CSV/XLSX/JSON) or 'url' (JSON/CSV feed; needs 'permission_basis'); 'country_code' (default country), "
                             "'list_url' (public URL of the published list, used as the signal source), optional 'event_name' / 'event_date' defaults and 'columns' overrides."),
        automated_collection_permitted=True,
        rate_policy=RatePolicy(requests_per_minute=None, concurrency=1),
        fields_available=("legal_name", "website", "stand", "event_name", "event_date", "event_url", "role"),
        incremental_support=True,
        requires_config=True,
        cost="free", credentials="none", refresh_frequency="per event / on import",
        access_method="manual_import", industries=("events", "trade_shows", "*"),
        allowed_usage="Only lists the operator may lawfully use. No scraping of restricted exhibitor sites; feed URLs need a recorded permission basis and pass robots.txt.",
        notes="Produces exhibitor/sponsor signals (speakers -> 'event') whose source_url is the published list URL.",
    )

    async def health(self) -> AdapterHealth:
        p = self.config.get("path")
        if p:
            ok = Path(p).exists()
            return AdapterHealth(ok, "ok" if ok else "unavailable", f"{'present' if ok else 'missing'}: {p}")
        if self.config.get("url"):
            return AdapterHealth(bool(self.config.get("permission_basis")), "ok" if self.config.get("permission_basis") else "not_configured",
                                 "feed URL configured" + ("" if self.config.get("permission_basis") else "; permission_basis missing"))
        return AdapterHealth(False, "not_configured", "set 'path' or 'url'")

    async def _acquire(self) -> tuple[Path, str | None]:
        p = self.config.get("path")
        if p:
            path = Path(p)
            if not path.exists():
                raise NotConfigured(f"configured path does not exist: {path}")
            return path, self.config.get("list_url")
        url = self.config.get("url")
        if not url:
            raise NotConfigured("set 'path' (operator file) or 'url' (permitted feed)")
        pb = str(self.config.get("permission_basis") or "")
        if len(pb.strip()) < 10 or not re.match(r"^\s*(open_licence|own_feed|written)\s*:", pb, re.I):
            raise NotPermitted("a feed URL needs permission_basis='open_licence: ...' | 'own_feed: ...' | 'written: ...' (default deny)")
        if self.config.get("check_robots", True):
            df = self.config.get("directory_fetcher") or DirectoryFetcher(self.config.get("fetcher"), self.config.get("robots"))
            await df.check(url)
        from ..config import get_settings

        dest = Path(self.config.get("data_dir") or get_settings().data_dir) / "exhibitors" / (url.rsplit("/", 1)[-1].split("?")[0] or "feed")
        res = await download_file(url, dest, client=self.config.get("http_client"), max_bytes=int(self.config.get("max_bytes", 100 * 1024**2)))
        return res.path, self.config.get("list_url") or url

    def row_to_record(self, row: dict[str, str], cmap: dict[str, str], list_url: str | None, index: int,
                      observed_at: datetime) -> BusinessRecord | None:
        def g(f: str) -> str | None:
            c = cmap.get(f)
            v = row.get(c, "") if c else ""
            return v.strip() or None

        name = g("company")
        cc = (g("country") or self.config.get("country_code") or "").upper()
        if not name or len(cc) != 2:
            return None
        role = normalise_role(g("role"))
        event = g("event_name") or self.config.get("event_name")
        edate_raw = g("event_date") or self.config.get("event_date")
        edate = parse_event_date(edate_raw)
        stand = g("stand")
        event_url = g("event_url") or self.config.get("event_url")
        src = list_url or event_url
        pc = g("postcode")
        ekey = normalise_name(event) if event else ""
        rec = BusinessRecord(
            source_ref=f"{ekey}|{normalise_name(name)}|{role}"[:300] or f"row{index}",
            legal_name=name,
            status="active",
            country_code=cc,
            city=g("city"),
            postal_code=normalise_postcode(pc, cc) if pc else None,
            website=g("website"),
            category_hint=f"{role}",
            source_url=src,
            licence=self.config.get("licence"),
            attribution=self.config.get("attribution"),
            observed_at=observed_at,
            raw={
                "participation": {"role": role, "event_name": event, "event_date": edate_raw, "stand": stand, "event_url": event_url},
                "event_key": ekey or None,
                "relationships": ([{"relation": "exhibits_with", "event_key": ekey, "event_name": event, "source_url": src,
                                    "confidence": 0.8}] if ekey and role == "exhibitor" and src else []),
            },
        )
        if src:
            stype = "exhibitor" if role == "exhibitor" else "sponsor" if role == "sponsor" else "event"
            verb = {"exhibitor": "Exhibiting", "sponsor": "Sponsoring", "speaker": "Speaking"}[role]
            rec.raw["signals"] = [make_signal(
                stype, f"{verb} at {event}"[:400] if event else f"{verb} (event not named)", src,
                detail={"role": role, "event_name": event, "event_date": edate_raw, "stand": stand, "event_url": event_url, "list_url": src},
                occurred_at=edate, confidence=0.8,
            )]
        return rec

    async def iter_businesses(
        self, query: DiscoveryQuery, checkpoint: Checkpoint | None = None
    ) -> AsyncIterator[tuple[BusinessRecord, Checkpoint]]:
        path, list_url = await self._acquire()
        flt = RecordFilter(query)
        fmt = detect_fmt(path, self.config.get("format"))
        cp = checkpoint or {}
        skip = int(cp.get("row", -1)) + 1 if cp.get("file") in (None, path.name) else 0
        first = True
        observed = datetime.now(UTC)
        emitted = 0
        stats = {"rows": 0, "no_company_or_country": 0, "no_signal_url": 0}
        self.last_stats = stats
        for n, row in _iter_dict_rows(path, fmt, self.config, skip):
            cmap = _column_map(list(row.keys()), self.config.get("columns"))
            if first and "company" not in cmap:
                raise NotConfigured(f"no company column found among {sorted(row.keys())}; set config 'columns': {{'company': '<column>'}}")
            first = False
            stats["rows"] += 1
            rec = self.row_to_record(row, cmap, list_url, n, observed)
            if rec is None:
                stats["no_company_or_country"] += 1
                continue
            if not rec.raw.get("signals"):
                stats["no_signal_url"] += 1
            rec.adapter_id = ADAPTER_ID
            if not flt.accept(self.stamp(rec)):
                continue
            yield rec, {"row": n, "file": path.name}
            emitted += 1
            if query.limit and emitted >= query.limit:
                return


__all__ = ["ExhibitorListImport", "parse_event_date", "normalise_role"]
