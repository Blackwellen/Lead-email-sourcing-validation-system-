"""Info-only registry entries for stages implemented elsewhere (crawler)."""
from __future__ import annotations

from collections.abc import AsyncIterator

from .base import (
    AdapterHealth,
    AdapterInfo,
    AdapterUnavailable,
    BusinessRecord,
    Checkpoint,
    DiscoveryQuery,
    RatePolicy,
    SourceAdapter,
    register,
)


class _InfoOnly(SourceAdapter):
    async def health(self) -> AdapterHealth:
        return AdapterHealth(True, "info_only", "implemented by the crawler stage, not a discovery adapter")

    async def iter_businesses(
        self, query: DiscoveryQuery, checkpoint: Checkpoint | None = None
    ) -> AsyncIterator[tuple[BusinessRecord, Checkpoint]]:
        raise AdapterUnavailable(f"{self.info.name} is handled by the crawler stage")
        yield  # pragma: no cover


@register
class CompanyWebsitesEntry(_InfoOnly):
    info = AdapterInfo(
        adapter_id="company_websites", name="Company websites (own pages)", country_coverage=("*",), source_type="website",
        licence="Content owned by site operators; only published contact details are recorded, with evidence URL",
        source_url="", access_requirements="robots.txt and site terms honoured by the crawler; domain must be confirmed first.",
        automated_collection_permitted=True, rate_policy=RatePolicy(requests_per_minute=30, concurrency=2), info_only=True,
        fields_available=("email_published", "phone", "address"), notes="Crawling is performed by app.crawler, not by this adapter.",
    )


@register
class PublicPdfsEntry(_InfoOnly):
    info = AdapterInfo(
        adapter_id="public_pdfs", name="Public PDFs on company sites", country_coverage=("*",), source_type="website",
        licence="Content owned by site operators", source_url="",
        access_requirements="Only PDFs linked from a confirmed company website, within robots.txt/size limits.",
        automated_collection_permitted=True, rate_policy=RatePolicy(requests_per_minute=10, concurrency=1), info_only=True,
        fields_available=("email_published", "phone"), notes="Handled by app.crawler.",
    )
