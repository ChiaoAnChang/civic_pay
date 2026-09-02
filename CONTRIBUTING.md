# Contributing to CivicPay Open Framework

Thank you for your interest in contributing. This project is developed **clean-room**: contributions must be derived from public regulatory materials, published technical standards, public datasets, or original design — never from any contributor's employer proprietary systems, code, data, internal documentation, thresholds, control logic, workflow definitions, schema shapes, value distributions, or performance benchmarks.

## Before you contribute

1. Read [PROVENANCE.md](PROVENANCE.md) and [DISCLAIMER.md](DISCLAIMER.md).
2. Ensure your contribution does not include any employer-confidential material. When in doubt, describe your approach as "informed by professional experience with enterprise-scale financial data systems" and base the implementation on public sources.
3. For each new design component, add an entry to the provenance log in [PROVENANCE.md](PROVENANCE.md).

## Development setup

```bash
pip install -e ".[dev]"
pre-commit install  # optional
```

## Workflow

1. Fork the repository and create a feature branch.
2. Add tests for new behavior. All tests must pass: `pytest -q --cov=civicpay`
3. Ensure linting passes: `ruff check .` and `ruff format --check .`
4. Update `PROVENANCE.md` if you introduced a new design input.
5. Open a pull request describing the change and its provenance.

## Code style

- Python 3.11+. Run `ruff format` before committing.
- Every public function needs a docstring.
- Deterministic data generators must accept a `seed` parameter.

## Code of Conduct

Participation in this project is governed by the [Code of Conduct](CODE_OF_CONDUCT.md).
