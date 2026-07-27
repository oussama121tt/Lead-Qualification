# Lead Qualification & Scoring Engine — brique 1

Implémente les étapes 1, 2, 3 et 5 du pipeline décrit dans le récap projet :
**Ingestion → Déduplication → Scraping (Firecrawl) → Scoring IA (Groq)**,
avec une interface web Flask.

## Installation

```bash
pip install -r requirements.txt
cp .env.example .env
# puis remplir FIRECRAWL_API_KEY et GROQ_API_KEY dans .env
```

## Lancer l'interface

```bash
python app.py
```

Une base SQLite `leads.db` est créée automatiquement à côté de `app.py`.
Chaque import crée une session d'analyse séparée, consultable ensuite depuis le sélecteur d'historique dans l'interface.

## Ce que fait l'interface

L'interface est une page web complète avec :

1. **Upload CSV** — import d'un CSV Apollo dans SQLite.
2. **Boutons d'action** — déduplication, scraping + scoring, ou analyse complète en une fois.
3. **Tableaux** — vue des leads bruts et vue des leads scorés avec filtres.
4. **Téléchargements** — CSV de scraping et CSV de scoring directement depuis le navigateur.
5. **Détail lead** — affiche `evidence_quotes`, `personalization_hooks` et la raison de disqualification.
6. **Historique** — sélection d'anciennes analyses pour revoir leurs tableaux et relancer les exports.

## Fichiers

| Fichier | Rôle |
|---|---|
| `db.py` | Schéma SQLite (`leads`, `lead_content`, `lead_scores`) + helpers CRUD |
| `dedup.py` | Déduplication 3 niveaux (RapidFuzz) |
| `scraper.py` | Reprise de `scrap.py`, en fonction réutilisable `scrape_website()` |
| `scorer.py` | Reprise de `evaluate_pass1.py`, en fonction réutilisable `score_content()` |
| `pipeline.py` | Orchestrateur : enchaîne scraping + scoring lead par lead, isole les échecs |
| `app.py` | Interface Flask complète |

## Statuts possibles d'un lead (`leads.status`)

`NEW` → `PARSED` / `FETCH_PARTIAL` / `FETCH_FAILED` → `SCORED` / `LOW_CONFIDENCE` / `SCORE_FAILED`

## Pas encore fait (suite logique)

- Interface de review humaine avec actions Approve/Reject/Change segment (étape 6)
- Escalade agentique conditionnelle (`confidence < 0.75` → 2e passage avec outils
  de recherche web, budget 3 itérations)
- Personnalisation (étape 7) et export final Instantly/Smartlead (étape 8)
- Table d'historique des exports pour la dédup inter-batch (`check_against_export_history`
  existe déjà dans `dedup.py` mais n'est pas encore branchée sur une vraie source
  d'historique)
- Logging coût/statut par batch (critère d'acceptation MVP #5)

## Notes tier gratuit

- Groq : ~30 req/min → pause par défaut de 2.5s entre chaque lead dans le pipeline.
- Firecrawl : ~500-1000 crédits gratuits, 1 crédit = 1 page scrapée. Le cap de
  contenu par page est fixé à ~32000 caractères (`MAX_CONTENT_CHARS_PER_PAGE`
  dans `scraper.py`) — à réduire si le quota tokens/minute de Groq pose souci.
