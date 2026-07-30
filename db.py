"""
Couche base de données (SQLite prototype).

Tables :
- analysis_sessions        : une ligne par analyse/relecture historique
- leads                    : une ligne par lead Apollo, rattachée à une session
- lead_content             : une ligne par page scrapée (Firecrawl) pour un lead
- lead_technical_signals   : signaux déterministes calculés par scraper.py
- lead_scores              : verdict du scoring IA (1 ligne = 1 verdict)
"""

import csv
import json
import sqlite3
from datetime import datetime, timezone

DB_PATH_DEFAULT = "leads.db"

COLUMN_ALIASES = {
    "first_name": ["first_name", "first name", "firstname"],
    "last_name": ["last_name", "last name", "lastname"],
    "title": ["title", "job title", "person title"],
    "company_name": ["company_name", "company", "company name", "organization"],
    "email": ["email", "email address", "work email"],
    "website_url": ["website_url", "website", "company website", "website url"],
}

FREE_EMAIL_PROVIDERS = {
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com",
    "proton.me", "protonmail.com", "aol.com", "gmx.com", "live.com",
    "yandex.com", "mail.com", "zoho.com",
}


def _email_domain(email: str) -> str:
    if not email or "@" not in email:
        return ""
    return email.strip().lower().split("@")[-1]


