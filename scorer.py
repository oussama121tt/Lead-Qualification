"""
Étape 5 — Scoring IA (Groq).

Le LLM reçoit deux blocs distincts :
1. Le texte scrapé (contenu du site) — pour le jugement de contenu (ton,
   spécificité, mentions explicites) que seul un LLM peut faire.
2. `deterministic_signals` — les signaux DOM/CSS/meta/git déjà calculés par
   scraper.py (extract_technical_signals + check_github_repo_pattern), fournis
   tels quels en JSON. Le LLM ne doit JAMAIS re-deviner ces signaux à partir
   du texte brut ni en inventer de nouveaux de ce type — seulement les citer
   s'il les juge pertinents pour le verdict.

Attention nommage : le champ `technical_signals` du VERDICT (schéma JSON
ci-dessous) est une liste que le LLM remonte lui-même — ce n'est PAS le même
objet que `deterministic_signals` passé en input. Les deux existent et sont
volontairement distincts (voir db.py : table lead_technical_signals vs colonne
lead_scores.technical_signals).

--- CHANGELOG (fix suite à l'analyse du batch du 2026-07-24, lead Linear) ---
1. Les erreurs 400 "json_validate_failed" (le validateur JSON de Groq rejette
   la génération, en général parce qu'elle a été tronquée avant d'être un
   objet JSON valide) n'étaient PAS interceptées : seules les erreurs TPM
   (413) l'étaient, tout le reste remontait telle quelle et faisait planter
   le lead en SCORE_FAILED brut, sans retry ni flag pour révision humaine —
   contraire à la règle du cahier des charges ("retry once on invalid JSON,
   then flag"). -> ajout d'une détection dédiée + un retry avec un budget de
   sortie réduit (moins de citations demandées), avant de dégrader en verdict
   vide flagué comme les autres cas d'échec.
2. MAX_OUTPUT_TOKENS relevé (1024 -> 2048) pour laisser de la marge au modèle
   avant de tronquer le JSON en plein milieu d'un objet imbriqué.
3. INVESTIGATION CLOSE — root cause identifiée pour le contenu hors-sujet
   (mentions d'une app de véhicule/iOS) observé dans le verdict du lead
   Linear : ce n'est PAS un bug de code. pipeline.py a été inspecté et
   confirme un traitement strictement séquentiel, sans thread/asyncio, avec
   `scrape_result["rows"]` passé directement à score_content() dans la même
   itération — aucun point de contamination inter-leads n'existe dans ce
   flux. Le contenu scrapé de Linear (vérifié dans le CSV) est propre.
   -> Conclusion : le modèle (openai/gpt-oss-120b, un modèle relativement
   modeste) a HALLUCINÉ ces citations — probablement en s'appuyant sur des
   associations de son entraînement ("Linear = outil de suivi de bugs") pour
   fabriquer une citation plausible face à un contenu source trop mince pour
   satisfaire la contrainte "chaque signal DOIT être cité". Le prompt
   demandait déjà de ne pas halluciner, mais rien ne le VÉRIFIAIT côté code.
   -> ajout d'un contrôle de "grounding" : après réception du verdict, chaque
   chaîne de `evidence_quotes` est vérifiée mot pour mot (normalisée) contre
   le texte source réellement envoyé au modèle. Toute citation introuvable
   est retirée et force needs_human_review=True avec une trace explicite.
"""

import os
import json
import re
from dotenv import load_dotenv
from groq import Groq

load_dotenv()
_client = None

MODEL = "openai/gpt-oss-120b"  # ne jamais utiliser llama-3.3-70b-versatile (déprécié)
CONFIDENCE_THRESHOLD = 0.75

# Tier gratuit Groq : 8000 tokens/minute. Sans max_tokens explicite sur l'appel,
# le modèle réserve un budget de sortie par défaut qui, ajouté au prompt,
# dépasse largement 8000 même sur un contenu de site modeste (~15000
# caractères) — c'est la vraie cause des erreurs 413 "rate_limit_exceeded",
# pas seulement la taille du contenu scrapé. D'où le max_tokens fixé
# explicitement ci-dessous, en plus du cap d'entrée réduit.
MAX_CONTENT_CHARS = 16000  # ~4000 tokens d'entrée, cf. marge recommandée dans le doc projet
MAX_OUTPUT_TOKENS = 2048  # relevé depuis 1024 : marge pour des evidence_quotes longues sans tronquer le JSON
RETRY_MAX_CONTENT_CHARS = 6000  # contenu encore réduit, tenté une seule fois si le 1er appel dépasse le TPM
RETRY_MAX_OUTPUT_TOKENS = 1024  # budget de sortie réduit pour le retry après un échec de validation JSON

