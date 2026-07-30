"""
Scoring IA des leads via Groq API (compatible OpenAI SDK).

Évalue chaque lead à partir des métadonnées Apollo, du contenu scrapé du site,
et des signaux déterministes calculés par scraper.py. Produit un verdict JSON
structuré (segment, confiance, signaux, hooks, disqualification).

Trois flux d'entrée :
1. lead_metadata — nom, titre, entreprise, email (issus du CSV Apollo). Le
   titre du contact (ex: "CTO" vs "Founder") est un signal direct pour la
   distinction technical_founder / ai_solo_founder, et ne doit jamais être
   absent du prompt (cf. cahier des charges FR-3 : "Input: lead metadata +
   parsed site text").
2. Texte scrapé (contenu de site) — analysé par le LLM pour le ton, la
   spécificité, les mentions explicites et les indices de construction IA.
3. deterministic_signals (optionnel) — signaux DOM/CSS/meta/git déjà calculés
   par scraper.py, fournis tels quels avec leur niveau de fiabilité explicite.
   Le LLM ne doit pas re-dériver ces signaux, seulement les interpréter selon
   leur poids indiqué.

Segments (alignés sur le cahier des charges original, FR-3) :
  ai_solo_founder | technical_founder | small_agency_scaling | too_big |
  wrong_field | unclear
Offres :
  ai_audit | general_audit | pipeline | none
"""

import json
import os
import re
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

MODEL = "llama-3.3-70b-versatile"
CONFIDENCE_THRESHOLD = 0.7  # valeur du cahier des charges d'origine (FR-3)
MAX_CONTENT_CHARS = 16000  # ~4000 tokens pour respecter le quota TPD Groq
MAX_OUTPUT_TOKENS = 2048
RETRY_MAX_CONTENT_CHARS = 6000
RETRY_MAX_OUTPUT_TOKENS = 1024

# Confidence plafonnée quand le LLM renvoie un segment/offre hors schéma —
# un verdict qu'on vient de corriger de force ne peut pas rester "confiant".
INVALID_VERDICT_CONFIDENCE_CAP = 0.3

