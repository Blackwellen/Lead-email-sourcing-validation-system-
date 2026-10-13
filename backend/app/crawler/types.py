"""Plain data containers shared by the crawler modules (no behaviour, no I/O)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

EMAIL_METHODS = ("mailto", "text", "jsonld", "vcard", "obfuscated", "table", "pdf")


@dataclass(slots=True)
class EmailObservation:
    """One e-mail address published on one page, with the context it was published in.

    Names/titles are ONLY ever copied from the page (structured data or DOM neighbours);
    nothing is derived from the local part of the address.
    """

    email: str
    method: str  # one of EMAIL_METHODS (primary discovery method)
    page_url: str
    context: str = ""
    name: str | None = None
    title: str | None = None
    department: str | None = None
    phone: str | None = None
    also_methods: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "method": self.method,
            "page_url": self.page_url,
            "context": self.context,
            "name": self.name,
            "title": self.title,
            "department": self.department,
            "phone": self.phone,
            "also_methods": list(self.also_methods),
        }


@dataclass(slots=True)
class OrgMetadata:
    """Organisation facts published on a page (schema.org, footer, meta)."""

    source: str  # jsonld | microdata | footer | meta
    name: str | None = None
    legal_name: str | None = None
    telephone: list[str] = field(default_factory=list)
    email: list[str] = field(default_factory=list)
    url: str | None = None
    street: str | None = None
    locality: str | None = None
    region: str | None = None
    postcode: str | None = None
    country: str | None = None
    same_as: list[str] = field(default_factory=list)
    company_number: str | None = None
    vat_id: str | None = None
    schema_type: str | None = None


@dataclass(slots=True)
class PageLink:
    url: str
    text: str = ""
    rel_nofollow: bool = False


@dataclass(slots=True)
class RankedLink:
    url: str
    score: int
    reason: str


@dataclass(slots=True)
class PageExtraction:
    page_url: str
    title: str | None = None
    h1: list[str] = field(default_factory=list)
    meta_description: str | None = None
    site_name: str | None = None  # og:site_name / application-name
    canonical: str | None = None
    lang: str | None = None
    robots_meta: str | None = None  # raw content of <meta name=robots>
    emails: list[EmailObservation] = field(default_factory=list)
    org: list[OrgMetadata] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)  # business phones (tel: links, address/footer)
    company_numbers: list[str] = field(default_factory=list)
    postcodes: list[str] = field(default_factory=list)
    footer_text: str = ""
    text_sample: str = ""  # first ~3000 chars of visible text
    text_length: int = 0
    links: list[PageLink] = field(default_factory=list)
    script_count: int = 0

    @property
    def noindex(self) -> bool:
        return bool(self.robots_meta and any(t in self.robots_meta.lower() for t in ("noindex", "none")))

    @property
    def nofollow(self) -> bool:
        return bool(self.robots_meta and any(t in self.robots_meta.lower() for t in ("nofollow", "none")))


@dataclass(slots=True)
class SiteEvidence:
    """Everything a (light) crawl learnt about a candidate website, for identity scoring."""

    domain: str
    final_url: str | None = None
    reachable: bool = False
    status: int | None = None
    titles: list[str] = field(default_factory=list)
    h1s: list[str] = field(default_factory=list)
    site_names: list[str] = field(default_factory=list)
    org: list[OrgMetadata] = field(default_factory=list)
    footer_texts: list[str] = field(default_factory=list)
    text_samples: list[str] = field(default_factory=list)
    company_numbers: list[str] = field(default_factory=list)
    postcodes: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    parked: str | None = None  # parking reason if parked/expired/default page
    redirected_domain: str | None = None  # registrable domain we were redirected to (if different)
    dns_failure: bool = False
    tls_failure: bool = False
    observed_at: datetime | None = None
    pages: int = 0
    source_kind: str = "guess"  # how the candidate domain was obtained (see identity.Candidate)
