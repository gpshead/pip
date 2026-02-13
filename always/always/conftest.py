"""Pytest configuration for always/always tests."""

from __future__ import annotations

import pytest
from pathlib import Path


@pytest.fixture
def repo_root() -> Path:
    """Return the repository root directory."""
    # Navigate up from this file to find the repo root
    current = Path(__file__).parent
    while current != current.parent:
        if (current / ".git").exists():
            return current
        current = current.parent
    # Fallback to two directories up from always/always
    return Path(__file__).parent.parent.parent
