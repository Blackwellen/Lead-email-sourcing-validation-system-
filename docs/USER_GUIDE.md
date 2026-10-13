# User guide

This guide is for operators. You don't need to know anything about DNS, SMTP, SQL or queues.

## Roles

| Role | Can |
|---|---|
| **Viewer** | Look at everything: dashboards, runs, businesses, contacts, evidence and exports they created |
| **Operator** | Create and control runs, verify, suppress, export, manage categories and presets, upload source files |
| **Admin** | All of the above, plus users, settings, source configuration and permissions, system controls, merges and deletions |

Every change is recorded in the audit log (Settings → Audit log).

## 1. Start a sourcing run

**Runs → New sourcing run** opens a five-step wizard. Your progress is saved as a draft automatically.

1. **What businesses?** Give the run a name and choose countries, regions or towns, categories (Category → Subcategory →
   Specialism) and optional keywords and exclusions. The summary on the right shows what will be targeted.
2. **Where should we look?** Pick sources. Each shows its licence, coverage and limits, and *Recommended* picks the
   official bulk datasets first. To crawl a list of company websites you already have, paste the domains or upload a CSV.
   For broad areas the planner streams one national dataset with filters rather than running thousands of town-by-town searches.
3. **What contacts?** Choose:
   * the number you need;
   * what counts toward it (distinct published emails, one per business, one per company domain, and so on);
   * named people, role addresses (info@, sales@…) or both;
   * a minimum confidence;
   * exclusions (free-mail providers, catch-all or inconclusive domains, previous bounces, suppressed addresses).
4. **Validation and speed.** *Fast* (recommended) or *Deep* verification, a speed preset, and optional limits such as
   maximum duration. *Advanced discovery* (collapsed) controls network expansion, activity signals, Common Crawl,
   source quotas and what to do when a business has no website. The desired rate is a request, not a promise: sources'
   rate limits always win, and the run page shows measured speed next to what you asked for.
5. **Review & start.** Check the summary, warnings and estimated requirements, then choose **Start run**,
   **Save draft** or **Save as preset**.

## 2. Watch and control a run

The run page updates live and shows:

* progress toward the target;
* the pipeline: Discovery → Domains → Crawl → Extraction → Validation → Qualified;
* measured throughput, the queue, errors, and an ETA range (or "unknown" when there isn't enough data).

The run controls are:

* **Pause**: work in flight finishes and nothing new starts.
* **Resume**: carries on from where it stopped.
* **Graceful stop**: finish current work, then complete.
* **Retry failed**: re-queue failed work.
* **Export current results**: export the contacts this run found.

When the target is reached, the run stops scheduling new work and finishes what is in flight. Addresses found after
that point are kept and checked in the background, never thrown away. If the sources run out first, the run completes
as "finished short of target" and shows how many it found.

## 3. Read a contact's evidence

Open any contact to see:

* where it was published (exact page URLs);
* the five independent statuses: syntax, mail routing, mailbox evidence, reachability and marketing;
* the score breakdown;
* the "Why this classification" sentences, which are generated only from stored evidence.

Key terms:

* **Published**: seen on the business's own website or another real source. **Inferred** addresses are guesses for
  research only. They are never counted as published and never marked *safe*.
* **Safe**: the available evidence is favourable. It is **not** a promise the email will reach an inbox.
* **Historical**: found only in a web archive (Common Crawl). It is not current until it is seen again on the live site.
* **Marketing status** is policy triage, separate from technical checks, and is not legal advice. Publicly listed
  business addresses are not automatically cleared for every kind of marketing. Configure your policy per country in
  Settings → Validation defaults.

## 4. Verify a list you already have

**Verification → Import CSV** creates a batch for the addresses already in the database. The page shows which addresses
were skipped as unknown. Choose a mode (an estimate of cost and limits is shown), then start, pause, resume or cancel it.
The tabs list invalid, inconclusive, catch-all and stale ("needs recheck") addresses. You can also import delivery
feedback (bounces, complaints, unsubscribes) from your sending platform; it updates suppressions automatically.

## 5. Export

On **Exports**, pick a preset and refine it with filters (category, country, region, confidence, published only,
uniqueness, role or named, exclusions, freshness, source). The number of matching rows updates as you go. The presets are:

* Full intelligence
* Published business emails
* Named decision makers
* Role/company emails
* One per company
* One per domain
* **Listmonk-ready**: the `email,name,attributes` columns, only marketing-eligible and not suppressed
* Validation audit

Formats are CSV, XLSX and JSON Lines. Cells that could be read as spreadsheet formulas are neutralised. Unknown fields
are left empty, never guessed.

## 6. Keep the data healthy

* **Data Quality** is a workbench for:
  * duplicate companies
  * unconfirmed domains
  * missing websites
  * stale emails
  * conflicting evidence
  * incomplete contacts
  * catch-all/unknown addresses
  * unresolved identities

  Select rows, choose an action and give a reason; every action is audited.
* **Businesses → a business → Domains**: confirm, reject or correct the official website (a reason is required).
* **Discovery**: shows which sources actually produce businesses, websites and emails, the completeness of research,
  and monitors for new registrations and website changes. It also has the similar-business finder.
* **Business Network** shows public relationships (branches, brands, suppliers, partners) as a bounded graph with a table view.
* **Opportunities** lists dated, source-linked activity signals (contract awards, new registrations, hiring, events…).

## 7. Sources

**Sources** and **Source Explorer** list every source with its coverage, licence, cost, credentials and whether
automated collection is allowed. Sites that do not permit automation are shown as *Restricted: manual import or written
permission only*; the platform will not scrape them. Admins can configure, enable and disable sources. Operators can
upload permitted dataset files.
