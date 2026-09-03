# CivicPay Open Framework — Clean-Room Provenance Log

This log records, for each design component of the Framework, the source category and citation.
Every component is derived from **public regulatory materials, published technical standards, public datasets,
or original design** — never from any employer's proprietary systems, code, data, internal documentation,
thresholds, control logic, workflow definitions, schema shapes, value distributions, or performance benchmarks.

**Source categories:**

- `public-regulatory` — publicly published supervisory guidance, rulemakings, or supervisory letters (FSOC, FDIC, OCC, FinCEN, CFPB, Federal Reserve).
- `public-dataset` — publicly available datasets (e.g., CFPB Consumer Complaint Database).
- `published-standard` — publicly published technical standards or industry frameworks (DAMA-DMBOK, dbt documentation, DuckDB/Streamlit docs).
- `original` — original design work by the contributor, developed independently on synthetic data.

| Component | Source category | Citation / note |
|---|---|---|
| Four-layer methodology (recon / DQ / exceptions / audit) | original | Original decomposition of generic financial-data-governance concerns |
| Reconciliation exact + fuzzy matching | published-standard | Standard reconciliation matching (exact key + tolerance/Levenshtein); RapidFuzz public library |
| Data-quality dimensions (completeness/accuracy/consistency/timeliness/anomaly) | published-standard | DAMA-DMBOK data-quality dimensions |
| Quality score formula (record-weighted pass rate) | original | Original aggregation of dimension check results |
| Exception priority formula (severity × amount-at-risk × age) | original | Original triage scoring |
| Audit hash-chaining (sha256(content + previous_hash)) | published-standard | Standard tamper-evidence technique (Merkle-style chaining) |
| Event canonicalization (sorted JSON keys, UTC ISO-8601, decimals as strings) | original | Original canonicalization rule for hash reproducibility |
| Synthetic data schemas (customers, accounts, transactions, payments, etc.) | original | Original synthetic schemas; distributions generated with Faker (public library); not derived from any employer system |
| DuckDB embedded storage | published-standard | DuckDB public documentation |
| Streamlit dashboard | published-standard | Streamlit public documentation |
| Federal-policy alignment (FSOC third-party risk, FDIC BaaS reconciliation, FinCEN ransomware, CFPB data access) | public-regulatory | See docs/source-appendix |
| Enrollment & validation module (Ticket 13): constrained-input capture + dual-source agreement gate | original | Generalizes a common, non-proprietary enterprise pattern (constrained data entry coupled with dual-source validation before a record is committed) into an original clean-room design: declarative validation rules (`config/enrollment_rules.yml`), a pure-Python proration path and an independent SQL-via-DuckDB path that must agree within tolerance to accept a record. Program codes, caps, and term ranges are original synthetic values, not derived from or matched to any real incentive/rebate program or employer system. |

**Review gate:** No public release occurs until (a) an attorney reviews the repository for IP and confidentiality concerns, and (b) the contributor confirms in writing that no employer confidential information is present.

This log is maintained continuously as part of the project's development process.