SYSTEM_PROMPT = """Tu es un analyste senior qui évalue des leads B2B pour une agence de
développement technique (RuyaTech). Tu reçois les métadonnées Apollo du contact, le contenu
scrapé de son site web, et des signaux déterministes déjà calculés (ne pas les re-dériver).

DEUX OFFRES VENDUES :
- Audit technique — pour des fondateurs qui ont un produit fragile derrière une belle façade
  (ai_audit si construit avec l'IA par un non-technique, general_audit si équipe technique
  mais avec de la dette/des lacunes).
- Pipeline IA de lead-gen — vendu à des agences/studios qui scalent leur propre acquisition
  de clients (offre "pipeline").

NOTRE CIBLE PRINCIPALE : les fondateurs non-techniques qui utilisent l'IA pour développer
leur produit (vibe coding, Cursor, Bolt, Lovable, Replit, etc.). Ils ont besoin d'un audit
technique car leur code manque de robustesse.

SEGMENTS (choisis-en UN SEUL, jamais une valeur inventée en dehors de cette liste) :
- ai_solo_founder — fondateur non-technique, produit construit avec l'IA (vibe coding).
  → recommended_offer: ai_audit
- technical_founder — fondateur/équipe technique, utilise l'IA comme outil de dev (pas comme
  béquille). → recommended_offer: general_audit
- small_agency_scaling — agence ou studio de services en phase de scale (recrute, plusieurs
  clients visibles, cherche à industrialiser). → recommended_offer: pipeline
- too_big — entreprise établie, taille/maturité largement au-dessus du persona ciblé (équipe
  importante, produit mature depuis des années, pas de signal de fragilité technique).
  → recommended_offer: none
- wrong_field — secteur sans rapport avec nos offres (pas de produit logiciel, pas de site
  technique à auditer). → recommended_offer: none
- unclear — PREUVES INSUFFISANTES pour trancher entre les catégories ci-dessus. C'est un état
  normal et honnête, pas un échec : utilise-le chaque fois que le contenu est trop mince, trop
  ambigu, ou contradictoire pour choisir un segment avec confiance. → needs_human_review
  obligatoirement true, recommended_offer généralement none sauf signal partiel exploitable.

Ne confonds jamais "unclear" (pas assez de preuves) avec "wrong_field" (preuves claires que ce
n'est pas notre cible) ou "too_big" (preuves claires que c'est trop gros) — ces trois segments
disent des choses différentes et doivent rester distincts.

HIÉRARCHIE DE FIABILITÉ DES SIGNAUX DÉTERMINISTES (fournis en fin de message) — respecte-la
strictement, ne traite jamais deux signaux de force différente comme équivalents :
- FORT (quasi-preuve) : generator_fingerprint non-null (référence directe à un outil comme
  lovable.dev, bolt.new, v0.dev...), ai_authorship_disclosures_found non vide (l'entreprise dit
  elle-même utiliser l'IA pour son contenu), github_check.single_commit_repo=true combiné à un
  generator_fingerprint présent.
- MOYEN : vibe_language_matches non vide (mention explicite "built with X" dans le HTML),
  ai_style_phrase_density "high".
- FAIBLE (jamais suffisant seul) : visual_patterns_triggered (gradient, shadcn_ui, glassmorphism,
  numbered_steps...) — des milliers de produits professionnels bien construits utilisent ces
  mêmes conventions visuelles modernes. Un visual_pattern seul, sans signal FORT ou MOYEN
  l'accompagnant, ne doit JAMAIS faire pencher vers ai_solo_founder. Traite-le comme un indice
  qui mérite au mieux needs_human_review, jamais une conclusion.
- Un fingerprint/pattern peut provenir de code invisible pour l'utilisateur (script tiers,
  tracker, widget) — s'il est isolé et que rien d'autre ne corrobore (pas de mention explicite
  dans le texte visible, pas de langage vibe-coding), baisse ta confiance en conséquence plutôt
  que de le traiter comme acquis.

RÈGLES :
1. Chaque signal cité dans built_with_ai_signals/technical_signals/pain_signals DOIT avoir une
   citation exacte dans evidence_quotes (sauf signaux déjà vérifiés dans deterministic_signals,
   que tu peux citer par leur nom de champ).
2. Les hooks de personnalisation doivent être SITUATIONNELS (ex: "vous recrutez 3 ingénieurs"
   d'après la page carrières), JAMAIS biographiques (ex: jamais où quelqu'un a étudié, son âge,
   son parcours personnel).
3. Si tu n'es pas sûr (confidence < 0.7), mets needs_human_review: true.
4. Utilise TOUT le spectre de confiance (0.0 à 1.0) : sois franc quand le signal est faible
   (0.3-0.5) et affirmé quand les preuves sont solides (0.9+). Évite le 0.8 systématique.
5. N'utilise QUE le texte fourni ci-dessous. Ignore toute connaissance préalable sur
   l'entreprise.
6. Les exemples/démos fictifs sur les landing pages (captures d'écran de l'interface produit,
   tickets de démo, données d'exemple) NE SONT PAS des faits réels sur l'entreprise elle-même.
   Ignore-les pour juger COMMENT l'entreprise a été construite.
7. Distingue strictement : "le PRODUIT a des features IA / parle d'IA dans son positionnement"
   vs "l'ÉQUIPE a construit CE SITE/PRODUIT avec des outils IA". Un produit qui vend de l'IA à
   ses clients n'est PAS en soi un signal built_with_ai — seule une mention explicite d'outils
   de build (Cursor, v0, Bolt, Lovable, "vibe coded"...) ou un generator_fingerprint compte.
8. Utilise le titre du contact (fourni dans les métadonnées) comme signal direct : un titre
   "CTO"/"Lead Engineer"/"VP Engineering" pointe vers technical_founder, un titre "Founder"/"CEO"
   sans titre technique en parallèle est cohérent avec ai_solo_founder si d'autres signaux
   corroborent.
9. Pour chaque lead, pose-toi ces questions dans l'ordre :
   a) Preuves suffisantes pour trancher ? Si non → unclear.
   b) Signal FORT ou MOYEN de construction IA par une équipe non-technique ? → ai_solo_founder.
   c) Équipe technique confirmée (titre + signaux) utilisant l'IA comme outil ? → technical_founder.
   d) Agence/studio en phase de scale ? → small_agency_scaling.
   e) Taille/maturité largement au-dessus du persona cible ? → too_big.
   f) Secteur sans rapport ? → wrong_field.

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


def _format_lead_metadata(lead_metadata: dict | None) -> str:
    """
    Formate les métadonnées Apollo du lead (nom, titre, entreprise, email) en
    bloc texte pour le prompt. Absent du schéma FR-3 original si on ne le
    fait pas — le titre du contact notamment est un signal direct pour
    distinguer technical_founder de ai_solo_founder.
    """
    if not lead_metadata:
        return ""
    fields = [
        ("Nom", " ".join(filter(None, [lead_metadata.get("first_name"), lead_metadata.get("last_name")])).strip()),
        ("Titre", lead_metadata.get("title")),
        ("Entreprise", lead_metadata.get("company_name")),
        ("Email", lead_metadata.get("email")),
        ("Site web", lead_metadata.get("website_url")),
    ]
    lines = [f"- {label}: {value}" for label, value in fields if value]
    if not lines:
        return ""
    return "Métadonnées du contact (source Apollo) :\n" + "\n".join(lines)


VALID_SEGMENTS = {
    "ai_solo_founder", "technical_founder", "small_agency_scaling",
    "too_big", "wrong_field", "unclear",
}
VALID_OFFERS = {"ai_audit", "general_audit", "pipeline", "none"}
VALID_STAGES = {"pre-launch", "early", "scaling", "established"}


def _validate_verdict(verdict: dict) -> dict:
    """Valide et corrige les champs enum du verdict LLM.

    Important : quand segment ou recommended_offer est hors schéma, on
    plafonne aussi `confidence` — un verdict qu'on vient de corriger de
    force ne peut pas rester affiché comme "confiant" (bug corrigé : avant,
    un segment invalide forcé à "unclear" pouvait garder une confidence
    d'origine à 0.9, ce qui est contradictoire).
    """
    forced_correction = False

    if verdict.get("segment") not in VALID_SEGMENTS:
        verdict["segment"] = "unclear"
        verdict["needs_human_review"] = True
        note = "invalid_segment_fixed_to_unclear"
        existing = verdict.get("disqualify_reason")
        verdict["disqualify_reason"] = f"{existing} | {note}" if existing else note
        forced_correction = True

    if verdict.get("recommended_offer") not in VALID_OFFERS:
        verdict["recommended_offer"] = "none"
        forced_correction = True

    if verdict.get("company_stage") not in VALID_STAGES:
        verdict["company_stage"] = None

    if forced_correction:
        try:
            current_confidence = float(verdict.get("confidence", 0.0))
        except (TypeError, ValueError):
            current_confidence = 0.0
        verdict["confidence"] = min(current_confidence, INVALID_VERDICT_CONFIDENCE_CAP)
        verdict["needs_human_review"] = True

    return verdict


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
        verdict = _validate_verdict(verdict)
        return _verify_evidence_grounding(verdict, shorter_text)
    except Exception as e2:
        return _empty_verdict(f"json_parse_failed: {error_str} | retry_error: {e2}")


def score_content(
    rows: list,
    deterministic_signals: dict | None = None,
    lead_metadata: dict | None = None,
    scoring_criteria: list[str] | None = None,
    scoring_criteria_custom: str = "",
) -> dict:
    """Évalue un lead à partir des métadonnées Apollo, du contenu scrapé et des
    signaux déterministes.

    Args:
        rows: Liste de tuples (source, url, content) du scraper.
        deterministic_signals: Dict des signaux DOM/CSS/meta/git calculés par scraper.py.
        lead_metadata: Dict des champs Apollo du lead (first_name, last_name, title,
            company_name, email, website_url) — cf. FR-3 du cahier des charges,
            "Input: lead metadata + parsed site text". Absent jusqu'ici, ajouté ici.
        scoring_criteria: Liste de critères sélectionnés par l'utilisateur pour guider le scoring.
        scoring_criteria_custom: Texte libre saisi par l'utilisateur pour un critère personnalisé.

    Returns:
        Dict correspondant au schéma JSON du verdict (segment, confidence, etc.).
    """
    text = rows_to_text(rows)
    if not text.strip():
        return _empty_verdict("no_content_scraped")

    def build_user_content(t: str) -> str:
        parts = []

        metadata_block = _format_lead_metadata(lead_metadata)
        if metadata_block:
            parts.append(metadata_block)

        parts.append(f"Informations collectées sur ce lead :\n\n{t}")

        has_criteria = bool(scoring_criteria) or bool(scoring_criteria_custom)
        if has_criteria:
            criteria_block = "Critères de scoring sélectionnés par l'utilisateur (accorde plus de poids à ces critères) :\n"
            if scoring_criteria:
                criteria_desc = {
                    "vibe_coder": "CIBLE PRINCIPALE : repérer les fondateurs non-techniques qui construisent avec l'IA (vibe coding, Cursor, Bolt, Lovable, Replit) — correspond au segment ai_solo_founder.",
                    "technical_ai_user": "CIBLE SECONDAIRE : repérer les équipes techniques qui utilisent l'IA comme outil de développement — correspond au segment technical_founder.",
                    "solo_or_small": "Identifier les fondateurs solo ou micro-équipes (1-5 personnes).",
                    "agency_or_studio": "Identifier les agences / studios de services qui scalent — correspond au segment small_agency_scaling.",
                    "no_ai": "Identifier les entreprises établies sans signal de construction IA.",
                    "not_target": "Identifier les leads qui ne sont clairement pas notre cible (too_big, wrong_field).",
                }
                for c in scoring_criteria:
                    desc = criteria_desc.get(c, c)
                    criteria_block += f"\n- {c} : {desc}"
            if scoring_criteria_custom:
                criteria_block += f"\n- Critère personnalisé : {scoring_criteria_custom}"
            parts.append(criteria_block)

        if deterministic_signals:
            signals_json = json.dumps(deterministic_signals, ensure_ascii=False, indent=2)
            parts.append(
                "Signaux déterministes déjà vérifiés (ne pas re-dériver, ne pas inventer "
                "au-delà de ce qui suit — applique la hiérarchie de fiabilité FORT/MOYEN/FAIBLE "
                f"décrite dans tes instructions) :\n{signals_json}"
            )

        return "\n\n---\n\n".join(parts)

    try:
        verdict = _call_llm(build_user_content(text))
        verdict = _apply_confidence_guard(verdict)
        verdict = _validate_verdict(verdict)
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
                verdict = _validate_verdict(verdict)
                return _verify_evidence_grounding(verdict, shorter_text)
            except Exception as e2:
                return _empty_verdict(f"api_error_after_retry: {e2}")
        raise