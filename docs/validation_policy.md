# Validation policy

The gate (`validation/gate.py`) reads the **rendered file** and the **persisted records**
independently. It is fail-closed for factual integrity.

| Area | Checks | On failure |
|---|---|---|
| File | `YYYY-MM-DD.md`, Asia/Shanghai date, `reports/` directory | block report |
| Front matter | valid YAML, required keys, date, timezone, paper_count = sections = selections | block report |
| Structure | title, Daily overview + Recommendations sections, overview ≥ 50 words and prose (no lists, checked on the token stream), no template placeholders or `None`/`nan` leaks, sequential positions, unique ids, no duplicate sections, all required fields/labels, complete score tables, URL/DOI cell formats | block report |
| Citations | every paper section corresponds to a persisted selection (no fabricated citations) | block report |
| Rendered metadata | title, authors, venue, dates, DOI, PMID, URL, preprint status, keywords, MeSH exactly as in the canonical record; abstract verbatim (or the exact unavailable marker); displayed score = stored score | block paper |
| Provenance | title/abstract/authors/journal found verbatim in the stored record of the recorded source; DOI/PMID present in some stored record | block paper |
| Identity (online) | DOI registered (Crossref), returned DOI and title match (normalized, Unicode-aware; ratio ≥ 0.93); PMID via PubMed; medRxiv via its API | mismatch/nonexistent: block paper · provider down: warning |
| URL (online) | follows redirects host by host respecting robots.txt; compares `citation_doi`/`citation_title`/DOI/title on the landing page | `identity_mismatch`/`invalid`: block paper · `verified_via_authoritative_metadata` / `temporarily_unavailable`: warning |
| Scores | weights sum to 1, current scoring version, all dimensions present and in range, LLM scores equal the stored evaluation, deterministic dimensions recomputed, composite recomputed (tolerance 1e-6), raw-score order, bounded diversity penalty, hard eligibility, no duplicates (incl. preprint/journal pairs), exploratory cap | block paper / report |

Bot protection (401/403/429/503, challenge pages) is never treated as proof that an article does
not exist.

## Repair loop

For papers blocked by the gate: (1) one bounded metadata refresh from Crossref/PubMed/medRxiv;
(2) if the metadata changed, the paper needs a new assessment and leaves today's list; otherwise
(3) it is marked `validation_status = failed` and excluded; (4) the selection is refilled from the
next eligible candidates; (5) the digest is re-rendered and re-validated — at most
`max_repair_rounds` (3) times. After a refill the Claude narrative no longer matches the selection,
so the clearly labelled structural overview is used instead.

If fewer than `min_verified_papers` (2) remain, no digest is published. Only a fully passing
artifact is promoted (atomic rename from `reports/.staging/`), marked `validation_status: passed`,
and committed. Every check result is stored in `validation_results` with the run id and round.
