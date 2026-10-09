"""Proves git revert of e017807 actually restored TSX 60/TSX Composite to
private, ahead of pushing the revert to main (Director-directed, 9 Oct
2026). e017807 had removed TSX 60 from scan_store._DEFAULT_PRIVATE_
UNIVERSES; Andrew's separate go for that change never arrived, so the
Director ordered a revert-on-top (no history rewrite) rather than a
reorder. This test is the proof step the Director asked for before the
revert is pushed."""
import os
from unittest import mock

import scan_store


def test_tsx60_and_tsx_composite_are_private_again():
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("PRIVATE_UNIVERSES", None)
        assert scan_store.is_private_universe("TSX 60") is True
        assert scan_store.is_private_universe("TSX Composite") is True


def test_default_private_universes_string_contains_tsx60():
    assert "TSX 60" in scan_store._DEFAULT_PRIVATE_UNIVERSES
    assert "TSX Composite" in scan_store._DEFAULT_PRIVATE_UNIVERSES


def test_non_owner_cannot_read_tsx60(tmp_path, monkeypatch):
    monkeypatch.setattr(scan_store, "_data_dir", lambda: str(tmp_path))
    scan_store.save_scan("TSX 60", [{"Ticker": "RY.TO"}], "test fixture")
    # Non-owner path: allow_private defaults to False.
    assert scan_store.load_scan("TSX 60") is None
    # Owner path still works - privacy is enforced per-read, not globally.
    assert scan_store.load_scan("TSX 60", allow_private=True) is not None


def test_other_defaults_still_private_unchanged():
    for name in ("FTSE 100", "FTSE 250", "Nikkei 225", "TOPIX 500", "DAX", "CAC 40", "AEX", "SMI", "OMX Stockholm 30"):
        assert scan_store.is_private_universe(name) is True
