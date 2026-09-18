"""Business-profile loader — one TOML file describes what we sell and to whom.

Usage:
    from profile import load_profile
    profile = load_profile()              # LEAD_PROFILE env or [profile] name in config.toml, default "ruyatech"
    profile.identity.company              # -> "RuyaTech"
    profile.target_segments               # -> {"ai_solo_founder", ...}

The result is cached per process (same model as runconfig.load_config):
the profile is not meant to change mid-run. Tests can call clear_cache().

Naming convention (TOML keys): snake_case everywhere; [offers.<id>],
[segments.<id>], [criteria.<key>], [sequences.offers.<id>];
[[derivation.rules]] rows with when_founder / when_build / set_segment /
set_offer / confidence / needs_review. See profiles/README.md.

Reserved machine names (never business): the "unclear" segment and the
"none" offer are the catch-all unknowns every profile must define; the
founder_profile x build_evidence axis vocabulary stays in the engine.
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROFILES_DIR = HERE / "profiles"
DEFAULT_PROFILE_NAME = "ruyatech"


@dataclass
class Identity:
    company: str
    sender_name: str = ""
    sender_title: str = ""
    one_liner: str = ""
    personal_line_blurb: str = ""
    sequence_blurb: str = ""
    proof_points: list[str] = field(default_factory=list)


@dataclass
class Offer:
    key: str
    name: str = ""
    label: str = ""
    short_who: str = ""
    who: str = ""
    detail: str = ""
    case_study: str = ""
    email_blurb: str = ""


@dataclass
class Segment:
    key: str
    description: str = ""
    offer: str = "none"
    is_target: bool = False
    label: str = ""
    escalate_on_hiring: bool = False


@dataclass
class CriteriaItem:
    key: str
    label: str = ""
    ui_desc: str = ""
    prompt_desc: str = ""


@dataclass
class Icp:
    max_headcount: int = 50
    min_headcount: int = 0
    countries: list[str] = field(default_factory=list)
    founded_year_min: int = 0
    founded_year_max: int = 0
    person_titles: list[str] = field(default_factory=list)
    employee_ranges: list[str] = field(default_factory=list)
    reject_company_markers: list[str] = field(default_factory=list)
    dev_shop_pattern: str = ""
    reject_title_markers: list[str] = field(default_factory=list)
    non_decision_title_markers: list[str] = field(default_factory=list)
    founder_title_markers: list[str] = field(default_factory=list)
    llm_prefilter_system: str = ""


@dataclass
class VoiceHooks:
    """Offer-specific hook sentences for the generators ([voice.hooks]).

    The two prompts word the implication slightly differently, so each has
    its own verbatim field; mention_ban is the offer noun the line must
    never name (rendered as "do not mention {company} or {mention_ban}").
    """
    personal_line_implication: str = ""
    sequence_implication: str = ""
    mention_ban: str = ""


@dataclass
class Voice:
    examples: list[str] = field(default_factory=list)
    subject_examples: list[str] = field(default_factory=list)
    question_examples: list[str] = field(default_factory=list)
    hooks: VoiceHooks = field(default_factory=VoiceHooks)
    # Offer-specific subject-phrase bans (regex alternation fragments),
    # appended to the generic anti-spam patterns in campaign_fields.
    extra_banned_subject_phrases: list[str] = field(default_factory=list)


@dataclass
class SequenceOffer:
    key: str
    id: str = ""
    sensitive_id: str = ""


@dataclass
class Sequences:
    enabled: bool = False
    send_from_email: str = ""
    personal_line_field_id: str = ""
    personalised_share: float = 0.5
    offers: dict[str, SequenceOffer] = field(default_factory=dict)

    def sequence_for(self, offer: str | None, sensitive: bool = False) -> str | None:
        """Offer -> Apollo sequence id. Sensitive variant wins when set."""
        if not offer:
            return None
        entry = self.offers.get(offer)
        if entry is None:
            return None
        if sensitive and entry.sensitive_id:
            return entry.sensitive_id
        return entry.id or None


@dataclass
class Stages:
    values: list[str] = field(default_factory=list)
    prompt_description: str = ""


@dataclass
class Sensitive:
    categories: list[str] = field(default_factory=list)
    empty_sentinel: str = "none"
    score_max: int = 100
    prompt_description: str = ""


@dataclass
class Budget:
    signals: list[str] = field(default_factory=list)
    empty_sentinel: str = "none"
    prompt_description: str = ""


@dataclass
class DerivationRule:
    when_founder: str = "any"
    when_build: str = "any"
    set_segment: str = ""
    set_offer: str = ""
    confidence: str = "keep"  # "keep" | "clamp_0_5_0_7"
    needs_review: bool = True


@dataclass
class Profile:
    name: str
    identity: Identity
    offers: dict[str, Offer]
    segments: dict[str, Segment]
    icp: Icp
    voice: Voice
    sequences: Sequences
    derivation_rules: list[DerivationRule] = field(default_factory=list)
    criteria: dict[str, CriteriaItem] = field(default_factory=dict)
    stages: Stages = field(default_factory=Stages)
    sensitive: Sensitive = field(default_factory=Sensitive)
    budget: Budget = field(default_factory=Budget)
    scoring_axes_prose: str = ""
    scoring_extra_rules: list[str] = field(default_factory=list)
    scoring_career_hint: str = ""
    scoring_unclear_note: str = ""
    # Compiled Stage-0 regexes, built from the marker lists at load time.
    agency_company_re: re.Pattern | None = None
    dev_shop_re: re.Pattern | None = None
    agency_title_re: re.Pattern | None = None
    non_decision_title_re: re.Pattern | None = None
    founder_title_re: re.Pattern | None = None

    @property
    def valid_segments(self) -> set[str]:
        return set(self.segments)

    @property
    def target_segments(self) -> set[str]:
        return {k for k, s in self.segments.items() if s.is_target}

    @property
    def out_of_target_segments(self) -> set[str]:
        return {k for k, s in self.segments.items()
                if not s.is_target and k not in ("unclear",)}

    @property
    def offer_ids(self) -> list[str]:
        return list(self.offers)

    @property
    def segment_ids(self) -> list[str]:
        return list(self.segments)

    def offers_sentence(self) -> str:
        """Scorer OFFERS sentence, derived from the offer atoms."""
        clauses = [f"{o.key} is for {o.short_who}" for o in self.offers.values()]
        return "; ".join(clauses) + "."

    def choice_sentence(self) -> str:
        keys = self.segment_ids
        if len(keys) == 1:
            return f"Choose exactly one segment: {keys[0]}."
        return "Choose exactly one segment: " + ", ".join(keys[:-1]) + ", or " + keys[-1] + "."

    def offer_map_sentence(self) -> str:
        offers = [s.offer for s in self.segments.values()]
        if len(offers) == 1:
            return f"Map this segment to {offers[0]}."
        return "Map those segments to " + ", ".join(offers[:-1]) + ", and normally " + offers[-1] + "."

    def allowed_segments(self, criteria: list[str] | None) -> list[str] | None:
        """Segment ids selected by checked criteria keys, in profile order.

        None (or empty) criteria → None = no filtering (default behavior:
        the model may propose any segment). A non-empty list that matches
        no segment (e.g. lenses only) returns [] — callers must refuse to
        score rather than silently ignore the selection.
        """
        if not criteria:
            return None
        wanted = set(criteria)
        return [s for s in self.segment_ids if s in wanted]

    def for_segments(self, allowed: list[str]) -> "Profile":
        """Filtered view: only the given segments (profile order) and the
        offers they reference. The existing sentence builders then render
        exclusively from the selection."""
        keep = [s for s in self.segment_ids if s in set(allowed)]
        segments = {k: self.segments[k] for k in keep}
        used_offers = {s.offer for s in segments.values()} & set(self.offers)
        offers = {k: self.offers[k] for k in self.offers if k in used_offers}
        return replace(self, segments=segments, offers=offers)

    def compile_deterministic_persona(self, criteria: list[str], custom_text: str = "") -> dict | None:
        """
        Deterministic lens→segment compiler (Étape 2 — court-circuit sans LLM).

        Returns a compiled persona dict if the criteria alone (without free text)
        can fully determine the target segments. Returns None if:
        - custom_text is non-empty (user provided free text → needs LLM)
        - criteria contains lens keys that don't map to any segment deterministically
        - no valid segments found

        This is the ZERO-LLM fast path: only direct segment keys or known lens
        mappings are resolved without any LLM call.
        """
        # If user provided free text, we must use the LLM compiler
        if custom_text and custom_text.strip():
            return None

        if not criteria:
            return None

        # Deterministic lens→segment mapping for RuyaTech (hardcoded for compatibility)
        lens_map = {
            "ai_solo_founder": [("ai_solo_founder", "ai_audit", "Non-technical founder whose product was built with AI and starts breaking under real users")],
            "technical_founder": [("technical_founder", "general_audit", "Technical team needing architecture or security review")],
            "solo_or_small": [],  # orthogonal size filter, no direct segment
            "agency_or_studio": [("small_agency_scaling", "pipeline", "Agency or studio that is scaling")],
            "no_ai": [],  # orthogonal signal, maps to too_big/wrong_field contextually
            "wrong_field": [("wrong_field", "none", "Lead clearly outside the target field")],
        }

        target_segments = []
        disqualifying_segments = []
        seen = set()

        for crit in criteria:
            if crit in lens_map:
                for seg_key, offer, desc in lens_map[crit]:
                    if seg_key not in seen and seg_key in self.segments:
                        seg = self.segments[seg_key]
                        target_segments.append({
                            "key": seg_key,
                            "description": desc,
                            "offer": seg.offer,
                        })
                        seen.add(seg_key)
                    # Also add disqualifying segments for lenses that imply exclusion
                    if crit == "no_ai":
                        # no_ai implies too_big and wrong_field are relevant as disqualifiers
                        for dq_key in ("too_big", "wrong_field"):
                            if dq_key in self.segments and dq_key not in seen:
                                dq_seg = self.segments[dq_key]
                                disqualifying_segments.append({
                                    "key": dq_key,
                                    "description": f"Leads that are {dq_seg.description or dq_key}",
                                })
                                seen.add(dq_key)

        if not target_segments and not disqualifying_segments:
            return None

        return {
            "target_segments": target_segments,
            "disqualifying_segments": disqualifying_segments,
            "raw_checkboxes": criteria,
            "raw_custom_text": "",
            "compiled_at": "",  # filled by caller
            "compiler_model": "deterministic",
        }

    def segment_enum(self) -> str:
        return " | ".join(self.segment_ids)

    def offer_enum(self) -> str:
        return " | ".join([*self.offer_ids, "none"])

    def criteria_options(self) -> list[dict]:
        """Review-queue picker entries, in profile order."""
        return [{"key": c.key, "label": c.label, "desc": c.ui_desc}
                for c in self.criteria.values()]

    def segment_labels(self) -> dict[str, str]:
        return {s.key: (s.label or s.key) for s in self.segments.values()}

    def offer_labels(self) -> dict[str, str]:
        return {o.key: (o.label or o.key) for o in self.offers.values()}


def _profile_name(explicit: str | None) -> str:
    if explicit:
        return explicit
    env = os.getenv("LEAD_PROFILE", "").strip()
    if env:
        return env
    try:
        raw = tomllib.loads((HERE / "config.toml").read_text(encoding="utf-8"))
        name = str(raw.get("profile", {}).get("name", "")).strip()
        if name:
            return name
    except (OSError, tomllib.TOMLDecodeError):
        pass
    return DEFAULT_PROFILE_NAME


def _compile(markers: list[str]) -> re.Pattern | None:
    if not markers:
        return None
    return re.compile(r"\b(?:" + "|".join(markers) + r")\b", re.I)


_cached: Profile | None = None
_cached_name: str | None = None


def load_profile(name: str | None = None, path: Path | None = None) -> Profile:
    """Load profiles/<name>.toml as typed dataclasses, cached per process."""
    global _cached, _cached_name
    resolved = _profile_name(name) if path is None else (name or _profile_name(None))
    if _cached is not None and path is None and _cached_name == resolved:
        return _cached

    toml_path = path or (PROFILES_DIR / f"{resolved}.toml")
    raw = tomllib.loads(toml_path.read_text(encoding="utf-8"))

    ident = raw.get("identity", {})
    identity = Identity(
        company=str(ident.get("company", "")),
        sender_name=str(ident.get("sender_name", "")),
        sender_title=str(ident.get("sender_title", "")),
        one_liner=str(ident.get("one_liner", "")),
        personal_line_blurb=str(ident.get("personal_line_blurb", "")),
        sequence_blurb=str(ident.get("sequence_blurb", "")),
        proof_points=[str(p) for p in ident.get("proof_points", [])],
    )

    offers = {
        key: Offer(key=key, name=str(v.get("name", "")), label=str(v.get("label", "")),
                   short_who=str(v.get("short_who", "")), who=str(v.get("who", "")),
                   detail=str(v.get("detail", "")), case_study=str(v.get("case_study", "")),
                   email_blurb=str(v.get("email_blurb", "")))
        for key, v in raw.get("offers", {}).items()
    }

    segments = {
        key: Segment(key=key, description=str(v.get("description", "")),
                     offer=str(v.get("offer", "none")),
                     is_target=bool(v.get("is_target", False)),
                     label=str(v.get("label", key)),
                     escalate_on_hiring=bool(v.get("escalate_on_hiring", False)))
        for key, v in raw.get("segments", {}).items()
    }

    criteria = {
        key: CriteriaItem(key=key, label=str(v.get("label", "")),
                          ui_desc=str(v.get("ui_desc", "")),
                          prompt_desc=str(v.get("prompt_desc", "")))
        for key, v in raw.get("criteria", {}).items()
    }

    icp_raw = raw.get("icp", {})
    icp = Icp(
        max_headcount=int(icp_raw.get("max_headcount", 50)),
        min_headcount=int(icp_raw.get("min_headcount", 0)),
        countries=[str(c) for c in icp_raw.get("countries", [])],
        founded_year_min=int(icp_raw.get("founded_year_min", 0)),
        founded_year_max=int(icp_raw.get("founded_year_max", 0)),
        person_titles=[str(t) for t in icp_raw.get("person_titles", [])],
        employee_ranges=[str(r) for r in icp_raw.get("employee_ranges", [])],
        reject_company_markers=[str(m) for m in icp_raw.get("reject_company_markers", [])],
        dev_shop_pattern=str(icp_raw.get("dev_shop_pattern", "")),
        reject_title_markers=[str(m) for m in icp_raw.get("reject_title_markers", [])],
        non_decision_title_markers=[str(m) for m in icp_raw.get("non_decision_title_markers", [])],
        founder_title_markers=[str(m) for m in icp_raw.get("founder_title_markers", [])],
        llm_prefilter_system=str(icp_raw.get("llm_prefilter_system", "")),
    )

    voice_raw = raw.get("voice", {})
    hooks_raw = voice_raw.get("hooks", {})
    voice = Voice(
        examples=[str(e) for e in voice_raw.get("examples", [])],
        subject_examples=[str(e) for e in voice_raw.get("subject_examples", [])],
        question_examples=[str(e) for e in voice_raw.get("question_examples", [])],
        hooks=VoiceHooks(
            personal_line_implication=str(hooks_raw.get("personal_line_implication", "")),
            sequence_implication=str(hooks_raw.get("sequence_implication", "")),
            mention_ban=str(hooks_raw.get("mention_ban", "")),
        ),
        extra_banned_subject_phrases=[str(e) for e in voice_raw.get("extra_banned_subject_phrases", [])],
    )

    seq_raw = raw.get("sequences", {})
    seq_offers = {
        key: SequenceOffer(key=key, id=str(v.get("id", "")),
                           sensitive_id=str(v.get("sensitive_id", "")))
        for key, v in seq_raw.get("offers", {}).items()
    }
    sequences = Sequences(
        enabled=bool(seq_raw.get("enabled", False)),
        send_from_email=str(seq_raw.get("send_from_email", "")),
        personal_line_field_id=str(seq_raw.get("personal_line_field_id", "")),
        personalised_share=float(seq_raw.get("personalised_share", 0.5)),
        offers=seq_offers,
    )

    rules = [
        DerivationRule(
            when_founder=str(r.get("when_founder", "any")),
            when_build=str(r.get("when_build", "any")),
            set_segment=str(r.get("set_segment", "")),
            set_offer=str(r.get("set_offer", "")),
            confidence=str(r.get("confidence", "keep")),
            needs_review=bool(r.get("needs_review", True)),
        )
        for r in raw.get("derivation", {}).get("rules", [])
    ]

    stages_raw = raw.get("stages", {})
    stages = Stages(
        values=[str(v) for v in stages_raw.get("values", [])],
        prompt_description=str(stages_raw.get("prompt_description", "")),
    )
    sensitive_raw = raw.get("sensitive", {})
    sensitive = Sensitive(
        categories=[str(c) for c in sensitive_raw.get("categories", [])],
        empty_sentinel=str(sensitive_raw.get("empty_sentinel", "none")),
        score_max=int(sensitive_raw.get("score_max", 100)),
        prompt_description=str(sensitive_raw.get("prompt_description", "")),
    )
    budget_raw = raw.get("budget", {})
    budget = Budget(
        signals=[str(s) for s in budget_raw.get("signals", [])],
        empty_sentinel=str(budget_raw.get("empty_sentinel", "none")),
        prompt_description=str(budget_raw.get("prompt_description", "")),
    )

    profile = Profile(
        name=resolved,
        identity=identity,
        offers=offers,
        segments=segments,
        icp=icp,
        voice=voice,
        sequences=sequences,
        derivation_rules=rules,
        criteria=criteria,
        stages=stages,
        sensitive=sensitive,
        budget=budget,
        scoring_axes_prose=str(raw.get("scoring", {}).get("axes_prose", "")),
        scoring_extra_rules=[str(r) for r in raw.get("scoring", {}).get("extra_rules", [])],
        scoring_career_hint=str(raw.get("scoring", {}).get("career_hint", "")),
        scoring_unclear_note=str(raw.get("scoring", {}).get("unclear_note", "")),
        agency_company_re=_compile(icp.reject_company_markers),
        dev_shop_re=re.compile(icp.dev_shop_pattern, re.I) if icp.dev_shop_pattern else None,
        agency_title_re=_compile(icp.reject_title_markers),
        non_decision_title_re=_compile(icp.non_decision_title_markers),
        founder_title_re=_compile(icp.founder_title_markers),
    )
    if path is None:
        _cached = profile
        _cached_name = resolved
    return profile


def clear_cache() -> None:
    """Reset the per-process cache (tests only)."""
    global _cached, _cached_name
    _cached = None
    _cached_name = None
