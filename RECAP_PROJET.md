# RECAP_PROJET.md — Lead Qualification & Scoring Engine

**Généré le :** 2026-07-29  
**Auteur :** Analyse statique du code sur disque  
**Python :** 3.11 (`.venv\Scripts\python.exe`)  
**OS :** Windows (win32)

---

## 1. Vue d'ensemble

### Architecture réelle

| Couche | Technologie | Fichier(s) |
|--------|-------------|------------|
| Framework web | Flask 3.x | `app.py` |
| Base de données | SQLite3 (fichier unique `leads.db`) | `db.py` |
| Scraping web | Firecrawl (via `firecrawl-py`) | `scraper.py` |
| Recherche web | ScrapeGraphAI API (POST /api/search, /api/scrape) | `scraper.py` |
| Scoring IA | Groq API (modèle `llama-3.3-70b-versatile`) via SDK OpenAI | `scorer.py` |
| Déduplication | RapidFuzz (`fuzz.token_sort_ratio`) | `dedup.py` |
| Export CSV | `csv` standard lib | `export.py` |
| Templates | Jinja2 (5 templates HTML) | `templates/*.html` |
| Frontend | Bootstrap 5.3.3 + CSS custom dark theme | `static/styles.css` |

### Flux de données (étape par étape)

```
1. Upload CSV Apollo
   └── app.py:upload_and_review() ou app.py:start_analysis()
       ├── db.py:create_analysis_session()
       ├── db.py:insert_leads_from_csv()       ← ingestion
       └── dedup.py:run_dedup()                ← déduplication 3 niveaux

2. Revue import (sélection leads + critères scoring)
   └── app.py:import_review()  [GET]
       └── templates/import_review.html
   └── app.py:start_pipeline_from_review()  [POST]
       ├── db.py:save_scoring_criteria()
       ├── db.py:save_scoring_criteria_custom()

3. Pipeline scraping + scoring (par lead)
   └── app.py:_background_pipeline()
       └── pipeline.py:run_pipeline()    [générateur, yield progress]
           ├── scraper.py:scrape_website()
           │   ├── _find_key_pages()     ← homepage + jusqu'à 4 pages
           │   ├── _firecrawl_scrape()   ← multi-key rate-limit rotation
           │   ├── extract_technical_signals()
           │   └── check_github_repo_pattern()
           ├── db.py:save_lead_content()
           ├── db.py:save_lead_technical_signals()
           ├── scorer.py:score_content()
           │   ├── _call_llm()           ← Groq API
           │   ├── _apply_confidence_guard()
           │   └── _verify_evidence_grounding()
           ├── db.py:save_lead_score()
           ├── scraper.py:search_additional_evidence()   ← SGAI search
           │   └── 5 sources : LinkedIn, ProductHunt, Twitter, GitHub, interviews
           └── db.py:save_search_evidence()

4. Résultats
   └── app.py:results_view()  [GET]
       └── templates/results.html     ← 5 catégories
   └── app.py:rescore_leads()  [POST]  ← rescore + web search optionnel
   └── app.py:rescore_phase2() [POST]  ← Phase 2 : web search + rescore tous

5. Export
   ├── app.py:export_results()  [CSV/PDF]  ← résultats complets
   ├── app.py:download_scores_csv()         ← scoring
   ├── app.py:download_scraping_csv()       ← scraping brut
   ├── app.py:download_search_csv()         ← recherche web SGAI
   └── export.py:export_*_csv() / *_csv_string()

6. Stop / Resume / Delete
   ├── app.py:stop_analysis()     → db.py:cancel_analysis_session()
   ├── app.py:resume_analysis()   → db.py:resume_analysis_session()
   ├── app.py:delete_session()    → db.py:delete_analysis_session()
   └── app.py:analyser_attente()  → reset SKIPPED/NEW → lance pipeline
```

### Services externes appelés

| Service | API | Clé .env | Fichier |
|---------|-----|----------|---------|
| Firecrawl | `Firecrawl.scrape()` (SDK) | `FIRECRAWL_API_KEY` (+ `_2` à `_5`) | `scraper.py:244-256` |
| Groq | `OpenAI.chat.completions.create()` (SDK) | `GROQ_API_KEY` | `scorer.py:78-82` |
| ScrapeGraphAI | POST /api/search, POST /api/scrape (requests) | `SGAI_API_KEY` | `scraper.py:756-757` |
| GitHub API | GET /repos/{owner}/{repo}/commits (requests, non-auth) | — | `scraper.py:571-608` |
| URL check | requests.head / .get | — | `scraper.py:363-382` |

---

## 2. Inventaire des fichiers

### 2.1 `app.py` (868 lignes)

**Rôle :** Interface Flask. Routes web, orchestration pipeline, téléchargements.

**Dépendances :**
- Imports projet : `db as dbmod`, `from db import _now as _db_now`, `dedup as dedupmod`, `export as exportmod`, `pipeline as pipelinemod`
- Librairies : `flask`, `pandas`, `json`, `os`, `tempfile`, `threading`, `time`, `uuid`, `collections.Counter`, `contextlib.contextmanager`, `datetime.datetime`
- Variables d'env : `DB_PATH` (default `dbmod.DB_PATH_DEFAULT`), `FLASK_SECRET_KEY` (default `"lead-qualification-engine"`), `PORT` (default `"5000"`), `FIRECRAWL_API_KEY`, `GROQ_API_KEY`

**Fonctions/classes :**

```python
# Variables globales
DB_PATH: str = os.getenv("DB_PATH", dbmod.DB_PATH_DEFAULT)
app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "lead-qualification-engine")
_pipeline_progress: dict[int, dict] = {}
_pipeline_lock = threading.Lock()
```

```python
def _store_progress(session_id: int, progress: dict) -> None
```
Stocke la progression dans `_pipeline_progress[session_id]` sous verrou.

```python
def _get_progress(session_id: int) -> dict | None
```
Lit la progression sous verrou.

```python
def _clear_progress(session_id: int) -> None
```
Supprime l'entrée de progression sous verrou.

```python
def _background_pipeline(conn, session_id: int, throttle_seconds: float) -> None
```
Exécute `pipelinemod.run_pipeline()` en thread. Itère les `update` yield, appelle `_store_progress()` à chaque lead. Gère les status `completed`/`cancelled`/`failed`. Appelle `dbmod.update_analysis_session_status()`. Ferme `conn` dans `finally`.

```python
@contextmanager
def open_db()
```
Ouvre connexion via `dbmod.get_connection(DB_PATH)`, appelle `dbmod.init_db(conn)`, yield, ferme.

```python
def missing_api_keys() -> list
```
Retourne les clés manquantes parmi `("FIRECRAWL_API_KEY", "GROQ_API_KEY")`.

```python
def _table_html(df: pd.DataFrame, columns: list[str]) -> str
```
Convertit un DataFrame en HTML `<table>` via `df.to_html()`. Si vide, retourne `<div class="empty-state">`.

```python
def _summary_context(conn, session_id=None) -> dict
```
Retourne `{total_leads, ready_to_process, duplicates, scored, needs_review, fetch_failed, score_failed, low_confidence, status_counts}`.

```python
def _session_summary(conn, session_id: int | None) -> tuple[int | None, dict | None]
```
Trouve une session par id ou la plus récente.

