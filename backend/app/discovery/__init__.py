"""Discovery intelligence engines and the smart source scheduler (deterministic Python: no AI, no paid APIs).

Modules (import them explicitly; nothing is imported here to keep start-up cheap):

* ``resolution``     DISC-12 cross-source company resolution, merge / split, ``resolving_upsert``
* ``graph``          DISC-06 business-network expansion + graph queries
* ``similar``        DISC-07 explainable lookalike search
* ``signals``        DISC-09 business signals (provenance-mandatory)
* ``changes``        DISC-10 website change detection
* ``documents``      DISC-11 document intelligence (PDF/CSV/XLSX/XML/JSON/vCard/HTML)
* ``completeness``   DISC-14 tri-state research completeness
* ``gap_recovery``   DISC-13 bounded gap-recovery ladder
* ``effectiveness``  measured source effectiveness counters / ratios
* ``scheduler``      priority, hard source budgets, rebalance, duplicate suppression, strategy pipeline
* ``strategy``       ``RunConfig.discovery`` -> planning parameters
* ``monitor``        recurring ``MonitorSchedule`` dispatch
"""
