# Lead Qualification & Scoring Engine — Documentation technique complète

> Version ultra-détaillée du projet. Ce document décrit l'intégralité de l'architecture, du schéma de base de données, des routes, des algorithmes et des comportements de l'application, chaque fait étant référencé à la source (`fichier.py:ligne`).
>
> **Périmètre** : ingestion CSV → déduplication → scraping web (Firecrawl) → scoring IA (Groq) → revue humaine → exports. Interface web Flask.

---

## Table des matières

1. [Vue d'ensemble](#1-vue-densemble)
2. [Architecture & flux de données](#2-architecture--flux-de-données)
3. [Installation & configuration](#3-installation--configuration)
4. [Démarrage](#4-démarrage)
5. [Modèle de données PostgreSQL](#5-modèle-de-données-postgresql)
6. [Authentification & rôles](#6-authentification--rôles)
7. [Routes Flask (32 routes)](#7-routes-flask-32-routes)
8. [Ingestion CSV](#8-ingestion-csv)
9. [Déduplication](#9-déduplication)
10. [Scraping Firecrawl](#10-scraping-firecrawl)
11. [Escalade web (ScrapeGraphAI)](#11-escalade-web-scrapegraphai)
12. [Signaux techniques déterministes](#12-signaux-techniques-déterministes)
13. [Scoring Groq](#13-scoring-groq)
14. [Pipeline orchestrateur](#14-pipeline-orchestrateur)
15. [Progression temps réel (SSE)](#15-progression-temps-réel-sse)
16. [Exports](#16-exports)
17. [Segments, statuts & machine à états](#17-segments-statuts--machine-à-états)
18. [Housekeeping des séquences](#18-housekeeping-des-séquences)
19. [Templates & UI](#19-templates--ui)
20. [Variables d'environnement](#20-variables-denvironnement)
21. [Dépendances & fichiers](#21-dépendances--fichiers)
22. [Notes de cohérence & pièges connus](#22-notes-de-cohérence--pièges-connus)

---

## 1. Vue d'ensemble

Pipeline complet : **Ingestion → Déduplication → Scraping (Firecrawl) → AI Scoring (Groq)**, avec une interface web Flask et un stockage **PostgreSQL (Neon uniquement** — pas de fallback SQLite, `db.py:1-14`).

**Cible marketing** : les fondateurs non techniques qui construisent avec l'IA (vibe coding, Cursor, Bolt, Lovable, Replit) — segment `ai_solo_founder`, offre recommandée `ai_audit`. L'application qualifie chaque lead B2B en 6 segments et produit un verdict JSON structuré (segment, confiance, offre, citations, hooks de personnalisation, raison de disqualification).

**Stack** :

| Composant | Technologie |
|---|---|
| Backend web | Flask (Python 3.11) |
| Base de données | PostgreSQL sur Neon (`DATABASE_URL`), pool psycopg2 |
| Scraping | Firecrawl (multi-clés API, parallélisme 1 thread/clé) |
| Escalade web | ScrapeGraphAI (recherche + full-scrape LinkedIn) |
| Scoring IA | Groq — `llama-3.3-70b-versatile` (SDK OpenAI, `base_url="https://api.groq.com/openai/v1"`) |
| Frontend | Bootstrap 5.3.3 (thème sombre), SSE pour la progression en direct |

---

## 2. Architecture & flux de données

### 2.1 Chaîne de traitement (vue utilisateur)

```
1. Import CSV Apollo   (POST /upload)             → ingestion + dédup
2. Revue d'import      (GET /import/<session_id>) → choix critères, ré-inclusion doublons
3. Pipeline            (POST /import/<session_id>/start) → scraping + scoring (thread de fond)
4. Progression         (GET /progress/<session_id>) → SSE temps réel
5. Résultats           (GET /results/<session_id>) → 5 catégories, revue humaine
6. Exports             (CSV scraping / scores / search / complet CSV ou PDF)
```

### 2.2 Modules et responsabilités

| Fichier | Rôle |
|---|---|
| `app.py` | Interface Flask : routes, auth, threads de fond, progression SSE, catégorisation des résultats |
| `db.py` | Schéma PostgreSQL + helpers CRUD, pool de connexions, housekeeping des séquences |
| `constants.py` | Source unique de vérité : segments, statuts, seuil de confiance |
| `dedup.py` | 3 niveaux de déduplication + vérification contre l'historique d'export |
| `scraper.py` | Scraping Firecrawl, filtres anti-fausses pages, signaux techniques, GitHub API, escalade SGAI |
| `scorer.py` | Scoring Groq : prompt système structuré, verdict JSON, gardes et retries |
| `pipeline.py` | Orchestrateur : enchaîne scraping + scoring lead par lead, isole les erreurs |
| `export.py` | 4 formats CSV + résumé lisible, CLI, dédup inter-batch |

### 2.3 Connexions base de données

- **Pool** : `psycopg2.pool.ThreadedConnectionPool`, min `DB_POOL_MINCONN` (défaut 1), max `DB_POOL_MAXCONN` (défaut 8) — `db.py:39-40, 60-61`.
- **Keepalives TCP** : `keepalives=1, keepalives_idle=60, keepalives_interval=15, keepalives_count=4` — `db.py:233-236`.
- **Reconnexion** : 5 tentatives espacées `2*(attempt+1)` s — `db.py:227-241` ; une `execute` retente une fois après reconnexion sur connexion morte (10 motifs d'erreur détectés : « could not receive data from server », « software caused connection abort », « server closed the connection », « connection reset by peer », « ssl syscall error », « broken pipe », « connection has been closed », « terminated by server », « no connection to the server », « connection refused » — `db.py:43-59, 144-153`).
- **Secours pool saturé** : `PoolError` → connexion directe `psycopg2.connect(DATABASE_URL)` — `db.py:260-263`.
- **Par lead** : le pipeline ouvre une connexion dédiée par lead, fermée dans un `finally` — `pipeline.py:131, 270-274`.
- **Wrappers** : `_PgConnection` (execute/executemany/executescript — split sur `;`, `db.py:184-186`, commit/rollback), `_PgCursor`, `_PgRow` (accessible par clé ET par index) — `db.py:105-210`.
- Horodatage : `datetime.now(timezone.utc).isoformat(timespec="seconds")` — `db.py:267-268`.

---

## 3. Installation & configuration

```bash
pip install -r requirements.txt
```

Créer `.env` à la racine (voir [§20](#20-variables-denvironnement) pour la liste exhaustive) :

```env
DATABASE_URL=postgresql://user:password@host/dbname
FIRECRAWL_API_KEY=...
FIRECRAWL_API_KEY_2=...        # optionnel, jusqu'à _5
GROQ_API_KEY=...
SGAI_API_KEY=...               # optionnel, jusqu'à _5
```

> ⚠️ Le README actuel contient un bloc ```bash non fermé dans sa section Installation, qui casse le rendu Markdown en aval (à corriger).

---

## 4. Démarrage

```bash
python app.py
```

- `_init_schema_once()` appelé **une seule fois** au démarrage (`__main__`, `app.py:1325`) : crée le schéma, ajoute les colonnes manquantes, crée l'index users, applique le housekeeping des séquences.
- Serveur : `app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=True, use_reloader=False)` — `app.py:1324-1326`.
- Sans `DATABASE_URL` → RuntimeError au démarrage (`db.py:217-221`).
- Sans `FIRECRAWL_API_KEY` ou `GROQ_API_KEY` → message flash sur l'interface (`app.py:287-288`).

---

## 5. Modèle de données PostgreSQL

PK commune : `id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY` — `db.py:500`.

### 5.1 `analysis_sessions`

| Colonne | Type | Contraintes |
|---|---|---|
| id | BIGINT | IDENTITY PK |
| label | TEXT | |
| source_filename | TEXT | |
| status | TEXT | NOT NULL DEFAULT 'imported' |
| created_at | TEXT | NOT NULL |
| completed_at | TEXT | |
| notes | TEXT | |
| owner_id | INTEGER | (ajoutée a posteriori — `db.py:724`) |
| cancelled | INTEGER | NOT NULL DEFAULT 0 |
| scoring_criteria | TEXT | (JSON list) |
| scoring_criteria_custom | TEXT | |
| last_batch_ids | TEXT | (JSON list) |

### 5.2 `users`

| Colonne | Type | Contraintes |
|---|---|---|
| id | BIGINT | IDENTITY PK |
| email | TEXT | NOT NULL UNIQUE (index `idx_users_email` créé dans `init_db`) |
| password_hash | TEXT | NOT NULL |
| role | TEXT | NOT NULL DEFAULT 'user' |
| is_active | INTEGER | NOT NULL DEFAULT 1 |
| created_at | TEXT | NOT NULL |
| last_login_at | TEXT | |

### 5.3 `leads`

| Colonne | Type | Contraintes |
|---|---|---|
| id | BIGINT | IDENTITY PK |
| session_id | INTEGER | FK → analysis_sessions(id) |
| first_name / last_name / title / company_name / email / website_url | TEXT | |
| domain_normalized | TEXT | |
| email_domain | TEXT | |
| domain_mismatch | INTEGER | NOT NULL DEFAULT 0 |
| domain_mismatch_reason | TEXT | |
| status | TEXT | NOT NULL DEFAULT 'NEW' |
| is_duplicate | INTEGER | NOT NULL DEFAULT 0 |
| duplicate_of_id | INTEGER | FK → leads(id) |
| duplicate_reason | TEXT | |
| batch_id | TEXT | |
| created_at | TEXT | NOT NULL |
| review_status | TEXT | (a posteriori) |
| review_segment_override | TEXT | (a posteriori) |
| reviewed_at | TEXT | (a posteriori) |
| last_error | TEXT | (a posteriori) |
| scrape_seconds | REAL | (a posteriori) |
| score_seconds | REAL | (a posteriori) |

### 5.4 `lead_content`

id PK, `session_id` INTEGER FK, `lead_id` INTEGER **NOT NULL** FK → leads(id), `source` TEXT, `url` TEXT, `content` TEXT, `fetched_at` TEXT NOT NULL.

### 5.5 `lead_technical_signals`

id PK, `session_id` INTEGER FK, `lead_id` INTEGER NOT NULL FK, `generator_fingerprint` TEXT, `vibe_language_matches` TEXT, `trend_fonts_found` TEXT, `visual_patterns_triggered` TEXT, `generator_meta_tag` TEXT, `github_repo_url` TEXT, `github_check` TEXT, `ai_style_phrases_found` TEXT, `ai_style_phrase_density` TEXT, `ai_authorship_disclosures_found` TEXT, `computed_at` TEXT NOT NULL. (Listes JSON-sérialisées — `db.py:1045-1112`.)

### 5.6 `lead_scores`

id PK, `session_id` INTEGER FK, `lead_id` INTEGER NOT NULL FK, `segment` TEXT, `confidence` DOUBLE PRECISION, `company_stage` TEXT, `built_with_ai_signals` TEXT, `technical_signals` TEXT, `pain_signals` TEXT, `evidence_quotes` TEXT, `recommended_offer` TEXT, `personalization_hooks` TEXT, `disqualify_reason` TEXT, `needs_human_review` INTEGER, `scored_at` TEXT NOT NULL.

### 5.7 `lead_search_evidence`

id PK, `session_id` INTEGER FK, `lead_id` INTEGER NOT NULL FK, `source` TEXT NOT NULL, `query` TEXT, `results` TEXT, `fetched_at` TEXT NOT NULL.

### 5.8 `export_history`

id PK, `session_id` INTEGER FK, `lead_id` INTEGER NOT NULL FK, `domain_normalized` TEXT NOT NULL, `exported_at` TEXT NOT NULL.

### 5.9 Index

`idx_sessions_created_at`, `idx_leads_session`, `idx_leads_email`, `idx_leads_domain`, `idx_leads_status`, `idx_content_session`, `idx_scores_session`, `idx_technical_signals_lead`, `idx_export_history_domain` — `db.py:621-629` ; `idx_users_email` — `db.py:727-731`.

### 5.10 Ajouts idempotents

`init_db` ajoute les colonnes manquantes via `_add_column` (idempotent — ignore « duplicate column » / « already exists », `db.py:64-67, 767-779`) : `cancelled`, `scoring_criteria*`, `last_batch_ids`, `owner_id` (sessions) ; `review_*` (leads) ; `last_error`, `scrape_seconds`, `score_seconds` (leads) ; colonnes `ai_style_*` (signals) — `db.py:712-764`.

---

## 6. Authentification & rôles

### 6.1 Règles

- **Session signée** : `session["user_id"]` + `session["role"]` stockés au login — le rôle N'EST PAS relu en base à chaque requête (commentaire `app.py:202-206`).
- **`is_active` relu en base à CHAQUE requête** (`app.py:208-211, 224-232`) — un utilisateur bloqué est déconnecté immédiatement ; `session.clear()` + flash si bloqué.
- **`_require_login`** (`@app.before_request`, `app.py:216-233`) : exclut `PUBLIC_ENDPOINTS = {"login", "signup", "static"}` (`app.py:213`) ; redirection login avec `next=request.path`.
- **`admin_required`** (décorateur, `app.py:236-244`) : `session.get("role") != "admin"` → flash + redirect `history`.
- **`_assert_session_access`** (`app.py:247-253`) : admin → tout accès ; sinon `owner_id == user_id` ; sessions legacy (owner_id NULL) réservées à l'admin.
- **Premier compte** : `create_user` → `role = "admin"` si `count_users == 0`, sinon `"user"` — `db.py:442-456`.

### 6.2 Rôles

| Rôle | Droits |
|---|---|
| `user` | Ses propres sessions, dashboard, exports, revue de ses leads |
| `admin` | Tout + `/admin/users` (liste, changement de rôle, block/unblock, suppression, historique d'un utilisateur) |

Garde-fous admin : impossible de rétrograder/bloquer/supprimer le **dernier admin actif** (`count_active_admins <= 1`, `app.py:1266, 1282`) ; self-delete interdit (`app.py:1304`).

### 6.3 Numérotation par utilisateur

`list_analysis_sessions` calcule `user_rank = ROW_NUMBER() OVER (PARTITION BY s.owner_id ORDER BY s.id)` (`db.py:405-430`) : chaque utilisateur voit ses sessions numérotées 1, 2, 3… dans son propre historique ; l'admin voit la numérotation globale combinée. Affichage conditionné par `show_rank` (`history.html:88`, `admin_user_history`).


---

## 7. Routes Flask (32 routes)

### 7.1 Auth / filtres Jinja

- `app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "lead-qualification-engine")` — `app.py:37`.
- Filtres Jinja : `map_offer`, `map_segment`, `map_status_label`, `badge_class`, `format_datetime`, `confidence_class` (seuils alignés sur `CONFIDENCE_THRESHOLD` et `INVALID_VERDICT_CONFIDENCE_CAP`) — `app.py:41-95`.
- Progression in-memory : `_pipeline_progress: dict[int, dict]` + `_pipeline_lock` — `app.py:97-98`.

### 7.2 Tableau des routes

| # | Méthode | Chemin | Fonction | Rôle | Paramètres | Template |
|---|---|---|---|---|---|---|
| 1 | GET | `/` | `home` (app.py:409-421) | Accueil : stats + sessions récentes | — | home.html |
| 2 | GET | `/history` | `history` (app.py:424-432) | Historique des sessions | — | history.html |
| 3 | GET | `/dashboard` | `dashboard` (app.py:435-504) | Tableau de bord : tables leads/scores, filtres, détail lead | query : lead_id, session_id, segment (getlist), needs_review=1, hide_duplicates (défaut "1") | dashboard.html |
| 4 | POST | `/upload` | `upload_and_review` (app.py:507-540) | Étape 1 : import CSV → ingest + dedup → redirect review | form : csv_file, fuzzy_threshold (défaut 90) | redirect → import_review |
| 5 | GET | `/import/<session_id>` | `import_review` (app.py:543-573) | Étape 2 : revue des leads + choix critères de scoring | — (criteria_options codées en dur app.py:556-563) | import_review.html |
| 6 | POST | `/import/<session_id>/start` | `start_pipeline_from_review` (app.py:576-631) | Étape 3 : sauvegarde critères, ré-inclusion des doublons cochés (SQL reset is_duplicate, app.py:597-601), marque SKIPPED les non sélectionnés (app.py:606-615), pipeline en thread | form : criteria (getlist), custom_criteria, throttle_seconds (défaut 12), concurrency (défaut `PIPELINE_CONCURRENCY`), lead_ids, dup_ids | redirect → progress_view |
| 7 | POST | `/analyze-pending/<session_id>` | `analyze_pending` (app.py:634-670) | Relance des leads pending/SKIPPED | form : lead_ids, throttle_seconds (12), concurrency | redirect → progress_view |
| 8 | POST | `/session/<session_id>/delete` | `delete_session` (app.py:673-688) | Suppression session + données (cascade explicite) | query : next (redirection si chemin interne) | redirect history |
| 9 | GET | `/results/<session_id>` | `results_view` (app.py:744-795) | Étape 4 : résultats en 5 catégories | — | results.html |
| 10 | POST | `/rescore/<session_id>` | `rescore_leads` (app.py:798-845) | Re-scoring sans re-scrape ; fallback = tous les leads LOW_CONFIDENCE ou needs_human_review sans disqualify api_error/no_content_scraped (app.py:811-823) ; DELETE des anciens lead_scores (app.py:832) | form : lead_ids | redirect → progress_view |
| 11 | POST | `/start-analysis` | `start_analysis` (app.py:849-890) | Analyse complète en un clic : ingest + dedup + pipeline thread | form : csv_file, fuzzy_threshold (90), throttle_seconds (12), concurrency | redirect → progress_view |
| 12 | POST | `/ingest` | `ingest_only` (app.py:893-915) | Import seul | form : csv_file | redirect → dashboard |
| 13 | POST | `/dedup` | `dedup_only` (app.py:918-932) | Dédup seule | form : fuzzy_threshold (90) ; query : session_id | redirect → dashboard |
| 14 | POST | `/pipeline` | `pipeline_only` (app.py:935-957) | Pipeline seul (scrape+score) | form : throttle_seconds (12), concurrency ; query : session_id | redirect → progress_view |
| 15 | POST | `/lead/<lead_id>/review` | `review_lead` (app.py:960-986) | Décision humaine APPROVED/REJECTED + override segment | form : decision, segment ; query : session_id (déduit si absent) | redirect → dashboard |
| 16 | GET | `/download/scraping.csv` | `download_scraping_csv` (app.py:989-997) | CSV scraping | query : session_id | fichier `scraping_results_<ts>.csv` |
| 17 | GET | `/download/scores.csv` | `download_scores_csv` (app.py:1000-1014) | CSV scores + dédup inter-batch (`run_export_dedup`) + `record_export` | query : session_id | fichier `scores_results_<ts>.csv` |
| 18 | GET | `/download/search.csv` | `download_search_csv` (app.py:1017-1026) | CSV web search SGAI | query : session_id | fichier `search_results_<ts>.csv` |
| 19 | GET | `/export/<session_id>/<format>` | `export_results` (app.py:1029-1075) | Export complet CSV ou PDF (HTML imprimable) | format ∈ {csv, pdf} | csv → `complete_results_<ts>.csv` ; pdf → results_print.html |
| 20 | GET | `/batch-results/<session_id>` | `batch_results_view` (app.py:1078-1104) | Résultats du dernier batch (`last_batch_ids`) | — | batch_results.html |
| 21 | GET | `/web-search/<session_id>` | `web_search_view` (app.py:1107-1122) | Page dédiée evidence web search | — | web_search.html |
| 22 | GET | `/sessions/<session_id>` | `session_redirect` (app.py:1125-1130) | Redirect vers results | — | — |
| 23 | GET | `/progress/<session_id>` | `progress_view` (app.py:1133-1139) | Page de progression temps réel | — | progress.html |
| 24 | GET | `/progress/<session_id>/stream` | `progress_stream` (app.py:1142-1174) | SSE : `data: {...}\n\n`, cycle 'waiting' toutes les 1 s, 'running' toutes les 0,5 s, break sur completed/failed/cancelled ; headers no-cache + X-Accel-Buffering no | — | text/event-stream |
| 25 | GET/POST | `/signup` | `signup` (app.py:1181-1204) | Inscription ; validations email + password ≥ 6 | form : email, password | signup.html |
| 26 | GET/POST | `/login` | `login` (app.py:1207-1232) | Connexion ; `check_password_hash` ; `update_last_login` ; `is_active` vérifié | form : email, password ; query : next | login.html |
| 27 | POST | `/logout` | `logout` (app.py:1235-1239) | `session.clear()` | — | redirect login |
| 28 | GET | `/admin/users` | `admin_users` (app.py:1246-1251) — @admin_required | Liste des utilisateurs | — | admin_users.html |
| 29 | POST | `/admin/users/<user_id>/role` | `admin_user_role` (app.py:1254-1271) — @admin_required | Changement de rôle ; garde-fou dernier admin actif | form : role ∈ {admin, user} | redirect admin_users |
| 30 | POST | `/admin/users/<user_id>/toggle-active` | `admin_user_toggle_active` (app.py:1274-1290) — @admin_required | Block/unblock ; garde-fou dernier admin | — | redirect admin_users |
| 31 | POST | `/admin/users/<user_id>/delete` | `admin_user_delete` (app.py:1293-1309) — @admin_required | Suppression ; self-delete interdit | — | redirect admin_users |
| 32 | GET | `/admin/users/<user_id>/history` | `admin_user_history` (app.py:1312-1321) — @admin_required | Historique des sessions d'un utilisateur | — | admin_user_history.html |

### 7.3 Threads de fond

- `_background_pipeline` (app.py:146-174) et `_background_rescore_pipeline` (app.py:116-143) : stockent la progression in-memory (`_pipeline_progress[session_id]`) et mettent à jour le statut session (completed/failed).
- Helpers : `_run_ingest` (tempfile suffix .csv, `batch_id = f"batch_{uuid.uuid4().hex[:8]}"` — app.py:392-406), `_summary_context`, `_session_summary`, `_load_dashboard_data`, `_csv_response` (app.py:298-389).

---

## 8. Ingestion CSV

- `insert_leads_from_csv(conn, csv_path, batch_id, session_id=None)` — `db.py:801-868` : lecture `utf-8-sig` via `csv.DictReader` ; saute les lignes sans website (`skipped_no_website`) ; retourne `{"inserted", "skipped_no_website"}`.
- **Colonnes attendues** : first_name, last_name, title, company_name, email, website_url — avec alias tolérés (majuscules/espaces) :

| Champ | Alias acceptés |
|---|---|
| first_name | first_name, first name, firstname |
| last_name | last_name, last name, lastname |
| title | title, job title, person title |
| company_name | company_name, company, company name, organization |
| email | email, email address, work email |
| website_url | website_url, website, company website, website url |

(`COLUMN_ALIASES`, `db.py:70-77` ; sélection de la 1ère colonne d'alias non vide — `_pick_column`, `db.py:793-798`.)

- **`domain_mismatch`** : 1 si l'email n'appartient pas à un fournisseur gratuit (`FREE_EMAIL_PROVIDERS` : gmail, yahoo, outlook, hotmail, icloud, proton.me, protonmail, aol, gmx, live, yandex, mail, zoho — `db.py:79-83`) ET que le domaine email ≠ domaine du site ; raison stockée dans `domain_mismatch_reason` (ex. email personnel sur domaine d'entreprise). Un lead `domain_mismatch=1` reçoit **forcément** `needs_human_review=True` au scoring (`pipeline.py:248-256`).

---

## 9. Déduplication

Principe (`dedup.py:1-12`) : **ne supprime jamais**, ne fait que poser le flag `is_duplicate` ; un lead déjà marqué doublon n'est jamais comparé (pas de chaînes de doublons) ; l'ordre d'insertion prime — le premier vu reste « l'original » (`dedup.py:21-24`).

`run_dedup(conn, fuzzy_threshold=90, session_id=None)` — `dedup.py:18-80` :

| Niveau | Méthode | Condition | duplicate_reason |
|---|---|---|---|
| 1 | Email exact | `(email).strip().lower()` déjà vu | `exact_email` |
| 2 | Domaine normalisé | `domain_normalized` (strip/lower) déjà vu | `domain_match` |
| 3 | Nom d'entreprise flou | `fuzz.token_sort_ratio(company, other_company) >= fuzzy_threshold` (défaut 90) | `fuzzy_company_name` |

Écriture batch : `executemany` + commit (`dedup.py:73-78`) ; stats retournées : `{"exact_email", "domain", "fuzzy_company", "kept_original"}`.

**Dédup inter-batch** : `check_against_export_history(conn, exported_domains)` marque `duplicate_reason='already_exported_previous_batch'` (is_duplicate=1, duplicate_of_id=NULL) pour tout lead dont le domaine figure dans `export_history` (`dedup.py:83-101`) ; `run_export_dedup` (dedup.py:104-110) est appelé automatiquement au téléchargement du CSV scores (`app.py:1000-1014`). Badge « already exported » dans la catégorisation (`app.py:707-717`).


---

## 10. Scraping Firecrawl

### 10.1 Constantes & listes de motifs

- `KEYWORDS` (scraper.py:27-32) : about → [about, team] ; pricing → [pricing, plans, price] ; careers → [careers, jobs] ; product → [product, services, solutions, features].
- `COMMON_PATH_CANDIDATES` (scraper.py:39-44) : chemins standard (/about, /pricing…) essayés en fallback.
- `MAX_CONTENT_CHARS_PER_PAGE = 32000` (scraper.py:46).
- `BROKEN_PAGE_MARKERS` (scraper.py:56-65) : 8 marqueurs littéraux (« client-side exception has occurred », « application error », « hydration failed », « unhandled runtime error », « this page could not be found », « 404 not found », « 404: this page could not be found », « 500 internal server error »).
- `BROKEN_PAGE_PATTERNS` (scraper.py:70-76) : 5 regex (404 Markdown, « 404 … page not found », ordre inverse, « oops! … vanished », « page you're looking for … doesn't exist/not found/vanished »).
- `MIN_VALID_CONTENT_CHARS = 50` (scraper.py:78).
- `GENERATOR_FINGERPRINTS` (scraper.py:86-93) : lovable (lovable.dev, lovable-tagger, gpteng.co), v0 (v0.dev, vusercontent.net), bolt (bolt.new, stackblitz), replit (replit.com, replit.dev), cursor (built with cursor, cursor.sh).
- `TREND_FONTS` (scraper.py:95-97) : Space Grotesk, Instrument Serif, Geist, Syne, Fraunces.
- `VISUAL_PATTERNS` (scraper.py:99-109) : 9 patterns — purple_accent, gradient, glassmorphism, colored_glow, numbered_steps, stat_banner, headline_badge, faq_accordion, shadcn_ui.
- `VIBE_LANGUAGE_MARKERS` (scraper.py:111-114) : « built with cursor », « built with v0 », « made with lovable », « built with bolt », « vibe coded », « vibe-coded », « no-code ».
- `AI_STYLE_PHRASES` (scraper.py:122-135) : 34 phrases (« seamless integration », « unlock the power of », « game-changer », …).
- `AI_AUTHORSHIP_DISCLOSURES` (scraper.py:139-142) : « written with ai », « generated with ai », « powered by gpt », « powered by chatgpt », « ai-generated content », « content generated by ai », « drafted by ai ».
- Signaux careers/pricing : `ENGINEERING_ROLE_KEYWORDS`, `OTHER_ROLE_KEYWORDS`, `SELF_SERVE_CTA_MARKERS`, `SALES_LED_CTA_MARKERS`, `VISIBLE_PRICE_PATTERN` (scraper.py:153-176).

### 10.2 Pool de clés Firecrawl

- `_get_client_pool()` (scraper.py:251-269) : une instance `Firecrawl(api_key=key, timeout=120)` par clé parmi `FIRECRAWL_API_KEY`, `_2`, `_3`, `_4`, `_5` (scraper.py:262) ; RuntimeError si aucune clé.
- `_is_quota_error` : motifs « insufficient credits », « 402 », « 429 quota », « billing » (scraper.py:272-279) ; `_parse_retry_after` : regex `retry after\s+([\d.]+)\s*s` +1,0 s de marge (scraper.py:282-291).
- `_firecrawl_scrape` (scraper.py:294-341) : round-robin sur les clés vivantes, `max_rounds = 2` ; clé en quota → marquée morte (`_client_pool_dead`) ; clé rate-limitée → clé suivante ; si toutes rate-limitées → attente du retry-after (15 s par défaut) puis nouvelle passe ; erreur non-quota/non-ratelimit → raise immédiat.
- **Parallélisme** : `_scrape_pages_in_parallel` (scraper.py:344-387) — `n_workers = max(1, min(len(clients), len(urls_by_category)))` : **1 thread max par clé** (pas de throttling interne quand plusieurs clés) ; mode séquentiel throttle si 1 seule clé.
- Appels Firecrawl : `client.scrape(url, formats=["markdown", "links"], only_main_content=True, timeout=10000)` (scraper.py:361, 371) ; homepage : `formats=["markdown", "rawHtml", "links"], timeout=10000` (scraper.py:505).

### 10.3 Filtres anti-fausses pages

| Fonction | Rôle |
|---|---|
| `_normalize_domain` (scraper.py:390-401) | netloc sans www, minuscules |
| `_is_same_domain` (scraper.py:404-413) | empêche qu'un lien externe (g2.com, blog, LinkedIn) contenant un mot-clé soit choisi comme page clé |
| `_is_real_subpage` (scraper.py:416-436) | rejette les ancres `#services` et tout lien pointant sur la même page que la homepage (sites one-page) ; les SPAs à contenu identique sont filtrées a posteriori par hash |
| `_url_exists` (scraper.py:439-458) | HEAD d'abord ; si 405/501 (Vercel/Netlify) → GET stream ; statut < 400 = existe — évite de dépenser un crédit Firecrawl sur un 404 |
| `_looks_broken` (scraper.py:461-480) | texte < 50 chars → True ; sinon marqueurs littéraux puis regex (insensibles à la casse) |
| `_content_fingerprint` (scraper.py:483-499) | sha256 du texte normalisé (images/URLs/base64 retirées) — détecte les pages SPA identiques |

### 10.4 `_find_key_pages(homepage_url)` (scraper.py:502-560)

1. Scrape homepage (`formats=["markdown", "rawHtml", "links"]`).
2. Filtre `all_links` par `_is_real_subpage` puis restriction même domaine.
3. Pour chaque catégorie de `KEYWORDS` : premier lien même-domaine contenant un mot-clé.
4. Fallback : chemins standards (`COMMON_PATH_CANDIDATES`) vérifiés par `_url_exists`.
5. Catch-all « product » : premier lien non assigné.
6. Retourne `(found_pages, result, all_links)` — **`all_links` NON filtré par domaine** (nécessaire pour le lien GitHub externe).

### 10.5 `scrape_website(homepage_url, throttle_seconds=1.0)` (scraper.py:688-824) — flow pas à pas

1. `_find_key_pages` ; toute exception → **FETCH_FAILED**, rows=[], error (`scraper.py:703-713`).
2. `homepage_markdown = homepage_result.markdown or ""` (scraper.py:715).
3. `_looks_broken(homepage_markdown)` → **FETCH_FAILED**, error `"homepage_render_error_or_empty_content"` (scraper.py:717-725).
4. `rows = [("homepage", homepage_url, homepage_markdown[:32000])]` ; `seen_fingerprints` initialisé avec le hash homepage (scraper.py:727-728).
5. Scrape parallèle des autres pages (`_scrape_pages_in_parallel`, scraper.py:736).
6. **Correction 1 — liens GitHub hors homepage** (scraper.py:738-789) : les liens des pages déjà scrapées (`r.links`) sont filtrés par `_is_real_subpage` et fusionnés (dédupliqués) dans `all_links` — un même repo GitHub en footer de plusieurs pages n'est compté qu'une fois.
7. Pour chaque page clé : échec → `failures += 1` ; `_looks_broken` → `failures += 1` ; fingerprint déjà vu → `duplicates += 1` (SPA shell) ; sinon : **careers** remplacé par le signal `extract_careers_signal` formaté, **pricing** par `extract_pricing_signal` formaté (`_format_signal_as_text`, scraper.py:237-243), autres pages texte brut (scraper.py:772-779).
8. `extract_technical_signals(raw_html, all_links, homepage_text)` (scraper.py:791-795).
9. `github_check = check_github_repo_pattern(...)` si `github_repo_url` trouvé (scraper.py:797-799).
10. `unusable = failures + duplicates` (scraper.py:804).
11. **Logique de statut** (scraper.py:806-816) :
    - `unusable > 0` → **FETCH_PARTIAL** (scraper.py:813-814) — même si TOUTES les sous-pages sont inutilisables, dès que la homepage existe ce n'est PAS FETCH_FAILED.
    - sinon → **PARSED** (scraper.py:815-816).
    - **FETCH_FAILED est réservé au site réellement mort** (homepage injoignable ou cassée → rows == [], géré en amont) (scraper.py:806-812).
12. Retour : `{"status", "rows", "technical_signals", "github_check", "error"}` (scraper.py:818-824).

> Sémantique (fix `cecc9b3`) : un humain regardant le dashboard doit pouvoir distinguer « site totalement mort » (FETCH_FAILED, aucune page) de « site up, sous-pages pauvres » (FETCH_PARTIAL, homepage récupérée).

### 10.6 `check_github_repo_pattern(repo_url)` (scraper.py:648-685)

- GitHub API publique non authentifiée : `GET https://api.github.com/repos/{owner}/{repo}/commits?per_page=100` timeout=10 (scraper.py:666-670).
- Retour : `{"repo_url", "checked", "evidence": {"total_commits_seen", "first_commit_message", "single_commit_repo"}, "error"}` — `single_commit_repo = len(commits) <= 1` (scraper.py:658, 677-681).

---

## 11. Escalade web (ScrapeGraphAI)

- `_SGAI_BASE_URL = "https://v2-api.scrapegraphai.com/api"` (scraper.py:835).
- `SEARCH_QUERY_TEMPLATES` (scraper.py:841-851) :

| Source | Requête |
|---|---|
| linkedin | `"{company}" site:linkedin.com/in OR site:linkedin.com/company` |
| product_hunt | (template dédié) |
| twitter | `"{company}" ... (vibe coded OR built with AI OR built in a weekend)` |
| github | (template dédié) |
| interviews | `"{founder}" OR "{company}" interview (vibe coding OR built with AI OR built with Cursor OR built with v0)` |
| person_linkedin | `"{founder}" site:linkedin.com/in` |
| person_github | `"{founder}" site:github.com` |

- `_get_sgai_keys()` (scraper.py:854-865) : `SGAI_API_KEY` à `_5` ; RuntimeError si aucune (scraper.py:880).
- `_sgai_request` (scraper.py:868-914) : POST `{base}/{path}` header `SGAI-APIKEY` ; quota → clé marquée morte ; toutes épuisées → raise.
- `_sgai_search_one(source, query, limit_per_query)` (scraper.py:917-935) : POST /search `{"query": query, "numResults": limit_per_query}` timeout=35 ; extrait url/title/content ; `{"error": ...}` en cas d'exception.
- `_sgai_linkedin_full_scrape(results, prefer_profile=False)` (scraper.py:938-990) : full-scrape de la meilleure page LinkedIn (`/company/` par défaut, `/in/` si `prefer_profile`) via POST /scrape `{"url":..., "formats": [{"type":"markdown"},{"type":"json","prompt":...}]}` timeout=45 ; best-effort (l'échec garde les snippets).
- `search_additional_evidence(company_name, founder_name=None, limit_per_query=3, throttle_seconds=1.0)` (scraper.py:993-1055) : sans clés → `{"_error": "SGAI_API_KEY not configured in .env"}` ; skip les templates `{founder}` sans nom de fondateur ; 1 thread par clé ; full-scrape LinkedIn pour « linkedin » et « person_linkedin ».

---

## 12. Signaux techniques déterministes

`extract_technical_signals(raw_html, all_links, homepage_text="")` (scraper.py:567-645) :

| Signal | Extraction |
|---|---|
| `generator_fingerprint` | null si aucun — testé sur raw_html (scraper.py:603-606) |
| `generator_meta_tag` | regex `<meta ... name="generator" content="...">` (scraper.py:594-600) |
| `vibe_language_matches` | marqueurs présents dans raw_html.lower() (scraper.py:609-612) |
| `trend_fonts_found` | noms de polices présents (scraper.py:614-615) |
| `visual_patterns_triggered` | noms des 9 patterns matchés (scraper.py:618-620) |
| `ai_style_phrases_found` + `ai_style_phrase_density` | comptage sur le texte visible homepage ; ≥4 → "high", ≥2 → "medium", ==1 → "low", sinon "none" (scraper.py:622-631) |
| `ai_authorship_disclosures_found` | (scraper.py:633-635) |
| `github_repo_url` | premier lien contenant "github.com" hors /issues et /pull — **volontairement non filtré par domaine** (scraper.py:637-643) — collecté sur homepage ET sous-pages (Correction 1) |
| `hiring_technical` | booléen du signal careers déterministe (`extract_careers_signal`, scraper.py:795-801), ajouté par `scrape_website` aux `technical_signals` — **lu par pipeline.py comme condition du déclencheur d'escalade web** (pipeline.py:255), à ne pas casser |

Ces signaux sont persistés dans `lead_technical_signals` et passés au scorer (hiérarchie de fiabilité en [§13](#13-scoring-groq)).


---

## 13. Scoring Groq

### 13.1 Constantes

- `MODEL = "llama-3.3-70b-versatile"` (scorer.py:44) ; `MAX_SITE_CONTENT_CHARS = 12000` ; `MAX_WEB_EVIDENCE_CHARS = 12000` (budgets égaux site/web) ; `MAX_OUTPUT_TOKENS = 2048` ; retry : `RETRY_MAX_CONTENT_CHARS = 6000`, `RETRY_MAX_OUTPUT_TOKENS = 1024` (scorer.py:45-51).
- `INVALID_VERDICT_CONFIDENCE_CAP = 0.3` (scorer.py:55) — cap de confiance quand le verdict est force-corrigé.
- `VALID_OFFERS = {"ai_audit", "general_audit", "pipeline", "none"}` (scorer.py:303) ; `VALID_STAGES = {"pre-launch", "early", "scaling", "established"}` (scorer.py:304).

### 13.2 Structure du SYSTEM_PROMPT (scorer.py:57-183)

1. **Rôle** : « senior analyst who evaluates B2B leads for a technical development agency (RuyaTech) ».
2. **THE TWO OFFERS WE SELL** : Technical audit (ai_audit / general_audit) + AI lead-gen pipeline (pipeline).
3. **OUR PRIMARY TARGET** : fondateurs non techniques utilisant l'IA (vibe coding, Cursor, Bolt, Lovable, Replit).
4. **SEGMENTS** : 6 segments + offre recommandée ; `unclear` → `needs_human_review` nécessairement true, offre none sauf signal partiel ; avertissement de ne pas confondre unclear/wrong_field/too_big.
5. **RELIABILITY HIERARCHY OF DETERMINISTIC SIGNALS** (scorer.py:94-125) :
   - **STRONG** (quasi-preuve) : generator_fingerprint non-null, ai_authorship_disclosures_found non-empty, github_check.single_commit_repo=true + generator_fingerprint.
   - **MEDIUM** : vibe_language_matches non-empty, ai_style_phrase_density "high".
   - **WEAK** (jamais suffisant seul) : visual_patterns_triggered.
   - Fingerprint isolé non corroboré → baisser la confiance.
   - **Site vs Web search : POIDS ÉGAL**.
   - Sources `person_*` = signal PRIORITAIRE (profil du fondateur lui-même).
6. **CURSOR — SPECIAL RULE** (scorer.py:127-137) : une simple mention de Cursor n'est JAMAIS suffisante seule pour `ai_solo_founder` ; doit être corroborée par `github_check.single_commit_repo = true` OU un profil person_linkedin/person_github sans background technique ; sinon → unclear ou technical_founder.
7. **RULES** (scorer.py:139-168) : (1) chaque signal cité doit avoir une citation exacte dans evidence_quotes ; (2) hooks situationnels, jamais biographiques ; (3) confiance < 0.7 → needs_human_review:true ; (4) spectre complet 0.0-1.0 ; (5) utiliser UNIQUEMENT le texte fourni ; (6) les exemples fictifs/démo ne sont pas des faits ; (7) distinguer « produit avec features AI » vs « équipe a construit avec des outils AI » ; (8) le titre du contact est un signal direct (CTO/Lead Engineer → technical_founder ; Founder/CEO seul → ai_solo_founder si corroboré) ; (9) questions dans l'ordre a→f.
8. **Schéma JSON du verdict** (scorer.py:170-183) :

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

### 13.3 Appels Groq

- `_get_client()` (scorer.py:188-192) : SDK OpenAI pointé sur `https://api.groq.com/openai/v1`.
- `_call_llm(user_content, max_output_tokens=2048)` (scorer.py:378-391) : `model=MODEL, temperature=0.2, max_tokens, response_format={"type": "json_object"}`.
- Détection : `_is_rate_limit_error` (413/429 ou "rate_limit_exceeded"/"rate limit") ; `_is_json_parse_error` (400 ou JSONDecodeError/KeyError/TypeError/ValueError) — scorer.py:361-375.

### 13.4 Gardes post-LLM (dans l'ordre)

| Garde | Comportement |
|---|---|
| `_apply_confidence_guard` (scorer.py:394-398) | `confidence < 0.7` → `needs_human_review=True` |
| `_validate_verdict` (scorer.py:307-341) | segment hors liste → forcé "unclear" + note `invalid_segment_fixed_to_unclear` ; offre hors liste → "none" ; stage hors liste → None ; si correction → `confidence = min(conf, 0.3)` + needs_human_review |
| `_verify_evidence_grounding` (scorer.py:406-428) | chaque evidence_quote doit apparaître mot pour mot dans le texte source (normalisation espaces/minuscules) ; les non-grounded sont retirées + note `ungrounded_evidence_quotes_removed: N…` + needs_human_review |
| `_apply_site_missing_guard` (scorer.py:447-471) | si `site_content_missing` → needs_human_review=True **inconditionnel** + note `site_content_missing: no official site content available…` ajoutée si absente |

- `SITE_MISSING_INSTRUCTION` (scorer.py:474-484) : instruction ajoutée au prompt quand le site officiel est indisponible (ne pas inventer de contenu, ne pas traiter l'absence comme un signal, needs_human_review=true si verdict basé seulement sur le web).
- `_retry_after_failure` (scorer.py:431-444) : retry contenu raccourci (6000 chars) / 1024 tokens ; échec → `_empty_verdict(f"json_parse_failed: … | retry_error: …")`.
- `_empty_verdict` (scorer.py:344-358) : segment="unclear", confidence=0.0, offer="none", needs_human_review=True.

### 13.5 `score_content(...)` (scorer.py:487-600)

Signature : `rows, deterministic_signals=None, lead_metadata=None, web_search_evidence=None, scoring_criteria=None, scoring_criteria_custom="", site_content_missing=False`.

- `rows_to_text(rows, max_chars=12000)` — accepte tuples ET dicts (bug rescore corrigé, scorer.py:286-289) ; `_format_web_search_evidence` — sources `person_*` ordonnées en premier (scorer.py:254-257) ; `_format_lead_metadata` (Name/Title/Company/Email/Website) ; `_strip_images` retire images/media/markdown images (scorer.py:195-205).
- Pas de contenu site NI web → `_empty_verdict("no_content_scraped")` (scorer.py:528-529).
- `build_user_content` (scorer.py:531-574) : metadata → site → web evidence → SITE_MISSING_INSTRUCTION (si flag) → critères utilisateur (dict par clé : ai_solo_founder, technical_founder, solo_or_small, agency_or_studio, no_ai, wrong_field) → deterministic_signals JSON indenté + rappel de hiérarchie.
- Chaîne : `_call_llm` → confidence_guard → validate → grounding → site_missing_guard (scorer.py:576-599) ; JSONDecodeError/parse → retry ; rate-limit → retry raccourci, échec → `_empty_verdict(f"api_error_after_retry: …")` ; autres exceptions → re-raise.

---

## 14. Pipeline orchestrateur

### 14.1 Constantes

- `DEFAULT_THROTTLE_SECONDS = 15` — « Firecrawl free tier ~10 req/min » (pipeline.py:19).
- `DEFAULT_CONCURRENCY = int(os.getenv("PIPELINE_CONCURRENCY", "3"))` (pipeline.py:20).

### 14.2 `_process_lead(...)` (pipeline.py:130-292) — un lead, étape par étape

1. **Connexion DB dédiée** par lead (pipeline.py:131) ; retourne des événements de progression (`_base`, pipeline.py:136-145).
2. **Scraping** : `scraper.scrape_website(website, throttle_seconds=1.0)` (pipeline.py:153) ; exception → `FETCH_FAILED` + scrape_seconds (pipeline.py:154-159) ; sinon `update_lead_progress` avec `scrape_result["status"]` (pipeline.py:162), `save_lead_content` si rows (pipeline.py:163-164), `save_lead_technical_signals` (pipeline.py:169-175).
3. **Pass 1 (site uniquement)** : `deterministic_signals` = technical_signals + github_check (pipeline.py:182-185) ; **`site_content_missing = not any((content or "").strip() for _, _, content in scrape_result["rows"])`** — basé sur le contenu RÉEL des rows, pas sur le statut (fix `cecc9b3`, pipeline.py:189-199) ; `_score(web_evidence={})` (pipeline.py:201-210) ; exception pass 1 → SCORE_FAILED (pipeline.py:214-219).
4. **Escalade web conditionnelle (FR-3, rule additive)** : si `needs_human_review` OU `confidence < 0.7` OU (`segment == "small_agency_scaling"` ET `technical_signals.hiring_technical == True` ET `confidence >= 0.7`) → `_fetch_web_search_evidence` (appel `search_additional_evidence(company_name, founder_name, limit_per_query=2)`, persistance via `save_search_evidence`) puis second `_score(web_evidence)` (pipeline.py:239-272) ; échec pass 2 → on garde le verdict pass 1 avec note `web_escalation_second_pass_failed: …` (pipeline.py:263-271). Le 3e bloc (agence confiante à forte valeur) s'AJOUTE au filet existant, il ne le remplace pas ; `hiring_technical` vient du signal déterministe careers (scraper.py:797-801), jamais du verdict LLM.
5. **Sauvegarde** : garde `domain_mismatch` (→ needs_human_review forcé + note, pipeline.py:248-256) ; `save_lead_score` ; `new_status = "LOW_CONFIDENCE" if needs_human_review else "SCORED"` (pipeline.py:258-259) ; `update_lead_progress(status, error=scrape_err)` — un statut écrit toujours last_error (None l'efface, db.py:968-999) ; exception → SCORE_FAILED (pipeline.py:262-266).
6. **Événements émis** : `scraping`, `scraping_done`, `web_search`, `done` (pipeline.py:166, 195, 260, 300).
7. **Isolation des erreurs** : try/except fatal global + `finally: conn.close()` (pipeline.py:270-274) — un lead qui plante ne fait pas échouer les autres.

### 14.3 `run_pipeline(...)` (pipeline.py:277-321)

- Charge critères + `get_leads_to_process` (filtre `is_duplicate = 0` + `status IN (NOT_YET_SCORED_STATUSES)` — db.py:906-919) ; `total == 0` → return.
- **Séquentiel** si `concurrency <= 1 or total == 1` : `_sleep_check(throttle_seconds)` entre chaque lead (pipeline.py:306-311).
- **Parallèle** sinon : `ThreadPoolExecutor(max_workers=min(concurrency, total))`, chaque futur = `_process_lead` avec sa propre connexion ; yield dans l'ordre de complétion (pipeline.py:313-321).

### 14.4 `run_rescore_pipeline(...)` (pipeline.py:324-416)

- **Pas de re-scrape ni web search** (pipeline.py:325-327) ; charge `get_leads_by_status(lead_status="RESCORE_PENDING")`.
- Pas de contenu scrapé → `RESCORE_FAILED` + error `"no_scraped_content"` (pipeline.py:354-359).
- Recharge les signals déterministes, recalcule `site_content_missing`, **recharge l'evidence web persistée** (bug rescore corrigé : les preuves web disparaissaient — `_load_persisted_web_evidence`, pipeline.py:94-109, 361-377).
- Mêmes gardes (domain_mismatch, statut LOW_CONFIDENCE/SCORED) ; exception → SCORE_FAILED (pipeline.py:406-412) ; `_sleep_check(throttle_seconds)` entre leads.

---

## 15. Progression temps réel (SSE)

- Endpoints (progress.html:164-166) : `PROGRESS_URL = url_for('progress_stream', ...)` (EventSource) ; `RESULTS_URL` → redirection auto 5 s après complétion (progress.html:342-344).
- Flux SSE (`progress_stream`, app.py:1142-1174) : `data: {...}\n\n` ; cycle 'waiting' toutes les 1 s, 'running' toutes les 0,5 s ; break sur completed/failed/cancelled ; headers `Cache-Control: no-cache`, `Connection: keep-alive`, `X-Accel-Buffering: no`.
- JS (progress.html) : `connectSSE()` ; `translateStep` (scraping / scraping_done / scoring / done / waiting) ; `translateStatus` (FETCH_FAILED, FETCH_PARTIAL, PARSED, SCORE_FAILED, LOW_CONFIDENCE, SCORED) ; segments Import/Scraping/Scoring ; erreur SSE → reconnexion après 3 s ; timer local 1 s.
- Progression stockée in-memory côté app (`_pipeline_progress[session_id]` : index/total, lead courant, step, started_at, completed_ts, errors).

---

## 16. Exports

`export.py` — 3 formats historiques + web search ; « reporting functions, no lifecycle operations » (export.py:1-15). `_flatten` : None → "" ; JSON string → parse+join ; list → join " | " ; dict → JSON compact (export.py:25-46).

### 16.1 Scraping CSV — `SCRAPING_FIELDS`, 20 colonnes (export.py:53-61)

`lead_id, company_name, website_url, status, error, source, url, content_chars, content, generator_fingerprint, generator_meta_tag, trend_fonts_found, visual_patterns_triggered, vibe_language_matches, github_repo_url, github_check, ai_style_phrases_found, ai_style_phrase_density, ai_authorship_disclosures_found`

- `_iter_scraping_rows` (export.py:64-106) : une ligne par page ; leads sans contenu → une ligne vide (source/url vides, content_chars 0) pour **ne perdre aucun lead** ; encodage `utf-8-sig`.

### 16.2 Scoring CSV — `SCORE_FIELDS`, 23 colonnes (export.py:144-151)

`lead_id, first_name, last_name, title, company_name, email, website_url, status, error, is_duplicate, duplicate_reason, segment, confidence, needs_human_review, company_stage, recommended_offer, disqualify_reason, built_with_ai_signals, technical_signals, pain_signals, evidence_quotes, personalization_hooks, scored_at`

- Via `get_leads_with_scores` (**dernier verdict** : `s.id = (SELECT MAX(id) FROM lead_scores WHERE lead_id = l.id)` — db.py:1289-1311).

### 16.3 CSV lisible (revue humaine) — `READABLE_FIELDS`, 15 colonnes (export.py:228-235)

`lead_id, company_name, website_url, status, segment, confidence, needs_human_review, recommended_offer, disqualify_reason, signals_summary, github_check_summary, homepage_preview, about_preview, product_preview, pricing_preview, careers_preview, evidence_quotes, personalization_hooks, search_evidence`

- `DEFAULT_PREVIEW_CHARS = 400` ; `_preview` : newlines écrasés, troncature avec " …", "(page not found / not scraped)" si vide ; `_format_signals_summary` (phrase lisible : generator, fonts, patterns x/9, vibe language, phrases + densité, disclosures, repo GitHub) ; `_format_github_check_summary` (« N commits seen (API page) », single-commit, premier message 60 chars) ; `search_evidence` = `{src}: {titres} ||| …`.
- ⚠️ TODO code (commentaire export.py:323-326) : `pricing_preview`/`careers_preview` restent du texte brut — extracteurs dédiés (« self-serve vs sales-only » et « N engineering jobs ») à coder.

### 16.4 Web search CSV — `SEARCH_FIELDS` (export.py:412-415)

`lead_id, company_name, website_url, source, query, result_url, result_title, result_snippet` — une ligne par résultat ; snippet tronqué à 500 chars ; erreur → result_title "ERROR".

### 16.5 CLI

`main()` (export.py:482-500) : `--scraping-out` (défaut scraping_results.csv), `--scores-out` (scores_results.csv), `--search-out` (search_results.csv) ; `init_db` (no-op) puis les 3 exports.

---

## 17. Segments, statuts & machine à états

### 17.1 Constantes (constants.py — source unique de vérité, jamais redéfinie localement)

```python
VALID_SEGMENTS = {"ai_solo_founder", "technical_founder", "small_agency_scaling", "too_big", "wrong_field", "unclear"}
TARGET_SEGMENTS = {"ai_solo_founder", "technical_founder", "small_agency_scaling"}
OUT_OF_TARGET_SEGMENTS = {"too_big", "wrong_field"}
NOT_YET_SCORED_STATUSES = ("NEW", "PARSED", "FETCH_PARTIAL", "FETCH_FAILED", "SCORE_FAILED", "RESCORE_PENDING", "RESCORE_FAILED")
CONFIDENCE_THRESHOLD = 0.7  # FR-3
```

### 17.2 Segments et offres

| Segment | Description | Offre recommandée |
|---|---|---|
| `ai_solo_founder` | Fondateur non technique construisant avec l'IA (CIBLE PRINCIPALE) | `ai_audit` |
| `technical_founder` | Équipe technique, IA comme outil de dev | `general_audit` |
| `small_agency_scaling` | Agence / studio en phase de scaling | `pipeline` |
| `too_big` | Entreprise établie, loin de la persona cible | `none` |
| `wrong_field` | Secteur sans rapport | `none` |
| `unclear` | Preuves insuffisantes | `none` (sauf signal partiel) |

### 17.3 Machine à états des statuts de lead

```
NEW
  └→ PARSED | FETCH_PARTIAL | FETCH_FAILED      (scraping)
        └→ SCORED | LOW_CONFIDENCE | SCORE_FAILED   (scoring)
              └→ APPROVED | REJECTED                 (revue humaine)
```

| Statut | Sens | Émis par |
|---|---|---|
| NEW | Lead importé, pas encore traité | ingestion |
| PARSED | Scraping complet (toutes pages utilisables) | scraper |
| FETCH_PARTIAL | Homepage OK, ≥1 sous-page inutilisable (broken, SPA, 404) | scraper |
| FETCH_FAILED | Site mort : homepage injoignable ou cassée (rows == []) | scraper |
| SCORED | Verdict confiance ≥ 0.7, sans doute résiduel | pipeline |
| LOW_CONFIDENCE | Verdict needs_human_review (confiance < 0.7, ou garde) | pipeline |
| SCORE_FAILED | Erreur LLM/pipeline au scoring | pipeline |
| RESCORE_PENDING / RESCORE_FAILED | Re-scoring sans re-scrape | rescore |
| SKIPPED | Non sélectionné à la revue d'import | app (start_pipeline) |
| APPROVED / REJECTED | Décision humaine | review_lead |

### 17.4 Catégories de la page résultats (`_categorize_leads`, app.py:691-741)

| Catégorie | Critère |
|---|---|
| Pending (En attente) | statut ∈ NOT_YET_SCORED_STATUSES |
| To review | needs_human_review |
| Ready to approve | segment ∈ TARGET_SEGMENTS |
| Out of target | segment ∈ OUT_OF_TARGET_SEGMENTS |
| Not selected | statut SKIPPED |

---

## 18. Housekeeping des séquences

Objectif : après suppression de lignes, les identifiants suivants reprennent `MAX(id)+1` (pas de trous de numérotation).

- `_SEQUENCE_TRIGGER_TABLES` (db.py:633-637) : les 8 tables — analysis_sessions, users, leads, lead_content, lead_technical_signals, lead_scores, lead_search_evidence, export_history.
- `_sequence_housekeeping_sql()` (db.py:640-700) retourne 3 statements **standalone** (le split sur `;` de `executescript` casserait les corps dollar-quotés) :
  1. `CREATE OR REPLACE FUNCTION public.sync_seq_after_delete()` (db.py:661-680) : lit `pg_get_serial_sequence(...)`, calcule `COALESCE(MAX(id), 0) + 1`, `setval(seq_name, next_val, false)`.
  2. Un trigger `trg_seq_<table>` **AFTER DELETE FOR EACH STATEMENT** par table, créé idempotemment via `IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = ...)`.
  3. Réalignement one-off : `setval(...)` pour chaque table (db.py:689-698).
- `_ensure_sequence_housekeeping(conn)` (db.py:703-709) exécute les 3 statements + commit ; appelé en fin de `init_db`.

---

## 19. Templates & UI

| Template | Rôle |
|---|---|
| `home.html` | Accueil : 6 stats, dropzone upload CSV, sessions récentes ; lien Users si admin (home.html:117-119) |
| `signup.html` | Inscription — « The first account of the database automatically becomes an admin » (signup.html:54) |
| `login.html` | Connexion |
| `history.html` | Historique des sessions (colonne rank utilisateur si show_rank, history.html:88) |
| `dashboard.html` | Dashboard : sélecteur de session, stats, ingestion, quick actions, tables leads/scores, détail lead |
| `import_review.html` | Revue d'import (Step 2/4) : table keepers, table doublons, cartes critères (wrong_field masqué, import_review.html:180-188), custom criterion, lancement |
| `progress.html` | Progression temps réel (SSE — cf. §15) |
| `results.html` | Résultats en 5 catégories + boutons CSV |
| `results_print.html` | Version imprimable / PDF (4 catégories, `@page landscape` — results_print.html:7) |
| `batch_results.html` | Résultats du dernier batch analysé |
| `web_search.html` | Evidence web search par lead (snippets 300 chars, web_search.html:119) |
| `admin_users.html` | Gestion des utilisateurs (rôle, block/unblock, delete, lien historique) |
| `admin_user_history.html` | Historique des sessions d'un utilisateur |
| `static/styles.css` | Feuille de style unique (thème sombre Bootstrap) |


---

## 20. Variables d'environnement

| Variable | Usage | Requise | Supportée |
|---|---|---|---|
| `DATABASE_URL` | Connexion PostgreSQL (Neon) | ✅ (sinon RuntimeError au démarrage) | — |
| `FIRECRAWL_API_KEY` | Clé Firecrawl principale | ✅ (sinon flash warning) | — |
| `FIRECRAWL_API_KEY_2` … `_5` | Clés Firecrawl additionnelles (pool, round-robin, 1 thread/clé) | ❌ | — |
| `GROQ_API_KEY` | Clé Groq pour le scoring | ✅ (sinon flash warning) | — |
| `SGAI_API_KEY` | Clé ScrapeGraphAI principale (escalade web) | ❌ (sinon escalade désactivée) | — |
| `SGAI_API_KEY_2` … `_5` | Clés SGAI additionnelles | ❌ | — |
| `DB_POOL_MINCONN` | Pool min (défaut 1) | ❌ | db.py:60 |
| `DB_POOL_MAXCONN` | Pool max (défaut 8) | ❌ | db.py:61 |
| `PIPELINE_CONCURRENCY` | Concurrence pipeline (défaut 3) | ❌ | pipeline.py:20 |
| `FLASK_SECRET_KEY` | Clé secrète Flask (défaut "lead-qualification-engine") | ❌ | app.py:37 |
| `PORT` | Port HTTP (défaut 5000) | ❌ | app.py:1326 |

> ⚠️ Pas de `.env.example` dans le repo. Les variables `_4`/`_5` et les autres variables optionnelles ne sont pas dans le `.env` actuel mais sont lues par le code.

---

## 21. Dépendances & fichiers

### 21.1 requirements.txt (8 dépendances, sans versions)

pandas, rapidfuzz, firecrawl-py, openai, python-dotenv, flask, requests, psycopg2-binary

### 21.2 Fichiers du repo (racine)

| Fichier | Taille | Rôle |
|---|---|---|
| `app.py` | ~55 Ko | Interface Flask |
| `constants.py` | ~0,6 Ko | Segments, statuts, seuil |
| `db.py` | ~46 Ko | Schéma + CRUD + pool |
| `dedup.py` | ~4 Ko | 3 niveaux de dédup |
| `export.py` | ~22 Ko | Exports CSV/CLI |
| `pipeline.py` | ~19 Ko | Orchestrateur |
| `scorer.py` | ~29 Ko | Scoring Groq |
| `scraper.py` | ~43 Ko | Scraping + signaux + SGAI |
| `README.md` | ~2,4 Ko | Doc courte (⚠️ bug bloc code non fermé) |
| `requirements.txt` | 90 o | Dépendances |
| `.env` | 571 o | Secrets (ignoré par git) |
| `.gitignore` | 86 o | .env, __pycache__/, *.pyc, *.db*, *.csv, .venv/ |
| `templates/` | 13 templates | HTML |
| `static/styles.css` | ~15 Ko | Thème sombre Bootstrap |

---

## 22. Notes de cohérence & pièges connus

- **Décision réglée (implémentée) — déclencheur d'escalade web pour `small_agency_scaling`** : la question « élargir la recherche web aux agences confiantes » (posée au moment de la validation du filet `confidence < 0.7`) est TRANCHÉE et codée : le 3e bloc du déclencheur (pipeline.py:250-258) ajoute `segment == small_agency_scaling ET hiring_technical ET confidence >= 0.7` AU filet existant, sans le remplacer. Verrouillé par test dédié (test_web_escalation_trigger.py) couvrant les deux branches du OR + les négatifs. Ne pas « simplifier » ce déclencheur en un OU exclusif.
- **Docstring erronée** : `app.py:4` mentionne « Claude scoring » alors que le scoring utilise Groq (`llama-3.3-70b-versatile`) — à corriger.
- **README cassé** : bloc ```bash non fermé dans la section Installation (toute la suite du rendu Markdown est affectée).
- **Vestiges `__pycache__`** : `pipeline_phase2.cpython-311.pyc` et `test_stop_analysis.cpython-311.pyc` sans source correspondante (fichiers supprimés).
- **TODO extracteurs pricing/careers** : dans l'export lisible, `pricing_preview`/`careers_preview` restent du texte brut ; les extracteurs dédiés (« self-serve vs sales-only », « N engineering jobs ») sont à coder (export.py:323-326).
- **`executescript` et point-virgules** : le split sur `;` impose que tout bloc contenant des `;` (fonctions PL/pgSQL, triggers) soit passé comme statement standalone.
- **Sessions legacy** (owner_id NULL) : visibles uniquement par l'admin (`_assert_session_access`).
- **Rôle en session signée** : un changement de rôle en base n'est effectif qu'après re-login.
- **Progress in-memory** : `_pipeline_progress` vit dans le process — un redémarrage de l'app pendant un run perd la progression (les statuts en base, eux, sont conservés).
- **Firecrawl free tier** : throttle par défaut 15 s en séquentiel ; en multi-clés, 1 requête parallèle par clé max.
- **Git** : 13+ commits sur main ; le dernier `cecc9b3` corrige la contradiction site_content_missing/statut et scinde les sémantiques FETCH_FAILED (site mort) vs FETCH_PARTIAL (homepage OK, sous-pages ratées). Attention : ~17 fichiers modifiés non commités (reskin, triggers, renumérotation, per-user numbering).

