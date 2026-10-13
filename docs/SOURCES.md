# Source adapters

Every adapter declares its country coverage, source type, licence, access requirements, whether automated collection is
permitted, its rate policy, available fields, incremental support, parser version and refresh frequency
(`backend/app/sources/base.py: AdapterInfo`). The **Sources** page and **Source Explorer** show this live,
together with health and measured effectiveness.

Default priority (lower runs first; admins can override it): official bulk datasets (10) → open data (20) → licensed
registries (30) → company websites (40) → search APIs (50) → manual imports (60).

## Business-discovery adapters

| id | Coverage | Type | Licence | Credentials | Notes |
|---|---|---|---|---|---|
| `companies_house_bulk` | GB | bulk dataset | OGL v3 | none | Monthly BasicCompanyData snapshot (~0.5 GB zip, 2.8 GB CSV). Streamed row by row with a row-offset checkpoint. Filters on SIC, postcode area/district, locality, keyword and active status. **Verified live** (2026-10-01 snapshot, 5.70 M rows streamed in 220 s at 79 MB peak RSS). |
| `companies_house_new_registrations` | GB | bulk dataset | OGL v3 | none | Diffs two snapshots (new incorporations, status/name/address changes) using an on-disk index, or an incorporation-date window. Daily, weekly or monthly monitor. Never assumes a website exists. |
| `charity_commission_ew` | GB (E&W) | bulk dataset | OGL v3 | none | Register-of-charities extract. Formats verified live; full download not run in tests. |
| `contracts_finder_ocds`, `find_a_tender_ocds` | GB | open data API | OGL v3 | none | Awarded suppliers from OCDS releases plus `contract_award` signals. Optional "established supplier" filter. Contact email is used only if published in the notice. Disabled until configured. |
| `gleif_lei` | worldwide (LEI holders) | bulk dataset | CC0 | none | Golden Copy files, including parent relationships for the business graph. |
| `eu_ted` | EU27 + IS/LI/NO | open data API | EU reuse policy | none | Awarded contractors via TED API v3. Ambiguous multi-tenderer notices are skipped, not guessed. |
| `usaspending` | US | open data API | public domain | none | Federal award recipients. |
| `abn_lookup_au` | AU | bulk dataset | CC BY 3.0 AU | optional GUID | ABN bulk extract. The web-service path needs a free ABR GUID and is unverified. |
| `fsa_fhrs` | GB | open data | OGL | none | Food hygiene establishments by local authority (venues, hospitality). Council contact data is never used as business contact data. |
| `osm_geofabrik` | worldwide | open data | ODbL 1.0 | none | Geofabrik extracts (`.osm.pbf` via `osmium`, `.osm[.bz2]`, Overpass JSON, GeoJSON). Category OSM tags drive filtering. Emails are emitted only if tagged on the object. The country path table has not been checked one by one against the Geofabrik index. |
| `wikidata` | worldwide | open data | CC0 | none | SPARQL query for organisations with an official website (P856). The live query timed out through the sandbox proxy, so it is unverified. |
| `open_data_member_register` | per dataset | open data | per dataset | none | Generic CSV/XLSX/JSON/XML register importer with column mapping. Licence must be set by the operator. |
| `exhibitor_list_import` | worldwide | manual import | per list | none | Exhibitor/sponsor lists plus `exhibitor`/`sponsor` signals. A feed URL requires a recorded permission basis. |
| `csv_import` | worldwide | manual import | operator-supplied | none | Business or domain lists (Scenario C). Email columns are ignored by design. |
| `manual_search_results` | worldwide | manual import | operator-supplied | none | Imported search-result CSV. |
| `brave_search` | worldwide | search API | key holder's plan | API key | Official API. Stops on 429 or exhausted quota (no workarounds). |
| `searxng` | worldwide | search API | varies | instance URL | Only for an instance you operate or are authorised to use. |
| `commoncrawl_index`, `commoncrawl_archive` | worldwide | web archive | Common Crawl ToU | none | CDX API, offline columnar (Parquet) index, WARC range fetch. Contacts found are **historical** until confirmed on the live site. |
| `company_websites`, `public_pdfs` | — | website | site owners' | — | Informational: crawling happens in the crawl stage. |

## Directory catalogue (63 entries)

`backend/app/data/directory_catalogue.json` lists directories, accreditation registers, trade associations and chambers
per country (Yell, Thomson Local, FreeIndex, TrustMark, RICS, Propertymark, Gas Safe, NICEIC, …). Each entry records an
`automated_extraction` status from our reading of the site's terms, and a recommended access path.

The default is **deny**: no directory is scraped unless it is open data or an admin records written permission
(`permission_basis`). Terms change, so re-verify them before relying on any entry.

## Live verification status (sandbox, 2026-10-13)

* **Verified:** Companies House snapshot (full download and stream). Charity extract formats. Contracts Finder and
  Find a Tender award structures. GLEIF publish index and headers. TED API v3 search. USAspending `spending_by_award`.
  ABN CKAN metadata. FSA FHRS XML. Home Office sponsor register CSV header.
* **Not verified:** ABN GUID web services, full multi-hundred-MB downloads other than Companies House, Geofabrik
  (unreachable from the sandbox), the Common Crawl CDX, Parquet and WARC paths at scale, a full Wikidata query,
  Brave Search (no key), and SearXNG (no instance).
* `nz_companies_office` was not implemented: no open bulk dataset could be confirmed without passing a bot challenge.
