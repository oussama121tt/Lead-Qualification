"""Personalized outreach email generation for the results page.

Each email is generated individually by the LLM (through the provider
abstraction in llm_provider.py), personalized to the detected need of the
company and the recommended RuyaTech offer. This is a NEW step downstream
of the scoring — scorer.py is never touched here.
"""
import json

from llm_provider import get_llm_provider

EMAIL_PROMPT_TEMPLATE = """Tu rédiges un email de prospection court et personnalisé pour RuyaTech,
une agence technique qui construit, sauve et fait évoluer des produits SaaS pour des fondateurs.

Entreprise : {company_name}
Prénom du contact (s'il est vide, utilise "Bonjour," sans prénom) : {contact_first_name}
Segment détecté : {segment}
Offre recommandée : {recommended_offer}
Hooks de personnalisation déjà identifiés par le scoring : {personalization_hooks}
Preuves/citations tirées du site : {evidence_quotes}
Extrait du contenu de la homepage : {homepage_content}

Contexte des offres RuyaTech (choisis celle qui correspond à recommended_offer, reste fidèle au
positionnement exact ci-dessous — ne généralise pas, ne réinvente pas ce qu'on propose) :

- ai_audit → service "Product Rescue & Scale-Up" : pour les fondateurs non-techniques dont le
  produit a été construit avec l'IA (vibe-coding — Cursor, Replit, ChatGPT, Lovable, Bolt) et
  commence à craquer sous de vrais utilisateurs. Audit complet du code, stabilisation,
  refactoring, et remise en état de marche — généralement en 4 à 8 semaines. Exemple concret à
  réutiliser si pertinent : on a repris un SaaS AI-généré qui s'effondrait, relancé en 2
  semaines, 600 membres payants 6 mois après (case study Bake Genie).

- general_audit → même service "Product Rescue & Scale-Up", version pour une équipe technique :
  audit de sécurité et d'architecture, recommandations concrètes, priorisation des correctifs.

- pipeline → service "AI Agents & Automation" : agents IA et automatisations sur-mesure branchés
  sur l'existant (triage de leads, traitement de documents, workflows), pas des "gadgets IA".
  Exemple concret à réutiliser si pertinent : pipeline de tri de leads livré à un cabinet B2B
  débordé — 5h/semaine de business dev au lieu de plusieurs heures par jour, 30K$+ de nouveaux
  contrats en 30 jours.

Preuves générales sur RuyaTech, à utiliser avec parcimonie (une seule si besoin, jamais toutes
d'un coup) pour donner de la crédibilité sans que l'email ressemble à une plaquette commerciale :
prix fixe annoncé avant de coder (pas de facturation à l'heure), 10+ projets livrés, 100% du
code appartient au client dès le premier jour, réponse sous 4h ouvrées.

Consignes strictes :
- Objet court et spécifique à cette entreprise (pas générique, pas de template visible) — jamais vide, obligatoire dans toutes les réponses.
- Structure du corps, dans cet ordre, avec un retour à la ligne entre chaque bloc :
  1. Formule d'appel courte et directe (ex. "Bonjour," ou "Bonjour [prénom]," si un prénom de contact est disponible dans le contexte, sinon "Bonjour,").
  2. Accroche personnalisée (1-2 phrases) : le détail situationnel concret repéré sur leur site.
  3. Présentation de l'offre (1-2 phrases) : le lien entre ce détail et le service RuyaTech recommandé, avec au maximum une preuve concrète (case study/chiffre) si elle apporte une vraie crédibilité.
  4. Call-to-action (1 phrase) : une seule action claire (ex. proposer un échange rapide).
  5. Formule de politesse + signature (ex. "Bien à vous," puis un saut de ligne, puis "Oussama Ibrahim — RuyaTech").
- 4 à 6 phrases au total pour les blocs 1 à 4 (hors signature), en français, ton direct et professionnel, pas de superlatifs creux.
- N'écris jamais le corps comme un seul bloc de texte continu — les 5 parties ci-dessus doivent rester visuellement séparées par des sauts de ligne dans le champ "body".
- Personnalisation SITUATIONNELLE uniquement (ce que l'entreprise fait/utilise/a publié) — jamais biographique (rien sur la personne elle-même).
- Réutilise les hooks déjà fournis plutôt que d'en inventer de nouveaux non vérifiés.
- N'utilise les preuves RuyaTech (case studies, chiffres) que si elles apportent une vraie
  crédibilité au message — jamais comme remplissage, jamais plus d'une par email.
- Un seul call-to-action clair, vers l'offre recommandée.
- N'invente aucun fait qui n'est pas dans le contexte fourni ci-dessus.
- Réponds uniquement avec ce JSON, rien d'autre : {{"subject": "...", "body": "..."}}
  Le champ "subject" ne doit jamais être vide. Le champ "body" doit contenir les sauts de ligne
  ("\\n\\n" entre chaque bloc) qui structurent l'email tel que décrit ci-dessus.
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
        contact_first_name=lead.get("first_name") or "",
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