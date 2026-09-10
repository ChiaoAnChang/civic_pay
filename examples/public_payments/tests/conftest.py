"""Shared fixtures for the public_payments demo test suite.

Puts ``examples/public_payments`` on ``sys.path`` so tests can import
``generate_synthetic_data``, ``detection``, and ``summary_report`` as plain
top-level modules — this directory is a runnable example, not an installed
package, so it isn't on ``sys.path`` by default the way ``civicpay`` is.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from generate_synthetic_data import DEFAULT_CLAIMANT_COUNT, generate_dataset  # noqa: E402


@pytest.fixture(scope="session")
def demo_dataset():
    """The same deterministic dataset ``run_demo.py`` generates by default."""
    return generate_dataset(seed=42, claimant_count=DEFAULT_CLAIMANT_COUNT)


@pytest.fixture(scope="session")
def roster(demo_dataset):
    return demo_dataset[0]


@pytest.fixture(scope="session")
def payments(demo_dataset):
    return demo_dataset[1]


@pytest.fixture(scope="session")
def ground_truth(demo_dataset):
    return demo_dataset[2]
