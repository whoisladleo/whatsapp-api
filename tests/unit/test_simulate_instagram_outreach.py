"""Unit tests for the pure (stdlib-only) parts of the Instagram outreach
simulation harness — personas, branching, token/cost estimation, and the
calibration math. Loads scripts/sim_outreach_personas.py by path so
no app import (and no API key) is needed."""

import importlib.util
from pathlib import Path

import pytest

HARNESS_PATH = Path(__file__).resolve().parents[2] / "scripts" / "sim_outreach_personas.py"


@pytest.fixture(scope="module")
def h():
    spec = importlib.util.spec_from_file_location("sim_outreach_personas", HARNESS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_module_has_no_app_import_at_top_level():
    top = HARNESS_PATH.read_text().split("App-bound section")[0]
    assert "import app" not in top
    assert "from app" not in top


def test_persona_variants_cover_brief(h):
    assert set(h.PERSONAS) == set("ABCDEFGH")
    for letter, spec in h.PERSONAS.items():
        assert set(spec["variants"]) == {"1", "2"}, letter
        assert spec["brief"], letter


def test_first_lines_come_from_pool_and_openers_mixed(h):
    seen_openers = set()
    for letter, spec in h.PERSONAS.items():
        for variant, vspec in spec["variants"].items():
            first = vspec["stages"][0]["line"]
            assert isinstance(first, str), (letter, variant)
            assert first in h.FIRST_REPLY_POOL, (letter, variant, first)
            assert vspec["opener"] in ("O1", "O2")
            seen_openers.add(vspec["opener"])
            assert vspec["fallback"], (letter, variant)
    assert seen_openers == {"O1", "O2"}


def test_ready_buyer_has_paid_paths(h):
    for variant in ("1", "2"):
        g = h.PERSONAS["G"]["variants"][variant]
        paid_standing = [r for r in g["standing"] if r.get("paid")]
        assert paid_standing, variant
        assert isinstance(g["fallback"], dict) and g["fallback"].get("paid"), variant
    # Nobody else may trigger the paid callback.
    for letter, spec in h.PERSONAS.items():
        if letter == "G":
            continue
        for variant, vspec in spec["variants"].items():
            assert not any(s.get("paid") for s in vspec["stages"]), (letter, variant)
            assert not any(r.get("paid") for r in vspec["standing"]), (letter, variant)
            assert not (isinstance(vspec["fallback"], dict) and vspec["fallback"].get("paid"))


def test_busy_seller_goes_quiet_then_back(h):
    for variant in ("1", "2"):
        stages = h.PERSONAS["B"]["variants"][variant]["stages"]
        lines = [s["line"] for s in stages]
        first_quiet = lines.index(None)
        assert any(line is not None for line in lines[first_quiet + 1:]), variant


def test_pick_stage_prefers_conditional_before_default(h):
    stages = [
        {"any_of": ["phone"], "line": "here is my number"},
        {"line": "default next line"},
    ]
    assert h.pick_stage(stages, 0, "what is your phone?") == (0, stages[0])
    assert h.pick_stage(stages, 0, "unrelated reply") == (1, stages[1])
    assert h.pick_stage(stages, 2, "phone") == (None, None)


def test_match_standing_rule_once_only(h):
    rules = [{"any_of": ["phone"], "line": "num", "once": "phone"}]
    assert h.match_standing_rule(rules, set(), "give me your phone") == rules[0]
    assert h.match_standing_rule(rules, {"phone"}, "give me your phone") is None


def test_resolve_line_shapes(h):
    assert h.resolve_line("hi") == ("hi", None)
    assert h.resolve_line(None) == (None, None)
    assert h.resolve_line({"text": "", "attachments": [{"type": "sticker"}]}) == (
        "", [{"type": "sticker"}],
    )


def test_cost_math_uses_published_muse_rates(h):
    assert h.cost_usd_est(1_000_000, 0) == pytest.approx(1.25)
    assert h.cost_usd_est(0, 1_000_000) == pytest.approx(4.25)
    assert h.estimate_tokens(400) == 100


def test_transcript_record_schema(h):
    rec = h.transcript_record("r", "main", "A1", 3, "amara", "hello", in_t=100, out_t=20)
    for key in ("run_id", "tree", "persona", "turn", "speaker", "text",
                "in_tokens_est", "out_tokens_est", "cost_usd_est", "cost_label"):
        assert key in rec, key
    assert rec["cost_label"] == "ESTIMATED"


def test_calibration_picks_richest_fitting_cap(h):
    # $0.01/turn: cap 15 -> 0.01*15*32*1.3 = $6.24 fits.
    got = h.recommend_turn_cap(0.03, 3)
    assert got["stop"] is False
    assert got["cap"] == 15


def test_calibration_stops_when_floor_does_not_fit(h):
    # $0.10/turn: even 8 -> 0.10*8*32*1.3 = $33.28.
    got = h.recommend_turn_cap(0.30, 3)
    assert got["stop"] is True
    assert got["cap"] is None


def test_calibration_respects_floor(h):
    # $0.015/turn: 15 -> $9.36 too rich; 11 -> $6.864 fits; 12 -> $7.488 over.
    got = h.recommend_turn_cap(0.045, 3)
    assert got["stop"] is False
    assert got["cap"] == 11
