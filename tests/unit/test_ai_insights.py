import pytest
from unittest.mock import patch, MagicMock
from backend.models.insights import CaseAIInsightResponse
from backend.services.gemini_service import GeminiService


def test_ai_insights_endpoint_success(client):
    """Calling GET /api/cases/{case_id}/ai-insights returns structured AI case dossier."""
    mock_resp = CaseAIInsightResponse(
        case_id="CASE_TEST_01",
        case_name="Operation Test Network",
        status="OPEN",
        risk_level="HIGH",
        executive_summary="Operation Test Network exhibits complex syndicate behavior.",
        modus_operandi="Layered digital payments and burner phones.",
        key_suspects=["Suspect A", "Suspect B"],
        critical_anomalies=["Velocity threshold breach"],
        investigative_leads=["Issue Section 91 CrPC notice"],
        ai_model="gemini-2.5-flash"
    )

    with patch.object(GeminiService, "generate_case_brief", return_value=mock_resp):
        response = client.get("/api/cases/CASE_TEST_01/ai-insights")
        assert response.status_code == 200
        data = response.json()
        assert data["case_id"] == "CASE_TEST_01"
        assert data["ai_model"] == "gemini-2.5-flash"
        assert data["risk_level"] == "HIGH"
        assert "Suspect A" in data["key_suspects"]
        assert len(data["investigative_leads"]) > 0


def test_ai_insights_not_found(client):
    """Calling AI insights for non-existent case returns 404."""
    with patch.object(GeminiService, "generate_case_brief", side_effect=ValueError("Case 'NON_EXISTENT' not found")):
        response = client.get("/api/cases/NON_EXISTENT/ai-insights")
        assert response.status_code == 404


def test_graph_copilot_endpoint_success(client):
    """Calling POST /api/graph/ai-query returns structured AI Copilot answer."""
    from backend.models.insights import GraphAIChatResponse
    mock_chat_resp = GraphAIChatResponse(
        answer="Primary Target is Devendra Sharma coordinating 3 suspect accounts.",
        key_findings=["Primary Target: Devendra Sharma", "Degree: 12"],
        ai_model="gemini-2.5-flash"
    )

    with patch("backend.case_access.resolve_session", return_value="TEST-OFFICER"), patch("backend.services.copilot_context.build_copilot_context", return_value={"cases": []}), patch.object(GeminiService, "chat", return_value=mock_chat_resp.model_dump()):
        response = client.post("/api/graph/ai-query", json={
            "question": "Who is the kingpin?",
            "case_id": "CASE_TEST_01",
            "nodes_count": 45,
            "edges_count": 52
        }, headers={"Authorization": "Bearer test-session"})
        assert response.status_code == 200
        data = response.json()
        assert "Devendra Sharma" in data["answer"]
        assert len(data["key_findings"]) == 2
        assert data["ai_model"] == "gemini-2.5-flash"


def test_graph_copilot_empty_question(client):
    """Calling POST /api/graph/ai-query with empty question returns 400."""
    response = client.post("/api/graph/ai-query", json={"question": "   "})
    assert response.status_code == 400
    assert "Question is required" in response.json()["detail"]


def test_graph_copilot_missing_sdk_returns_error_without_demonstration_fallback(monkeypatch):
    """Unavailable Gemini must never produce a canned analysis response."""
    from fastapi import HTTPException
    from backend.services import gemini_service
    monkeypatch.setattr(gemini_service, "HAS_GENAI", False)
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-test-only-key")
    with pytest.raises(HTTPException) as error:
        GeminiService.chat("Explain uncertainty", {"cases": []}, [])
    assert error.value.status_code == 503
    assert "SDK" in error.value.detail

