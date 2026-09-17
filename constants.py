"""
Taxonomy constants — resolved from the active profile, not redefined locally.

VALID_SEGMENTS / TARGET_SEGMENTS / OUT_OF_TARGET_SEGMENTS snapshot the
profile at import (one profile per running instance, same model as
scorer.SYSTEM_PROMPT). NOT_YET_SCORED_STATUSES and CONFIDENCE_THRESHOLD
are machine concepts and stay literal here.
"""

from profile import load_profile

_profile = load_profile()

VALID_SEGMENTS = _profile.valid_segments
TARGET_SEGMENTS = _profile.target_segments
OUT_OF_TARGET_SEGMENTS = _profile.out_of_target_segments

NOT_YET_SCORED_STATUSES = (
    "NEW", "PARSED", "FETCH_PARTIAL", "FETCH_FAILED",
    "SCORE_FAILED", "RESCORE_PENDING", "RESCORE_FAILED",
)

CONFIDENCE_THRESHOLD = 0.7  # value from the original spec (FR-3)
