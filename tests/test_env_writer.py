"""Tests for services/dashboard/helpers/env_writer.py — value sanitization.

The security-relevant behavior: update_env writes KEY=value lines that
docker-compose later interpolates, so a value containing a newline could
inject extra .env keys and bypass the ALLOWED_KEYS allowlist (the injected
line is parsed by compose, not by the allowlist check). update_env must
reject newline/carriage-return in any value.
"""

import sys
from pathlib import Path

import pytest

# env_writer imports cleanly on its own (stdlib only). Make the dashboard
# helpers importable regardless of how pytest is invoked.
_DASH = Path(__file__).parent.parent / "services" / "dashboard"
sys.path.insert(0, str(_DASH))

from helpers.env_writer import update_env  # noqa: E402


def _seed_env(tmp_path: Path) -> Path:
    p = tmp_path / ".env"
    # LOCATION_NAME is in ALLOWED_KEYS and has no validator upstream, so it's
    # the realistic injection vector from /api/setup/apply-config.
    p.write_text("LOCATION_NAME=old\nDETECTOR_GPU=0\n")
    return p


def test_rejects_newline_in_value(tmp_path):
    p = _seed_env(tmp_path)
    res = update_env({"LOCATION_NAME": "home\nDETECTOR_GPU=1"}, path=p)
    assert res["ok"] is False
    assert "newline" in res["error"].lower()
    assert res["written"] == []
    # The injection must NOT have landed — DETECTOR_GPU stays 0, no second line.
    content = p.read_text()
    assert "DETECTOR_GPU=1" not in content
    assert content.count("DETECTOR_GPU") == 1
    # The original value is untouched.
    assert "LOCATION_NAME=old" in content


def test_rejects_carriage_return_in_value(tmp_path):
    p = _seed_env(tmp_path)
    res = update_env({"LOCATION_NAME": "home\rMALICIOUS=x"}, path=p)
    assert res["ok"] is False
    assert "MALICIOUS=x" not in p.read_text()


def test_clean_value_still_writes(tmp_path):
    p = _seed_env(tmp_path)
    res = update_env({"LOCATION_NAME": "Front Yard"}, path=p)
    assert res["ok"] is True
    assert "LOCATION_NAME" in res["written"]
    assert "LOCATION_NAME=Front Yard" in p.read_text()


def test_one_bad_value_blocks_the_whole_batch(tmp_path):
    """A clean key in the same call must not be written if a sibling value
    is rejected — fail the batch atomically rather than partially apply."""
    p = _seed_env(tmp_path)
    res = update_env(
        {"LOCATION_NAME": "ok", "LOCATION_REGION": "bad\nINJECT=1"}, path=p
    )
    assert res["ok"] is False
    content = p.read_text()
    assert "INJECT=1" not in content
    # LOCATION_NAME must remain the seeded value, not "ok"
    assert "LOCATION_NAME=old" in content
