# Email verification: what each check proves, and what it does not

Each address carries **independent** statuses. They are never collapsed into one "verified" flag.

| Status | Values | Comes from |
|---|---|---|
| Syntax | `valid` `invalid` `unknown` | V01: `email-validator` (RFC-conscious, IDNA, Unicode), special-use domains rejected |
| Domain mail | `accepts_mail` `no_mail_service` `null_mx` `temporary_dns_failure` `unknown` | V02/V03: DNS MX, RFC 7505 null MX, RFC 5321 implicit MX, cached per domain |
| Recipient evidence | `published` `corroborated` `smtp_recipient_accepted` `smtp_recipient_rejected` `inconclusive` `untested` | Where the address was published, plus authorised SMTP diagnostics |
| Catch-all | `confirmed` `suspected` `rejected_unknown_recipient` `unknown` `untested` | V06: active test only on authorised domains; passive evidence otherwise |
| Overall reachability | `safe` `risky` `invalid` `unknown` | Explainable 0–100 score (`app/verify/scoring.py`) |
| Marketing | `eligible` `needs_review` `consent_required` `opted_out` `suppressed` `not_permitted` | Operator policy + suppression list (`app/verify/marketing.py`) |

**`safe` does not mean guaranteed inbox delivery.** It means the evidence currently available is relatively favourable:
the address was published by the business on its own domain, the domain routes mail, and nothing adverse is known.
No pre-send technique can guarantee final delivery.

## Modes

* **FAST** (default for discovery): syntax, normalisation, DNS/MX (cached), disposable/free/role flags, domain↔company
  identity, source credibility, de-duplication and reuse of fresh evidence. It makes no SMTP connections.
* **DEEP**: everything in FAST, plus provider classification, catch-all evidence, authorised SMTP diagnostics,
  delivery history and a more detailed score.
* **CUSTOM**: per-stage switches plus DNS/SMTP concurrency, cache TTLs, revalidation interval, thresholds and timeouts.
  The UI shows the estimated cost and limitations (`POST /api/verification/estimate`).

## SMTP diagnostics (V07–V09): off by default

SMTP recipient checks run only when **all** of these hold:

1. `LEAD_SMTP_DIAGNOSTICS_ENABLED=true`, a HELO host and a MAIL FROM on a domain **you own** are configured.
2. The recipient domain is listed in `LEAD_SMTP_AUTHORISED_DOMAINS`, or an admin has recorded a domain permission (with a
   written basis) in Verification → SMTP diagnostics.
3. Outbound port 25 works. Most clouds block it, Oracle Cloud included by default. Use System → *Check outbound port 25*.

For unauthorised domains no connection is made; the check is recorded as `untested` with reason `not_authorised`.

The probe sends EHLO/HELO, optional STARTTLS, `MAIL FROM`, `RCPT TO`, `RSET` and `QUIT`, and **never `DATA`**.
The only socket-write function asserts the verb allow-list; this is tested against a local SMTP server that records every verb.

How replies are interpreted:

* `250`/`251` at RCPT becomes `smtp_recipient_accepted`. Acceptance can still bounce later, and catch-all servers accept everything.
* `550 5.1.1` and similar become `smtp_recipient_rejected`.
* `550 5.7.x`, blocklist and PTR text, and other policy denials become `inconclusive` (policy restricted), never "invalid".
* `421`/`450`/`451`/`452` and other 4xx replies become `inconclusive` (deferred). Retries use bounded backoff; the sender identity is never rotated.
* Mailbox full or disabled is reported only when the reply explicitly says so.

## Infrastructure self-checks

* **DNS resolver health.** Some networks answer A queries but never MX. On such a resolver every domain would look
  mail-capable through implicit MX. The verifier probes a few domains that publish MX, and if none answers, implicit-MX
  results are reported as `unknown` (`app/verify/dns_health.py`).
* **Outbound TLS.** Certificate verification is never disabled. Behind a TLS-inspecting proxy, set `LEAD_CA_BUNDLE`
  (or the standard `SSL_CERT_FILE`).

## Scoring

The score is the sum of capped components, each listed with its points, maximum and a note in the contact detail page:
source quality, identity confidence, corroboration, domain mail configuration, freshness, SMTP evidence, catch-all
penalty, bounce history, conflicts, and free/role/disposable adjustments.

Hard caps:

* `pattern_inferred` / `role_speculative` ≤ 55 and never `safe`.
* Catch-all confirmed or suspected ≤ `risky`.
* Syntax invalid, no mail service, null MX, hard bounce or SMTP rejection make the address `invalid`.

## Things this platform deliberately does not do

* Probe random recipients on third-party domains to detect catch-all.
* Use Have I Been Pwned or breach data on harvested contacts.
* Claim honeypot detection. Risk indicators are reported instead.
* Treat SPF, DKIM or DMARC as evidence that a mailbox exists.