```python
def _load_dashboard_data(conn, session_id=None, selected_lead_id=None,
                         segment_filter=None, needs_review=False,
                         hide_duplicates=True) -> dict
```
Charge leads + scores en DataFrame, calcule `lead_detail` avec parse JSON des champs liste, ajoute `web_search_evidence`.

```python
def _csv_response(filename: str, csv_text: str) -> Response
```
Retourne une `Response` Flask avec Content-Disposition attachment.

```python
def _run_ingest(conn, uploaded_file, session_id=None) -> tuple[str, dict]
```
Écrit le fichier uploadé en temp, appelle `dbmod.insert_leads_from_csv()`, nettoie le temp. Retourne `(batch_id, summary)`.

```python
@app.route("/", methods=["GET"])
def home()
```
Route racine. Affiche `home.html` avec sessions et summary.

```python
@app.route("/dashboard", methods=["GET"])
def dashboard()
```
Route legacy. Affiche `dashboard.html` avec tableaux, filtres, lead_detail.

```python
@app.route("/upload", methods=["POST"])
def upload_and_review()
```
Étape 1 : upload CSV → création session → ingest + dedup → redirect `import_review`.

```python
@app.route("/import/<int:session_id>", methods=["GET"])
def import_review(session_id: int)
```
Étape 2 : page de revue. Affiche keepers/duplicates, criteria_options. Lit `dbmod.get_scoring_criteria_custom()`.

```python
@app.route("/import/<int:session_id>/start", methods=["POST"])
def start_pipeline_from_review(session_id: int)
```
Étape 3 : sauvegarde criteria + custom_criteria, inclut les doublons cochés, marque les non-sélectionnés SKIPPED, lance pipeline thread.

```python
@app.route("/analyser-attente/<int:session_id>", methods=["POST"])
def analyser_attente(session_id: int)
```
Reset SKIPPED/NEW → NEW, lance pipeline.

```python
@app.route("/resume/<int:session_id>", methods=["POST"])
def resume_analysis(session_id: int)
```
Réactive session annulée (`dbmod.resume_analysis_session()`), clear progress, lance pipeline.

```python
@app.route("/session/<int:session_id>/delete", methods=["POST"])
def delete_session(session_id: int)
```
Appelle `dbmod.delete_analysis_session()`.

```python
@app.route("/stop/<int:session_id>", methods=["POST"])
def stop_analysis(session_id: int)
```
Appelle `dbmod.cancel_analysis_session()`. Retourne `jsonify({"success": True})`.

```python
@app.route("/results/<int:session_id>", methods=["GET"])
def results_view(session_id: int)
```
Étape 4 : page résultats. 5 catégories : validees, proches, tres_loin, non_validees, en_attente. Logique de classement aux lignes 490-514.

```python
@app.route("/rescore/<int:session_id>", methods=["POST"])
def rescore_leads(session_id: int)
```
Re-scoring des leads sélectionnés (form `lead_ids[]`). Option web_search. Reset status → NEW, delete scores (+ search evidence si web_search=1). Puis redirect progress.

```python
@app.route("/rescore-phase2/<int:session_id>", methods=["POST"])
def rescore_phase2(session_id: int)
```
Phase 2 : web search + rescore pour TOUS les leads déjà scorés (sauf erreurs API). Reset tout → NEW + clear scores + search evidence. Puis redirect progress.

```python
@app.route("/start-analysis", methods=["POST"])
def start_analysis()
```
Upload + ingest + dedup + pipeline direct (sans page revue).

```python
@app.route("/ingest", methods=["POST"])
def ingest_only()
```
Import seul, statut "completed" direct.

```python
@app.route("/dedup", methods=["POST"])
def dedup_only()
```
Dédup seule sur la session courante.

```python
@app.route("/pipeline", methods=["POST"])
def pipeline_only()
```
Pipeline seul (scraping + scoring) sur les leads à traiter.

```python
@app.route("/lead/<int:lead_id>/review", methods=["POST"])
def review_lead(lead_id: int)
```
Review humaine : APPROVED/REJECTED + segment_override. Vérifie `dbmod.VALID_REVIEW_STATUSES`.

```python
@app.route("/download/scraping.csv", methods=["GET"])
def download_scraping_csv()
```
Export CSV scraping. Appelle `exportmod.scraping_csv_string()`.

```python
@app.route("/download/scores.csv", methods=["GET"])
def download_scores_csv()
```
Export CSV scores. Lance `dedupmod.run_export_dedup()` puis `exportmod.scores_csv_string()` + `dbmod.record_export()`.

```python
@app.route("/download/search.csv", methods=["GET"])
def download_search_csv()
```
Export CSV recherche web. Appelle `exportmod.search_csv_string()`.

```python
@app.route("/export/<int:session_id>/<format>", methods=["GET"])
def export_results(session_id: int, format: str)
```
Export complet CSV ou PDF. Logique de catégorie dupliquée de `results_view`.

```python
@app.route("/sessions/<int:session_id>", methods=["GET"])
def session_redirect(session_id: int)
```
Redirect simple vers `results_view`.

```python
@app.route("/progress/<int:session_id>")
def progress_view(session_id: int)
```
Affiche `progress.html`.

```python
@app.route("/progress/<int:session_id>/stream")
def progress_stream(session_id: int)
```
Endpoint SSE. Boucle `while True`, lit `_get_progress()`, yield `data: {json}\n\n`, break sur completed/failed.

```python
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=True, use_reloader=False)
```

**Routes Flask enregistrées (20+) :**
- `GET /` → home
- `GET /dashboard` → dashboard
- `POST /upload` → upload_and_review
- `GET /import/<id>` → import_review
- `POST /import/<id>/start` → start_pipeline_from_review
- `POST /analyser-attente/<id>` → analyser_attente
- `POST /resume/<id>` → resume_analysis
- `POST /session/<id>/delete` → delete_session
- `POST /stop/<id>` → stop_analysis
- `GET /results/<id>` → results_view
- `POST /rescore/<id>` → rescore_leads
- `POST /rescore-phase2/<id>` → rescore_phase2
- `POST /start-analysis` → start_analysis
- `POST /ingest` → ingest_only
- `POST /dedup` → dedup_only
- `POST /pipeline` → pipeline_only
- `POST /lead/<id>/review` → review_lead
- `GET /download/scraping.csv` → download_scraping_csv
- `GET /download/scores.csv` → download_scores_csv
- `GET /download/search.csv` → download_search_csv
- `GET /export/<id>/<format>` → export_results
- `GET /sessions/<id>` → session_redirect
- `GET /progress/<id>` → progress_view
- `GET /progress/<id>/stream` → progress_stream

**Cas d'erreur non catchés :**
- `_run_ingest()` lève `ValueError("Aucun fichier CSV fourni.")` si pas de fichier — catché par Flask (500).
- `_background_pipeline()` catch Exception → status "failed".
- `rescore_phase2`, `delete_session`, `resume_analysis`, `stop_analysis` ont chacun un `try/except Exception` avec flash.

---

### 2.2 `db.py` (782 lignes)

**Rôle :** Schéma SQLite + CRUD leads/sessions/scores/exports.