def _domains_related(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def get_connection(db_path: str = DB_PATH_DEFAULT) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_analysis_session(
    conn: sqlite3.Connection,
    label: str | None = None,
    source_filename: str | None = None,
    notes: str | None = None,
) -> int:
    now = _now()
    conn.execute(
        """
        INSERT INTO analysis_sessions (label, source_filename, status, created_at, notes)
        VALUES (?, ?, ?, ?, ?)
        """,
        (label, source_filename, "imported", now, notes),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def update_analysis_session_status(
    conn: sqlite3.Connection,
    session_id: int,
    status: str,
    completed_at: str | None = None,
) -> None:
    conn.execute(
        "UPDATE analysis_sessions SET status = ?, completed_at = COALESCE(?, completed_at) WHERE id = ?",
        (status, completed_at, session_id),
    )
    conn.commit()


def get_analysis_session(conn: sqlite3.Connection, session_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM analysis_sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def delete_analysis_session(conn: sqlite3.Connection, session_id: int) -> None:
    """Supprime une session et toutes ses données associées."""
    conn.execute("DELETE FROM lead_search_evidence WHERE lead_id IN (SELECT id FROM leads WHERE session_id = ?)", (session_id,))
    conn.execute("DELETE FROM lead_scores WHERE lead_id IN (SELECT id FROM leads WHERE session_id = ?)", (session_id,))
    conn.execute("DELETE FROM lead_technical_signals WHERE lead_id IN (SELECT id FROM leads WHERE session_id = ?)", (session_id,))
    conn.execute("DELETE FROM lead_content WHERE lead_id IN (SELECT id FROM leads WHERE session_id = ?)", (session_id,))
    conn.execute("DELETE FROM export_history WHERE lead_id IN (SELECT id FROM leads WHERE session_id = ?)", (session_id,))
    conn.execute("DELETE FROM leads WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM analysis_sessions WHERE id = ?", (session_id,))
    # Réinitialise les compteurs auto-incrément si les tables sont vides
    for table in ("leads", "lead_content", "lead_technical_signals", "lead_scores", "lead_search_evidence", "export_history", "analysis_sessions"):
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if count == 0:
            conn.execute("DELETE FROM sqlite_sequence WHERE name = ?", (table,))
    conn.commit()


def cancel_analysis_session(conn: sqlite3.Connection, session_id: int) -> None:
    """Marque une session comme annulée. Le pipeline vérifie ce flag."""
    conn.execute("UPDATE analysis_sessions SET cancelled = 1 WHERE id = ?", (session_id,))
    conn.commit()


def resume_analysis_session(conn: sqlite3.Connection, session_id: int) -> None:
    """Réactive une session annulée pour permettre la reprise du pipeline."""
    conn.execute("UPDATE analysis_sessions SET cancelled = 0, status = 'running' WHERE id = ?", (session_id,))
    conn.commit()


def save_scoring_criteria_custom(conn: sqlite3.Connection, session_id: int, custom_text: str) -> None:
    """Sauvegarde le critère personnalisé saisi par l'utilisateur."""
    conn.execute(
        "UPDATE analysis_sessions SET scoring_criteria_custom = ? WHERE id = ?",
        (custom_text, session_id),
    )
    conn.commit()


def get_scoring_criteria_custom(conn: sqlite3.Connection, session_id: int) -> str:
    """Retourne le critère personnalisé pour une session."""
    row = conn.execute("SELECT scoring_criteria_custom FROM analysis_sessions WHERE id = ?", (session_id,)).fetchone()
    if not row or not row[0]:
        return ""
    return row[0]


def is_session_cancelled(conn: sqlite3.Connection, session_id: int) -> bool:
    """Vérifie si une session a été annulée."""
    row = conn.execute("SELECT cancelled FROM analysis_sessions WHERE id = ?", (session_id,)).fetchone()
    return bool(row and row[0])


def save_scoring_criteria(conn: sqlite3.Connection, session_id: int, criteria: list[str]) -> None:
    """Sauvegarde les critères checkés par l'utilisateur pour le scoring."""
    import json
    conn.execute(
        "UPDATE analysis_sessions SET scoring_criteria = ? WHERE id = ?",
        (json.dumps(criteria, ensure_ascii=False), session_id),
    )
    conn.commit()


def get_scoring_criteria(conn: sqlite3.Connection, session_id: int) -> list[str]:
    """Retourne les critères de scoring pour une session."""
    import json
    row = conn.execute("SELECT scoring_criteria FROM analysis_sessions WHERE id = ?", (session_id,)).fetchone()
    if not row or not row[0]:
        return []
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return []


def set_last_batch_ids(conn: sqlite3.Connection, session_id: int, lead_ids: list[int]) -> None:
    """Stocke les IDs des leads du dernier lot traite (pour la page batch results)."""
    import json
    conn.execute("UPDATE analysis_sessions SET last_batch_ids = ? WHERE id = ?",
                 (json.dumps(lead_ids), session_id))
    conn.commit()


def get_last_batch_ids(conn: sqlite3.Connection, session_id: int) -> list[int]:
    """Recupere les IDs du dernier lot traite."""
    import json
    row = conn.execute("SELECT last_batch_ids FROM analysis_sessions WHERE id = ?", (session_id,)).fetchone()
    if not row or not row[0]:
        return []
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return []


def get_latest_session_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT id FROM analysis_sessions ORDER BY id DESC LIMIT 1").fetchone()
    return row["id"] if row else None


def list_analysis_sessions(conn: sqlite3.Connection, limit: int = 50) -> list:
    query = """
        SELECT s.*,
               COUNT(DISTINCT l.id) AS lead_count,
               SUM(CASE WHEN l.is_duplicate = 1 THEN 1 ELSE 0 END) AS duplicate_count,
               SUM(CASE WHEN l.status IN ('SCORED', 'LOW_CONFIDENCE') THEN 1 ELSE 0 END) AS scored_count,
               SUM(CASE WHEN l.status = 'NEW' THEN 1 ELSE 0 END) AS pending_count
        FROM analysis_sessions s
        LEFT JOIN leads l ON l.session_id = s.id
        GROUP BY s.id
        ORDER BY s.id DESC
        LIMIT ?
    """
    return [dict(r) for r in conn.execute(query, (limit,)).fetchall()]


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS analysis_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT,
            source_filename TEXT,
            status TEXT NOT NULL DEFAULT 'imported',
            created_at TEXT NOT NULL,
            completed_at TEXT,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            first_name TEXT,
            last_name TEXT,
            title TEXT,
            company_name TEXT,
            email TEXT,
            website_url TEXT,
            domain_normalized TEXT,
            email_domain TEXT,
            domain_mismatch INTEGER NOT NULL DEFAULT 0,
            domain_mismatch_reason TEXT,
            status TEXT NOT NULL DEFAULT 'NEW',
            is_duplicate INTEGER NOT NULL DEFAULT 0,
            duplicate_of_id INTEGER,
            duplicate_reason TEXT,
            batch_id TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES analysis_sessions(id),
            FOREIGN KEY (duplicate_of_id) REFERENCES leads(id)
        );

        CREATE TABLE IF NOT EXISTS lead_content (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            lead_id INTEGER NOT NULL,
            source TEXT,
            url TEXT,
            content TEXT,
            fetched_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES analysis_sessions(id),
            FOREIGN KEY (lead_id) REFERENCES leads(id)
        );

        CREATE TABLE IF NOT EXISTS lead_technical_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            lead_id INTEGER NOT NULL,
            generator_fingerprint TEXT,
            vibe_language_matches TEXT,
            trend_fonts_found TEXT,
            visual_patterns_triggered TEXT,
            generator_meta_tag TEXT,
            github_repo_url TEXT,
            github_check TEXT,
            ai_style_phrases_found TEXT,
            ai_style_phrase_density TEXT,
            ai_authorship_disclosures_found TEXT,
            computed_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES analysis_sessions(id),
            FOREIGN KEY (lead_id) REFERENCES leads(id)
        );

        CREATE TABLE IF NOT EXISTS lead_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            lead_id INTEGER NOT NULL,
            segment TEXT,
            confidence REAL,
            company_stage TEXT,
            built_with_ai_signals TEXT,
            technical_signals TEXT,
            pain_signals TEXT,
            evidence_quotes TEXT,
            recommended_offer TEXT,
            personalization_hooks TEXT,
            disqualify_reason TEXT,
            needs_human_review INTEGER,
            scored_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES analysis_sessions(id),
            FOREIGN KEY (lead_id) REFERENCES leads(id)
        );

        CREATE TABLE IF NOT EXISTS lead_search_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            lead_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            query TEXT,
            results TEXT,
            fetched_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES analysis_sessions(id),
            FOREIGN KEY (lead_id) REFERENCES leads(id)
        );

        CREATE TABLE IF NOT EXISTS export_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            lead_id INTEGER NOT NULL,
            domain_normalized TEXT NOT NULL,
            exported_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES analysis_sessions(id),
            FOREIGN KEY (lead_id) REFERENCES leads(id)
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_created_at ON analysis_sessions(created_at);
        CREATE INDEX IF NOT EXISTS idx_leads_session ON leads(session_id);
        CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email);
        CREATE INDEX IF NOT EXISTS idx_leads_domain ON leads(domain_normalized);
        CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
        CREATE INDEX IF NOT EXISTS idx_content_session ON lead_content(session_id);
        CREATE INDEX IF NOT EXISTS idx_scores_session ON lead_scores(session_id);
        CREATE INDEX IF NOT EXISTS idx_technical_signals_lead ON lead_technical_signals(lead_id);
        CREATE INDEX IF NOT EXISTS idx_export_history_domain ON export_history(domain_normalized);
        """
    )

    # Colonnes d'annulation pipeline + critères scoring + batch tracking
    for col, coltype in [
        ("cancelled", "INTEGER NOT NULL DEFAULT 0"),
        ("scoring_criteria", "TEXT"),
        ("scoring_criteria_custom", "TEXT"),
        ("last_batch_ids", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE analysis_sessions ADD COLUMN {col} {coltype}")
        except sqlite3.OperationalError:
            pass

    # Colonnes de review humaine (APPROVED/REJECTED + override de segment)
    for col, coltype in [
        ("review_status", "TEXT"),
        ("review_segment_override", "TEXT"),
        ("reviewed_at", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {coltype}")
        except sqlite3.OperationalError:
            pass

    # Colonnes d'erreur et timing pour le diagnostic dans le dashboard
    for col, coltype in [
        ("last_error", "TEXT"),
        ("scrape_seconds", "REAL"),
        ("score_seconds", "REAL"),
    ]:
        try:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {coltype}")
        except sqlite3.OperationalError:
            pass

    for table in ("leads", "lead_content", "lead_technical_signals", "lead_scores"):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN session_id INTEGER")
        except sqlite3.OperationalError:
            pass

    for col, coltype in [
        ("email_domain", "TEXT"),
        ("domain_mismatch", "INTEGER NOT NULL DEFAULT 0"),
        ("domain_mismatch_reason", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {coltype}")
        except sqlite3.OperationalError:
            pass

    for col, coltype in [
        ("ai_style_phrases_found", "TEXT"),
        ("ai_style_phrase_density", "TEXT"),
        ("ai_authorship_disclosures_found", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE lead_technical_signals ADD COLUMN {col} {coltype}")
        except sqlite3.OperationalError:
            pass

    # Migrer les donnees existantes sans session_id vers un session "legacy"
    unassigned_count = 0
    for table in ("leads", "lead_content", "lead_technical_signals", "lead_scores"):
        try:
            count = conn.execute(f"SELECT COUNT(*) AS c FROM {table} WHERE session_id IS NULL").fetchone()["c"]
            unassigned_count += count
        except sqlite3.OperationalError:
            pass

    if unassigned_count > 0:
        legacy = conn.execute(
            "SELECT id FROM analysis_sessions WHERE label = ? ORDER BY id LIMIT 1",
            ("legacy",),
        ).fetchone()
        if legacy is None:
            conn.execute(
                """
                INSERT INTO analysis_sessions (label, source_filename, status, created_at, completed_at, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("legacy", None, "completed", _now(), _now(), "Imported existing data before session support"),
            )
            legacy_session_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        else:
            legacy_session_id = legacy["id"]

        for table in ("leads", "lead_content", "lead_technical_signals", "lead_scores"):
            conn.execute(
                f"UPDATE {table} SET session_id = ? WHERE session_id IS NULL",
                (legacy_session_id,),
            )

    conn.commit()


