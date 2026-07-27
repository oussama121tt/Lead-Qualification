# Plan d'implémentation : Scoper la dédup par session

## Statut : ✅ COMPLETED

### Changements appliqués

| Fichier | Changement | Statut |
|---------|-----------|--------|
| `dedup.py` | `run_dedup()` — ajout du paramètre `session_id` passé à `get_leads()` | ✅ |
| `app.py` | `start_analysis()` — ajout `session_id=session_id` à l'appel `run_dedup()` | ✅ |
| `app.py` | `dedup_only()` — ajout `session_id=selected_session_id` à l'appel `run_dedup()` | ✅ |
| `templates/dashboard.html` | Bouton toggle "Voir l'historique" + collapse wrapper + Bootstrap JS | ✅ |

### Résumé des modifications

**dedup.py** — 1 ligne modifiée :
```python
def run_dedup(conn, fuzzy_threshold: int = 90, session_id: int | None = None) -> dict:
    leads = dbmod.get_leads(conn, include_duplicates=True, session_id=session_id)
```

**app.py** — 2 lignes modifiées :
```python
# Ligne ~302 (start_analysis)
dedup_summary = dedupmod.run_dedup(conn, fuzzy_threshold=fuzzy_threshold, session_id=session_id)

# Ligne ~345 (dedup_only)
summary = dedupmod.run_dedup(conn, fuzzy_threshold=threshold, session_id=selected_session_id)
```

**templates/dashboard.html** — 3 changements :
1. Bouton "Voir l'historique" ajouté dans `hero-actions` avec `data-bs-toggle="collapse"`
2. Formulaire `.session-switcher` wrappé dans `<div class="collapse mt-4" id="historyPanel">`
3. Bootstrap JS (`bootstrap.bundle.min.js`) ajouté avant `</body>`

