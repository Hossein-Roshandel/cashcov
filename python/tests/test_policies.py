"""Validate that Python IntEnum values match Go iota constants.

These tests have zero dependencies on the compiled shim or Redis.  They guard
against drift between ``cashcov/policies.py`` and ``policies.go``: if someone
renumbers a Go ``const`` without updating the Python IntEnum, these fail.

The module is imported directly via importlib to bypass ``cashcov/__init__.py``,
which would trigger the shim load and fail when the library is not compiled.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

# Load policies.py as a standalone module, bypassing cashcov/__init__.py
# (which imports _bindings and would fail without a compiled .so).
_spec = importlib.util.spec_from_file_location(
    "_cashcov_policies_direct",
    Path(__file__).parent.parent / "cashcov" / "policies.py",
)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

MissFillPolicy = _mod.MissFillPolicy
HitRefreshPolicy = _mod.HitRefreshPolicy
ErrorPolicy = _mod.ErrorPolicy


# ---------------------------------------------------------------------------
# MissFillPolicy — must mirror Go's MissFillPolicy iota in policies.go
# ---------------------------------------------------------------------------


def test_miss_fill_default_is_zero():
    assert MissFillPolicy.DEFAULT == 0


def test_miss_fill_sync():
    assert MissFillPolicy.SYNC == 1


def test_miss_fill_async():
    assert MissFillPolicy.ASYNC == 2


def test_miss_fill_stale_or_sync():
    assert MissFillPolicy.STALE_OR_SYNC == 3


def test_miss_fill_fail_fast():
    assert MissFillPolicy.FAIL_FAST == 4


def test_miss_fill_cooperative():
    assert MissFillPolicy.COOPERATIVE == 5


def test_miss_fill_policy_count():
    """Fail if new values are added to Go without being mirrored here."""
    assert len(MissFillPolicy) == 6


# ---------------------------------------------------------------------------
# HitRefreshPolicy — must mirror Go's HitRefreshPolicy iota in policies.go
# ---------------------------------------------------------------------------


def test_hit_refresh_default_is_zero():
    assert HitRefreshPolicy.DEFAULT == 0


def test_hit_refresh_ahead():
    assert HitRefreshPolicy.AHEAD == 1


def test_hit_refresh_probabilistic():
    assert HitRefreshPolicy.PROBABILISTIC == 2


def test_hit_refresh_older_than():
    assert HitRefreshPolicy.OLDER_THAN == 3


def test_hit_refresh_none():
    assert HitRefreshPolicy.NONE == 4


def test_hit_refresh_policy_count():
    assert len(HitRefreshPolicy) == 5


# ---------------------------------------------------------------------------
# ErrorPolicy — must mirror Go's ErrorPolicy iota in policies.go
# ---------------------------------------------------------------------------


def test_error_policy_surface_is_zero():
    assert ErrorPolicy.SURFACE == 0


def test_error_policy_zero_value():
    assert ErrorPolicy.ZERO_VALUE == 1


def test_error_policy_count():
    assert len(ErrorPolicy) == 2