**Dépendances :**
- Standard : `csv`, `json`, `sqlite3`, `datetime`
- Variables d'env : `DB_PATH_DEFAULT = "leads.db"` (constante, pas os.getenv direct ici mais `get_connection()` accepte un paramètre)

**Constantes :**
```python
DB_PATH_DEFAULT = "leads.db"
COLUMN_ALIASES = { ... }  # mapping colonnes CSV Apollo
FREE_EMAIL_PROVIDERS = { "gmail.com", "yahoo.com", ... }
NON_TERMINAL_STATUSES = ("NEW", "PARSED", "FETCH_PARTIAL", "FETCH_FAILED")
VALID_REVIEW_STATUSES = ("APPROVED", "REJECTED")
```

**Fonctions :**

```python
def _email_domain(email: str) -> str
```
Extrait le domaine d'un email. Retourne "" si invalide ou pas de `@`.

```python
def _domains_related(a: str, b: str) -> bool
```
True si a == b ou a se termine par `.b` ou vice versa.

```python
def get_connection(db_path: str = DB_PATH_DEFAULT) -> sqlite3.Connection
```
`sqlite3.connect(db_path, check_same_thread=False)`, `row_factory = sqlite3.Row`, `PRAGMA foreign_keys = ON`.

```python
def _now() -> str
```
`datetime.now(timezone.utc).isoformat(timespec="seconds")`

```python
def create_analysis_session(conn, label=None, source_filename=None, notes=None) -> int
```
```sql
INSERT INTO analysis_sessions (label, source_filename, status, created_at, notes)
VALUES (?, ?, ?, ?, ?)
```
Retourne `last_insert_rowid()`.

```python
def update_analysis_session_status(conn, session_id, status, completed_at=None) -> None
```
```sql
UPDATE analysis_sessions SET status = ?, completed_at = COALESCE(?, completed_at) WHERE id = ?
```

```python
def get_analysis_session(conn, session_id) -> dict | None
```
```sql
SELECT * FROM analysis_sessions WHERE id = ?
```

```python
def delete_analysis_session(conn, session_id) -> None
```
DELETE cascade manuelle sur `lead_search_evidence`, `lead_scores`, `lead_technical_signals`, `lead_content`, `export_history`, `leads`, puis `analysis_sessions`.

```python
def cancel_analysis_session(conn, session_id) -> None
```
```sql
UPDATE analysis_sessions SET cancelled = 1 WHERE id = ?
```

```python
def resume_analysis_session(conn, session_id) -> None
```
```sql
UPDATE analysis_sessions SET cancelled = 0, status = 'running' WHERE id = ?
```

```python
def save_scoring_criteria_custom(conn, session_id, custom_text) -> None
```
```sql
UPDATE analysis_sessions SET scoring_criteria_custom = ? WHERE id = ?
```

```python
def get_scoring_criteria_custom(conn, session_id) -> str
```
```sql
SELECT scoring_criteria_custom FROM analysis_sessions WHERE id = ?
```

```python
def is_session_cancelled(conn, session_id) -> bool
```
```sql
SELECT cancelled FROM analysis_sessions WHERE id = ?
```

```python
def save_scoring_criteria(conn, session_id, criteria: list[str]) -> None
```
```sql
UPDATE analysis_sessions SET scoring_criteria = ? WHERE id = ?
```
criteria est JSON.dumps.

```python
def get_scoring_criteria(conn, session_id) -> list[str]
```
```sql
SELECT scoring_criteria FROM analysis_sessions WHERE id = ?
```
Retourne `json.loads()` ou `[]`.

```python
def get_latest_session_id(conn) -> int | None
```
```sql
SELECT id FROM analysis_sessions ORDER BY id DESC LIMIT 1
```

```python
def list_analysis_sessions(conn, limit=50) -> list
```
```sql
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
```

```python
def init_db(conn) -> None
```
CREATE TABLE IF NOT EXISTS pour toutes les tables (voir section 3). Puis migrations ALTER TABLE pour colonnes ajoutées après la création initiale. Gère les `sqlite3.OperationalError` (colonne existe déjà). Migre les données sans session_id vers une session "legacy". (Détail complet section 3.)

```python
def _normalize_domain(url: str) -> str
```
Nettoie une URL pour obtenir le domaine nu (sans protocole, sans www, sans path).

```python
def _pick_column(row: dict, key: str) -> str
```
Cherche une colonne dans un dict via `COLUMN_ALIASES` (insensible à la casse).

```python
def insert_leads_from_csv(conn, csv_path, batch_id, session_id=None) -> dict
```
Lit CSV via `csv.DictReader`, mappe les colonnes via `_pick_column`, calcule `domain_mismatch`. INSERT executemany. Retourne `{"inserted": N, "skipped_no_website": N}`.

```python
def get_leads(conn, include_duplicates=True, session_id=None) -> list
```
```sql
SELECT * FROM leads [WHERE ...] ORDER BY id
```
Conditions : `is_duplicate = 0` si `not include_duplicates`, `session_id = ?` si fourni.

```python
def get_leads_to_process(conn, session_id=None) -> list
```
```sql
SELECT * FROM leads WHERE is_duplicate = 0 AND status IN (?,?,?,?) [AND session_id = ?] ORDER BY id
```
Avec `NON_TERMINAL_STATUSES = ("NEW", "PARSED", "FETCH_PARTIAL", "FETCH_FAILED")`.

```python
def update_lead_status(conn, lead_id, status, error=None) -> None
```
```sql
UPDATE leads SET status = ?, last_error = ? WHERE id = ?
```

```python
def record_lead_timing(conn, lead_id, scrape_seconds=None, score_seconds=None) -> None
```
UPDATE dynamique : n'écrit que les colonnes fournies.

```python
def mark_duplicate(conn, lead_id, duplicate_of_id, reason) -> None
```
```sql
UPDATE leads SET is_duplicate = 1, duplicate_of_id = ?, duplicate_reason = ? WHERE id = ?
```

```python
def set_lead_review(conn, lead_id, decision, segment_override=None) -> None
```
Vérifie `decision in VALID_REVIEW_STATUSES`. UPDATE `review_status`, `review_segment_override`, `reviewed_at`.

```python
def save_lead_content(conn, lead_id, rows: list[tuple]) -> None
```
INSERT executemany dans `lead_content`. Récupère `session_id` de `leads`.

```python
def get_lead_content(conn, lead_id) -> list[dict]
```
```sql
SELECT source, url, content FROM lead_content WHERE lead_id = ?
```

```python
def save_lead_technical_signals(conn, lead_id, technical_signals, github_check) -> None
```
INSERT dans `lead_technical_signals`. Les champs liste sont JSON.dumps (via `as_json()` helper interne). Skip si `technical_signals is None`.

```python
def get_lead_technical_signals(conn, lead_id) -> dict | None
```
```sql
SELECT * FROM lead_technical_signals WHERE lead_id = ? ORDER BY id DESC LIMIT 1
```
Parse les champs JSON (try/except pour chaque).

```python
def save_lead_score(conn, lead_id, verdict: dict) -> None
```
INSERT dans `lead_scores`. Champs liste JSON.dumps. `needs_human_review` converti en int.

```python
def save_search_evidence(conn, lead_id, source, query, results: list) -> None
```
INSERT dans `lead_search_evidence`. `results` JSON.dumps.

