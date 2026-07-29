"""
Export CSV — trois formats de sortie pour le reporting et la review humaine.

1. export_scraping_csv() : une ligne par page scrapée (source, url, contenu,
   signaux déterministes calculés par scraper.py). Utilisable dans Excel/Sheets.

2. export_scores_csv() : une ligne par lead avec le dernier verdict de scoring
   (segment, confiance, signaux, hooks). Point de sortie principal vers les
   outils d'envoi (Instantly/Smartlead).

3. export_readable_csv() : une ligne par lead avec aperçus tronqués des pages
   et signaux traduits en phrases. Conçu pour la review humaine rapide.

Séparé de db.py : fonctions de reporting, pas d'opérations sur le cycle de vie.
"""

import csv
import json
import io
import argparse

import db as dbmod


def _flatten(value):
    """
    Rend une valeur exploitable dans une cellule CSV :
    - None -> chaîne vide
    - liste/tuple -> jointe par ' | '
    - dict -> JSON compact (lisible mais recopiable)
    - chaîne déjà sérialisée en JSON (colonnes TEXT de db.py) -> parsée
      d'abord pour un rendu propre, sinon renvoyée telle quelle
    - scalaire -> inchangé
    """
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    if isinstance(value, (list, tuple)):
        return " | ".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return value


# ---------------------------------------------------------------------------
# 1) CSV du scraping (pages + signaux déterministes)
# ---------------------------------------------------------------------------

SCRAPING_FIELDS = [
    "lead_id", "company_name", "website_url", "status", "error",
    "source", "url", "content_chars", "content",
    "generator_fingerprint", "generator_meta_tag",
    "trend_fonts_found", "visual_patterns_triggered",
    "vibe_language_matches", "github_repo_url", "github_check",
    "ai_style_phrases_found", "ai_style_phrase_density",
    "ai_authorship_disclosures_found",
]


def _iter_scraping_rows(conn, session_id=None):
    """Générateur de lignes pour le CSV de scraping. Factorisé pour fichier et mémoire."""
    leads = dbmod.get_leads(conn, include_duplicates=True, session_id=session_id)

    for lead in leads:
        lead_id = lead["id"]
        pages = dbmod.get_lead_content(conn, lead_id)
        signals = dbmod.get_lead_technical_signals(conn, lead_id) or {}

        base_row = {
            "lead_id": lead_id,
            "company_name": lead.get("company_name", ""),
            "website_url": lead.get("website_url", ""),
            "status": lead.get("status", ""),
            "error": lead.get("error", ""),
            "generator_fingerprint": signals.get("generator_fingerprint", ""),
            "generator_meta_tag": signals.get("generator_meta_tag", ""),
            "trend_fonts_found": _flatten(signals.get("trend_fonts_found")),
            "visual_patterns_triggered": _flatten(signals.get("visual_patterns_triggered")),
            "vibe_language_matches": _flatten(signals.get("vibe_language_matches")),
            "github_repo_url": signals.get("github_repo_url", ""),
            "github_check": _flatten(signals.get("github_check")),
            "ai_style_phrases_found": _flatten(signals.get("ai_style_phrases_found")),
            "ai_style_phrase_density": signals.get("ai_style_phrase_density", ""),
            "ai_authorship_disclosures_found": _flatten(signals.get("ai_authorship_disclosures_found")),
        }

        if not pages:
            yield {**base_row, "source": "", "url": "", "content_chars": 0, "content": ""}
            continue

        for page in pages:
            content = page.get("content") or ""
            yield {
                **base_row,
                "source": page.get("source", ""),
                "url": page.get("url", ""),
                "content_chars": len(content),
                "content": content,
            }


