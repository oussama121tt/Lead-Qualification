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
        (label, source_filename, "running", now, notes),
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
            status TEXT NOT NULL DEFAULT 'running',
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

        CREATE INDEX IF NOT EXISTS idx_sessions_created_at ON analysis_sessions(created_at);
        CREATE INDEX IF NOT EXISTS idx_leads_session ON leads(session_id);
        CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email);
        CREATE INDEX IF NOT EXISTS idx_leads_domain ON leads(domain_normalized);
        CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
        CREATE INDEX IF NOT EXISTS idx_content_session ON lead_content(session_id);
        CREATE INDEX IF NOT EXISTS idx_scores_session ON lead_scores(session_id);
        CREATE INDEX IF NOT EXISTS idx_technical_signals_lead ON lead_technical_signals(lead_id);
        """
    )

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

    has_existing_data = False
    for table in ("leads", "lead_content", "lead_technical_signals", "lead_scores"):
        try:
            count = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
            if count:
                has_existing_data = True
        except sqlite3.OperationalError:
            pass

    if has_existing_data:
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


def get_leads_to_process(conn: sqlite3.Connection, session_id: int | None = None) -> list:
    placeholders = ",".join("?" for _ in NON_TERMINAL_STATUSES)
    query = f"SELECT * FROM leads WHERE is_duplicate = 0 AND status IN ({placeholders})"
    params = list(NON_TERMINAL_STATUSES)
    if session_id is not None:
        query += " AND session_id = ?"
        params.append(session_id)
    query += " ORDER BY id"
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def update_lead_status(conn: sqlite3.Connection, lead_id: int, status: str) -> None:
    conn.execute("UPDATE leads SET status = ? WHERE id = ?", (status, lead_id))
    conn.commit()


def mark_duplicate(conn: sqlite3.Connection, lead_id: int, duplicate_of_id: int, reason: str) -> None:
    conn.execute(
        "UPDATE leads SET is_duplicate = 1, duplicate_of_id = ?, duplicate_reason = ? WHERE id = ?",
        (duplicate_of_id, reason, lead_id),
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
