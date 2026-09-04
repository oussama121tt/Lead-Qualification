"""
Database layer (PostgreSQL/Neon only).

Tables:
- analysis_sessions        : one row per historical analysis/review
- leads                    : one row per Apollo lead, attached to a session
- lead_content             : one row per scraped page (Firecrawl) for a lead
- lead_technical_signals   : deterministic signals computed by scraper.py
- lead_scores              : AI scoring verdict (1 row = 1 verdict)

It connects to PostgreSQL via psycopg2 using DATABASE_URL (environment
variable, e.g. Neon connection string). No SQLite fallback: without
DATABASE_URL, the application refuses to start.
"""

import csv
import json
import os
import re
import threading
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from constants import NOT_YET_SCORED_STATUSES

import psycopg2
import psycopg2.extras
import psycopg2.pool

DATABASE_URL = os.getenv("DATABASE_URL")
# Connection pool: reuses already-established TCP connections instead of
# opening a fresh one (SSL handshake + auth) on EVERY request. Unlike
# pgBouncer, we keep a simple client-side pool; each connection is returned
# to the pool at the end of a request and reused by the next one.
_pg_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pg_pool_lock = threading.Lock()


def _is_dead_connection_error(exc: psycopg2.OperationalError) -> bool:
    """True if the OperationalError means the pooled connection was killed
    server-side (Neon closes idle connections): the query can safely be
    retried on a fresh connection."""
    msg = str(exc).lower()
    return any(k in msg for k in (
        "could not receive data from server",
        "software caused connection abort",
        "server closed the connection",
        "connection reset by peer",
        "ssl syscall error",
        "broken pipe",
        "connection has been closed",
        "terminated by server",
        "no connection to the server",
        "connection refused",
    ))
DB_POOL_MINCONN = max(1, int(os.getenv("DB_POOL_MINCONN", "1")))
DB_POOL_MAXCONN = max(int(os.getenv("DB_POOL_MAXCONN", "8")), DB_POOL_MINCONN)


def _is_duplicate_column(e: Exception) -> bool:
    """True if the error means 'column already exists' (PostgreSQL)."""
    msg = str(e)
    return "duplicate column" in msg or "already exists" in msg


COLUMN_ALIASES = {
    "first_name": ["first_name", "first name", "firstname"],
    "last_name": ["last_name", "last name", "lastname"],
    "title": ["title", "job title", "person title"],
    "company_name": ["company_name", "company", "company name", "organization"],
    "email": ["email", "email address", "work email"],
    "website_url": ["website_url", "website", "company website", "website url"],
    # Optional in the spec (FR-1) — used by the LinkedIn founder lane when
    # present, so the person harvest never has to guess a profile by name.
    "linkedin_url": ["linkedin_url", "linkedin url", "person linkedin url", "linkedin"],
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


# ---------------------------------------------------------------------------
# PostgreSQL connection layer (compatible with the rest of the code)
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"\?")


class _PgRow(dict):
    """Row: accessible both by key AND by index (wraps a RealDictRow)."""

    def __init__(self, mapping):
        super().__init__(mapping)
        self._colnames = list(mapping.keys())

    def __getitem__(self, key):
        if isinstance(key, str):
            return dict.__getitem__(self, key)
        if int(key) >= len(self._colnames):
            raise IndexError(key)
        return dict.__getitem__(self, self._colnames[int(key)])

    def keys(self):
        return self._colnames


class _PgCursor:
    """Wrapper cursor: converts RealDictRow into _PgRow (key + index)."""

    def __init__(self, cur):
        self._cur = cur
        self.description = cur.description

    def fetchone(self):
        row = self._cur.fetchone()
        return _PgRow(dict(row)) if row is not None else None

    def fetchall(self):
        return [_PgRow(dict(r)) for r in self._cur.fetchall()]


class _PgConnection:
    """PostgreSQL connection wrapper exposing the execute/fetch API used by the rest of the code."""

    def __init__(self, raw):
        self._conn = raw

    def execute(self, sql, params=None):
        try:
            return self._execute(sql, params)
        except psycopg2.errors.InFailedSqlTransaction:
            # Previous statement in this transaction failed and left it aborted
            # (e.g. Neon pool reuse after a prior error, or a concurrent
            # pipeline write that failed). Roll back and retry once.
            try:
                self._conn.rollback()
            except psycopg2.Error:
                pass
            return self._execute(sql, params)
        except psycopg2.OperationalError as exc:
            if not _is_dead_connection_error(exc):
                raise
            # The pooled connection died (Neon closes idle connections):
            # replace it with a fresh one and retry the query once.
            self._reconnect()
            return self._execute(sql, params)

    def _execute(self, sql, params=None):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        translated = _PLACEHOLDER_RE.sub("%s", sql)
        if params is not None:
            cur.execute(translated, params)
        else:
            cur.execute(translated)
        return _PgCursor(cur)

    def _reconnect(self):
        try:
            self._conn.close()
        except psycopg2.Error:
            pass
        self._conn = psycopg2.connect(DATABASE_URL)

    def executemany(self, sql, seq):
        try:
            return self._executemany(sql, seq)
        except psycopg2.OperationalError as exc:
            if not _is_dead_connection_error(exc):
                raise
            self._reconnect()
            return self._executemany(sql, seq)

    def _executemany(self, sql, seq):
        cur = self._conn.cursor()
        cur.executemany(_PLACEHOLDER_RE.sub("%s", sql), seq)

    def executescript(self, sql):
        for stmt in (s.strip() for s in sql.split(";") if s and s.strip()):
            self.execute(stmt)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        # Roll back to release any unfinished transaction (error path),
        # then return to the pool — the connection stays alive for the next
        # request instead of being actually closed.
        try:
            if not self._conn.closed:
                self._conn.rollback()
        except psycopg2.Error:
            pass
        pool = _get_pg_pool()
        try:
            pool.putconn(self._conn)
        except psycopg2.pool.PoolError:
            try:
                self._conn.close()
            except psycopg2.Error:
                pass


