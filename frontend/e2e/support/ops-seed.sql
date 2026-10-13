-- SYNTHETIC rows only (example.test domains) for the ops e2e suite. Idempotent: skipped when already seeded.
DO $$
DECLARE o1 bigint; o2 bigint; o3 bigint; o4 bigint; c1 bigint; c2 bigint; c3 bigint;
BEGIN
  IF EXISTS (SELECT 1 FROM organisations WHERE canonical_identity LIKE 'e2e-ops:%') THEN RETURN; END IF;

  INSERT INTO organisations (legal_name, trading_name, country_code, region, city, industry, canonical_identity, discovery_source, sic_codes)
  VALUES ('Alpha Plumbing Ltd', 'Alpha Plumbing', 'GB', 'England', 'Leeds', 'Plumbing', 'e2e-ops:alpha', 'csv_import', '[]') RETURNING id INTO o1;
  INSERT INTO organisations (legal_name, trading_name, country_code, region, city, industry, canonical_identity, discovery_source, sic_codes)
  VALUES ('Beta Bakery Ltd', '=HYPERLINK("http://evil.example/x","Beta Bakery")', 'GB', 'England', 'York', 'Bakery', 'e2e-ops:beta', 'csv_import', '[]') RETURNING id INTO o2;
  INSERT INTO organisations (legal_name, trading_name, country_code, region, city, industry, canonical_identity, discovery_source, sic_codes)
  VALUES ('Gamma Gmbh', 'Gamma Werkstatt', 'DE', 'Bayern', 'Munich', 'Workshop', 'e2e-ops:gamma', 'manual_search_results', '[]') RETURNING id INTO o3;
  INSERT INTO organisations (legal_name, trading_name, country_code, region, city, industry, canonical_identity, discovery_source, sic_codes)
  VALUES ('Delta Dental', 'Delta Dental', 'GB', 'Wales', 'Cardiff', 'Dental', 'e2e-ops:delta', 'csv_import', '[]') RETURNING id INTO o4;

  INSERT INTO contacts (organisation_id, full_name, given_name, family_name, job_title, identity_confidence, identity_key)
  VALUES (o1, 'Ann Alpha', 'Ann', 'Alpha', 'Owner', 0.9, 'ann-alpha') RETURNING id INTO c1;
  INSERT INTO contacts (organisation_id, full_name, given_name, family_name, job_title, identity_confidence, identity_key)
  VALUES (o2, '=SUM(1+1)', 'Bob', 'Beta', 'Baker', 0.9, 'bob-beta') RETURNING id INTO c2;
  INSERT INTO contacts (organisation_id, full_name, given_name, family_name, job_title, identity_confidence, identity_key)
  VALUES (o3, 'Gus Gamma', 'Gus', 'Gamma', 'Director', 0.9, 'gus-gamma') RETURNING id INTO c3;

  INSERT INTO email_addresses (contact_id, organisation_id, email_original, email_normalised, local_part, domain, registrable_domain, source_type, origin, role_type, is_free_provider, is_disposable, confidence, confidence_breakdown, syntax_status, domain_mail_status, recipient_evidence_status, catch_all_status, overall_reachability, marketing_status, needs_review, previously_bounced, last_validated_at) VALUES
   (c1, o1, 'ann@alpha-plumbing.example.test', 'ann@alpha-plumbing.example.test', 'ann', 'alpha-plumbing.example.test', 'alpha-plumbing.example.test', 'website', 'published', NULL, false, false, 92, '{}', 'valid', 'accepts_mail', 'none', 'not_catch_all', 'safe', 'eligible', false, false, now()),
   (NULL, o1, 'info@alpha-plumbing.example.test', 'info@alpha-plumbing.example.test', 'info', 'alpha-plumbing.example.test', 'alpha-plumbing.example.test', 'website', 'published', 'info', false, false, 80, '{}', 'valid', 'accepts_mail', 'none', 'not_catch_all', 'safe', 'needs_review', false, false, now()),
   (c2, o2, 'bob@beta-bakery.example.test', 'bob@beta-bakery.example.test', 'bob', 'beta-bakery.example.test', 'beta-bakery.example.test', 'website', 'published', NULL, false, false, 85, '{}', 'valid', 'accepts_mail', 'none', 'not_catch_all', 'safe', 'eligible', false, false, now()),
   (NULL, o2, 'sales@beta-bakery.example.test', 'sales@beta-bakery.example.test', 'sales', 'beta-bakery.example.test', 'beta-bakery.example.test', 'website', 'pattern_inferred', 'sales', false, false, 40, '{}', 'valid', 'accepts_mail', 'none', 'suspected', 'unknown', 'needs_review', false, false, NULL),
   (c3, o3, 'gus@gamma.example.test', 'gus@gamma.example.test', 'gus', 'gamma.example.test', 'gamma.example.test', 'website', 'published', NULL, false, false, 70, '{}', 'valid', 'accepts_mail', 'none', 'confirmed', 'risky', 'consent_required', false, false, now()),
   (NULL, o4, 'contact@delta-dental.example.test', 'contact@delta-dental.example.test', 'contact', 'delta-dental.example.test', 'delta-dental.example.test', 'website', 'published', 'contact', false, false, 88, '{}', 'valid', 'accepts_mail', 'none', 'not_catch_all', 'safe', 'eligible', false, false, now()),
   (NULL, o4, 'suppressed@delta-dental.example.test', 'suppressed@delta-dental.example.test', 'suppressed', 'delta-dental.example.test', 'delta-dental.example.test', 'website', 'published', NULL, false, false, 88, '{}', 'valid', 'accepts_mail', 'none', 'not_catch_all', 'safe', 'suppressed', false, false, now());
  UPDATE email_addresses SET suppressed_at = now() WHERE email_normalised = 'suppressed@delta-dental.example.test';
END $$;
