"""
Scoring IA des leads via Groq API (compatible OpenAI SDK).

Évalue chaque lead à partir du contenu scrapé du site et des signaux
déterministes calculés par scraper.py. Produit un verdict JSON structuré
(segment, confiance, signaux, hooks, disqualification).

Deux flux d'entrée :
1. Texte scrapé (contenu de site) — analysé par le LLM pour le ton, la spécificité,
   les mentions explicites et les indices de construction IA.
2. deterministic_signals (optionnel) — signaux DOM/CSS/meta/git déjà calculés
   par scraper.py, fournis tels quels. Le LLM ne doit pas re-dériver ces signaux.
"""

import json
import os
import re
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

MODEL = "llama-3.3-70b-versatile"
CONFIDENCE_THRESHOLD = 0.7
MAX_CONTENT_CHARS = 16000  # ~4000 tokens pour respecter le quota TPD Groq
MAX_OUTPUT_TOKENS = 2048
RETRY_MAX_CONTENT_CHARS = 6000
RETRY_MAX_OUTPUT_TOKENS = 1024

SYSTEM_PROMPT = """Tu es un analyste senior qui évalue des leads B2B. Tu lis le contenu du site web
et tu détermines si cette entreprise correspond à nos offres :

1. Audit technique — pour les fondateurs solo ou techniques dont le produit a été
   construit avec l'aide de l'IA, ou qui ont besoin d'un regard extérieur.
2. Pipeline IA (lead-gen, $30K) — pour les agences qui scale.

Tu es intelligent : lis attentivement le contenu scrapé du site et décide du segment
le plus approprié. Ne force pas de catégorie — si c'est ambigu, dis-le.

RÈGLES :
1. Chaque signal cité DOIT avoir une citation exacte dans evidence_quotes (sauf signaux
   déjà vérifiés dans deterministic_signals).
2. Les hooks de personnalisation doivent être SITUATIONNELS (ex: "vous recrutez 3
   ingénieurs" d'après la page carrières), JAMAIS biographiques.
3. Si tu n'es pas sûr (confidence < 0.7), mets needs_human_review: true.
4. Utilise TOUT le spectre de confiance (0.0 à 1.0) : sois franc quand le signal est faible (0.3-0.5) et affirmé quand les preuves sont solides (0.9+). Évite le 0.8 systématique.
5. N'utilise QUE le texte fourni ci-dessous. Ignore toute connaissance préalable.
6. Les exemples/démos fictifs sur les landing pages ne sont PAS des faits réels sur
   l'entreprise. Ignore-les pour l'évaluation.
7. Distingue : "le PRODUIT a des features IA" vs "l'ÉQUIPE a construit avec l'IA".
   Si le site vend un produit avec des fonctionnalités IA, ce n'est PAS un signal
   built_with_ai — sauf mention explicite d'outils comme Cursor, v0, Bolt, etc.
8. Pour chaque lead, pose-toi ces questions :
   - Est-ce une agence/studio qui vend des services ? → small_agency_scaling
   - Est-ce un fondateur solo / micro-équipe avec des signaux IA ? → ai_solo_founder
   - Est-ce une équipe technique classique (produit SaaS, équipe visible) ? → technical_founder
   - Est-ce une grande organisation sans aucun signal IA ? → too_big
   - Est-ce un secteur sans rapport ? → wrong_field
   - Impossible à déterminer ? → unclear

Réponds UNIQUEMENT en JSON respectant ce schéma :
{
  "segment": "ai_solo_founder | technical_founder | small_agency_scaling | too_big | wrong_field | unclear",
  "confidence": 0.0,
  "company_stage": "pre-launch | early | scaling | established",
  "built_with_ai_signals": [],
  "technical_signals": [],
  "pain_signals": [],
  "evidence_quotes": [],
  "recommended_offer": "ai_audit | general_audit | pipeline | none",
  "personalization_hooks": [],
  "disqualify_reason": null,
  "needs_human_review": false
}"""

_client = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.getenv("GROQ_API_KEY"), base_url="https://api.groq.com/openai/v1")
    return _client


