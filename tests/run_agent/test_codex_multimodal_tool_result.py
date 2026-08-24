"""Tests for codex_responses_adapter multimodal tool-result handling.

Tool messages can contain a list of OpenAI-style content parts
(``[{type:"text"...}, {type:"image_url"...}]``) when the
``vision_analyze`` native fast path returns image bytes for the main model.
This file verifies the Codex Responses adapter:

  1. Converts that list into ``function_call_output.output`` as an array of
     ``input_text``/``input_image`` items (not a stringified blob).
  2. Preserves array-shaped output through the preflight validator.
"""

from __future__ import annotations

from agent.codex_responses_adapter import (
    _chat_messages_to_responses_input,
    _preflight_codex_input_items,
)


def _build_messages_with_multimodal_tool_result():
    return [
        {"role": "user", "content": "What's in /tmp/foo.png?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_abc",
                "type": "function",
                "function": {
                    "name": "vision_analyze",
                    "arguments": '{"image_url": "/tmp/foo.png", "question": "describe"}',
                },
            }],
        },
        {
            "role": "tool",
            "name": "vision_analyze",
            "tool_call_id": "call_abc",
            "content": [
                {"type": "text", "text": "Image loaded."},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,XYZ"}},
            ],
        },
    ]


class TestMultimodalToolResultConversion:
    def test_list_content_becomes_output_array(self):
        items = _chat_messages_to_responses_input(
            _build_messages_with_multimodal_tool_result()
        )
        # Find the function_call_output item
        outputs = [it for it in items if it.get("type") == "function_call_output"]
        assert len(outputs) == 1
        out = outputs[0]
        assert out["call_id"] == "call_abc"
        # Output should be a LIST (array form), not a string
        assert isinstance(out["output"], list), \
            f"Expected array output for multimodal tool result, got {type(out['output']).__name__}: {out['output']!r}"
        types = [p.get("type") for p in out["output"]]
        assert "input_text" in types
        assert "input_image" in types

    def test_input_image_preserves_data_url(self):
        items = _chat_messages_to_responses_input(
            _build_messages_with_multimodal_tool_result()
        )
        out = next(it for it in items if it.get("type") == "function_call_output")
        image_parts = [p for p in out["output"] if p.get("type") == "input_image"]
        assert len(image_parts) == 1
        assert image_parts[0]["image_url"] == "data:image/png;base64,XYZ"

    def test_string_tool_content_still_string_output(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant", "content": "",
                "tool_calls": [{
                    "id": "call_x", "type": "function",
                    "function": {"name": "terminal", "arguments": "{}"},
                }],
            },
            {
                "role": "tool", "name": "terminal", "tool_call_id": "call_x",
                "content": "ls output here",
            },
        ]
        items = _chat_messages_to_responses_input(msgs)
        out = next(it for it in items if it.get("type") == "function_call_output")
        assert isinstance(out["output"], str)
        assert out["output"] == "ls output here"


class TestPreflightAcceptsArrayOutput:
    def test_preflight_passes_array_through(self):
        raw = [
            {
                "type": "function_call",
                "call_id": "call_abc",
                "name": "vision_analyze",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "call_abc",
                "output": [
                    {"type": "input_text", "text": "Image loaded."},
                    {"type": "input_image", "image_url": "data:image/png;base64,ABC"},
                ],
            },
        ]
        normalized = _preflight_codex_input_items(raw)
        out = [it for it in normalized if it.get("type") == "function_call_output"][0]
        assert isinstance(out["output"], list)
        assert len(out["output"]) == 2
        assert out["output"][1]["type"] == "input_image"
        assert out["output"][1]["image_url"] == "data:image/png;base64,ABC"

    def test_preflight_canonicalizes_chat_style_output_parts(self):
        raw = [
            {
                "type": "function_call",
                "call_id": "call_abc", "name": "vision_analyze", "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "call_abc",
                "output": [
                    {"type": "text", "text": "chat-style text"},
                    {"type": "output_text", "text": "assistant-style text"},
                    "bare string text",
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,ZZ"}},
                    {"type": "garbage", "data": "nope"},  # unknown — should be dropped
                ],
            },
        ]
        normalized = _preflight_codex_input_items(raw)
        out = [it for it in normalized if it.get("type") == "function_call_output"][0]
        # The "garbage" part is dropped; every textual variant canonicalizes
        # to input_text, and chat-style dict-wrapped image urls unwrap.
        types = [p.get("type") for p in out["output"]]
        assert types == ["input_text", "input_text", "input_text", "input_image"]
        assert out["output"][0]["text"] == "chat-style text"
        assert out["output"][1]["text"] == "assistant-style text"
        assert out["output"][2]["text"] == "bare string text"
        assert out["output"][3]["image_url"] == "data:image/png;base64,ZZ"