def export_scraping_csv(conn, output_path: str, session_id=None) -> int:
    """
    Une ligne par (lead, page scrapée). Les leads sans contenu (pas encore
    scrapés, ou FETCH_FAILED) sont quand même écrits avec une ligne vide de
    contenu, pour ne perdre aucun lead du batch dans le fichier de sortie.

    Retourne le nombre de lignes écrites.
    """
    rows_written = 0
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=SCRAPING_FIELDS)
        writer.writeheader()
        for row in _iter_scraping_rows(conn, session_id=session_id):
            writer.writerow(row)
            rows_written += 1
    return rows_written


def scraping_csv_string(conn, session_id=None) -> str:
    """
    Même contenu que export_scraping_csv, mais renvoyé comme chaîne en
    mémoire (pas d'écriture disque) — pour le bouton de téléchargement.
    """
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=SCRAPING_FIELDS)
    writer.writeheader()
    for row in _iter_scraping_rows(conn, session_id=session_id):
        writer.writerow(row)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 2) CSV du scoring (verdicts LLM)
# ---------------------------------------------------------------------------

SCORE_FIELDS = [
    "lead_id", "first_name", "last_name", "title", "company_name", "email", "website_url",
    "status", "error", "is_duplicate", "duplicate_reason",
    "segment", "confidence", "needs_human_review", "company_stage",
    "recommended_offer", "disqualify_reason",
    "built_with_ai_signals", "technical_signals", "pain_signals",
    "evidence_quotes", "personalization_hooks", "scored_at",
]


def _iter_score_rows(conn, session_id=None):
    """
    Générateur de lignes pour le CSV de scoring — factorisé pour être
    consommé à la fois par export_scores_csv (fichier) et scores_csv_string
    (mémoire, pour le bouton de téléchargement).
    """
    leads = dbmod.get_leads_with_scores(conn, session_id=session_id)
    for lead in leads:
        yield {
            "lead_id": lead["id"],
            "first_name": lead.get("first_name", ""),
            "last_name": lead.get("last_name", ""),
            "title": lead.get("title", ""),
            "company_name": lead.get("company_name", ""),
            "email": lead.get("email", ""),
            "website_url": lead.get("website_url", ""),
            "status": lead.get("status", ""),
            "is_duplicate": lead.get("is_duplicate", 0),
            "duplicate_reason": lead.get("duplicate_reason", ""),
            "segment": lead.get("segment", ""),
            "confidence": lead.get("confidence", ""),
            "needs_human_review": lead.get("needs_human_review", ""),
            "company_stage": lead.get("company_stage", ""),
            "recommended_offer": lead.get("recommended_offer", ""),
            "disqualify_reason": lead.get("disqualify_reason", ""),
            "built_with_ai_signals": _flatten(lead.get("built_with_ai_signals")),
            "technical_signals": _flatten(lead.get("technical_signals")),
            "pain_signals": _flatten(lead.get("pain_signals")),
            "evidence_quotes": _flatten(lead.get("evidence_quotes")),
            "personalization_hooks": _flatten(lead.get("personalization_hooks")),
            "scored_at": lead.get("scored_at", ""),
        }


def export_scores_csv(conn, output_path: str, session_id=None) -> int:
    """
    Une ligne par lead (dernier verdict de scoring en date, via
    db.get_leads_with_scores). Les leads jamais scorés apparaissent quand
    même, avec des colonnes de verdict vides, pour garder une trace complète
    du batch (utile pour repérer les FETCH_FAILED / SCORE_FAILED).

    Retourne le nombre de lignes écrites.
    """
    rows_written = 0
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=SCORE_FIELDS)
        writer.writeheader()
        for row in _iter_score_rows(conn, session_id=session_id):
            writer.writerow(row)
            rows_written += 1
    return rows_written


def scores_csv_string(conn, session_id=None) -> str:
    """
    Même contenu que export_scores_csv, mais renvoyé comme chaîne en mémoire
    (pas d'écriture disque) — pour le bouton de téléchargement Flask.
    """
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=SCORE_FIELDS)
    writer.writeheader()
    for row in _iter_score_rows(conn, session_id=session_id):
        writer.writerow(row)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 3) CSV "lisible" — une ligne par lead, pensé pour être scanné à l'œil
