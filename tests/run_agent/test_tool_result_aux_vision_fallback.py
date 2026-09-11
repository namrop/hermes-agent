"""Image content for a non-vision main model goes to the auxiliary vision model.

Before this fix, a tool that returned image content while the active model
could not see images was downgraded to the envelope's ``text_summary``. For
``vision_analyze``'s native fast path that summary is "Image attached natively
for the main model (N KB). Answer using built-in vision." — no analysis at all.
The 2026-09-10 composer job burned seven vision calls that way.

The auxiliary vision path has its own provider and credentials (and is kept off
the main model's quota bench by design), so it can answer when the main model
cannot. The text summary stays as the last resort.

Scar: 07_systems/hermes/incidents/
scar_hermes_vision_native_fast_path_under_benched_pin_2026-09-10.md
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _make_agent(provider: str = "zai", model: str = "glm-5.3"):
    from run_agent import AIAgent
    agent = object.__new__(AIAgent)
    agent.provider = provider
    agent.model = model
    return agent


def _native_envelope(question: str = "what do the flags say?"):
    return {
        "_multimodal": True,
        "content": [
            {
                "type": "text",
                "text": (
                    "Image loaded into your context — you can see it natively "
                    f"now. Use your built-in vision to answer the user.\n\n"
                    f"Question: {question}"
                ),
            },
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64,iVBORw0KGgoAAAA"}},
        ],
        "text_summary": (
            "Image attached natively for the main model (12.0 KB). "
            "Answer using built-in vision."
        ),
        "meta": {"image_url": "/tmp/flags.png", "size_bytes": 12288,
                 "native_vision": True},
    }


def _aux_response(text: str = "A row of small American flags on a lawn."):
    return SimpleNamespace(
        model="gpt-5.5",
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
    )


@pytest.fixture(autouse=True)
def _no_vision_main(monkeypatch):
    """The active model is text-only for every test in this module."""
    from run_agent import AIAgent
    monkeypatch.setattr(AIAgent, "_model_supports_vision", lambda self: False)


class TestAuxVisionFallback:
    def test_returns_aux_analysis_instead_of_empty_summary(self):
        agent = _make_agent()
        with patch("agent.auxiliary_client.call_llm",
                   return_value=_aux_response()) as mock_llm:
            out = agent._tool_result_content_for_active_model(
                "vision_analyze", _native_envelope()
            )
        mock_llm.assert_called_once()
        payload = json.loads(out)
        assert payload["success"] is True
        assert payload["via"] == "auxiliary_vision"
        assert "American flags" in payload["analysis"]
        assert "Answer using built-in vision" not in out

    def test_aux_call_carries_the_image_and_the_question(self):
        agent = _make_agent()
        with patch("agent.auxiliary_client.call_llm",
                   return_value=_aux_response()) as mock_llm:
            agent._tool_result_content_for_active_model(
                "vision_analyze", _native_envelope("how many flags?")
            )
        kwargs = mock_llm.call_args.kwargs
        assert kwargs["task"] == "vision"
        content = kwargs["messages"][0]["content"]
        assert content[0]["type"] == "text"
        assert "how many flags?" in content[0]["text"]
        # The boilerplate about built-in vision is false on this path.
        assert "built-in vision" not in content[0]["text"]
        assert content[1]["image_url"]["url"].startswith("data:image/")

    def test_responses_style_input_image_is_normalized(self):
        agent = _make_agent()
        envelope = _native_envelope()
        envelope["content"][1] = {
            "type": "input_image",
            "image_url": "data:image/png;base64,iVBORw0KGgoAAAA",
        }
        with patch("agent.auxiliary_client.call_llm",
                   return_value=_aux_response()) as mock_llm:
            agent._tool_result_content_for_active_model("vision_analyze", envelope)
        content = mock_llm.call_args.kwargs["messages"][0]["content"]
        assert content[1] == {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,iVBORw0KGgoAAAA"},
        }

    def test_aux_failure_falls_back_to_text_summary(self):
        agent = _make_agent()
        with patch("agent.auxiliary_client.call_llm",
                   side_effect=RuntimeError("no vision client")):
            out = agent._tool_result_content_for_active_model(
                "vision_analyze", _native_envelope()
            )
        assert isinstance(out, str)
        assert out == _native_envelope()["text_summary"]

    def test_empty_aux_answer_falls_back_to_text_summary(self):
        agent = _make_agent()
        with patch("agent.auxiliary_client.call_llm",
                   return_value=_aux_response("   ")):
            out = agent._tool_result_content_for_active_model(
                "vision_analyze", _native_envelope()
            )
        assert out == _native_envelope()["text_summary"]

    def test_computer_use_keeps_its_explicit_error(self):
        """computer_use tells the user to switch models — unchanged."""
        agent = _make_agent()
        with patch("agent.auxiliary_client.call_llm",
                   return_value=_aux_response()) as mock_llm:
            out = agent._tool_result_content_for_active_model(
                "computer_use", _native_envelope()
            )
        mock_llm.assert_not_called()
        payload = json.loads(out)
        assert "does not support image input" in payload["error"]

    def test_vision_capable_model_never_reaches_the_aux_path(self, monkeypatch):
        from run_agent import AIAgent
        monkeypatch.setattr(AIAgent, "_model_supports_vision", lambda self: True)
        agent = _make_agent(provider="openai-codex", model="gpt-6-astra")
        agent._no_list_tool_content_models = set()
        with patch("agent.auxiliary_client.call_llm",
                   return_value=_aux_response()) as mock_llm:
            out = agent._tool_result_content_for_active_model(
                "vision_analyze", _native_envelope()
            )
        mock_llm.assert_not_called()
        assert isinstance(out, list)
        assert any(p.get("type") == "image_url" for p in out)

    def test_text_only_result_passes_through(self):
        agent = _make_agent()
        with patch("agent.auxiliary_client.call_llm",
                   return_value=_aux_response()) as mock_llm:
            out = agent._tool_result_content_for_active_model(
                "read_file", '{"content": "plain text"}'
            )
        mock_llm.assert_not_called()
        assert out == '{"content": "plain text"}'
