"""
Backfill ponctuel : calcule email_domain / domain_mismatch / domain_mismatch_reason
pour les leads déjà en base AVANT le patch de db.py (colonnes ajoutées mais
jamais remplies pour ces lignes-là).

Usage :
    python backfill_domain_mismatch.py [chemin_vers_leads.db]

Sans argument, utilise "leads.db" (le chemin par défaut de l'app).
Idempotent : peut être relancé sans risque, il ne fait que recalculer et
écraser ces trois colonnes à partir de email/website_url déjà en base.
"""

import sqlite3
import sys

import db as dbmod  # réutilise _email_domain / _domains_related / FREE_EMAIL_PROVIDERS


def backfill(db_path: str) -> None:
    conn = dbmod.get_connection(db_path)
    dbmod.init_db(conn)  # s'assure que les colonnes/migration existent déjà

    leads = conn.execute(
        "SELECT id, email, website_url, domain_normalized FROM leads"
    ).fetchall()

    updated = 0
    flagged = 0

    for lead in leads:
        email_domain = dbmod._email_domain(lead["email"])
        site_domain = lead["domain_normalized"] or dbmod._normalize_domain(lead["website_url"])

        domain_mismatch = 0
        reason = None
        if (
            email_domain
            and email_domain not in dbmod.FREE_EMAIL_PROVIDERS
            and site_domain
            and not dbmod._domains_related(email_domain, site_domain)
        ):
            domain_mismatch = 1
            reason = (
                f"email domain '{email_domain}' does not match "
                f"website domain '{site_domain}'"
            )
            flagged += 1

        conn.execute(
            """
            UPDATE leads
            SET email_domain = ?, domain_mismatch = ?, domain_mismatch_reason = ?
            WHERE id = ?
            """,
            (email_domain, domain_mismatch, reason, lead["id"]),
        )
        updated += 1

    conn.commit()

    # Deuxième passe : pour tout lead déjà SCORED/LOW_CONFIDENCE qui se
    # révèle en mismatch, on le repasse en LOW_CONFIDENCE tout de suite,
    # sans attendre un nouveau run du pipeline — sinon le backfill ne sert
    # à rien pour les leads du batch du 2026-07-24 qui sont déjà scorés.
    rescoped = conn.execute(
        """
        UPDATE leads
        SET status = 'LOW_CONFIDENCE'
        WHERE domain_mismatch = 1 AND status = 'SCORED'
        """
    )
    conn.commit()

    # Il faut aussi corriger la table des scores, sinon l'onglet Résultats
    # ne reflète pas le besoin de revue humaine pour ces verdicts existants.
    conn.execute(
        """
        UPDATE lead_scores
        SET needs_human_review = 1,
            disqualify_reason = COALESCE(disqualify_reason || ' | ', '') ||
                'domain_mismatch: verdict may describe the wrong company, confirm manually'
        WHERE lead_id IN (SELECT id FROM leads WHERE domain_mismatch = 1)
          AND id = (SELECT MAX(id) FROM lead_scores ls2 WHERE ls2.lead_id = lead_scores.lead_id)
        """
    )
    conn.commit()

    print(f"Leads inspectés : {updated}")
    print(f"Leads flagués domain_mismatch=1 : {flagged}")
    print(f"Leads rebasculés SCORED -> LOW_CONFIDENCE : {rescoped.rowcount}")

    if flagged:
        print("\nDétail des mismatches détectés :")
        rows = conn.execute(
            """
            SELECT id, company_name, email, website_url, domain_mismatch_reason
            FROM leads WHERE domain_mismatch = 1 ORDER BY id
            """
        ).fetchall()
        for r in rows:
            print(f"  #{r['id']} {r['company_name']} — {r['domain_mismatch_reason']}")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "leads.db"
    backfill(path)