#    (review humaine rapide sur un batch entier), pas pour l'analyse ligne
#    à ligne du contenu brut (ça reste le rôle du CSV de scraping ci-dessus).
# ---------------------------------------------------------------------------

DEFAULT_PREVIEW_CHARS = 400

READABLE_FIELDS = [
    "lead_id", "company_name", "website_url", "status",
    "segment", "confidence", "needs_human_review", "recommended_offer", "disqualify_reason",
    "signals_summary", "github_check_summary",
    "homepage_preview", "about_preview", "product_preview", "pricing_preview", "careers_preview",
    "evidence_quotes", "personalization_hooks",
    "search_evidence",
]


def _preview(text, max_chars: int = DEFAULT_PREVIEW_CHARS) -> str:
    """
    Aperçu tronqué et nettoyé d'un contenu de page : sauts de ligne écrasés
    en espaces simples (évite qu'une cellule CSV explose visuellement sur
    plusieurs lignes dans Excel/Sheets), coupé à `max_chars` avec un « … »
    explicite pour signaler que ce n'est qu'un extrait.
    """
    if not text:
        return "(page non trouvée / non scrapée)"
    flat = " ".join(str(text).split())
    if len(flat) <= max_chars:
        return flat
    return flat[:max_chars].rstrip() + " …"


def _format_signals_summary(signals: dict) -> str:
    """
    Traduit le dict `technical_signals` (JSON brut, pensé pour le prompt LLM)
    en une phrase unique lisible par un humain en review rapide. Aucune
    interprétation ajoutée : uniquement les signaux déjà calculés de façon
    déterministe par scraper.py, mis en forme.
    """
    if not signals:
        return "Aucun signal technique calculé."

    parts = []

    if signals.get("generator_fingerprint"):
        parts.append(f"Générateur IA détecté : {signals['generator_fingerprint']}")

    if signals.get("generator_meta_tag"):
        parts.append(f"Balise <meta generator> : {signals['generator_meta_tag']}")

    fonts = signals.get("trend_fonts_found") or []
    if fonts:
        parts.append(f"Polices tendance : {', '.join(fonts)}")

    patterns = signals.get("visual_patterns_triggered") or []
    if patterns:
        parts.append(f"Patterns visuels ({len(patterns)}/9) : {', '.join(patterns)}")

    vibe = signals.get("vibe_language_matches") or []
    if vibe:
        parts.append(f"Langage \"vibe-coding\" explicite : {', '.join(vibe)}")

    phrases = signals.get("ai_style_phrases_found") or []
    density = signals.get("ai_style_phrase_density")
    if phrases:
        shown = ", ".join(phrases[:5])
        suffix = ", ..." if len(phrases) > 5 else ""
        parts.append(f"Phrases marketing génériques (densité {density}) : {shown}{suffix}")

    disclosures = signals.get("ai_authorship_disclosures_found") or []
    if disclosures:
        parts.append(f"Mention explicite de contenu généré par IA : {', '.join(disclosures)}")

    if signals.get("github_repo_url"):
        parts.append(f"Repo GitHub public : {signals['github_repo_url']}")

    if not parts:
        return "Aucun signal technique déterministe détecté."
    return " | ".join(parts)


def _format_github_check_summary(github_check) -> str:
    """Résumé lisible du check git, s'il a été effectué."""
    if not isinstance(github_check, dict) or not github_check.get("checked"):
        return ""
    evidence = github_check.get("evidence", {}) or {}
    parts = [f"{evidence.get('total_commits_seen', '?')} commits vus (page API)"]
    if evidence.get("single_commit_repo"):
        parts.append("⚠️ repo à commit unique")
    first_msg = evidence.get("first_commit_message")
    if first_msg:
        parts.append(f'premier commit : "{first_msg[:60]}"')
    return " | ".join(parts)