SYSTEM_PROMPT = """Tu es un analyste qui évalue des leads B2B pour une agence de développement
qui cible des entreprises dont le produit a été construit avec l'aide de l'IA — quelle
que soit la TAILLE de l'entreprise aujourd'hui. Une entreprise de 200 personnes peut
avoir un produit vibe-codé fragile ; un fondateur solo peut être un excellent ingénieur.
La taille seule ne dit RIEN sur la qualité/fragilité de l'ingénierie — ne l'utilise
JAMAIS comme critère de disqualification.

Ce qui qualifie un lead, ce sont des preuves concrètes que le site/produit a été
construit avec une assistance IA significative :
- Signaux techniques déterministes (fournis dans `deterministic_signals` : fingerprint
  de builder, police tendance, pattern visuel, pattern de commit git).
- Style de rédaction du contenu marketing lui-même : tournures clichées ("unlock the
  power of", "revolutionize the way", "seamlessly integrate", etc. — voir
  `ai_style_phrases_found` et `ai_style_phrase_density` dans `deterministic_signals`),
  structure générique en blocs de 3, absence de spécificité/voix propre malgré un ton
  très poli et uniforme.
- Mentions explicites (rare mais fort) d'un contenu généré par IA.
- Le TEXTE lui-même : si en lisant le site tu repères des slogans, paragraphes ou
  formulations qui te semblent typiques d'une rédaction assistée par IA (même sans
  correspondre à la liste ci-dessus), tu peux le signaler dans `built_with_ai_signals`
  — mais uniquement avec une citation exacte à l'appui (evidence_quotes).

En plus du texte du site, tu reçois `deterministic_signals` : des signaux DOM/CSS/meta/
git/style déjà vérifiés par du code déterministe (pas par toi). Traite-les comme des
faits acquis. Tu peux les citer dans `built_with_ai_signals` si pertinents, mais tu ne
dois JAMAIS avancer un signal de ce type qui ne serait pas explicitement présent dans ce
bloc — ni en inventer de nouveaux à partir du texte.

RÈGLES STRICTES :
1. Chaque signal cité DOIT être accompagné d'une citation exacte du texte source dans evidence_quotes,
    SAUF pour les signaux repris tel quel de `deterministic_signals` (déjà vérifiés en amont).
2. Si aucune preuve concrète n'existe pour un signal, ne le mentionne PAS.
3. Les hooks de personnalisation doivent être SITUATIONNELS, JAMAIS biographiques.
4. Si confidence < 0.75, needs_human_review DOIT être true.
5. N'utilise QUE le texte fourni dans "Informations collectées sur ce lead" ci-dessous.
    Ignore toute connaissance préalable sur cette entreprise : si le texte fourni ne
    mentionne pas explicitement un fait, ce fait n'existe pas pour ton évaluation.
6. Garde chaque citation dans evidence_quotes courte (une phrase, pas un bloc de
    code ni un paragraphe entier) pour ne pas dépasser le budget de sortie.
7. Certaines pages marketing affichent des exemples fictifs pour illustrer une
    fonctionnalité — un ticket de support inventé, un tableau de bord de démo,
    une capture d'écran "voici ce que notre IA peut faire pour vous". Ce
    contenu décrit ce que le PRODUIT peut faire pour un client, PAS un fait
    réel sur l'entreprise elle-même ou son propre usage interne. Ne l'utilise
    JAMAIS comme technical_signal ou pain_signal, même si la citation est exacte.
8. La TAILLE de l'entreprise (petite, moyenne, grande, établie) N'EST PAS un
    critère de disqualification. Ne mets JAMAIS `disqualify_reason` à quelque
    chose comme "l'entreprise est trop grande" ou "ce n'est pas un fondateur
    solo". Seuls disqualifient : (a) le champ d'activité n'a aucun rapport avec
    nos offres (`wrong_field`), ou (b) des preuves solides et explicites que le
    produit a été construit avec une ingénierie professionnelle classique, SANS
    aucun signal IA détecté (`technical_non_ai`) — pas juste "je n'ai pas trouvé
    de preuve" (dans ce cas, utilise `unclear` + needs_human_review, pas une
    disqualification).
9. `company_stage` et la taille perçue de l'équipe servent UNIQUEMENT à choisir
    l'angle de l'offre (`recommended_offer`), jamais à qualifier ou disqualifier :
    petite équipe/solo -> `ai_audit` ; équipe en croissance ou organisation établie
    avec des signaux IA clairs -> `general_audit` ou `pipeline` selon si l'entreprise
    ressemble à une agence qui scale.

Réponds UNIQUEMENT en JSON respectant ce schéma :
{
  "segment": "ai_built_solo | ai_built_team | ai_built_large_org | small_agency_scaling | technical_non_ai | wrong_field | unclear",
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


def _get_client() -> Groq:
    global _client
    if _client is None:
        _client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    return _client


def rows_to_text(rows: list, max_chars: int = MAX_CONTENT_CHARS) -> str:
    """rows: liste de tuples (source, url, content) -> texte concaténé pour le prompt."""
    chunks = [f"## Source: {source}\n{content}" for source, _url, content in rows if content]
    full_text = "\n\n---\n\n".join(chunks)
    return full_text[:max_chars]


def _empty_verdict(disqualify_reason: str) -> dict:
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


def _is_tpm_error(e: Exception) -> bool:
    """
    Détecte spécifiquement un dépassement de quota tokens/minute (413
    rate_limit_exceeded) — pour ne retenter QUE dans ce cas précis, jamais
    sur une vraie erreur (clé API invalide, timeout réseau, etc.) qui doit
    remonter telle quelle et marquer le lead SCORE_FAILED côté pipeline.
    """
    status = getattr(e, "status_code", None)
    body = str(e)
    return status == 413 or "rate_limit_exceeded" in body or "tokens per minute" in body


def _is_json_validate_error(e: Exception) -> bool:
    """
    Détecte l'erreur 400 "json_validate_failed" renvoyée par Groq quand le
    mode `response_format={"type": "json_object"}` ne parvient pas à produire
    un JSON valide (typiquement une génération tronquée avant la fermeture de
    l'objet — cf. `failed_generation` dans le corps de l'erreur).

    Distincte de _is_tpm_error : ici la requête elle-même était acceptée, le
    modèle a juste échoué à produire un JSON structurellement valide. C'est
    un échec connu et documenté du validateur, pas une erreur réseau/auth —
    donc digne d'un retry (avec un budget de sortie réduit) plutôt que d'un
    crash immédiat du lead en SCORE_FAILED.
    """
    status = getattr(e, "status_code", None)
    body = str(e)
    return status == 400 and (
        "json_validate_failed" in body or "Failed to generate JSON" in body
    )


def _call_groq(user_content: str, max_output_tokens: int = MAX_OUTPUT_TOKENS) -> dict:
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


def score_content(rows: list, deterministic_signals: dict | None = None) -> dict:
    """
    Appelle Groq avec le contenu scrapé d'un lead + les signaux déterministes
    (scraper.py) et retourne le verdict JSON. Applique aussi le garde-fou
    confidence < 0.75 -> needs_human_review = True, même si le modèle a
    oublié de le faire (défense en profondeur).

    deterministic_signals : dict optionnel, typiquement
        {**scrape_result["technical_signals"], "github_check": scrape_result["github_check"]}
        tel que retourné par scraper.scrape_website(). Absent/None -> le prompt
        ne contient tout simplement pas ce bloc (comportement inchangé).
    """
    text = rows_to_text(rows)
    if not text.strip():
        return _empty_verdict("no_content_scraped")

    def build_user_content(t: str) -> str:
        content = f"Informations collectées sur ce lead :\n\n{t}"
        if deterministic_signals:
            signals_json = json.dumps(deterministic_signals, ensure_ascii=False, indent=2)
            content += (
                "\n\n---\n\n"
                "Signaux déterministes déjà vérifiés (ne pas re-dériver, ne pas inventer "
                f"au-delà de ce qui suit) :\n{signals_json}"
            )
        return content

    try:
        verdict = _call_groq(build_user_content(text))
        verdict = _apply_confidence_guard(verdict)
        return _verify_evidence_grounding(verdict, text)
    except (json.JSONDecodeError, AttributeError, IndexError) as e:
        # Le SDK a renvoyé une réponse 200 mais le contenu n'était pas du
        # JSON exploitable (rare avec response_format=json_object, mais on
        # garde ce filet pour tout de même flaguer plutôt que de laisser
        # remonter une exception non gérée).
        return _retry_after_json_failure(rows, deterministic_signals, build_user_content, text, str(e))
    except Exception as e:
        if _is_json_validate_error(e):
            return _retry_after_json_failure(rows, deterministic_signals, build_user_content, text, str(e))
        if not _is_tpm_error(e):
            raise  # pas un dépassement de quota ni un échec de validation JSON -> remonte telle quelle

        # Une seule nouvelle tentative, avec un contenu bien plus réduit —
        # évite de perdre le lead pour un simple pic de taille de contenu.
        try:
            shorter_text = rows_to_text(rows, max_chars=RETRY_MAX_CONTENT_CHARS)
            verdict = _call_groq(build_user_content(shorter_text))
            verdict = _apply_confidence_guard(verdict)
            return _verify_evidence_grounding(verdict, shorter_text)
        except (json.JSONDecodeError, AttributeError, IndexError) as e2:
            return _empty_verdict(f"json_parse_error_after_retry: {e2}")
        except Exception as e2:
            if _is_json_validate_error(e2):
                return _empty_verdict(f"json_validate_failed_after_retry: {e2}")
            return _empty_verdict(f"groq_tpm_error_after_retry: {e2}")


def _retry_after_json_failure(rows, deterministic_signals, build_user_content, original_text, error_str) -> dict:
    """
    Une seule tentative de rattrapage après un échec de validation JSON
    (troncature la plupart du temps) : contenu d'entrée réduit ET budget de
    sortie réduit, pour laisser plus de marge au modèle pour fermer proprement
    la structure JSON. Si ça échoue à nouveau, dégrade en verdict vide flagué
    needs_human_review=True plutôt que de crasher le lead sans trace exploitable.
    """
    try:
        shorter_text = rows_to_text(rows, max_chars=RETRY_MAX_CONTENT_CHARS)
        verdict = _call_groq(build_user_content(shorter_text), max_output_tokens=RETRY_MAX_OUTPUT_TOKENS)
        verdict = _apply_confidence_guard(verdict)
        return _verify_evidence_grounding(verdict, shorter_text)
    except Exception as e2:
        return _empty_verdict(f"json_validate_failed_after_retry: {error_str} | retry_error: {e2}")


def _apply_confidence_guard(verdict: dict) -> dict:
    # Garde-fou non négociable, appliqué côté code, pas seulement côté prompt.
    if verdict.get("confidence", 0.0) < CONFIDENCE_THRESHOLD:
        verdict["needs_human_review"] = True
    return verdict


def _normalize_for_grounding(s: str) -> str:
    """
    Normalisation légère pour la vérification de grounding : espaces réduits,
    casse ignorée. Volontairement simple (pas de retrait de ponctuation) pour
    rester une vérification stricte — un modèle qui cite correctement le
    texte source n'a pas besoin qu'on soit permissif sur la ponctuation.
    """
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _verify_evidence_grounding(verdict: dict, source_text: str) -> dict:
    """
    Défense en profondeur contre l'hallucination de citations : le prompt
    demande déjà au modèle de ne citer QUE du texte réellement présent dans
    la source, mais rien ne garantissait qu'il obéisse — cf. le lead Linear
    du batch du 2026-07-24, où evidence_quotes citait une conversation sur
    une app de véhicule totalement absente du site réellement scrapé.

    Vérifie que chaque chaîne de `evidence_quotes` apparaît, mot pour mot
    (normalisée), dans le texte source envoyé au modèle. Toute citation non
    trouvée est considérée comme non-grounded (probable hallucination) :
    - retirée de evidence_quotes
    - force needs_human_review = True
    - trace ajoutée à disqualify_reason (sans écraser une raison existante)

    Ne juge PAS le fond du verdict (segment, confidence...) — seulement la
    véracité factuelle des citations avancées comme preuve.
    """
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