def _normalize_domain(url: str) -> str:
    if not url:
        return ""
    u = url.strip().lower()
    u = u.replace("https://", "").replace("http://", "")
    if u.startswith("www."):
        u = u[4:]
    u = u.split("/")[0]
    return u


def _pick_column(row: dict, key: str) -> str:
    lower_row = {k.strip().lower(): v for k, v in row.items() if k}
    for alias in COLUMN_ALIASES[key]:
        if alias in lower_row and lower_row[alias]:
            return lower_row[alias].strip()
    return ""


def insert_leads_from_csv(
    conn: sqlite3.Connection,
    csv_path: str,
    batch_id: str,
    session_id: int | None = None,
) -> dict:
    inserted = 0
    skipped = 0
    now = _now()
    if session_id is None:
        session_id = get_latest_session_id(conn)

    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows_to_insert = []
        for row in reader:
            website = _pick_column(row, "website_url")
            if not website:
                skipped += 1
                continue

            email = _pick_column(row, "email")
            email_domain = _email_domain(email)
            site_domain = _normalize_domain(website)

            domain_mismatch = 0
            domain_mismatch_reason = None
            if (
                email_domain
                and email_domain not in FREE_EMAIL_PROVIDERS
                and site_domain
                and not _domains_related(email_domain, site_domain)
            ):
                domain_mismatch = 1
                domain_mismatch_reason = f"email domain '{email_domain}' does not match website domain '{site_domain}'"

            rows_to_insert.append(
                (
                    session_id,
                    _pick_column(row, "first_name"),
                    _pick_column(row, "last_name"),
                    _pick_column(row, "title"),
                    _pick_column(row, "company_name"),
                    email,
                    website,
                    site_domain,
                    email_domain,
                    domain_mismatch,
                    domain_mismatch_reason,
                    "NEW",
                    batch_id,
                    now,
                )
            )

    conn.executemany(
        """
        INSERT INTO leads
            (session_id, first_name, last_name, title, company_name, email, website_url,
             domain_normalized, email_domain, domain_mismatch, domain_mismatch_reason,
             status, batch_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows_to_insert,
    )
    conn.commit()
    inserted = len(rows_to_insert)
    return {"inserted": inserted, "skipped_no_website": skipped}


def get_leads(conn: sqlite3.Connection, include_duplicates: bool = True, session_id: int | None = None) -> list:
    query = "SELECT * FROM leads"
    conditions = []
    params = []
    if not include_duplicates:
        conditions.append("is_duplicate = 0")
    if session_id is not None:
        conditions.append("session_id = ?")
        params.append(session_id)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY id"
    return [dict(r) for r in conn.execute(query, params).fetchall()]


NON_TERMINAL_STATUSES = ("NEW", "PARSED", "FETCH_PARTIAL", "FETCH_FAILED")


def get_leads_by_status(conn: sqlite3.Connection, status: str, session_id: int | None = None) -> list:
    """Récupère les leads avec un statut donné (ex: PHASE2_PENDING)."""
    query = "SELECT * FROM leads WHERE is_duplicate = 0 AND status = ?"
    params = [status]
    if session_id is not None:
        query += " AND session_id = ?"
        params.append(session_id)
    query += " ORDER BY id"
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def get_leads_to_process(conn: sqlite3.Connection, session_id: int | None = None) -> list:
    placeholders = ",".join("?" for _ in NON_TERMINAL_STATUSES)
    query = f"SELECT * FROM leads WHERE is_duplicate = 0 AND status IN ({placeholders})"
    params = list(NON_TERMINAL_STATUSES)
    if session_id is not None:
        query += " AND session_id = ?"
        params.append(session_id)
    query += " ORDER BY id"
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def update_lead_status(conn: sqlite3.Connection, lead_id: int, status: str, error: str | None = None) -> None:
    """Met à jour le statut et l'erreur d'un lead. Un appel avec error=None efface l'erreur précédente."""
    conn.execute(
        "UPDATE leads SET status = ?, last_error = ? WHERE id = ?",
        (status, error, lead_id),
    )
    conn.commit()


def record_lead_timing(
    conn: sqlite3.Connection,
    lead_id: int,
    scrape_seconds: float | None = None,
    score_seconds: float | None = None,
) -> None:
    """
    Enregistre le temps passé sur chaque étape (scraping / scoring)
    séparément, pour pouvoir répondre à "où sont passées les 8 minutes ?"
    avec des chiffres plutôt qu'une hypothèse. N'écrase que les colonnes
    fournies : un appel scrape_seconds-only ne touche pas score_seconds.
    """
    updates, params = [], []
    if scrape_seconds is not None:
        updates.append("scrape_seconds = ?")
        params.append(scrape_seconds)
    if score_seconds is not None:
        updates.append("score_seconds = ?")
        params.append(score_seconds)
    if not updates:
        return
    params.append(lead_id)
    conn.execute(f"UPDATE leads SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()


def mark_duplicate(conn: sqlite3.Connection, lead_id: int, duplicate_of_id: int, reason: str) -> None:
    conn.execute(
        "UPDATE leads SET is_duplicate = 1, duplicate_of_id = ?, duplicate_reason = ? WHERE id = ?",
        (duplicate_of_id, reason, lead_id),
    )
    conn.commit()


VALID_REVIEW_STATUSES = ("APPROVED", "REJECTED")


def set_lead_review(
    conn: sqlite3.Connection,
    lead_id: int,
    decision: str,
    segment_override: str | None = None,
) -> None:
    """Enregistre la décision de review humaine (APPROVED/REJECTED) et un éventuel override de segment."""
    if decision not in VALID_REVIEW_STATUSES:
        raise ValueError(f"decision invalide : {decision!r} (attendu APPROVED ou REJECTED)")
    conn.execute(
        "UPDATE leads SET review_status = ?, review_segment_override = ?, reviewed_at = ? WHERE id = ?",
        (decision, segment_override, _now(), lead_id),
    )
    conn.commit()


def save_lead_content(conn: sqlite3.Connection, lead_id: int, rows: list) -> None:
    now = _now()
    session_row = conn.execute("SELECT session_id FROM leads WHERE id = ?", (lead_id,)).fetchone()
    session_id = session_row[0] if session_row else None
    conn.executemany(
        "INSERT INTO lead_content (session_id, lead_id, source, url, content, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
        [(session_id, lead_id, source, url, content, now) for source, url, content in rows],
    )
    conn.commit()


def get_lead_content(conn: sqlite3.Connection, lead_id: int) -> list:
    query = "SELECT source, url, content FROM lead_content WHERE lead_id = ?"
    return [dict(r) for r in conn.execute(query, (lead_id,)).fetchall()]


def save_lead_technical_signals(
    conn: sqlite3.Connection,
    lead_id: int,
    technical_signals: dict | None,
    github_check: dict | None,
) -> None:
    if not technical_signals:
        return

    def as_json(v):
        return json.dumps(v, ensure_ascii=False) if v is not None else None

    session_row = conn.execute("SELECT session_id FROM leads WHERE id = ?", (lead_id,)).fetchone()
    session_id = session_row[0] if session_row else None
    conn.execute(
        """
        INSERT INTO lead_technical_signals
            (session_id, lead_id, generator_fingerprint, vibe_language_matches, trend_fonts_found,
             visual_patterns_triggered, generator_meta_tag, github_repo_url, github_check,
             ai_style_phrases_found, ai_style_phrase_density, ai_authorship_disclosures_found,
             computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            lead_id,
            technical_signals.get("generator_fingerprint"),
            as_json(technical_signals.get("vibe_language_matches", [])),
            as_json(technical_signals.get("trend_fonts_found", [])),
            as_json(technical_signals.get("visual_patterns_triggered", [])),
            technical_signals.get("generator_meta_tag"),
            technical_signals.get("github_repo_url"),
            as_json(github_check),
            as_json(technical_signals.get("ai_style_phrases_found", [])),
            technical_signals.get("ai_style_phrase_density"),
            as_json(technical_signals.get("ai_authorship_disclosures_found", [])),
            _now(),
        ),
    )
    conn.commit()


def get_lead_technical_signals(conn: sqlite3.Connection, lead_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT * FROM lead_technical_signals
        WHERE lead_id = ?
        ORDER BY id DESC LIMIT 1
        """,
        (lead_id,),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    for json_field in (
        "vibe_language_matches",
        "trend_fonts_found",
        "visual_patterns_triggered",
        "github_check",
        "ai_style_phrases_found",
        "ai_authorship_disclosures_found",
    ):
        if result.get(json_field):
            try:
                result[json_field] = json.loads(result[json_field])
            except (json.JSONDecodeError, TypeError):
                pass
    return result


def save_lead_score(conn: sqlite3.Connection, lead_id: int, verdict: dict) -> None:
    def as_json(v):
        return json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v

    session_row = conn.execute("SELECT session_id FROM leads WHERE id = ?", (lead_id,)).fetchone()
    session_id = session_row[0] if session_row else None
    conn.execute(
        """
        INSERT INTO lead_scores
            (session_id, lead_id, segment, confidence, company_stage, built_with_ai_signals,
             technical_signals, pain_signals, evidence_quotes, recommended_offer,
             personalization_hooks, disqualify_reason, needs_human_review, scored_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            lead_id,
            verdict.get("segment"),
            verdict.get("confidence"),
            verdict.get("company_stage"),
            as_json(verdict.get("built_with_ai_signals", [])),
            as_json(verdict.get("technical_signals", [])),
            as_json(verdict.get("pain_signals", [])),
            as_json(verdict.get("evidence_quotes", [])),
            verdict.get("recommended_offer"),
            as_json(verdict.get("personalization_hooks", [])),
            verdict.get("disqualify_reason"),
            1 if verdict.get("needs_human_review") else 0,
            _now(),
        ),
    )
    conn.commit()


def save_search_evidence(
    conn: sqlite3.Connection,
    lead_id: int,
    source: str,
    query: str,
    results: list,
) -> None:
    """Enregistre les résultats d'une recherche web SGAI pour un lead."""
    session_row = conn.execute(
        "SELECT session_id FROM leads WHERE id = ?", (lead_id,)
    ).fetchone()
    session_id = session_row[0] if session_row else None
    conn.execute(
        "INSERT INTO lead_search_evidence (session_id, lead_id, source, query, results, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, lead_id, source, query, json.dumps(results, ensure_ascii=False), _now()),
    )
    conn.commit()


def get_lead_search_evidence(conn: sqlite3.Connection, lead_id: int) -> list:
    """Retourne toutes les recherches web SGAI pour un lead."""
    rows = conn.execute(
        "SELECT * FROM lead_search_evidence WHERE lead_id = ? ORDER BY id", (lead_id,)
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["results"] = json.loads(d["results"]) if isinstance(d["results"], str) else d["results"]
        except (json.JSONDecodeError, TypeError):
            pass
        result.append(d)
    return result


def get_exported_domains(conn: sqlite3.Connection) -> set:
    """Retourne l'ensemble des domaines déjà exportés (toutes sessions confondues)."""
    rows = conn.execute("SELECT DISTINCT domain_normalized FROM export_history").fetchall()
    return {r["domain_normalized"] for r in rows if r["domain_normalized"]}


def record_export(conn: sqlite3.Connection, lead_ids: list, session_id: int | None = None) -> int:
    """
    Enregistre l'export d'une liste de leads (par id) dans export_history,
    pour que le prochain batch les retrouve via get_exported_domains().
    Retourne le nombre de lignes effectivement enregistrées (les leads sans
    domain_normalized sont ignorés).
    """
    now = _now()
    rows_to_insert = []
    for lead_id in lead_ids:
        row = conn.execute(
            "SELECT domain_normalized, session_id FROM leads WHERE id = ?", (lead_id,)
        ).fetchone()
        if row and row["domain_normalized"]:
            rows_to_insert.append((session_id or row["session_id"], lead_id, row["domain_normalized"], now))

    if not rows_to_insert:
        return 0

    conn.executemany(
        "INSERT INTO export_history (session_id, lead_id, domain_normalized, exported_at) VALUES (?, ?, ?, ?)",
        rows_to_insert,
    )
    conn.commit()
    return len(rows_to_insert)


def get_leads_with_scores(conn: sqlite3.Connection, session_id: int | None = None) -> list:
    query = """
        SELECT l.*, s.segment, s.confidence, s.company_stage, s.evidence_quotes,
               s.personalization_hooks, s.disqualify_reason, s.needs_human_review,
               s.recommended_offer, s.built_with_ai_signals, s.technical_signals,
               s.pain_signals, s.scored_at
        FROM leads l
        LEFT JOIN lead_scores s ON s.lead_id = l.id
            AND s.id = (SELECT MAX(id) FROM lead_scores WHERE lead_id = l.id)
    """
    params = []
    if session_id is not None:
        query += " WHERE l.session_id = ?"
        params.append(session_id)
    query += " ORDER BY l.id"
    return [dict(r) for r in conn.execute(query, params).fetchall()]