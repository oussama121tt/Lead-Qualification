"""Run the offline scoring regression set."""
import argparse
import inspect
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scorer

CASES_PATH = ROOT / "golden" / "cases.jsonl"
FIXTURES_DIR = ROOT / "golden" / "fixtures"


def _load_cases(path=CASES_PATH):
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _score_case(case, fixture):
    canned = fixture["mock_verdict"]
    original_call = scorer._call_llm
    scorer._call_llm = lambda _content, max_output_tokens=scorer.MAX_OUTPUT_TOKENS: dict(canned)
    try:
        kwargs = {
            "deterministic_signals": fixture.get("technical_signals"),
            "web_search_evidence": fixture.get("web_search_evidence"),
            "site_content_missing": fixture.get("site_content_missing", False),
        }
        if "cost_cb" in inspect.signature(scorer.score_content).parameters:
            kwargs["cost_cb"] = lambda *_args, **_kwargs: None
        return scorer.score_content(fixture.get("rows", []), **kwargs)
    finally:
        scorer._call_llm = original_call


def _checks(case, verdict):
    checks = {
        "segment": verdict.get("segment") == case["expected_segment"],
        "offer": verdict.get("recommended_offer") == case["expected_offer"],
        "confidence": float(verdict.get("confidence", 0)) >= float(case.get("min_confidence", 0)),
    }
    if "expected_needs_human_review" in case:
        checks["review"] = bool(verdict.get("needs_human_review")) == bool(case["expected_needs_human_review"])
    return checks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-agreement", type=float, default=0.8)
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    args = parser.parse_args()
    cases = _load_cases(args.cases)
    results = []
    confusion = Counter()
    confidence_correct = []
    confidence_incorrect = []

    print("id         result  segment                    offer             checks")
    print("-" * 78)
    for case in cases:
        fixture_path = FIXTURES_DIR / case["fixture"]
        with fixture_path.open(encoding="utf-8") as handle:
            fixture = json.load(handle)
        verdict = _score_case(case, fixture)
        checks = _checks(case, verdict)
        passed = all(checks.values())
        results.append(passed)
        confusion[(case["expected_segment"], verdict.get("segment", ""))] += 1
        confidence = float(verdict.get("confidence", 0))
        (confidence_correct if passed else confidence_incorrect).append(confidence)
        check_text = ",".join(name for name, ok in checks.items() if not ok) or "all"
        print(f"{case['id']:<10} {'PASS' if passed else 'FAIL':<7} {verdict.get('segment',''):<26} {verdict.get('recommended_offer',''):<17} {check_text}")

    agreement = sum(results) / len(results) if results else 0.0
    print(f"\nAgreement: {agreement:.1%} ({sum(results)}/{len(results)})")
    print("\nConfusion matrix (expected -> predicted):")
    for (expected, predicted), count in sorted(confusion.items()):
        print(f"  {expected} -> {predicted}: {count}")
    def average(values):
        return sum(values) / len(values) if values else 0.0
    print(f"\nConfidence calibration: correct={average(confidence_correct):.3f}, incorrect={average(confidence_incorrect):.3f}")
    return 0 if agreement >= args.min_agreement else 1


if __name__ == "__main__":
    sys.exit(main())