def _iter_readable_rows(conn, session_id=None, preview_chars: int = DEFAULT_PREVIEW_CHARS):
    """
    Générateur de lignes pour le CSV lisible — une ligne par lead. Regroupe
    les pages scrapées par `source` (homepage/about/product/pricing/careers)
    pour donner à chacune sa propre colonne d'aperçu, au lieu d'une ligne par
    page comme dans le CSV de scraping brut.

    NB : pricing_preview et careers_preview restent pour l'instant un aperçu
    du texte brut tronqué (pas encore le signal ciblé "self-serve vs sales-only"
    / "N postes ingénierie" recommandé — ce point reste à coder séparément,
    dans scraper.py, une fois les extracteurs dédiés écrits).
    """
    leads = dbmod.get_leads_with_scores(conn, session_id=session_id)

    for lead in leads:
        lead_id = lead["id"]
        pages_by_source = {}
        for page in dbmod.get_lead_content(conn, lead_id):
            pages_by_source[page.get("source", "")] = page.get("content", "")

        signals = dbmod.get_lead_technical_signals(conn, lead_id) or {}
        search_evidence_list = dbmod.get_lead_search_evidence(conn, lead_id)
        search_summary_parts = []
        for ev in search_evidence_list:
            src = ev.get("source", "?")
            hits = ev.get("results") or []
            titles = [h.get("title", "") for h in hits if isinstance(h, dict)]
            search_summary_parts.append(f"{src}: {' | '.join(titles)}")
        search_summary = " ||| ".join(search_summary_parts) if search_summary_parts else ""

        yield {
            "lead_id": lead_id,
            "company_name": lead.get("company_name", ""),
            "website_url": lead.get("website_url", ""),
            "status": lead.get("status", ""),
            "segment": lead.get("segment", ""),
            "confidence": lead.get("confidence", ""),
            "needs_human_review": lead.get("needs_human_review", ""),
            "recommended_offer": lead.get("recommended_offer", ""),
            "disqualify_reason": lead.get("disqualify_reason", ""),
            "signals_summary": _format_signals_summary(signals),
            "github_check_summary": _format_github_check_summary(signals.get("github_check")),
            "homepage_preview": _preview(pages_by_source.get("homepage", ""), preview_chars),
            "about_preview": _preview(pages_by_source.get("about", ""), preview_chars),
            "product_preview": _preview(pages_by_source.get("product", ""), preview_chars),
            "pricing_preview": _preview(pages_by_source.get("pricing", ""), preview_chars),
            "careers_preview": _preview(pages_by_source.get("careers", ""), preview_chars),
            "evidence_quotes": _flatten(lead.get("evidence_quotes")),
            "personalization_hooks": _flatten(lead.get("personalization_hooks")),
            "search_evidence": search_summary,
        }


def export_readable_csv(conn, output_path: str, session_id=None, preview_chars: int = DEFAULT_PREVIEW_CHARS) -> int:
    """
    Une ligne par lead, pensée pour être ouverte dans Excel/Sheets et scannée
    du regard : segment/confiance en premier, signaux techniques traduits en
    une phrase, aperçu court de chaque page (pas le texte brut intégral).

    C'est un complément à export_scraping_csv (audit complet, une ligne par
    page) — pas un remplacement. Utilise celui-ci pour une review rapide,
    l'autre pour ré-analyser le contenu brut en détail si besoin.

    Retourne le nombre de lignes écrites (= nombre de leads).
    """
    rows_written = 0
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=READABLE_FIELDS)
        writer.writeheader()
        for row in _iter_readable_rows(conn, session_id=session_id, preview_chars=preview_chars):
            writer.writerow(row)
            rows_written += 1
    return rows_written


