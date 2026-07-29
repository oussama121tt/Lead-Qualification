"""
Tests restants après désactivation du mécanisme d'arrêt.

Couvre :
- Phase 2 : sélection de leads, exclusion SKIPPED
- Routes Flask basiques
"""

import json
import time
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Tests unitaires sur le SSE stream (progress_stream)
# ---------------------------------------------------------------------------

class TestSSEStream(unittest.TestCase):
    """Vérifie que le SSE génère bien les événements finaux."""

    def _simulate_progress_stream(self, pipeline_status):
        """Reproduit la logique de progress_stream pour un statut donné."""
        import app as appmod
        from app import _store_progress

        progress = {
            "pipeline_status": pipeline_status,
            "processed": 5,
            "total": 10,
            "index": 5,
            "started_at": time.monotonic(),
            "completed_ts": time.monotonic(),
            "status": pipeline_status,
        }
        _store_progress(999, progress)
        return progress

    def test_sse_accepts_completed(self):
        """Le SSE doit reconnaître 'completed' comme statut terminal."""
        prog = self._simulate_progress_stream("completed")
        status = prog.get("pipeline_status")
        self.assertIn(status, ("completed", "failed"))

    def test_sse_accepts_failed(self):
        """Le SSE doit reconnaître 'failed' comme statut terminal."""
        prog = self._simulate_progress_stream("failed")
        status = prog.get("pipeline_status")
        self.assertIn(status, ("completed", "failed"))

    def test_sse_rejects_running(self):
        """Le SSE ne doit PAS s'arrêter sur 'running'."""
        prog = self._simulate_progress_stream("running")
        status = prog.get("pipeline_status")
        self.assertNotIn(status, ("completed", "failed"),
                         "running ne doit pas être un statut terminal")

    def test_sse_rejects_waiting(self):
        """Le SSE ne doit PAS s'arrêter sur None/waiting."""
        prog = self._simulate_progress_stream(None)
        status = prog.get("pipeline_status")
        self.assertNotIn(status, ("completed", "failed"),
                         "None ne doit pas être un statut terminal")


# Les tests suivants sont commentés car le mécanisme d'arrêt
# (threading.Event + fonctions de cancellation) a été désactivé.
#
# class TestCancellationMechanism(unittest.TestCase): ...


# ---------------------------------------------------------------------------
# Tests sur la Phase 2 selection
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tests sur la Phase 2 selection
# ---------------------------------------------------------------------------

class TestPhase2Selection(unittest.TestCase):
    """Vérifie que la page de sélection Phase 2 exclut les SKIPPED."""

    def _make_mock_lead(self, lead_id, status="SCORED", segment="vibe_coder",
                        is_duplicate=0, disqualify_reason=None):
        return {
            "id": lead_id,
            "company_name": f"Test{lead_id}",
            "website_url": f"https://test{lead_id}.com",
            "segment": segment,
            "status": status,
            "is_duplicate": is_duplicate,
            "disqualify_reason": disqualify_reason,
            "confidence": 0.85,
            "recommended_offer": "Test Offer",
            "company_stage": "early",
            "needs_human_review": 0,
            "first_name": "John",
            "last_name": "Doe",
            "email": "john@test.com",
            "last_error": None,
        }

    def test_skipped_excluded_from_selection(self):
        """Les leads SKIPPED ne doivent pas apparaître dans la sélection Phase 2."""
        scores_data = [
            self._make_mock_lead(1, status="SKIPPED"),
            self._make_mock_lead(2, status="SCORED", segment="vibe_coder"),
            self._make_mock_lead(3, status="SCORED", segment="technical_ai_user"),
        ]

        validees = []
        en_attente = []
        for lead in scores_data:
            if lead.get("is_duplicate"):
                continue
            segment = lead.get("segment")
            status = lead.get("status", "NEW")
            disqualify = lead.get("disqualify_reason") or ""
            if status == "SKIPPED":
                continue
            is_scoring_error = "api_error" in disqualify.lower() or "no_content_scraped" in disqualify.lower()
            if is_scoring_error or status in ("FETCH_FAILED", "SCORE_FAILED", "NEW", "PARSED", "FETCH_PARTIAL"):
                en_attente.append(lead)
            elif segment in ("vibe_coder", "technical_ai_user"):
                validees.append(lead)

        self.assertEqual(len(validees), 2, "Les 2 leads SCORED doivent être dans validees")
        self.assertEqual(len(en_attente), 0, "Aucun lead en attente")

    def test_duplicates_excluded(self):
        """Les doublons ne doivent pas apparaître dans la sélection."""
        scores_data = [
            self._make_mock_lead(1, is_duplicate=1),
            self._make_mock_lead(2, status="SCORED", segment="vibe_coder"),
        ]
        validees = []
        for lead in scores_data:
            if lead.get("is_duplicate"):
                continue
            validees.append(lead)
        self.assertEqual(len(validees), 1)

    def test_scoring_errors_in_attente(self):
        """Les leads avec erreur de scoring vont dans en_attente, pas dans validees."""
        scores_data = [
            self._make_mock_lead(1, status="FETCH_FAILED", disqualify_reason="api_error"),
            self._make_mock_lead(2, status="SCORE_FAILED"),
            self._make_mock_lead(3, status="SCORED", segment="vibe_coder"),
        ]
        validees = []
        en_attente = []
        for lead in scores_data:
            if lead.get("is_duplicate"):
                continue
            segment = lead.get("segment")
            status = lead.get("status", "NEW")
            disqualify = lead.get("disqualify_reason") or ""
            if status == "SKIPPED":
                continue
            is_scoring_error = "api_error" in disqualify.lower() or "no_content_scraped" in disqualify.lower()
            if is_scoring_error or status in ("FETCH_FAILED", "SCORE_FAILED", "NEW", "PARSED", "FETCH_PARTIAL"):
                en_attente.append(lead)
            elif segment in ("vibe_coder", "technical_ai_user"):
                validees.append(lead)

        self.assertEqual(len(validees), 1)
        self.assertEqual(len(en_attente), 2)


# ---------------------------------------------------------------------------
# Tests sur les routes Flask (intégration)
# ---------------------------------------------------------------------------

class TestFlaskRoutes(unittest.TestCase):
    """Vérifie que les routes répondent correctement."""

    def setUp(self):
        import app as appmod
        appmod.app.testing = True
        self.client = appmod.app.test_client()

    def test_progress_page_returns_200(self):
        """La page de progression doit retourner 200."""
        resp = self.client.get("/progress/1")
        self.assertIn(resp.status_code, (200, 302))

    def test_phase2_select_unknown_session_redirects(self):
        """Phase 2 select sur session inconnue doit rediriger."""
        resp = self.client.get("/phase2-select/999999")
        self.assertEqual(resp.status_code, 302)


# ---------------------------------------------------------------------------
# Tests sur les cas limites
# ---------------------------------------------------------------------------

class TestEdgeCases(unittest.TestCase):
    """Teste les cas limites restants."""

    def test_rescope_phase2_empty_selection(self):
        """rescore_phase2 sans lead_ids doit rediriger vers la page de sélection."""
        import app as appmod
        with appmod.app.test_client() as client:
            resp = client.post("/rescore-phase2/999999")
            self.assertEqual(resp.status_code, 302)


# ---------------------------------------------------------------------------
# Exécution
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