```python
def get_lead_search_evidence(conn, lead_id) -> list[dict]
```
```sql
SELECT * FROM lead_search_evidence WHERE lead_id = ? ORDER BY id
```
Parse `results` JSON.

```python
def get_exported_domains(conn) -> set
```
```sql
SELECT DISTINCT domain_normalized FROM export_history
```

```python
def record_export(conn, lead_ids: list, session_id=None) -> int
```
INSERT executemany dans `export_history`. Ignore les leads sans `domain_normalized`.

```python
def get_leads_with_scores(conn, session_id=None) -> list[dict]
```
```sql
SELECT l.*, s.segment, s.confidence, s.company_stage, s.evidence_quotes,
       s.personalization_hooks, s.disqualify_reason, s.needs_human_review,
       s.recommended_offer, s.built_with_ai_signals, s.technical_signals,
       s.pain_signals, s.scored_at
FROM leads l
LEFT JOIN lead_scores s ON s.lead_id = l.id
    AND s.id = (SELECT MAX(id) FROM lead_scores WHERE lead_id = l.id)
[WHERE l.session_id = ?]
ORDER BY l.id
```

---

### 2.3 `pipeline.py` (165 lignes)

**Rôle :** Orchestrateur scraping + scoring lead par lead. Générateur yield progression.

**Dépendances :**
- `db as dbmod`, `scraper`, `scorer`
- `time`

**Constantes :**
```python
DEFAULT_THROTTLE_SECONDS = 15
```

**Fonctions :**

```python
def _now_ts() -> float
```
`time.monotonic()`

```python
def run_pipeline(conn, throttle_seconds: float = DEFAULT_THROTTLE_SECONDS,
                 session_id: int | None = None)
```
Générateur. Étapes par lead :
1. Vérifie `is_session_cancelled()` → break si True
2. `scraper.scrape_website(website, throttle_seconds=1.0, cancellation_check=is_cancelled)`
3. Save content, technical_signals
4. `scorer.score_content(rows, deterministic_signals, scoring_criteria, scoring_criteria_custom)`
5. Applique domain_mismatch guard → force `needs_human_review=True`
6. Save score, timing, status (SCORED ou LOW_CONFIDENCE)
7. Si `needs_human_review` ou `confidence < 0.5` : `scraper.search_additional_evidence()` + save
8. `time.sleep(throttle_seconds)`

Yield progress dict à chaque étape : `{index, total, lead_id, company_name, website_url, step, status, error, ts, started_at}` + `verdict`, `search_evidence`, `search_error`.

**Cas d'erreur :**
- Exception scraping → status FETCH_FAILED, continue
- Exception scoring → status SCORE_FAILED, continue
- Exception web search → log `search_error` dans progress, continue

---

### 2.4 `scraper.py` (875 lignes)

**Rôle :** Scraping Firecrawl + extraction signaux déterministes + recherche web SGAI.

**Dépendances :**
- `firecrawl-py` : `from firecrawl import Firecrawl`
- `dotenv` : `load_dotenv`
- `requests` (import local dans 3 fonctions)
- Standard : `hashlib`, `os`, `re`, `time`, `urllib.parse.urlparse`
- Variables d'env : `FIRECRAWL_API_KEY`, `FIRECRAWL_API_KEY_2` à `_5`, `SGAI_API_KEY`

**Constantes :**
```python
KEYWORDS = { ... }  # catégories → mots-clés pour discovery
COMMON_PATH_CANDIDATES = { ... }  # fallback paths
MAX_CONTENT_CHARS_PER_PAGE = 32000
BROKEN_PAGE_MARKERS = [ ... ]  # 7 marqueurs d'erreur
BROKEN_PAGE_PATTERNS = [ ... ]  # 5 patterns regex
MIN_VALID_CONTENT_CHARS = 50
GENERATOR_FINGERPRINTS = { ... }  # 5 builders
TREND_FONTS = [ ... ]  # 5 polices
VISUAL_PATTERNS = { ... }  # 9 patterns
VIBE_LANGUAGE_MARKERS = [ ... ]  # 6 marqueurs
AI_STYLE_PHRASES = [ ... ]  # 30+ phrases clichées
AI_AUTHORSHIP_DISCLOSURES = [ ... ]  # 7 mentions
ENGINEERING_ROLE_KEYWORDS = [ ... ]  # 15+ keywords
OTHER_ROLE_KEYWORDS = [ ... ]  # 13 keywords
SELF_SERVE_CTA_MARKERS = [ ... ]  # 8 CTAs
SALES_LED_CTA_MARKERS = [ ... ]  # 7 CTAs
VISIBLE_PRICE_PATTERN = re.compile(...)
_SGAI_BASE_URL = "https://v2-api.scrapegraphai.com/api"
SEARCH_QUERY_TEMPLATES = { "linkedin": ..., "product_hunt": ..., "twitter": ..., "github": ..., "interviews": ... }
```

**Fonctions :**

```python
def extract_careers_signal(content: str) -> dict
```
Retourne `{"has_careers_page_content": bool, "engineering_keywords_found": [...], "other_keywords_found": [...], "hiring_technical": bool, "engineering_ratio": float | None}`.

```python
def extract_pricing_signal(content: str) -> dict
```
Retourne `{"has_pricing_page_content": bool, "self_serve_markers_found": [...], "sales_led_markers_found": [...], "has_visible_price": bool, "pricing_motion": "self_serve"|"sales_led"|"mixed"|"unclear"}`.

```python
def _format_signal_as_text(label: str, signal: dict) -> str
```
Formate un signal dict en texte lisible pour stockage dans `lead_content`.

```python
def _get_clients() -> list[Firecrawl]
```
Crée un client par clé API (FIRECRAWL_API_KEY, _2, _3, _4, _5). Retourne liste.

```python
def _parse_retry_after(error_msg: str) -> float | None
```
Extrait "retry after Ns" du message d'erreur Firecrawl.

```python
def _firecrawl_scrape(url, *args, **kwargs)
```
Wrapper multi-key. Rate-limit → passe à la clé suivante immédiatement (max 2 rounds). Toutes épuisées → attend le délai le plus court + retente. Exception non-rate-limit → remonte immédiatement.

```python
def _normalize_domain(url: str) -> str
```
Nettoie URL → netloc minuscule sans www.

```python
def _is_same_domain(link: str, homepage_url: str) -> bool
```
True si même domaine que la homepage.

