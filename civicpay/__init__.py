"""CivicPay Open Framework.

A clean-room, open-source reference implementation of financial-data-governance
methodology (reconciliation, data quality, exception workflow, audit evidence).
"""

# Version is derived from git tags at build time (hatch-vcs writes
# civicpay/_version.py). Fall back gracefully when running from a source
# checkout that hasn't been built.
try:
    from civicpay._version import __version__
except ImportError:  # pragma: no cover - dev fallback
    __version__ = "0.1.0+unknown"