def _strip_images(text: str) -> str:
    """Supprime les marqueurs d'images et médias du texte avant envoi au LLM."""
    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)
    text = re.sub(r'!\[.*?\]\s*\[.*?\]', '', text)
    text = re.sub(r'<(img|figure|picture|video|source|svg|canvas)[^>]*>', '', text)
    text = re.sub(r'</(img|figure|picture|video|source|svg|canvas)>', '', text)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
    text = re.sub(r'https?://\S+\.(?:png|jpe?g|gif|svg|webp|bmp|ico)(?:\?\S*)?', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\[.*?\]\(\s*[^)]+\.(?:png|jpe?g|gif|svg|webp|bmp|ico)\s*\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\b\w+\.(?:png|jpe?g|gif|svg|webp|bmp|ico)(?:\?\S*)?', '', text, flags=re.IGNORECASE)
    return text


def rows_to_text(rows: list, max_chars: int = MAX_CONTENT_CHARS) -> str:
    """Concatène les pages scrapées en un seul bloc texte pour le prompt LLM."""
    chunks = [f"## Source: {source}\n{content}" for source, _url, content in rows if content]
    full_text = "\n\n---\n\n".join(chunks)
    return _strip_images(full_text[:max_chars])


def _empty_verdict(disqualify_reason: str) -> dict:
    """Verdict vide pour les cas d'échec (pas de contenu, erreur API, etc.)."""
    return {
        "segment": "unclear",
        "confidence": 0.0,
        "company_stage": None,
        "built_with_ai_signals": [],
        "technical_signals": [],
        "pain_signals": [],
        "evidence_quotes": [],
        "recommended_offer": "none",
        "personalization_hooks": [],
        "disqualify_reason": disqualify_reason,
        "needs_human_review": True,
    }


def _is_rate_limit_error(e: Exception) -> bool:
    """Détecte un dépassement de quota API (429) pour retenter avec un contenu réduit."""
    status = getattr(e, "status_code", None)
    body = str(e)
    return status in (413, 429) or "rate_limit_exceeded" in body or "rate limit" in body


def _is_json_parse_error(e: Exception) -> bool:
    """Détecte une réponse API non JSON (troncature, malformation)."""
    status = getattr(e, "status_code", None)
    if status == 400:
        return True
    if isinstance(e, (json.JSONDecodeError, KeyError, TypeError, ValueError)):
        return True
    return False


def _call_llm(user_content: str, max_output_tokens: int = MAX_OUTPUT_TOKENS) -> dict:
    """Appelle Groq et parse la réponse JSON."""
    client = _get_client()
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        temperature=0.2,
        max_tokens=max_output_tokens,
        response_format={"type": "json_object"},
    )
    return json.loads(response.choices[0].message.content)


def _apply_confidence_guard(verdict: dict) -> dict:
    """Force needs_human_review=True si la confiance est sous le seuil."""
    if verdict.get("confidence", 0.0) < CONFIDENCE_THRESHOLD:
        verdict["needs_human_review"] = True
    return verdict


def _normalize_for_grounding(s: str) -> str:
    """Normalisation légère pour comparer les citations avec le texte source."""
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _verify_evidence_grounding(verdict: dict, source_text: str) -> dict:
    """Vérifie que chaque evidence_quote apparaît mot pour mot dans le texte source."""
    quotes = verdict.get("evidence_quotes") or []
    if not quotes:
        return verdict

    normalized_source = _normalize_for_grounding(source_text)
    grounded = []
    ungrounded = []
    for q in quotes:
        if isinstance(q, str) and _normalize_for_grounding(q) in normalized_source:
            grounded.append(q)
        else:
            ungrounded.append(q)

    if ungrounded:
        verdict["evidence_quotes"] = grounded
        verdict["needs_human_review"] = True
        note = f"ungrounded_evidence_quotes_removed: {len(ungrounded)} citation(s) not found in source text"
        existing = verdict.get("disqualify_reason")
        verdict["disqualify_reason"] = f"{existing} | {note}" if existing else note

    return verdict