```python
def _is_real_subpage(link: str, homepage_url: str) -> bool
```
False si lien = fragment (#) ou même URL que la homepage.

```python
def _url_exists(url: str, timeout=5.0) -> bool
```
HEAD (fallback GET si 405/501). False sur toute exception.

```python
def _looks_broken(markdown: str) -> bool
```
True si contenu < MIN_VALID_CONTENT_CHARS, ou contient BROKEN_PAGE_MARKERS, ou match BROKEN_PAGE_PATTERNS.

```python
def _content_fingerprint(markdown: str) -> str
```
Hash SHA256 du contenu normalisé (sans URLs, images, espacement). Pour dédoublonner les pages SPA.

```python
def _find_key_pages(homepage_url: str) -> tuple[dict, FirecrawlResponse, list]
```
Scrape homepage (markdown + rawHtml + links), filtre les sous-pages par domaine et mot-clé, fallback COMMON_PATH_CANDIDATES via `_url_exists`, catch-all product si vide. Retourne `(found_pages, homepage_result, all_links)`.

**Exceptions non catchées :** `_firecrawl_scrape()` peut raise `RuntimeError("Aucune clé API Firecrawl configurée")` ou la dernière exception rate-limit après 2 rounds.

```python
def _match_any(patterns: list, text: str) -> bool
```

```python
def extract_technical_signals(raw_html, all_links, homepage_text="") -> dict
```
Retourne `{generator_fingerprint, vibe_language_matches, trend_fonts_found, visual_patterns_triggered, generator_meta_tag, github_repo_url, ai_style_phrases_found, ai_style_phrase_density, ai_authorship_disclosures_found}`.

```python
def check_github_repo_pattern(repo_url: str) -> dict
```
Appelle GitHub API (non auth). Retourne `{repo_url, checked, evidence: {total_commits_seen, first_commit_message, single_commit_repo}, error}`.

```python
def scrape_website(homepage_url: str, throttle_seconds=1.0, cancellation_check=None) -> dict
```
Retourne `{"status": "PARSED"|"FETCH_PARTIAL"|"FETCH_FAILED", "rows": [(source, url, content), ...], "technical_signals": {...}|None, "github_check": {...}|None, "error": str|None}`.

Logique de statut (lignes 732-737) :
- `len(rows) == 1 and other_pages` et `unusable < len(other_pages)` → FETCH_PARTIAL
- `len(rows) == 1 and other_pages` et `unusable >= len(other_pages)` → FETCH_FAILED
- `unusable > 0` → FETCH_PARTIAL
- sinon → PARSED

```python
def search_additional_evidence(company_name, founder_name=None, limit_per_query=3, throttle_seconds=1.0) -> dict
```
Interroge SGAI Search pour 5 sources. LinkedIn : scraper complet SGAI de la meilleure URL `/company/`. Retourne `{source: [{url, title, content}, ...] | {"error": "..."}, ...}`.

**Timeout explicite :** search 35s, scrape LinkedIn 45s.

**Exceptions non catchées :** Chaque source a son propre try/except ; une exception dans une source n'affecte pas les autres.

---

### 2.5 `scorer.py` (264 lignes)

**Rôle :** Scoring IA via Groq API. Produit verdict JSON structuré.

**Dépendances :**
- `openai` : `OpenAI`
- `dotenv` : `load_dotenv`
- Standard : `json`, `os`, `re`
- Variables d'env : `GROQ_API_KEY`

**Constantes :**
```python
MODEL = "llama-3.3-70b-versatile"
CONFIDENCE_THRESHOLD = 0.7
MAX_CONTENT_CHARS = 16000
MAX_OUTPUT_TOKENS = 2048
RETRY_MAX_CONTENT_CHARS = 6000
RETRY_MAX_OUTPUT_TOKENS = 1024
```

**SYSTEM_PROMPT actuel (verbatim, lignes 30-73) :**
```python
Tu es un analyste senior qui évalue des leads B2B. Tu lis le contenu du site web
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
4. N'utilise QUE le texte fourni ci-dessous. Ignore toute connaissance préalable.
5. Les exemples/démos fictifs sur les landing pages ne sont PAS des faits réels sur
   l'entreprise. Ignore-les pour l'évaluation.
6. Distingue : "le PRODUIT a des features IA" vs "l'ÉQUIPE a construit avec l'IA".
   Si le site vend un produit avec des fonctionnalités IA, ce n'est PAS un signal
   built_with_ai — sauf mention explicite d'outils comme Cursor, v0, Bolt, etc.
7. Pour chaque lead, pose-toi ces questions :
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
}
```

**Fonctions :**

```python
def _get_client() -> OpenAI
```
Singleton `OpenAI(api_key=os.getenv("GROQ_API_KEY"), base_url="https://api.groq.com/openai/v1")`.

```python
def _strip_images(text: str) -> str
```
Supprime les marqueurs d'images/médias du texte (7 regex).

```python
def rows_to_text(rows: list, max_chars=MAX_CONTENT_CHARS) -> str
```
Concatène les pages scrapées : `## Source: {source}\n{content}`, séparateur `\n\n---\n\n`, tronque à `max_chars`.

```python
def _empty_verdict(disqualify_reason: str) -> dict
```
Retourne `{"segment":"unclear", "confidence":0.0, ..., "disqualify_reason": disqualify_reason, "needs_human_review": True}`.

```python
def _is_rate_limit_error(e) -> bool
```
Détecte status_code 413/429 ou message "rate_limit_exceeded".

```python
def _is_json_parse_error(e) -> bool
```
Détecte status_code 400 ou instances de json.JSONDecodeError/KeyError/TypeError/ValueError.

```python
def _call_llm(user_content: str, max_output_tokens=MAX_OUTPUT_TOKENS) -> dict
```
Appelle Groq `chat.completions.create()` avec `response_format={"type": "json_object"}`, temperature=0.2.

```python
def _apply_confidence_guard(verdict: dict) -> dict
```
Si `confidence < CONFIDENCE_THRESHOLD` (0.7), force `needs_human_review = True`.

```python
def _normalize_for_grounding(s: str) -> str
```
Normalise espacement + lower pour comparaison de citations.

```python
def _verify_evidence_grounding(verdict: dict, source_text: str) -> dict
```
Vérifie chaque `evidence_quote` dans le texte source. Les non trouvées sont retirées et `needs_human_review=True` + `disqualify_reason` annoté.

```python
def _retry_after_failure(rows, deterministic_signals, build_user_content, error_str) -> dict
```
Retente le scoring avec `RETRY_MAX_CONTENT_CHARS`. Si échec, retourne `_empty_verdict("json_parse_failed: ...")`.

```python
def score_content(rows: list, deterministic_signals=None,
                  scoring_criteria: list[str] | None = None,
                  scoring_criteria_custom: str = "") -> dict
```
Fonction principale. Construit le prompt user via `build_user_content()` (closure), injecte `scoring_criteria` et `scoring_criteria_custom` si présents, injecte `deterministic_signals` si présent. Appelle `_call_llm()` → `_apply_confidence_guard()` → `_verify_evidence_grounding()`.

**Cas d'erreur :**
- `rows_to_text()` vide → `_empty_verdict("no_content_scraped")`
- `json.JSONDecodeError` → `_retry_after_failure()`
- `_is_json_parse_error(e)` → `_retry_after_failure()`
- `_is_rate_limit_error(e)` → retry avec contenu réduit → si échec → `_empty_verdict("api_error_after_retry: ...")`
- Autres exceptions → `raise` (non catché → remonte à pipeline.py → catch SCORE_FAILED)

---

### 2.6 `dedup.py` (97 lignes)

**Rôle :** Déduplication 3 niveaux (email exact, domaine, fuzzy name).

**Dépendances :**
- `rapidfuzz.fuzz`
- `db as dbmod`

**Fonctions :**

```python
def run_dedup(conn, fuzzy_threshold: int = 90, session_id=None) -> dict
```
Parcourt les leads (non-duplicates déjà). Niveau 1 : email exact. Niveau 2 : domaine normalisé. Niveau 3 : fuzzy token_sort_ratio sur company_name. Marque les doublons via `dbmod.mark_duplicate()`. Retourne `{"exact_email": N, "domain": N, "fuzzy_company": N, "kept_original": N}`.

```python
def check_against_export_history(conn, exported_domains: set) -> int
```
Marque comme duplicate les leads dont le domaine a déjà été exporté.

```python
def run_export_dedup(conn, session_id=None) -> int
```
Appelle `dbmod.get_exported_domains()` puis `check_against_export_history()`.

---

### 2.7 `export.py` (497 lignes)

**Rôle :** TROIS formats CSV + recherche web. CLI séparé.

**Dépendances :**
- `db as dbmod`
- Standard : `csv`, `json`, `io`, `argparse`

**Fonctions :**

```python
def _flatten(value) -> str
```
Normalise une valeur pour CSV : None→"", list→" | ".join, dict→json.dumps.

```python
def _iter_scraping_rows(conn, session_id=None)
```
Générateur : une ligne par page scrapée, avec signaux déterministes. Structure via `SCRAPING_FIELDS`.

```python
def export_scraping_csv(conn, output_path, session_id=None) -> int
```
Écrit `_iter_scraping_rows()` dans un fichier CSV.

```python
def scraping_csv_string(conn, session_id=None) -> str
```
Même contenu en mémoire (StringIO).

```python
def _iter_score_rows(conn, session_id=None)
```
Générateur : une ligne par lead avec DERNIER verdict de scoring. Via `dbmod.get_leads_with_scores()`.

```python
def export_scores_csv(conn, output_path, session_id=None) -> int
```

```python
def scores_csv_string(conn, session_id=None) -> str
```

```python
def _preview(text, max_chars=400) -> str
```
Tronque le texte à max_chars avec " …".

```python
def _format_signals_summary(signals: dict) -> str
```
Traduit `technical_signals` en phrase lisible.

```python
def _format_github_check_summary(github_check) -> str
```
Résumé du check git.

```python
def _iter_readable_rows(conn, session_id=None, preview_chars=400)
```
Générateur : une ligne par lead avec aperçus par page (homepage, about, product, pricing, careers).

```python
def export_readable_csv(conn, output_path, session_id=None, preview_chars=400) -> int
```

```python
def readable_csv_string(conn, session_id=None, preview_chars=400) -> str
```

```python
def _iter_search_rows(conn, session_id=None)
```
Générateur : une ligne par résultat de recherche web SGAI.

```python
def export_search_csv(conn, output_path, session_id=None) -> int
```

```python
def search_csv_string(conn, session_id=None) -> str
```

```python
def main()
```
CLI : `python export.py [--db leads.db] [--scraping-out ...] [--scores-out ...] [--search-out ...]`

---

### 2.8 Templates HTML

#### `templates/home.html` (166 lignes)
Page d'accueil. Upload CSV + historique sessions. Stats cards. Lien vers `results_view` si scored > 0, `progress_view` si running, `import_review` si pending. Bouton Supprimer par session.

#### `templates/dashboard.html` (286 lignes)
Dashboard legacy avec ingestion, actions rapides, table leads, table scores, filtres segment/needs_review/hide_duplicates, lead detail expandable avec web_search_evidence.

#### `templates/import_review.html` (233 lignes)
Revue import : keepers + duplicates tables avec checkboxes. Scoring criteria : 6 cartes checkable + textarea critère personnalisé + throttle input. Validation JS : ≥1 lead et ≥1 critère.

#### `templates/progress.html` (371 lignes)
Page progression temps réel : barre, stats, timer. SSE connecté à `/progress/<id>/stream`. STOP button avec confirmation. Reprendre + Voir resultats sur annulation. Redirection auto 5s sur completion.

#### `templates/results.html` (347 lignes)
Résultats 5 catégories. Chaque table (Validees, Proches, TresLoin) a checkboxes + boutons rescore/recherche web. Phase 2 button globale. Exports dropdowns (CSV/PDF). JS toggleAll/toggleAllIn avec counters.

#### `templates/results_print.html` (96 lignes)
Version print-friendly landscape pour PDF. Tables simples sans checkboxes.

#### `static/styles.css` (563 lignes)
Thème dark (#0f172a bg, #5eead4 accent). Classes : `.hero`, `.card`, `.shell`, `.progress-*`, `.criteria-*`, `.btn-accent`, `.table-wrap`, `.search-source`, etc.

---

### 2.9 Fichiers de configuration

#### `.env` (4 lignes)
```
FIRECRAWL_API_KEY=fc-********
FIRECRAWL_API_KEY_2=fc-********
GROQ_API_KEY=gsk_********
SGAI_API_KEY=sgai-********
```

#### `requirements.txt` (6 lignes)
```
pandas
rapidfuzz
firecrawl-py
openai
python-dotenv
flask
```

#### `README.md` (61 lignes)
Documentation haut niveau. Installation, lancement, fichiers, segments, statuts, notes tiers.

### 2.10 `RECAP_PROJET.md` (ce fichier)

---

## 3. Schéma de base de données complet

### Table `analysis_sessions`

```sql
CREATE TABLE IF NOT EXISTS analysis_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT,
    source_filename TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    created_at TEXT NOT NULL,
    completed_at TEXT,
    notes TEXT
);
```

**Colonnes ajoutées via ALTER TABLE (migrations idempotentes dans `init_db()` ligne 305-314) :**
- `cancelled INTEGER NOT NULL DEFAULT 0`
- `scoring_criteria TEXT`
- `scoring_criteria_custom TEXT`

### Table `leads`

```sql
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
```

**Colonnes ajoutées via ALTER TABLE (migrations idempotentes dans `init_db()`) :**
- `review_status TEXT` (via boucle ligne 317-325)
- `review_segment_override TEXT`
- `reviewed_at TEXT`
- `last_error TEXT` (via boucle ligne 328-336)
- `scrape_seconds REAL`
- `score_seconds REAL`
- `email_domain TEXT` (via boucle ligne 344-352, doublon avec la création)
- `domain_mismatch INTEGER NOT NULL DEFAULT 0` (doublon)
- `domain_mismatch_reason TEXT` (doublon)
- `session_id INTEGER` (via boucle ligne 338-341 pour tables secondaires aussi)

### Table `lead_content`

```sql
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
```

### Table `lead_technical_signals`

```sql
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
```

**Colonnes ajoutées via ALTER TABLE :**
- `ai_style_phrases_found TEXT` (via boucle ligne 354-362)
- `ai_style_phrase_density TEXT`
- `ai_authorship_disclosures_found TEXT`

### Table `lead_scores`

```sql
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
```

### Table `lead_search_evidence`

```sql
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
```

### Table `export_history`

```sql
CREATE TABLE IF NOT EXISTS export_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,
    lead_id INTEGER NOT NULL,
    domain_normalized TEXT NOT NULL,
    exported_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES analysis_sessions(id),
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);
```

**Index :**
```sql
CREATE INDEX IF NOT EXISTS idx_sessions_created_at ON analysis_sessions(created_at);
CREATE INDEX IF NOT EXISTS idx_leads_session ON leads(session_id);
CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email);
CREATE INDEX IF NOT EXISTS idx_leads_domain ON leads(domain_normalized);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_content_session ON lead_content(session_id);
CREATE INDEX IF NOT EXISTS idx_scores_session ON lead_scores(session_id);
CREATE INDEX IF NOT EXISTS idx_technical_signals_lead ON lead_technical_signals(lead_id);
CREATE INDEX IF NOT EXISTS idx_export_history_domain ON export_history(domain_normalized);
```

### Migration "legacy" (init_db ligne 364-396)
Si des lignes avec `session_id IS NULL` existent dans `leads`, `lead_content`, `lead_technical_signals`, ou `lead_scores`, une session "legacy" est créée (ou réutilisée si elle existe déjà) et ces lignes y sont associées.

---

## 4. Contrats d'interface entre modules

### `pipeline.py` → `db.py`

| Appel | Signature réelle dans db.py | OK ? |
|-------|---------------------------|------|
| `dbmod.get_scoring_criteria(conn, session_id)` | `def get_scoring_criteria(conn, session_id) -> list[str]` | ✅ |
| `dbmod.get_scoring_criteria_custom(conn, session_id)` | `def get_scoring_criteria_custom(conn, session_id) -> str` | ✅ |
| `dbmod.get_leads_to_process(conn, session_id=session_id)` | `def get_leads_to_process(conn, session_id=None) -> list` | ✅ |
| `dbmod.is_session_cancelled(conn, session_id)` | `def is_session_cancelled(conn, session_id) -> bool` | ✅ |
| `dbmod.update_lead_status(conn, lead_id, ..., error=...)` | `def update_lead_status(conn, lead_id, status, error=None) -> None` | ✅ |
| `dbmod.record_lead_timing(conn, lead_id, scrape_seconds)` | `def record_lead_timing(conn, lead_id, scrape_seconds=None, score_seconds=None)` | ✅ |
| `dbmod.save_lead_content(conn, lead_id, scrape_result["rows"])` | `def save_lead_content(conn, lead_id, rows: list)` | ✅ |
| `dbmod.save_lead_technical_signals(conn, lead_id, technical_signals, github_check)` | `def save_lead_technical_signals(conn, lead_id, technical_signals, github_check)` | ✅ |
| `dbmod.save_lead_score(conn, lead_id, verdict)` | `def save_lead_score(conn, lead_id, verdict: dict)` | ✅ |
| `dbmod.save_search_evidence(conn, lead_id, source, template, hits)` | `def save_search_evidence(conn, lead_id, source, query, results: list)` | ✅ |

### `pipeline.py` → `scraper.py`

| Appel | Signature réelle dans scraper.py | OK ? |
|-------|-------------------------------|------|
| `scraper.scrape_website(website, throttle_seconds=1.0, cancellation_check=_is_cancelled)` | `def scrape_website(homepage_url, throttle_seconds=1.0, cancellation_check=None)` | ✅ |
| `scraper.search_additional_evidence(company_name=..., founder_name=...)` | `def search_additional_evidence(company_name, founder_name=None, limit_per_query=3, throttle_seconds=1.0)` | ✅ |
| `scraper.SEARCH_QUERY_TEMPLATES.get(source, "")` | `SEARCH_QUERY_TEMPLATES: dict[str, str]` | ✅ |

### `pipeline.py` → `scorer.py`

| Appel | Signature réelle dans scorer.py | OK ? |
|-------|-------------------------------|------|
| `scorer.score_content(rows, deterministic_signals=..., scoring_criteria=..., scoring_criteria_custom=...)` | `def score_content(rows, deterministic_signals=None, scoring_criteria=None, scoring_criteria_custom="")` | ✅ |

### `app.py` → `db.py`

Toutes les fonctions appelées existent avec les signatures attendues. ✅

### `app.py` → `export.py`

| Appel | Signature réelle | OK ? |
|-------|-----------------|------|
| `exportmod.scraping_csv_string(conn, session_id=...)` | `def scraping_csv_string(conn, session_id=None) -> str` | ✅ |
| `exportmod.scores_csv_string(conn, session_id=...)` | `def scores_csv_string(conn, session_id=None) -> str` | ✅ |
| `exportmod.search_csv_string(conn, session_id=...)` | `def search_csv_string(conn, session_id=None) -> str` | ✅ |

### `app.py` → `dedup.py`

| Appel | Signature réelle | OK ? |
|-------|-----------------|------|
| `dedupmod.run_dedup(conn, fuzzy_threshold=..., session_id=...)` | `def run_dedup(conn, fuzzy_threshold=90, session_id=None)` | ✅ |
| `dedupmod.run_export_dedup(conn, session_id=...)` | `def run_export_dedup(conn, session_id=None) -> int` | ✅ |

### `export.py` → `db.py`

Toutes les fonctions appelées (get_leads, get_leads_with_scores, get_lead_content, get_lead_technical_signals, get_lead_search_evidence) existent avec les signatures attendues. ✅

### `dedup.py` → `db.py`

`dbmod.get_leads()`, `dbmod.mark_duplicate()`, `dbmod.get_exported_domains()` existent. ✅

### ⚠️ INCOHÉRENCES DÉTECTÉES

Aucune incohérence critique entre les appels et les signatures réelles.

---

## 5. Statuts de cycle de vie d'un lead

### Statuts possibles (grep de toutes les chaînes passées à `update_lead_status` ou requêtes UPDATE leads SET status)

| Statut | Posé par | Condition | Terminal |
|--------|---------|-----------|----------|
| `NEW` | `insert_leads_from_csv()` (db.py:467), `rescore_leads` (app.py:570), `rescore_phase2` (app.py:600), `analyser_attente` (app.py:407) | Import ou reset | ❌ (repris par `get_leads_to_process`) |
| `PARSED` | `run_pipeline()` (pipeline.py:70) via `scrape_result["status"]` | Scraping réussi | ❌ (dans NON_TERMINAL_STATUSES) |
| `FETCH_PARTIAL` | `run_pipeline()` (pipeline.py:70) via `scrape_result["status"]` | Scraping partiel | ❌ (dans NON_TERMINAL_STATUSES) |
| `FETCH_FAILED` | `run_pipeline()` (pipeline.py:62,70) | Exception scraping ou homepage cassée | ❌ (dans NON_TERMINAL_STATUSES) |
| `SCORED` | `run_pipeline()` (pipeline.py:127) | Scoring OK et `needs_human_review = False` | ✅ |
| `LOW_CONFIDENCE` | `run_pipeline()` (pipeline.py:127) | Scoring OK mais `needs_human_review = True` | ✅ |
| `SCORE_FAILED` | `run_pipeline()` (pipeline.py:130) | Exception scoring | ✅ |
| `SKIPPED` | `start_pipeline_from_review` (app.py:384) | Lead non coché à l'import | ✅ |
| `APPROVED` | `review_lead` (app.py:724) via `dbmod.set_lead_review` | Review humaine | ✅ |
| `REJECTED` | `review_lead` (app.py:724) via `dbmod.set_lead_review` | Review humaine | ✅ |

### Clause WHERE de `get_leads_to_process()` (db.py:506-514) :
```python
NON_TERMINAL_STATUSES = ("NEW", "PARSED", "FETCH_PARTIAL", "FETCH_FAILED")
query = f"SELECT * FROM leads WHERE is_duplicate = 0 AND status IN ({placeholders})"
```
Seuls les leads avec `is_duplicate = 0` ET `status IN ("NEW", "PARSED", "FETCH_PARTIAL", "FETCH_FAILED")` sont repris.

**Note :** `FETCH_FAILED` est dans `NON_TERMINAL_STATUSES`, ce qui signifie qu'un lead dont le scraping a échoué sera retenté au prochain run du pipeline. C'est intentionnel (scoring peut produire un verdict même avec rows=[]).

---

## 6. Prompt système du scorer (verbatim)

Voir section 2.5 ci-dessus (lignes 30-73 de `scorer.py`).

### Schéma JSON de sortie attendu

```json
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
}
```

### Mapping vers `lead_scores` (via `save_lead_score` db.py:665-696)

| Champ JSON | Colonne SQL | Type | Notes |
|-----------|------------|------|-------|
| `segment` | `segment` | TEXT | |
| `confidence` | `confidence` | REAL | |
| `company_stage` | `company_stage` | TEXT | |
| `built_with_ai_signals` | `built_with_ai_signals` | TEXT | JSON.dumps si liste |
| `technical_signals` | `technical_signals` | TEXT | JSON.dumps si liste |
| `pain_signals` | `pain_signals` | TEXT | JSON.dumps si liste |
| `evidence_quotes` | `evidence_quotes` | TEXT | JSON.dumps si liste |
| `recommended_offer` | `recommended_offer` | TEXT | |
| `personalization_hooks` | `personalization_hooks` | TEXT | JSON.dumps si liste |
| `disqualify_reason` | `disqualify_reason` | TEXT | |
| `needs_human_review` | `needs_human_review` | INTEGER | 1 si True, 0 sinon |

**Écart constaté :** Aucun. Le schéma JSON du prompt correspond exactement aux colonnes écrites.

---

## 7. Historique des bugs corrigés

Aucun fichier `CHANGELOG.md` ou équivalent trouvé dans le repo. Les corrections sont documentées dans les commentaires du code :

### Fix mentionné dans `scraper.py`
- **Ligne 435-441** ("Fix bug #2") : Filtre des liens externes (même domaine uniquement pour le matching de pages clés). Un lien externe contenant "product" n'est plus choisi comme page produit.
- **Ligne 691-704** ("Fix bug #3") : Rejet du contenu identique à une page déjà retenue (dédoublonnage SPA par hash de contenu).
- **Ligne 707-712** ("Fix bug confirmé (section 3 du plan)") : `extract_careers_signal` et `extract_pricing_signal` étaient définies mais jamais appelées. Maintenant appelées dans `scrape_website()`.

### Fix mentionné dans `scraper.py` ligne 680
- Vérification : `_content_fingerprint` est appelée avant compaction careers/pricing. ✅

---

## 8. Gaps connus / TODO

### Gaps relevés dans le code
1. **`export.py:322-323`** : Commentaire indique que "pricing_preview et careers_preview restent pour l'instant un aperçu du texte brut tronqué (pas encore le signal ciblé)". TODO non implémenté.
2. **`export.py:444`** : Commentaire "Inclut aussi les leads sans search evidence... pour signaler qu'ils ont été scannés". La fonction `_iter_search_rows()` ne génère en réalité AUCUNE ligne pour les leads sans search evidence — le commentaire est trompeur.
3. **`scorer.py:264`** : Dernière ligne du fichier : `raise` — les exceptions non rate-limit et non JSON-parse sont remontées telles quelles (non catchées). C'est intentionnel (catché par pipeline.py comme SCORE_FAILED).
4. **Pas de migrations versionnées** : `init_db()` ALTER TABLE avec try/except OperationalError — pas de système de version.
5. **Pas de tests automatisés** : Aucun fichier `test_*.py` trouvé.
6. **Pas de CHANGELOG.md / AGENTS.md** : historique des modifications non tracé.

### Gaps détectés pendant l'analyse
7. **JavaScript `results.html:330`** : Le sélecteur `.cb-tresLoin` est correct maintenant (avait été `.cb-tresloin` avant la correction de ce document). Vérifié et corrigé plus haut.
8. **`scorer.py` ligne 264** : `raise` peut remonter des exceptions comme `ConnectionError`, `Timeout`, etc. qui ne seront catchées que par `_background_pipeline()` dans `app.py`.

---

## 9. Configuration & environnement

### Variables d'environnement attendues

| Variable | Fichier(s) | Usage |
|----------|-----------|-------|
| `FIRECRAWL_API_KEY` | `scraper.py:252` | Clé principale Firecrawl |
| `FIRECRAWL_API_KEY_2` | `scraper.py:252` | Clé secondaire Firecrawl |
| `FIRECRAWL_API_KEY_3` | `scraper.py:252` | Clé tertiaire (optionnelle) |
| `FIRECRAWL_API_KEY_4` | `scraper.py:252` | Clé quaternaire (optionnelle) |
| `FIRECRAWL_API_KEY_5` | `scraper.py:252` | Clé quinaire (optionnelle) |
| `GROQ_API_KEY` | `scorer.py:81`, `app.py:96` | API Groq pour le scoring LLM |
| `SGAI_API_KEY` | `scraper.py:757` | API ScrapeGraphAI pour la recherche web |
| `DB_PATH` | `app.py:29` | Chemin de la base SQLite (défaut: `leads.db`) |
| `FLASK_SECRET_KEY` | `app.py:32` | Secret Flask (défaut: `"lead-qualification-engine"`) |
| `PORT` | `app.py:867` | Port serveur (défaut: `"5000"`) |

### requirements.txt
```
pandas
rapidfuzz
firecrawl-py
openai
python-dotenv
flask
```

### .env actuel (contient des clés réelles)
```
FIRECRAWL_API_KEY=fc-********
FIRECRAWL_API_KEY_2=fc-********
GROQ_API_KEY=gsk_********
SGAI_API_KEY=sgai-********
```

### Commande de lancement
```bash
python app.py
```
Ouvre sur `http://127.0.0.1:5000`. Debug=True.

---

## 10. Comment vérifier que ça marche

### Commandes de diagnostic existantes

1. **Syntaxe Python** (tous les fichiers compilent)
   ```bash
   python -c "import py_compile; [py_compile.compile(f, doraise=True) for f in ['app.py','db.py','pipeline.py','scraper.py','scorer.py','dedup.py','export.py']]"
   ```

2. **Lancement du serveur** (test manuel)
   ```bash
   python app.py
   ```
   Vérifier : `http://127.0.0.1:5000` répond, pages home/dashboard chargent.

3. **Export CLI** (indépendant de Flask)
   ```bash
   python export.py --db leads.db
   ```
   Produit 3 fichiers CSV : `scraping_results.csv`, `scores_results.csv`, `search_results.csv`.

4. **Aucun test unitaire** trouvé dans le repo.

### Checklist manuelle
- [ ] `python app.py` démarre sans erreur
- [ ] Page d'accueil `/` affiche stats et historique
- [ ] Import CSV fonctionne (POST `/upload`)
- [ ] Déduplication marque les doublons
- [ ] Pipeline scraping + scoring complète un lead
- [ ] Résultats affichent 5 catégories
- [ ] STOP arrête le pipeline après le lead en cours
- [ ] Phase 2 (web search + rescore) fonctionne
- [ ] Exports CSV/PDF téléchargeables