def readable_csv_string(conn, session_id=None, preview_chars: int = DEFAULT_PREVIEW_CHARS) -> str:
    """
    Même contenu que export_readable_csv, renvoyé en mémoire (pas d'écriture
    disque) — pour st.download_button côté Flask.
    """
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=READABLE_FIELDS)
    writer.writeheader()
    for row in _iter_readable_rows(conn, session_id=session_id, preview_chars=preview_chars):
        writer.writerow(row)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 4) CSV de la recherche web SGAI
# ---------------------------------------------------------------------------

SEARCH_FIELDS = [
    "lead_id", "company_name", "website_url", "source", "query",
    "result_url", "result_title", "result_snippet",
]


def _iter_search_rows(conn, session_id=None):
    """Générateur de lignes pour le CSV de recherche web — une ligne par résultat."""
    leads = dbmod.get_leads(conn, session_id=session_id, include_duplicates=False)
    for lead in leads:
        evidence = dbmod.get_lead_search_evidence(conn, lead["id"])
        for ev in evidence:
            source = ev.get("source", "")
            query = ev.get("query", "")
            results = ev.get("results") or []
            if isinstance(results, list):
                for hit in results:
                    yield {
                        "lead_id": lead["id"],
                        "company_name": lead.get("company_name", ""),
                        "website_url": lead.get("website_url", ""),
                        "source": source,
                        "query": query,
                        "result_url": hit.get("url", ""),
                        "result_title": hit.get("title", ""),
                        "result_snippet": (hit.get("content") or "")[:500],
                    }
            elif isinstance(results, dict) and "error" in results:
                yield {
                    "lead_id": lead["id"],
                    "company_name": lead.get("company_name", ""),
                    "website_url": lead.get("website_url", ""),
                    "source": source,
                    "query": query,
                    "result_url": "",
                    "result_title": "ERROR",
                    "result_snippet": results["error"],
                }

    # Inclut aussi les leads sans search evidence (ligne vide avec juste l'en-tête)
    # pour signaler qu'ils ont été scannés mais n'ont pas déclenché de recherche.


def export_search_csv(conn, output_path: str, session_id=None) -> int:
    """Exporte la recherche web SGAI en CSV — une ligne par résultat."""
    rows_written = 0
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=SEARCH_FIELDS)
        writer.writeheader()
        for row in _iter_search_rows(conn, session_id=session_id):
            writer.writerow(row)
            rows_written += 1
    return rows_written


def search_csv_string(conn, session_id=None) -> str:
    """Même contenu que export_search_csv, mais en mémoire."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=SEARCH_FIELDS)
    writer.writeheader()
    for row in _iter_search_rows(conn, session_id=session_id):
        writer.writerow(row)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# CLI — pour lancer les deux exports sans passer par Flask
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Exporte les résultats du scraping et du scoring en CSV depuis la base SQLite."
    )
    parser.add_argument("--db", default=dbmod.DB_PATH_DEFAULT, help="Chemin de la base SQLite (défaut: leads.db)")
    parser.add_argument("--scraping-out", default="scraping_results.csv", help="Chemin du CSV de scraping en sortie")
    parser.add_argument("--scores-out", default="scores_results.csv", help="Chemin du CSV de scoring en sortie")
    parser.add_argument("--search-out", default="search_results.csv", help="Chemin du CSV de recherche web en sortie")
    args = parser.parse_args()

    conn = dbmod.get_connection(args.db)
    dbmod.init_db(conn)  # no-op si les tables existent déjà (CREATE TABLE IF NOT EXISTS)

    n_scraping = export_scraping_csv(conn, args.scraping_out)
    n_scores = export_scores_csv(conn, args.scores_out)
    n_search = export_search_csv(conn, args.search_out)

    print(f"[export] {n_scraping} lignes écrites -> {args.scraping_out}")
    print(f"[export] {n_scores} lignes écrites -> {args.scores_out}")
    print(f"[export] {n_search} lignes écrites -> {args.search_out}")


if __name__ == "__main__":
    main()