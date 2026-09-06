import json
import subprocess
import sys
from pathlib import Path

import scorer
from app import _categorize_leads

ROOT = Path(__file__).resolve().parents[1]


def test_every_golden_fixture_loads():
    cases = [json.loads(line) for line in (ROOT / "golden" / "cases.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(cases) >= 15
    for case in cases:
        fixture = ROOT / "golden" / "fixtures" / case["fixture"]
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        assert isinstance(payload.get("rows"), list)
        assert isinstance(payload.get("mock_verdict"), dict)


def test_golden_harness_runs_offline():
    result = subprocess.run(
        [sys.executable, "tools/run_golden.py", "--min-agreement", "0.8"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Agreement: 100.0%" in result.stdout


def test_golden_harness_fails_for_broken_case(tmp_path):
    cases_path = tmp_path / "broken-cases.jsonl"
    cases = (ROOT / "golden" / "cases.jsonl").read_text(encoding="utf-8").splitlines()
    for index in range(4):
        case = json.loads(cases[index])
        case["expected_segment"] = "wrong_field"
        cases[index] = json.dumps(case)
    cases_path.write_text("\n".join(cases) + "\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "tools/run_golden.py", "--cases", str(cases_path), "--min-agreement", "0.8"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "Agreement: 73.3%" in result.stdout


def test_sensitive_data_verdict_is_normalized():
    verdict = scorer._validate_verdict({
        "segment": "unclear",
        "recommended_offer": "none",
        "sensitive_data_categories": ["minors", "unknown", "none"],
        "data_sensitivity_score": 140,
    })
    assert verdict["sensitive_data_categories"] == ["minors"]
    assert verdict["data_sensitivity_score"] == 100


def test_budget_blocker_demotes_approved_lead():
    lead = {
        "id": 1,
        "status": "SCORED",
        "segment": "ai_solo_founder",
        "needs_human_review": 0,
        "budget_signal": "none",
        "budget_blockers": ["nonprofit / donation-funded"],
    }
    categories = _categorize_leads([lead])
    assert categories["approved"] == []
    assert categories["to_review"] == [lead]
    assert lead["disqualify_reason"] == "budget blocker"