def _retry_after_failure(rows, deterministic_signals, build_user_content, error_str) -> dict:
    """Retente le scoring avec un contenu réduit après un échec de parsing JSON."""
    try:
        shorter_text = rows_to_text(rows, max_chars=RETRY_MAX_CONTENT_CHARS)
        verdict = _call_llm(build_user_content(shorter_text), max_output_tokens=RETRY_MAX_OUTPUT_TOKENS)
        verdict = _apply_confidence_guard(verdict)
        return _verify_evidence_grounding(verdict, shorter_text)
    except Exception as e2:
        return _empty_verdict(f"json_parse_failed: {error_str} | retry_error: {e2}")


def score_content(rows: list, deterministic_signals: dict | None = None, scoring_criteria: list[str] | None = None, scoring_criteria_custom: str = "") -> dict:
    """Évalue un lead à partir du contenu scrapé et des signaux déterministes.

    Args:
        rows: Liste de tuples (source, url, content) du scraper.
        deterministic_signals: Dict des signaux DOM/CSS/meta/git calculés par scraper.py.
        scoring_criteria: Liste de critères sélectionnés par l'utilisateur pour guider le scoring.
        scoring_criteria_custom: Texte libre saisi par l'utilisateur pour un critère personnalisé.

    Returns:
        Dict correspondant au schéma JSON du verdict (segment, confidence, etc.).
    """
    text = rows_to_text(rows)
    if not text.strip():
        return _empty_verdict("no_content_scraped")

    def build_user_content(t: str) -> str:
        content = f"Informations collectées sur ce lead :\n\n{t}"
        has_criteria = bool(scoring_criteria) or bool(scoring_criteria_custom)
        if has_criteria:
            content += "\n\n---\n\nCritères de scoring sélectionnés par l'utilisateur (accorde plus de poids à ces critères) :\n"
            if scoring_criteria:
                criteria_desc = {
                    "built_by_ai": "Détecter si le site a été développé PAR l'IA (vibe-codé, Cursor, Bolt, Lovable, fondateur solo).",
                    "built_with_ai": "Détecter si le site a été développé AVEC l'aide de l'IA (équipe technique qui utilise l'IA comme outil).",
                    "solo_or_small": "Identifier les fondateurs solo ou micro-équipes (1-5 personnes).",
                    "agency_or_studio": "Identifier les agences / studios de services qui scale.",
                    "no_ai": "Identifier les entreprises établies sans signal de construction IA.",
                    "wrong_field": "Identifier les secteurs d'activité sans rapport avec nos offres.",
                }
                for c in scoring_criteria:
                    desc = criteria_desc.get(c, c)
                    content += f"\n- {c} : {desc}"
            if scoring_criteria_custom:
                content += f"\n- Critère personnalisé : {scoring_criteria_custom}"
        if deterministic_signals:
            signals_json = json.dumps(deterministic_signals, ensure_ascii=False, indent=2)
            content += (
                "\n\n---\n\n"
                "Signaux déterministes déjà vérifiés (ne pas re-dériver, ne pas inventer "
                f"au-delà de ce qui suit) :\n{signals_json}"
            )
        return content

    try:
        verdict = _call_llm(build_user_content(text))
        verdict = _apply_confidence_guard(verdict)
        return _verify_evidence_grounding(verdict, text)
    except json.JSONDecodeError as e:
        return _retry_after_failure(rows, deterministic_signals, build_user_content, str(e))
    except Exception as e:
        if _is_json_parse_error(e):
            return _retry_after_failure(rows, deterministic_signals, build_user_content, str(e))
        if _is_rate_limit_error(e):
            try:
                shorter_text = rows_to_text(rows, max_chars=RETRY_MAX_CONTENT_CHARS)
                verdict = _call_llm(build_user_content(shorter_text))
                verdict = _apply_confidence_guard(verdict)
                return _verify_evidence_grounding(verdict, shorter_text)
            except Exception as e2:
                return _empty_verdict(f"api_error_after_retry: {e2}")
        raise
