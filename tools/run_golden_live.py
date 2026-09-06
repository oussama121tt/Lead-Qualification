"""Live golden set runner (real Groq calls, no mock)"""
import json, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import scorer

CASES_PATH = ROOT / "golden" / "cases.jsonl"
FIXTURES_DIR = ROOT / "golden" / "fixtures"

def main():
    cases = [json.loads(l) for l in open(CASES_PATH, encoding="utf-8") if l.strip()]
    results=[]
    confusion={}
    print("id         expected                 got                      conf  review  result")
    print("-"*90)
    for case in cases:
        fixture = json.load(open(FIXTURES_DIR / case["fixture"], encoding="utf-8"))
        rows = fixture.get("rows", [])
        sig = fixture.get("technical_signals")
        web = fixture.get("web_search_evidence")
        missing = fixture.get("site_content_missing", False)
        try:
            v = scorer.score_content(rows, deterministic_signals=sig, web_search_evidence=web, site_content_missing=missing)
        except Exception as e:
            print(f"{case['id']:<10} ERROR {e}")
            results.append(False)
            confusion[(case["expected_segment"], f"ERROR:{e}")] = confusion.get((case["expected_segment"], f"ERROR:{e}"),0)+1
            time.sleep(3)
            continue
        checks = {
            "segment": v.get("segment")==case["expected_segment"],
            "offer": v.get("recommended_offer")==case["expected_offer"],
            "confidence": float(v.get("confidence",0)) >= float(case.get("min_confidence",0)),
        }
        if "expected_needs_human_review" in case:
            checks["review"] = bool(v.get("needs_human_review"))==bool(case["expected_needs_human_review"])
        passed = all(checks.values())
        results.append(passed)
        key=(case["expected_segment"], v.get("segment",""))
        confusion[key]=confusion.get(key,0)+1
        fail_keys=",".join(k for k,ok in checks.items() if not ok) or "all"
        print(f"{case['id']:<10} {case['expected_segment']:<12}/{case['expected_offer']:<10} -> {str(v.get('segment')):<12}/{str(v.get('recommended_offer')):<10} {v.get('confidence'):<4} {str(v.get('needs_human_review')):<6} {'PASS' if passed else 'FAIL('+fail_keys+')'} sens={v.get('sensitive_data_categories')}/{v.get('data_sensitivity_score')}")
        # debug for roxie
        if case["id"]=="roxie":
            print("  DEBUG roxie verdict:", json.dumps(v, indent=2, ensure_ascii=False))
        time.sleep(3)
    agreement = sum(results)/len(results) if results else 0
    print(f"\nAgreement: {agreement:.1%} ({sum(results)}/{len(results)})")
    print("\nConfusion matrix (expected -> got):")
    for (exp,got),cnt in sorted(confusion.items()):
        print(f"  {exp} -> {got}: {cnt}")
    return 0

if __name__=="__main__":
    main()
