# Lead Qualification & Scoring Engine — brique 1

Implémente les étapes 1, 2, 3 et 5 du pipeline décrit dans le récap projet :
**Ingestion → Déduplication → Scraping (Firecrawl) → Scoring IA (Groq)**,
avec une interface Streamlit.

## Installation

```bash
pip install -r requirements.txt
cp .env.example .env
# puis remplir FIRECRAWL_API_KEY et GROQ_API_KEY dans .env
```

## Lancer l'interface

```bash
streamlit run app.py
```

Une base SQLite `leads.db` est créée automatiquement à côté de `app.py`.

## Ce que fait chaque onglet

1. **Ingestion** — upload d'un CSV Apollo → insertion dans la table `leads`
   (statut `NEW`). Les colonnes Apollo courantes (`First Name`, `Company`,
   `Website`, etc.) sont reconnues automatiquement. Un lead sans site web est
   ignoré et compté dans `skipped_no_website`.
2. **Déduplication** — flag `is_duplicate` (jamais de suppression), 3 niveaux :
   email exact → domaine normalisé → fuzzy matching RapidFuzz sur le nom
   d'entreprise (seuil réglable, 90 par défaut).
3. **Scraping + Scoring** — pour chaque lead `NEW` non dupliqué : Firecrawl
   scrape la homepage + jusqu'à 4 pages clés découvertes automatiquement, puis
   Groq (`openai/gpt-oss-120b`) score le lead avec le schéma JSON strict du
   projet (`evidence_quotes` obligatoire, seuil de confiance 0.75). Une pause
   entre chaque lead protège les quotas des tiers gratuits.
4. **Résultats** — table filtrable par segment / `needs_human_review`, avec
   vue détail (`evidence_quotes`, `personalization_hooks`) pour préparer la
   review humaine (étape 6, pas encore construite ici).

## Fichiers

| Fichier | Rôle |
|---|---|
| `db.py` | Schéma SQLite (`leads`, `lead_content`, `lead_scores`) + helpers CRUD |
| `dedup.py` | Déduplication 3 niveaux (RapidFuzz) |
| `scraper.py` | Reprise de `scrap.py`, en fonction réutilisable `scrape_website()` |
| `scorer.py` | Reprise de `evaluate_pass1.py`, en fonction réutilisable `score_content()` |
| `pipeline.py` | Orchestrateur : enchaîne scraping + scoring lead par lead, isole les échecs |
| `app.py` | Interface Streamlit (4 onglets ci-dessus) |

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