def _get_pg_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pg_pool
    with _pg_pool_lock:
        if _pg_pool is None:
            if not DATABASE_URL:
                raise RuntimeError(
                    "DATABASE_URL is not set. PostgreSQL (Neon) is required: "
                    "add DATABASE_URL=postgresql://... to .env."
                )
            # The first connection of the pool (minconn=1) can fail
            # transiently at startup (e.g. Neon pooler still cleaning up
            # connections from a previous process, or a cold start):
            # retry a few times instead of dying on the first attempt.
            last_error: Exception | None = None
            for attempt in range(5):
                try:
                    _pg_pool = psycopg2.pool.ThreadedConnectionPool(
                        DB_POOL_MINCONN,
                        DB_POOL_MAXCONN,
                        dsn=DATABASE_URL,
                        keepalives=1,
                        keepalives_idle=60,
                        keepalives_interval=15,
                        keepalives_count=4,
                    )
                    return _pg_pool
                except psycopg2.OperationalError as exc:
                    last_error = exc
                    time.sleep(2 * (attempt + 1))
            raise RuntimeError(
                "Could not connect to PostgreSQL (Neon) after 5 attempts: "
                f"{last_error}"
            )
        return _pg_pool


def get_connection():
    """Returns a PostgreSQL (Neon) connection taken from the shared pool.
    SQLite is no longer supported. The connection is never actually closed:
    `close()` returns it to the pool so it can be reused for the next request."""
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set. PostgreSQL (Neon) is required: "
            "add DATABASE_URL=postgresql://... to .env."
        )
    pool = _get_pg_pool()
    try:
        raw = pool.getconn()
    except psycopg2.pool.PoolError:
        # Pool saturated (too many simultaneous requests): fallback connection.
        raw = psycopg2.connect(DATABASE_URL)
    return _PgConnection(raw)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_analysis_session(
    conn,
    label: str | None = None,
    source_filename: str | None = None,
    notes: str | None = None,
    owner_id: int | None = None,
) -> int:
    now = _now()
    row = conn.execute(
        """
        INSERT INTO analysis_sessions (label, source_filename, status, created_at, notes, owner_id)
        VALUES (?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (label, source_filename, "imported", now, notes, owner_id),
    ).fetchone()
    conn.commit()
    return row["id"]


def update_analysis_session_status(
    conn,
    session_id: int,
    status: str,
    completed_at: str | None = None,
) -> None:
    conn.execute(
        "UPDATE analysis_sessions SET status = ?, completed_at = COALESCE(?, completed_at) WHERE id = ?",
        (status, completed_at, session_id),
    )
    conn.commit()


def get_analysis_session(conn, session_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM analysis_sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def get_lead(conn, lead_id: int) -> dict | None:
    """Fetches a single lead by id."""
    row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    return dict(row) if row else None


def delete_analysis_session(conn, session_id: int) -> None:
    """Deletes a session and all of its associated data."""
    conn.execute("DELETE FROM lead_search_evidence WHERE lead_id IN (SELECT id FROM leads WHERE session_id = ?)", (session_id,))
    conn.execute("DELETE FROM lead_scores WHERE lead_id IN (SELECT id FROM leads WHERE session_id = ?)", (session_id,))
    conn.execute("DELETE FROM lead_technical_signals WHERE lead_id IN (SELECT id FROM leads WHERE session_id = ?)", (session_id,))
    conn.execute("DELETE FROM lead_content WHERE lead_id IN (SELECT id FROM leads WHERE session_id = ?)", (session_id,))
    conn.execute("DELETE FROM export_history WHERE lead_id IN (SELECT id FROM leads WHERE session_id = ?)", (session_id,))
    conn.execute("DELETE FROM leads WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM analysis_sessions WHERE id = ?", (session_id,))
    conn.commit()


def cancel_analysis_session(conn, session_id: int) -> None:
    """Marks a session as cancelled. The pipeline checks this flag."""
    conn.execute("UPDATE analysis_sessions SET cancelled = 1 WHERE id = ?", (session_id,))
    conn.commit()


def resume_analysis_session(conn, session_id: int) -> None:
    """Reactivates a cancelled session to allow the pipeline to resume."""
    conn.execute("UPDATE analysis_sessions SET cancelled = 0, status = 'running' WHERE id = ?", (session_id,))
    conn.commit()


def save_scoring_criteria_custom(conn, session_id: int, custom_text: str) -> None:
    """Saves the custom criterion entered by the user."""
    conn.execute(
        "UPDATE analysis_sessions SET scoring_criteria_custom = ? WHERE id = ?",
        (custom_text, session_id),
    )
    conn.commit()


def get_scoring_criteria_custom(conn, session_id: int) -> str:
    """Returns the custom criterion for a session."""
    row = conn.execute("SELECT scoring_criteria_custom FROM analysis_sessions WHERE id = ?", (session_id,)).fetchone()
    if not row or not row[0]:
        return ""
    return row[0]


def is_session_cancelled(conn, session_id: int) -> bool:
    """Checks whether a session has been cancelled."""
    row = conn.execute("SELECT cancelled FROM analysis_sessions WHERE id = ?", (session_id,)).fetchone()
    return bool(row and row[0])


def save_scoring_criteria(conn, session_id: int, criteria: list[str]) -> None:
    """Saves the criteria checked by the user for scoring."""
    conn.execute(
        "UPDATE analysis_sessions SET scoring_criteria = ? WHERE id = ?",
        (json.dumps(criteria, ensure_ascii=False), session_id),
    )
    conn.commit()


def get_scoring_criteria(conn, session_id: int) -> list[str]:
    """Returns the scoring criteria for a session."""
    row = conn.execute("SELECT scoring_criteria FROM analysis_sessions WHERE id = ?", (session_id,)).fetchone()
    if not row or not row[0]:
        return []
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return []


def set_last_batch_ids(conn, session_id: int, lead_ids: list[int]) -> None:
    """Stores the IDs of the leads from the last processed batch (for the batch results page)."""
    conn.execute("UPDATE analysis_sessions SET last_batch_ids = ? WHERE id = ?",
                 (json.dumps(lead_ids), session_id))
    conn.commit()


def get_last_batch_ids(conn, session_id: int) -> list[int]:
    """Retrieves the IDs of the last processed batch."""
    row = conn.execute("SELECT last_batch_ids FROM analysis_sessions WHERE id = ?", (session_id,)).fetchone()
    if not row or not row[0]:
        return []
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return []


def get_latest_session_id(conn, owner_id: int | None = None) -> int | None:
    query = "SELECT id FROM analysis_sessions"
    params = []
    if owner_id is not None:
        query += " WHERE owner_id = ?"
        params.append(owner_id)
    query += " ORDER BY id DESC LIMIT 1"
    row = conn.execute(query, params).fetchone()
    return row["id"] if row else None


def list_analysis_sessions(conn, limit: int = 50, owner_id: int | None = None) -> list:
    where = ""
    params = []
    if owner_id is not None:
        where = " WHERE s.owner_id = ?"
        params.append(owner_id)
    query = f"""
        SELECT s.id, s.label, s.source_filename, s.status, s.created_at, s.completed_at,
               s.notes, s.cancelled, s.scoring_criteria, s.scoring_criteria_custom,
               s.last_batch_ids,
               ROW_NUMBER() OVER (PARTITION BY s.owner_id ORDER BY s.id) AS user_rank,
               COUNT(DISTINCT l.id) AS lead_count,
               SUM(CASE WHEN l.is_duplicate = 1 THEN 1 ELSE 0 END) AS duplicate_count,
               SUM(CASE WHEN l.status IN ('SCORED', 'LOW_CONFIDENCE') THEN 1 ELSE 0 END) AS scored_count,
               SUM(CASE WHEN l.status = 'NEW' THEN 1 ELSE 0 END) AS pending_count
        FROM analysis_sessions s
        LEFT JOIN leads l ON l.session_id = s.id
        {where}
        GROUP BY s.id, s.label, s.source_filename, s.status, s.created_at, s.completed_at,
                 s.notes, s.cancelled, s.scoring_criteria, s.scoring_criteria_custom,
                 s.last_batch_ids
        ORDER BY s.id DESC
        LIMIT ?
    """
    params.append(limit)
    return [dict(r) for r in conn.execute(query, params).fetchall()]


# ---------------------------------------------------------------------------
# Users (authentication)
# ---------------------------------------------------------------------------

def count_users(conn) -> int:
    row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
    return row[0] if row else 0


def create_user(conn, email: str, password_hash: str, role: str | None = None) -> int:
    """Creates a user. The very first account of the database becomes an
    admin automatically; every following account defaults to 'user'."""
    if role is None:
        role = "admin" if count_users(conn) == 0 else "user"
    row = conn.execute(
        """
        INSERT INTO users (email, password_hash, role, is_active, created_at)
        VALUES (?, ?, ?, 1, ?)
        RETURNING id
        """,
        (email, password_hash, role, _now()),
    ).fetchone()
    conn.commit()
    return row["id"]


def get_user_by_email(conn, email: str) -> dict | None:
    row = conn.execute("SELECT * FROM users WHERE LOWER(email) = LOWER(?)", (email,)).fetchone()
    return dict(row) if row else None


def get_user_by_id(conn, user_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def list_users(conn) -> list:
    return [dict(r) for r in conn.execute("SELECT * FROM users ORDER BY id").fetchall()]


def update_last_login(conn, user_id: int) -> None:
    conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (_now(), user_id))
    conn.commit()


def set_user_role(conn, user_id: int, role: str) -> None:
    conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    conn.commit()


def set_user_active(conn, user_id: int, is_active: bool) -> None:
    conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (1 if is_active else 0, user_id))
    conn.commit()


def delete_user(conn, user_id: int) -> None:
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()


def count_active_admins(conn) -> int:
    """Number of active admin accounts (guard for the last-admin protection)."""
    row = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1").fetchone()
    return row[0] if row else 0


def _schema_sql() -> str:
    pk = "id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"
    int_col = "INTEGER"
    real_col = "DOUBLE PRECISION"
    return f"""
        CREATE TABLE IF NOT EXISTS analysis_sessions (
            {pk},
            label TEXT,
            source_filename TEXT,
            status TEXT NOT NULL DEFAULT 'imported',
            created_at TEXT NOT NULL,
            completed_at TEXT,
            notes TEXT,
            owner_id INTEGER
        );

        CREATE TABLE IF NOT EXISTS users (
            {pk},
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_login_at TEXT
        );

        CREATE TABLE IF NOT EXISTS leads (
            {pk},
            session_id INTEGER,
            first_name TEXT,
            last_name TEXT,
            title TEXT,
            company_name TEXT,
            email TEXT,
            website_url TEXT,
            domain_normalized TEXT,
            email_domain TEXT,
            domain_mismatch {int_col} NOT NULL DEFAULT 0,
            domain_mismatch_reason TEXT,
            status TEXT NOT NULL DEFAULT 'NEW',
            is_duplicate {int_col} NOT NULL DEFAULT 0,
            duplicate_of_id INTEGER,
            duplicate_reason TEXT,
            batch_id TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES analysis_sessions(id),
            FOREIGN KEY (duplicate_of_id) REFERENCES leads(id)
        );

        CREATE TABLE IF NOT EXISTS lead_content (
            {pk},
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
            {pk},
            session_id INTEGER,
            lead_id INTEGER NOT NULL,
            app_builder_fingerprint TEXT,
            site_builder_fingerprint TEXT,
            on_builder_subdomain INTEGER,
            on_builder_subdomain_builder TEXT,
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
            {pk},
            session_id INTEGER,
            lead_id INTEGER NOT NULL,
            segment TEXT,
            confidence {real_col},
            company_stage TEXT,
            built_with_ai_signals TEXT,
            technical_signals TEXT,
            pain_signals TEXT,
            evidence_quotes TEXT,
            sensitive_data_categories TEXT,
            data_sensitivity_score INTEGER,
            budget_signal TEXT,
            budget_evidence TEXT,
            budget_blockers TEXT,
            recommended_offer TEXT,
            personalization_hooks TEXT,
            disqualify_reason TEXT,
            needs_human_review INTEGER,
            scored_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES analysis_sessions(id),
            FOREIGN KEY (lead_id) REFERENCES leads(id)
        );

        CREATE TABLE IF NOT EXISTS lead_search_evidence (
            {pk},
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
            {pk},
            session_id INTEGER,
            lead_id INTEGER NOT NULL,
            domain_normalized TEXT NOT NULL,
            exported_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES analysis_sessions(id),
            FOREIGN KEY (lead_id) REFERENCES leads(id)
        );

        CREATE TABLE IF NOT EXISTS llm_calls (
            {pk},
            session_id INTEGER,
            lead_id INTEGER,
            purpose TEXT,
            provider TEXT,
            model TEXT,
            tokens_in INTEGER,
            tokens_out INTEGER,
            cost_usd {real_col},
            latency_ms INTEGER,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS li_daily_counter (
            day TEXT PRIMARY KEY,
            profiles_done INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS apollo_usage (
            month TEXT PRIMARY KEY,
            credits_used INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS apollo_recipes (
            {pk},
            name TEXT,
            filters TEXT,
            created_at TEXT,
            runs INTEGER NOT NULL DEFAULT 0,
            leads_pulled INTEGER NOT NULL DEFAULT 0,
            qualified INTEGER NOT NULL DEFAULT 0,
            enriched INTEGER NOT NULL DEFAULT 0,
            sent INTEGER NOT NULL DEFAULT 0,
            replies INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS do_not_contact (
            {pk},
            email TEXT,
            domain TEXT,
            reason TEXT,
            added_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_dnc_email ON do_not_contact(email);
        CREATE INDEX IF NOT EXISTS idx_dnc_domain ON do_not_contact(domain);

        CREATE INDEX IF NOT EXISTS idx_sessions_created_at ON analysis_sessions(created_at);
        CREATE INDEX IF NOT EXISTS idx_leads_session ON leads(session_id);
        CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email);
        CREATE INDEX IF NOT EXISTS idx_leads_domain ON leads(domain_normalized);
        CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
        CREATE INDEX IF NOT EXISTS idx_content_session ON lead_content(session_id);
        CREATE INDEX IF NOT EXISTS idx_scores_session ON lead_scores(session_id);
        CREATE INDEX IF NOT EXISTS idx_technical_signals_lead ON lead_technical_signals(lead_id);
        CREATE INDEX IF NOT EXISTS idx_export_history_domain ON export_history(domain_normalized);
        CREATE INDEX IF NOT EXISTS idx_llm_calls_session ON llm_calls(session_id);
        """


_SEQUENCE_TRIGGER_TABLES = (
    "analysis_sessions", "users", "leads", "lead_content",
    "lead_technical_signals", "lead_scores", "lead_search_evidence",
    "export_history",
)


def _sequence_housekeeping_sql() -> list[str]:
    """Returns the sequence housekeeping statements, each as ONE standalone
    SQL statement (the wrapper's executescript() splits on ';' and would
    break the dollar-quoted bodies below).

    1. sync_seq_after_delete() — sets the identity sequence back to
       MAX(id)+1 as soon as a DELETE frees rows (the trigger runs AFTER the
       statement, so the new max is already visible).
    2. One AFTER DELETE trigger per table (idempotent).
    3. One-off realignment of every identity sequence to MAX(id)+1.
    """
    triggers = "\n".join(
        f"""        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_seq_{tbl}') THEN
            CREATE TRIGGER trg_seq_{tbl} AFTER DELETE ON public.{tbl}
            FOR EACH STATEMENT EXECUTE FUNCTION public.sync_seq_after_delete();
        END IF;"""
        for tbl in _SEQUENCE_TRIGGER_TABLES
    )
    tables_array = "ARRAY['" + "','".join(_SEQUENCE_TRIGGER_TABLES) + "']"
    return [
        f"""
        CREATE OR REPLACE FUNCTION public.sync_seq_after_delete()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            seq_name TEXT;
            next_val BIGINT;
        BEGIN
            BEGIN
                SELECT pg_get_serial_sequence(TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME, 'id') INTO seq_name;
            EXCEPTION WHEN OTHERS THEN
                RETURN NULL;
            END;
            IF seq_name IS NOT NULL THEN
                EXECUTE format('SELECT COALESCE(MAX(id), 0) + 1 FROM %I.%I', TG_TABLE_SCHEMA, TG_TABLE_NAME) INTO next_val;
                PERFORM setval(seq_name, next_val, false);
            END IF;
            RETURN NULL;
        END;
        $$;
        """,
        f"""
        DO $$
        BEGIN
        {triggers}
        END;
        $$;
        """,
        f"""
        DO $$
        DECLARE t TEXT;
        BEGIN
            FOREACH t IN ARRAY {tables_array}
            LOOP
                EXECUTE format('SELECT setval(pg_get_serial_sequence(%L, %L), COALESCE(MAX(id), 0) + 1, false) FROM %I', t, 'id', t);
            END LOOP;
        END;
        $$;
        """,
    ]


def _ensure_sequence_housekeeping(conn) -> None:
    """Installs the sequence-reset triggers (idempotent) and realigns every
    identity sequence to MAX(id)+1: deleting a session/user/lead frees its
    ids, the counter steps back and the freed ids are reused."""
    for stmt in _sequence_housekeeping_sql():
        conn.execute(stmt)
    conn.commit()


def init_db(conn) -> None:
    conn.executescript(_schema_sql())
    conn.commit()

    # Pipeline cancellation columns + scoring criteria + batch tracking
    for col, coltype in [
        ("cancelled", "INTEGER NOT NULL DEFAULT 0"),
        ("scoring_criteria", "TEXT"),
        ("scoring_criteria_custom", "TEXT"),
        ("last_batch_ids", "TEXT"),
        ("owner_id", "INTEGER"),
    ]:
        _add_column(conn, "analysis_sessions", col, coltype)

    # Users index (authentication)
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
        conn.commit()
    except Exception:
        conn.rollback()

    # Human review columns (APPROVED/REJECTED + segment override)
    for col, coltype in [
        ("review_status", "TEXT"),
        ("review_segment_override", "TEXT"),
        ("reviewed_at", "TEXT"),
    ]:
        _add_column(conn, "leads", col, coltype)

    # Error and timing columns for diagnosis in the dashboard
    for col, coltype in [
        ("last_error", "TEXT"),
        ("scrape_seconds", "REAL"),
        ("score_seconds", "REAL"),
    ]:
        _add_column(conn, "leads", col, coltype)

    for col, coltype in [
        ("email_domain", "TEXT"),
        ("domain_mismatch", "INTEGER NOT NULL DEFAULT 0"),
        ("domain_mismatch_reason", "TEXT"),
    ]:
        _add_column(conn, "leads", col, coltype)

    for col, coltype in [
        ("app_builder_fingerprint", "TEXT"),
        ("site_builder_fingerprint", "TEXT"),
        ("on_builder_subdomain", "INTEGER"),
        ("on_builder_subdomain_builder", "TEXT"),
        ("traction_signals", "TEXT"),
        ("ai_style_phrases_found", "TEXT"),
        ("ai_style_phrase_density", "TEXT"),
        ("ai_authorship_disclosures_found", "TEXT"),
    ]:
        _add_column(conn, "lead_technical_signals", col, coltype)

    for col, coltype in [
        ("sensitive_data_categories", "TEXT"),
        ("data_sensitivity_score", "INTEGER"),
        ("budget_signal", "TEXT"),
        ("budget_evidence", "TEXT"),
        ("budget_blockers", "TEXT"),
    ]:
        _add_column(conn, "lead_scores", col, coltype)

    # Outreach email columns (generated and sent from the results page)
    for col, coltype in [
        ("email_subject", "TEXT"),
        ("email_body", "TEXT"),
        ("email_status", "TEXT"),
        ("email_provider", "TEXT"),
        ("email_error", "TEXT"),
        ("email_sent_at", "TIMESTAMPTZ"),
    ]:
        _add_column(conn, "leads", col, coltype)

    # Merge additions: the founder's LinkedIn URL from the Apollo CSV
    # (optional column, FR-1), and per-lead evidence coverage notes (JSON
    # list) so no data gap is ever silent.
    for col, coltype in [
        ("linkedin_url", "TEXT"),
        ("coverage_notes", "TEXT"),
    ]:
        _add_column(conn, "leads", col, coltype)

    _ensure_sequence_housekeeping(conn)
    conn.commit()


def _add_column(conn, table: str, col: str, coltype: str) -> None:
    """Adds a column if it does not exist. Handles the 'duplicate column' error of both backends."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        conn.commit()
    except Exception as _e:
        if not _is_duplicate_column(_e):
            raise
        # PostgreSQL: an error aborts the current transaction, we clean it up
        try:
            conn.rollback()
        except Exception:
            pass


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
    conn,
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
        normalized = [
            {
                "first_name": _pick_column(row, "first_name"),
                "last_name": _pick_column(row, "last_name"),
                "title": _pick_column(row, "title"),
                "company_name": _pick_column(row, "company_name"),
                "email": _pick_column(row, "email"),
                "website_url": _pick_column(row, "website_url"),
                "linkedin_url": _pick_column(row, "linkedin_url"),
            }
            for row in reader
        ]
    return insert_leads_from_rows(conn, normalized, batch_id, session_id=session_id)


def _build_lead_insert_row(row: dict, session_id, now):
    """Shared lead-row builder for CSV and Apollo-API ingestion. Returns the
    insert tuple, or None when the row has no website (skipped)."""
    website = (row.get("website_url") or "").strip()
    if not website:
        return None
    email = (row.get("email") or "").strip()
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

    return (
        session_id,
        (row.get("first_name") or "").strip(),
        (row.get("last_name") or "").strip(),
        (row.get("title") or "").strip(),
        (row.get("company_name") or "").strip(),
        email,
        website,
        site_domain,
        email_domain,
        domain_mismatch,
        domain_mismatch_reason,
        (row.get("linkedin_url") or "").strip(),
        "NEW",
        batch_id_placeholder := None,  # replaced below
        now,
    )


def insert_leads_from_rows(conn, rows: list[dict], batch_id: str,
                           session_id: int | None = None) -> dict:
    """Insert leads from a list of normalized dicts (keys: first_name,
    last_name, title, company_name, email, website_url, linkedin_url). Shared
    by the CSV ingester and the Apollo sourcing pipeline. Rows without a
    website are skipped and counted."""
    now = _now()
    if session_id is None:
        session_id = get_latest_session_id(conn)
    skipped = 0
    to_insert = []
    for row in rows:
        built = _build_lead_insert_row(row, session_id, now)
        if built is None:
            skipped += 1
            continue
        built = list(built)
        built[-2] = batch_id  # fill batch_id
        to_insert.append(tuple(built))

    if to_insert:
        conn.executemany(
            """
            INSERT INTO leads
                (session_id, first_name, last_name, title, company_name, email, website_url,
                 domain_normalized, email_domain, domain_mismatch, domain_mismatch_reason,
                 linkedin_url, status, batch_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            to_insert,
        )
        conn.commit()
    return {"inserted": len(to_insert), "skipped_no_website": skipped}


def get_leads(
    conn,
    include_duplicates: bool = True,
    session_id: int | None = None,
    owner_id: int | None = None,
) -> list:
    query = "SELECT l.* FROM leads l"
    conditions = []
    params = []
    if not include_duplicates:
        conditions.append("l.is_duplicate = 0")
    if session_id is not None:
        conditions.append("l.session_id = ?")
        params.append(session_id)
    if owner_id is not None:
        query += " JOIN analysis_sessions s ON s.id = l.session_id"
        conditions.append("s.owner_id = ?")
        params.append(owner_id)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY l.id"
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def get_leads_by_status(conn, status: str, session_id: int | None = None) -> list:
    """Retrieves the leads with a given status (e.g. PHASE2_PENDING)."""
    query = "SELECT * FROM leads WHERE is_duplicate = 0 AND status = ?"
    params = [status]
    if session_id is not None:
        query += " AND session_id = ?"
        params.append(session_id)
    query += " ORDER BY id"
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def get_leads_to_process(conn, session_id: int | None = None, owner_id: int | None = None) -> list:
    placeholders = ",".join("?" for _ in NOT_YET_SCORED_STATUSES)
    joins = ""
    conditions = [f"l.is_duplicate = 0", f"l.status IN ({placeholders})"]
    params = list(NOT_YET_SCORED_STATUSES)
    if session_id is not None:
        conditions.append("l.session_id = ?")
        params.append(session_id)
    if owner_id is not None:
        joins = " JOIN analysis_sessions s ON s.id = l.session_id"
        conditions.append("s.owner_id = ?")
        params.append(owner_id)
    query = f"SELECT l.* FROM leads l{joins} WHERE " + " AND ".join(conditions) + " ORDER BY l.id"
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def update_lead_status(conn, lead_id: int, status: str, error: str | None = None) -> None:
    """Updates a lead's status and error. A call with error=None clears the previous error."""
    conn.execute(
        "UPDATE leads SET status = ?, last_error = ? WHERE id = ?",
        (status, error, lead_id),
    )
    conn.commit()


def update_leads_status(conn, lead_ids: list, status: str, error: str | None = None) -> None:
    """Bulk version of update_lead_status: one executemany + a single commit for all ids."""
    if not lead_ids:
        return
    conn.executemany(
        "UPDATE leads SET status = ?, last_error = ? WHERE id = ?",
        [(status, error, lead_id) for lead_id in lead_ids],
    )
    conn.commit()


def update_lead_email_status(
    conn,
    lead_id: int,
    subject: str | None = None,
    body: str | None = None,
    status: str | None = None,
    provider: str | None = None,
    error: str | None = None,
    sent_at: str | None = None,
) -> None:
    """Records the generated content / sending state of a lead's outreach email."""
    updates, params = [], []
    if subject is not None:
        updates.append("email_subject = ?")
        params.append(subject)
    if body is not None:
        updates.append("email_body = ?")
        params.append(body)
    if status is not None:
        updates.append("email_status = ?")
        params.append(status)
    if provider is not None:
        updates.append("email_provider = ?")
        params.append(provider)
    if error is not None:
        updates.append("email_error = ?")
        params.append(error)
    if sent_at is not None:
        updates.append("email_sent_at = ?")
        params.append(sent_at)
    if not updates:
        return
    params.append(lead_id)
    conn.execute(f"UPDATE leads SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()


def record_lead_timing(
    conn,
    lead_id: int,
    scrape_seconds: float | None = None,
    score_seconds: float | None = None,
) -> None:
    """
    Records the time spent on each step (scraping / scoring) separately,
    so we can answer "where did the 8 minutes go?" with numbers rather
    than a guess. Only overwrites the columns provided: a scrape_seconds-only
    call does not touch score_seconds.
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


def update_lead_progress(
    conn,
    lead_id: int,
    status: str | None = None,
    error: str | None = None,
    scrape_seconds: float | None = None,
    score_seconds: float | None = None,
) -> None:
    """
    Single UPDATE + one commit for a lead's per-step results (status,
    last_error and the scrape/score timings) — the pipeline replaces its
    update_lead_status + record_lead_timing pairs with this. A status call
    always (re)writes last_error (None clears it), matching update_lead_status;
    the timing columns are only written when provided.
    """
    updates, params = [], []
    if status is not None:
        updates.append("status = ?")
        params.append(status)
        updates.append("last_error = ?")
        params.append(error)
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


def mark_duplicate(conn, lead_id: int, duplicate_of_id: int, reason: str) -> None:
    conn.execute(
        "UPDATE leads SET is_duplicate = 1, duplicate_of_id = ?, duplicate_reason = ? WHERE id = ?",
        (duplicate_of_id, reason, lead_id),
    )
    conn.commit()


VALID_REVIEW_STATUSES = ("APPROVED", "REJECTED")


def set_lead_review(
    conn,
    lead_id: int,
    decision: str,
    segment_override: str | None = None,
) -> None:
    """Records the human review decision (APPROVED/REJECTED) and any segment override."""
    if decision not in VALID_REVIEW_STATUSES:
        raise ValueError(f"invalid decision: {decision!r} (expected APPROVED or REJECTED)")
    conn.execute(
        "UPDATE leads SET review_status = ?, review_segment_override = ?, reviewed_at = ? WHERE id = ?",
        (decision, segment_override, _now(), lead_id),
    )
    conn.commit()


def save_lead_content(conn, lead_id: int, rows: list) -> None:
    now = _now()
    session_row = conn.execute("SELECT session_id FROM leads WHERE id = ?", (lead_id,)).fetchone()
    session_id = session_row[0] if session_row else None
    conn.executemany(
        "INSERT INTO lead_content (session_id, lead_id, source, url, content, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
        [(session_id, lead_id, source, url, content, now) for source, url, content in rows],
    )
    conn.commit()


def get_lead_content(conn, lead_id: int) -> list:
    query = "SELECT source, url, content FROM lead_content WHERE lead_id = ?"
    return [dict(r) for r in conn.execute(query, (lead_id,)).fetchall()]


def save_lead_technical_signals(
    conn,
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
              (session_id, lead_id, app_builder_fingerprint, site_builder_fingerprint,
               on_builder_subdomain, on_builder_subdomain_builder, generator_fingerprint,
               vibe_language_matches, trend_fonts_found, generator_meta_tag, github_repo_url, github_check,
             traction_signals, ai_style_phrases_found, ai_style_phrase_density, ai_authorship_disclosures_found,
             computed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            lead_id,
            technical_signals.get("app_builder_fingerprint"),
            technical_signals.get("site_builder_fingerprint"),
            1 if technical_signals.get("on_builder_subdomain") else 0,
            technical_signals.get("on_builder_subdomain_builder"),
            technical_signals.get("generator_fingerprint"),
            as_json(technical_signals.get("vibe_language_matches", [])),
            as_json(technical_signals.get("trend_fonts_found", [])),
            technical_signals.get("generator_meta_tag"),
            technical_signals.get("github_repo_url"),
            as_json(github_check),
            as_json(technical_signals.get("traction_signals", [])),
            as_json(technical_signals.get("ai_style_phrases_found", [])),
            technical_signals.get("ai_style_phrase_density"),
            as_json(technical_signals.get("ai_authorship_disclosures_found", [])),
            _now(),
        ),
    )
    conn.commit()


def get_lead_technical_signals(conn, lead_id: int) -> dict | None:
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
        "traction_signals",
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


def save_lead_score(conn, lead_id: int, verdict: dict) -> None:
    def as_json(v):
        return json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v

    session_row = conn.execute("SELECT session_id FROM leads WHERE id = ?", (lead_id,)).fetchone()
    session_id = session_row[0] if session_row else None
    conn.execute(
        """
        INSERT INTO lead_scores
            (session_id, lead_id, segment, confidence, company_stage, built_with_ai_signals,
                             technical_signals, pain_signals, sensitive_data_categories, data_sensitivity_score,
                             budget_signal, budget_evidence, budget_blockers, evidence_quotes, recommended_offer,
             personalization_hooks, disqualify_reason, needs_human_review, scored_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            as_json(verdict.get("sensitive_data_categories", [])),
            verdict.get("data_sensitivity_score", 0),
            verdict.get("budget_signal", "none"),
            as_json(verdict.get("budget_evidence", [])),
            as_json(verdict.get("budget_blockers", [])),
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
    conn,
    lead_id: int,
    source: str,
    query: str,
    results: list,
) -> None:
    """Records the results of an SGAI web search for a lead."""
    session_row = conn.execute(
        "SELECT session_id FROM leads WHERE id = ?", (lead_id,)
    ).fetchone()
    session_id = session_row[0] if session_row else None
    conn.execute(
        "INSERT INTO lead_search_evidence (session_id, lead_id, source, query, results, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, lead_id, source, query, json.dumps(results, ensure_ascii=False), _now()),
    )
    conn.commit()


def get_lead_search_evidence(conn, lead_id: int) -> list:
    """Returns all SGAI web searches for a lead."""
    rows = conn.execute(
        "SELECT * FROM lead_search_evidence WHERE lead_id = ? ORDER BY id", (lead_id,)
    ).fetchall()
    return _group_search_evidence(rows).get(lead_id, [])


def _group_search_evidence(rows) -> dict:
    """Groups raw lead_search_evidence rows by lead_id and parses the `results` JSON."""
    grouped = {}
    for r in rows:
        d = dict(r)
        try:
            d["results"] = json.loads(d["results"]) if isinstance(d["results"], str) else d["results"]
        except (json.JSONDecodeError, TypeError):
            pass
        grouped.setdefault(d["lead_id"], []).append(d)
    return grouped


def get_search_evidence_for_session(conn, session_id: int) -> dict:
    """Returns {lead_id: [evidence...]} for all leads of a session in 1 query."""
    rows = conn.execute(
        "SELECT * FROM lead_search_evidence WHERE lead_id IN (SELECT id FROM leads WHERE session_id = ?) ORDER BY lead_id, id",
        (session_id,),
    ).fetchall()
    return _group_search_evidence(rows)


def get_lead_search_evidence_map(conn, lead_ids: list) -> dict:
    """Returns {lead_id: [evidence...]} for the given lead ids in 1 query."""
    if not lead_ids:
        return {}
    rows = conn.execute(
        "SELECT * FROM lead_search_evidence WHERE lead_id = ANY(%s) ORDER BY lead_id, id",
        (lead_ids,),
    ).fetchall()
    return _group_search_evidence(rows)


def get_lead_content_map(conn, lead_ids: list) -> dict:
    """Returns {lead_id: [{source, url, content}, ...]} in 1 query."""
    if not lead_ids:
        return {}
    rows = conn.execute(
        "SELECT lead_id, source, url, content FROM lead_content WHERE lead_id = ANY(%s) ORDER BY lead_id, id",
        (lead_ids,),
    ).fetchall()
    grouped = {}
    for r in rows:
        grouped.setdefault(r["lead_id"], []).append(dict(r))
    return grouped


def get_lead_technical_signals_map(conn, lead_ids: list) -> dict:
    """Returns {lead_id: latest signals dict} for the given lead ids in 1 query."""
    if not lead_ids:
        return {}
    rows = conn.execute(
        "SELECT * FROM lead_technical_signals WHERE lead_id = ANY(%s) ORDER BY lead_id, id DESC",
        (lead_ids,),
    ).fetchall()
    grouped = {}
    for r in rows:
        d = dict(r)
        if d["lead_id"] in grouped:
            continue
        for json_field in (
            "vibe_language_matches",
            "trend_fonts_found",
            "traction_signals",
            "visual_patterns_triggered",
            "github_check",
            "ai_style_phrases_found",
            "ai_authorship_disclosures_found",
        ):
            if d.get(json_field):
                try:
                    d[json_field] = json.loads(d[json_field])
                except (json.JSONDecodeError, TypeError):
                    pass
        grouped[d["lead_id"]] = d
    return grouped


def get_exported_domains(conn) -> set:
    """Returns the set of domains already exported (across all sessions)."""
    rows = conn.execute("SELECT DISTINCT domain_normalized FROM export_history").fetchall()
    return {r["domain_normalized"] for r in rows if r["domain_normalized"]}


def record_export(conn, lead_ids: list, session_id: int | None = None) -> int:
    """
    Records the export of a list of leads (by id) in export_history,
    so the next batch can find them again via get_exported_domains().
    Returns the number of rows actually recorded (leads without a
    domain_normalized are ignored).
    """
    if not lead_ids:
        return 0
    now = _now()
    rows = conn.execute(
        "SELECT id, domain_normalized, session_id FROM leads WHERE id = ANY(%s)",
        (lead_ids,),
    ).fetchall()
    rows_to_insert = []
    for row in rows:
        if row["domain_normalized"]:
            rows_to_insert.append((session_id or row["session_id"], row["id"], row["domain_normalized"], now))

    if not rows_to_insert:
        return 0

    conn.executemany(
        "INSERT INTO export_history (session_id, lead_id, domain_normalized, exported_at) VALUES (?, ?, ?, ?)",
        rows_to_insert,
    )
    conn.commit()
    return len(rows_to_insert)


def get_leads_with_scores(conn, session_id: int | None = None, owner_id: int | None = None) -> list:
    query = """
        SELECT l.*, s.segment, s.confidence, s.company_stage, s.evidence_quotes,
               s.personalization_hooks, s.disqualify_reason, s.needs_human_review,
               s.recommended_offer, s.built_with_ai_signals, s.technical_signals,
               s.pain_signals, s.sensitive_data_categories, s.data_sensitivity_score,
               s.budget_signal, s.budget_evidence, s.budget_blockers, s.scored_at
        FROM leads l
        LEFT JOIN lead_scores s ON s.lead_id = l.id
            AND s.id = (SELECT MAX(id) FROM lead_scores WHERE lead_id = l.id)
    """
    conditions = []
    params = []
    if session_id is not None:
        conditions.append("l.session_id = ?")
        params.append(session_id)
    if owner_id is not None:
        query += " JOIN analysis_sessions a ON a.id = l.session_id"
        conditions.append("a.owner_id = ?")
        params.append(owner_id)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY l.id"
    return [dict(r) for r in conn.execute(query, params).fetchall()]

def get_lead_with_score(conn, lead_id: int) -> dict | None:
    """One lead + its latest verdict, fetched directly by id.

    Replaces the previous pattern of loading EVERY lead in the database and
    scanning for one id (app.py lead_review_view) — that worked at demo
    volume and would crawl at a few thousand leads.
    """
    row = conn.execute(
        """
        SELECT l.*, s.segment, s.confidence, s.company_stage, s.evidence_quotes,
               s.personalization_hooks, s.disqualify_reason, s.needs_human_review,
               s.recommended_offer, s.built_with_ai_signals, s.technical_signals,
               s.pain_signals, s.sensitive_data_categories, s.data_sensitivity_score,
               s.budget_signal, s.budget_evidence, s.budget_blockers, s.scored_at
        FROM leads l
        LEFT JOIN lead_scores s ON s.lead_id = l.id
            AND s.id = (SELECT MAX(id) FROM lead_scores WHERE lead_id = l.id)
        WHERE l.id = ?
        """,
        (lead_id,),
    ).fetchone()
    return dict(row) if row else None


def append_coverage_notes(conn, lead_id: int, notes: list[str]) -> None:
    """Appends data-quality/coverage notes to a lead (JSON list column).

    Coverage notes are the merge's "nothing fails silently" rule: which
    evidence lanes ran, which were skipped/capped/failed, what was thin or
    truncated. Duplicate notes are not re-appended.
    """
    if not notes:
        return
    row = conn.execute("SELECT coverage_notes FROM leads WHERE id = ?", (lead_id,)).fetchone()
    existing: list = []
    if row and row["coverage_notes"]:
        try:
            existing = json.loads(row["coverage_notes"])
        except (json.JSONDecodeError, TypeError):
            existing = [str(row["coverage_notes"])]
    for n in notes:
        if n and n not in existing:
            existing.append(n)
    conn.execute(
        "UPDATE leads SET coverage_notes = ? WHERE id = ?",
        (json.dumps(existing, ensure_ascii=False), lead_id),
    )
    conn.commit()


def get_coverage_notes(conn, lead_id: int) -> list[str]:
    row = conn.execute("SELECT coverage_notes FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if not row or not row["coverage_notes"]:
        return []
    try:
        return json.loads(row["coverage_notes"])
    except (json.JSONDecodeError, TypeError):
        return [str(row["coverage_notes"])]
