# Changelog

All notable changes to the CivicPay Open Framework are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Trusted Publishing (OIDC) for PyPI: releases are published via GitHub Actions
  identity, no shared API token secret required.
- Version is now derived from git tags via `hatch-vcs` (`dynamic = ["version"]`).

## [0.1.0] — 2026-09-01

### Added
- **Payment Reconciliation** (Ticket 1–3): deterministic synthetic-data generation
  (Faker, seed=42), DuckDB storage layer, audit-ledger hash-chained append-only
  log, fuzzy + exact matching with RapidFuzz, configurable pipeline.
- **Data-Quality Monitoring** (Ticket 4, spec T5): nine typed checks
  (completeness, accuracy, consistency, timeliness, anomaly), per-dataset
  quality score (§12), exception routing, `config/dq_checks.yml`.
- **Exception Workflow** (Ticket 5, spec T6): priority formula
  (`severity × amount_at_risk × age_factor`), SLA aging, list/resolve CLI,
  root-cause capture with audit events.
- **Audit-Evidence Layer** (Ticket 6, spec T7): `verify_chain` (hash-chain
  traversal, detects tampering/deletion/forks/orphans), `export_evidence`
  (JSON evidence package), CLI.
- **Streamlit Dashboard** (Ticket 7, spec T8): four views — reconciliation
  summary, DQ scores, exception queue with aging, recent audit events.
- **CLI Wiring** (Ticket 8, spec T10): `civicpay run-all` end-to-end
  (seed → reconcile → DQ → exception list → audit verify), `--run-id` for fresh
  batch ids, PyPI publish script, install CI (Py 3.11 + 3.12).
- **Documentation** (Ticket 9, spec T11): README with four-layer architecture
  diagram and full CLI reference, `docs/data-quality.md`,
  `docs/reconciliation.md`, updated `docs/architecture.md` status table,
  `CONTRIBUTING.md` ticket roadmap.

### Fixed
- `verify_chain` rewrite: the old sort-by-timestamp approach broke when batches
  shared a timestamp (D < R reordering put DQ events before RECON events,
  breaking linkage). Now walks the hash chain via `previous_hash → event_hash`.
  Genesis validation tightened for full-chain verification.

### Tests
- 153 tests passing; ruff clean (check + format).

[Unreleased]: https://github.com/joannechang39/civicpay-open-framework/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/joannechang39/civicpay-open-framework/releases/tag/v0.1.0
