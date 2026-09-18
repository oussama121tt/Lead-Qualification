"""Target persona compiler — transforms user checkboxes + free text into a
structured targeting specification (target_segments + disqualifying_segments).

This module is separate from scorer.py to keep the compilation logic separate
from the scoring logic. It uses the same model as scoring
(openai/gpt-oss-120b) with a dedicated 'compile' purpose for cost tracking.
"""
from __future__ import annotations

import json
import datetime

from llm_provider import get_llm_provider
from costlog import log_call
from profile import load_profile
import db as dbmod

MAX_TARGET_SPEC_CHARS = 1500  # mirrors MAX_SITE_CONTENT_CHARS / MAX_WEB_EVIDENCE_CHARS


# System prompt for the target compiler — separate from scorer's prompt
# This compiler transforms checkboxes + free text into structured JSON.
# It must NOT include scoring rules (citation, confidence, hooks, etc.)
_COMPILER_SYSTEM_PROMPT = """You are a targeting specification compiler. Your job is to translate
a user's checkbox selections and free-text description into a structured
targeting specification for a B2B lead scoring system.

Input:
- Checked criteria (list of keys): semantic lenses the user selected
- Free text (string): the user's own words describing their ideal customer

Output: Strict JSON with exactly these keys:
{
  "target_segments": [
    {"key": "...", "description": "...", "offer": "..."}
  ],
  "disqualifying_segments": [
    {"key": "...", "description": "..."}
  ],
  "ambiguous_points": ["..."]
}

Rules:
- target_segments: segments the user WANTS to target. Each has a key (segment id),
  a one-sentence description, and the offer it maps to.
- disqualifying_segments: segments the user explicitly wants to EXCLUDE or that
  are implied by their criteria (e.g., "no_ai" implies excluding early-stage AI builders).
- ambiguous_points: points where the user's intent is unclear and needs clarification.
  Empty list [] means the spec is ready to use.

Rules:
- Every description MUST be a single sentence, ≤ 160 chars. No paragraphs.
- Do NOT invent segments. Use only segment keys that exist in the user's profile.
- If the user's free text contradicts their checkboxes, flag the conflict in
  ambiguous_points rather than guessing.
- If input is insufficient to decide, add to ambiguous_points rather than guessing.
- Output ONLY the JSON, nothing else."""


# Maximum total size of the compiled spec (mirrors MAX_SITE_CONTENT_CHARS)
MAX_TARGET_SPEC_CHARS = 1500


def _build_compiler_prompt(profile, criteria: list[str], custom_text: str) -> str:
    """Builds the user prompt for the compiler LLM."""
    # Get segment info from profile for context
    segments_info = []
    for seg_key, seg in profile.segments.items():
        segments_info.append(f"- {seg_key}: {seg.description or seg_key} (offer: {seg.offer})")

    criteria_desc = []
    for crit in criteria:
        if crit in profile.criteria:
            c = profile.criteria[crit]
            criteria_desc.append(f"- {crit}: {c.ui_desc}")

    parts = [
        "User's checked criteria:",
        "\n".join(f"- {c}" for c in criteria_desc) if criteria_desc else "(none)",
        "",
        "User's free-text description of ideal customer:",
        custom_text if custom_text.strip() else "(none provided)",
        "",
        "Available segments in this profile:",
        "\n".join(segments_info),
        "",
        "Produce the JSON specification now.",
    ]
    return "\n".join(parts)


def _compile_with_llm(profile, criteria: list[str], custom_text: str) -> dict:
    """Calls the LLM to compile the persona. Returns the parsed JSON dict."""
    provider = get_llm_provider("scoring")  # same provider as scoring (cheapest available)
    user_prompt = _build_compiler_prompt(profile, criteria, custom_text)
    data, meta = provider.generate_json(
        user_prompt,
        system=_COMPILER_SYSTEM_PROMPT,
        temperature=0.1,
        max_tokens=2048,
    )

    # Log cost with purpose='compile'
    log_call(meta={**meta, "purpose": "compile"})

    # Parse and validate
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            raise ValueError("Compiler returned non-JSON output")

    # Validate required keys
    for key in ("target_segments", "disqualifying_segments", "ambiguous_points"):
        if key not in data:
            data[key] = []

    # Truncate if too large
    spec_json = json.dumps(data, ensure_ascii=False)
    if len(spec_json) > MAX_TARGET_SPEC_CHARS:
        # Truncate longest descriptions first
        for arr_key in ("target_segments", "disqualifying_segments"):
            arr = data.get(arr_key, [])
            for item in arr:
                for field in ("description",):
                    if item.get(field, "") and len(spec_json) > MAX_TARGET_SPEC_CHARS:
                        item[field] = item[field][:157] + "..."
        # Re-check
        spec_json = json.dumps(data, ensure_ascii=False)
        if len(spec_json) > MAX_TARGET_SPEC_CHARS:
            # Fallback: truncate ambiguous_points
            data["ambiguous_points"] = data.get("ambiguous_points", [])[:3]

    return data


def compile_persona(
    criteria: list[str],
    custom_text: str,
    session_id: int,
    profile_name: str | None = None,
) -> dict:
    """
    Main entry point: compiles a persona from checkboxes + free text.

    Flow:
    1. If custom_text is empty/blank -> deterministic path (Étape 2):
       uses profile.compile_deterministic_persona(), saves to DB, returns.
    2. Else -> LLM compiler (Étape 3):
       calls LLM, validates, truncates if needed, saves to DB, returns.
    """
    from profile import load_profile

    profile = load_profile(profile_name)

    # Check deterministic fast path (Étape 2)
    deterministic = profile.compile_deterministic_persona(criteria, custom_text)
    if deterministic is not None:
        # Save to DB
        import db as dbmod
        conn = dbmod.get_connection()
        try:
            from datetime import datetime
            profile_dict = {
                "target_segments": deterministic["target_segments"],
                "disqualifying_segments": deterministic["disqualifying_segments"],
                "raw_checkboxes": deterministic["raw_checkboxes"],
                "raw_custom_text": deterministic["raw_custom_text"],
            }
            save_scoring_profile(
                conn,
                session_id,
                profile_dict,
                "deterministic"
            )
        finally:
            conn.close()
        deterministic["compiled_at"] = datetime.datetime.utcnow().isoformat()
        deterministic["compiler_model"] = "deterministic"
        return deterministic

    # LLM compilation path (Étape 3)
    try:
        result = _compile_with_llm(profile, criteria, custom_text)
    except Exception:
        # On any failure, fall back to deterministic with what we have
        fallback = profile.compile_deterministic_persona(criteria, "")
        if fallback:
            return fallback
        raise

    # Save to DB
    conn = dbmod.get_connection()
    try:
        from datetime import datetime
        save_scoring_profile(
            conn,
            session_id,
            {
                "target_segments": result["target_segments"],
                "disqualifying_segments": result["disqualifying_segments"],
                "raw_checkboxes": criteria,
                "raw_custom_text": custom_text,
            },
            "openai/gpt-oss-120b"  # model name for audit
        )
    finally:
        conn.close()

    result["compiled_at"] = datetime.datetime.utcnow().isoformat()
    result["compiler_model"] = "openai/gpt-oss-120b"
    return result