"""
Déduplication (étape 2) — jamais de suppression, uniquement un flag `is_duplicate`.

3 niveaux, dans cet ordre :
1. Email exact
2. Domaine normalisé identique (www.acme.io/pricing -> acme.io)
3. Fuzzy matching sur le nom d'entreprise (RapidFuzz), seuil réglable (défaut 90)

Un lead n'est jamais comparé à un lead déjà marqué doublon (on compare toujours
contre les leads "originaux" restants), pour éviter les chaînes de doublons
qui pointent les uns sur les autres.
"""

from rapidfuzz import fuzz
import db as dbmod


def run_dedup(conn, fuzzy_threshold: int = 90, session_id: int | None = None) -> dict:
    leads = dbmod.get_leads(conn, include_duplicates=True, session_id=session_id)
    # On ne traite que ceux pas encore marqués doublon, dans l'ordre d'insertion :
    # le premier vu pour un email/domaine/nom donné reste "l'original".
    seen_emails = {}
    seen_domains = {}
    originals_for_fuzzy = []  # liste de (lead_id, company_name_normalized)

    stats = {"exact_email": 0, "domain": 0, "fuzzy_company": 0, "kept_original": 0}

    for lead in leads:
        if lead["is_duplicate"]:
            continue  # déjà traité dans une passe précédente, on ne le retouche pas

        lead_id = lead["id"]
        email = (lead["email"] or "").strip().lower()
        domain = (lead["domain_normalized"] or "").strip().lower()
        company = (lead["company_name"] or "").strip().lower()

        # --- Niveau 1 : email exact ---
        if email and email in seen_emails:
            dbmod.mark_duplicate(conn, lead_id, seen_emails[email], "exact_email")
            stats["exact_email"] += 1
            continue

        # --- Niveau 2 : domaine normalisé ---
        if domain and domain in seen_domains:
            dbmod.mark_duplicate(conn, lead_id, seen_domains[domain], "domain_match")
            stats["domain"] += 1
            continue

        # --- Niveau 3 : fuzzy sur nom d'entreprise ---
        duplicate_of = None
        if company:
            for other_id, other_company in originals_for_fuzzy:
                score = fuzz.token_sort_ratio(company, other_company)
                if score >= fuzzy_threshold:
                    duplicate_of = other_id
                    break

        if duplicate_of is not None:
            dbmod.mark_duplicate(conn, lead_id, duplicate_of, "fuzzy_company_name")
            stats["fuzzy_company"] += 1
            continue

        # Aucun doublon trouvé : ce lead devient un "original" pour les suivants
        stats["kept_original"] += 1
        if email:
            seen_emails[email] = lead_id
        if domain:
            seen_domains[domain] = lead_id
        if company:
            originals_for_fuzzy.append((lead_id, company))

    return stats


def check_against_export_history(conn, exported_domains: set) -> int:
    """
    Vérification inter-batch (dédup contre tous les exports passés).
    `exported_domains` = ensemble des domaines déjà exportés (à charger depuis
    un fichier/table d'historique d'export — branché à l'étape 8, pas encore ici).
    Retourne le nombre de leads nouvellement flaggés.
    """
    leads = dbmod.get_leads(conn, include_duplicates=False)
    flagged = 0
    for lead in leads:
        domain = (lead["domain_normalized"] or "").strip().lower()
        if domain and domain in exported_domains:
            dbmod.mark_duplicate(conn, lead["id"], None, "already_exported_previous_batch")
            flagged += 1
    return flagged
