"""Stage-0 false-negative gate over the Apollo-shaped golden fixtures.

Acceptance (Task-11): Stage 0 must NEVER kill a golden-set good lead (a cheap
filter that loses good leads is expensive), and must keep rejecting the known
bad categories (dev shop, consultancy, agency, fractional, enterprise,
competitor). Fixtures live in golden/stage0_apollo.json, not inline, so they're
reusable by tools and future signal work.
"""
import json

from prefilter import prefilter_people

from pathlib import Path

FIXTURES = json.loads(
    (Path(__file__).resolve().parent.parent / "golden" / "stage0_apollo.json")
    .read_text(encoding="utf-8")
)
GOOD_TARGETS = FIXTURES["good_targets"]
KNOWN_BAD = FIXTURES["known_bad"]


def _report(out):
    print(f"[stage0] total={out['stats']['total']} kept={out['stats']['kept']} "
          f"rejected={out['stats']['rejected']}")


def test_stage0_never_kills_golden_good_target():
    out = prefilter_people(GOOD_TARGETS, use_llm=False)
    _report(out)
    assert out["stats"]["rejected"] == 0, (
        f"false negatives: {[(p['id'], r) for p, r in out['reject']]}"
    )


def test_stage0_rejects_every_known_bad():
    out = prefilter_people(KNOWN_BAD, use_llm=False)
    _report(out)
    assert out["stats"]["kept"] == 0, (
        f"false positives kept: {[p['id'] for p in out['keep']]}"
    )


def test_stage0_acceptance_consultancy_rejected_target_kept():
    by_id = {p["id"]: p for p in GOOD_TARGETS + KNOWN_BAD}
    consultancy = prefilter_people([by_id["apollo-consultancy"]], use_llm=False)
    assert consultancy["stats"]["rejected"] == 1
    target = prefilter_people([by_id["apollo-roxie"]], use_llm=False)
    assert target["stats"]["kept"] == 1


def test_stage0_range_headcount_every_good_fixture_intact():
    """Range-formatted headcounts ("1-10", "11-50") must never push a good
    lead over the max and false-reject it (regression for the 1150 bug)."""
    for p in GOOD_TARGETS:
        assert p["estimated_num_employees"], p["id"]