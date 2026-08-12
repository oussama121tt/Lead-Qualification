"""Personalized outreach email generation for the results page.

Each email is generated individually by the LLM (through the provider
abstraction in llm_provider.py), personalized to the detected need of the
company and the recommended RuyaTech offer. This is a NEW step downstream
of the scoring — scorer.py is never touched here.
"""
import json

from llm_provider import get_llm_provider

EMAIL_PROMPT_TEMPLATE = """Tu rédiges un email de prospection court et personnalisé pour RuyaTech.

Entreprise : {company_name}
Segment détecté : {segment}
Offre recommandée : {recommended_offer}
Hooks de personnalisation déjà identifiés par le scoring : {personalization_hooks}
Preuves/citations tirées du site : {evidence_quotes}
Extrait du contenu de la homepage : {homepage_content}

Contexte des offres RuyaTech (choisis celle qui correspond à recommended_offer) :
- ai_audit : audit technique pour fondateurs non-techniques ayant construit avec l'IA (vibe-coding), pour sécuriser et consolider une base fragile.
- general_audit : audit technique pour fondateurs/équipes techniques.
- pipeline : pipeline IA de lead-gen (case study à $30K) pour agences/studios en phase de scale.

Consignes strictes :
- Objet court et spécifique à cette entreprise (pas générique, pas de template visible).
- Corps de 4 à 6 phrases maximum, en français, ton direct et professionnel, pas de superlatifs creux.
- Personnalisation SITUATIONNELLE uniquement (ce que l'entreprise fait/utilise/a publié) — jamais biographique (rien sur la personne elle-même).
- Réutilise les hooks déjà fournis plutôt que d'en inventer de nouveaux non vérifiés.
- Un seul call-to-action clair, vers l'offre recommandée.
- N'invente aucun fait qui n'est pas dans le contexte fourni ci-dessus.
- Réponds uniquement avec ce JSON, rien d'autre : {{"subject": "...", "body": "..."}}
"""

MAX_HOOK_CHARS = 800
MAX_HOMEPAGE_CHARS = 1200


def _as_text(value) -> str:
    """Renders a hook/quotes value into a plain text snippet.

    The scoring stores these fields as JSON (list of objects). Passed
    through get_leads_with_scores they arrive as raw TEXT, so both the
    already-parsed and the still-string forms are handled here.
    """
    if not value:
        return "aucun"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)[:MAX_HOOK_CHARS]
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in "[{":
            try:
                parsed = json.loads(stripped)
                return json.dumps(parsed, ensure_ascii=False)[:MAX_HOOK_CHARS]
            except Exception:
                pass
        return stripped[:MAX_HOOK_CHARS]
    return str(value)[:MAX_HOOK_CHARS]


def build_prompt(lead: dict, homepage_content: str) -> str:
    return EMAIL_PROMPT_TEMPLATE.format(
        company_name=lead["company_name"] or "cette entreprise",
        segment=lead.get("segment") or "inconnu",
        recommended_offer=lead.get("recommended_offer") or "none",
        personalization_hooks=_as_text(lead.get("personalization_hooks")),
        evidence_quotes=_as_text(lead.get("evidence_quotes")),
        homepage_content=(homepage_content or "")[:MAX_HOMEPAGE_CHARS],
    )


def generate_email_for_lead(lead: dict, homepage_content: str) -> dict:
    """Returns {"subject": ..., "body": ...} or raises an exception, handled by the caller."""
    provider = get_llm_provider()
    prompt = build_prompt(lead, homepage_content)
    return provider.generate_json(prompt)