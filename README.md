# Lead Qualification & Scoring Engine

Pipeline complet : **Ingestion → Déduplication → Scraping (Firecrawl) → Scoring IA (Groq)**, avec interface web Flask.

## Installation

```bash
pip install -r requirements.txt
# Créer .env avec FIRECRAWL_API_KEY et GROQ_API_KEY
```

## Lancer l'interface

```bash
python app.py
```

Une base SQLite `leads.db` est créée automatiquement. Chaque import crée une session d'analyse séparée.

## Fonctionnalités de l'interface

1. **Upload CSV** — import d'un CSV Apollo dans SQLite (columns: first_name, last_name, title, company_name, email, website_url).
2. **Analyse complète** — ingestion + dédup + scraping Firecrawl + scoring Claude, en un clic.
3. **Actions indépendantes** — import seul, dédup seule, pipeline seul.
4. **Tableaux** — vue des leads bruts et vue des leads scorés avec filtres par segment.
5. **Review humaine** — approuver/rejeter un lead, modifier son segment.
6. **Téléchargements** — CSV de scraping brut et CSV de scoring.
7. **Détail lead** — evidence_quotes, personalization_hooks, disqualify_reason.
8. **Historique** — sélection d'anciennes sessions pour revoir leurs résultats.

## Fichiers

| Fichier | Rôle |
|---|---|
| `db.py` | Schéma SQLite + helpers CRUD (leads, sessions, scores, exports) |
| `dedup.py` | Déduplication 3 niveaux (email exact, domaine, fuzzy name via RapidFuzz) |
| `scraper.py` | Scraping Firecrawl + extraction de signaux techniques déterministes |
| `scorer.py` | Scoring Claude : évalue chaque lead et produit un verdict JSON structuré |
| `pipeline.py` | Orchestrateur : enchaîne scraping + scoring lead par lead, isole les échecs |
| `export.py` | Export CSV : scraping brut, scores, et format lisible pour review humaine |
| `app.py` | Interface Flask complète |

## Segments de scoring

| Segment | Description | Offre recommandée |
|---|---|---|
| `vibe_coder` | Fondateur non-tech, construit avec l'IA (CIBLE PRINCIPALE) | `ai_audit` |
| `technical_ai_user` | Équipe technique, utilise l'IA comme outil de dev | `general_audit` |
| `not_target` | Agence, trop gros, hors secteur, indéterminé | `none` |

## Statuts d'un lead

`NEW` → `PARSED` / `FETCH_PARTIAL` / `FETCH_FAILED` → `SCORED` / `LOW_CONFIDENCE` / `SCORE_FAILED` → `APPROVED` / `REJECTED`
