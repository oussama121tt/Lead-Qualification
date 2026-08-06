"""
Taxonomy constants — single source of truth for segments, statuses and the
confidence threshold. Imported by app.py, db.py and scorer.py; never
redefined locally.
"""

VALID_SEGMENTS = {
    "ai_solo_founder", "technical_founder", "small_agency_scaling",
    "too_big", "wrong_field", "unclear",
}

TARGET_SEGMENTS = {"ai_solo_founder", "technical_founder", "small_agency_scaling"}
OUT_OF_TARGET_SEGMENTS = {"too_big", "wrong_field"}

NOT_YET_SCORED_STATUSES = (
    "NEW", "PARSED", "FETCH_PARTIAL", "FETCH_FAILED",
    "SCORE_FAILED", "RESCORE_PENDING", "RESCORE_FAILED",
)

CONFIDENCE_THRESHOLD = 0.7  # value from the original spec (FR-3)
