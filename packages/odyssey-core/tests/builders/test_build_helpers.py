"""Tests for the public format-adapter helpers in odyssey.build.messages.

These are the primitives customers reach for when writing their own
``messages_from_<myformat>`` recipe; the shipped recipes
(``messages_from_openai_chat``, etc.) exercise them end-to-end and are
tested separately in ``test_build_messages.py``.
"""

from __future__ import annotations

import pytest

from odyssey.builders.messages import (
    flatten_text_content,
    normalize_role,
    parse_tool_arguments,
)


class TestNormalizeRole:
    def test_canonical_passthrough(self):
        for r in ("system", "user", "assistant", "tool"):
            assert normalize_role(r) == r

    def test_aliases(self):
        assert normalize_role("human") == "user"
        assert normalize_role("HUMAN") == "user"
        assert normalize_role("ai") == "assistant"
        assert normalize_role("chatbot") == "assistant"
        assert normalize_role("model") == "assistant"
        assert normalize_role("function") == "tool"
        assert normalize_role("tool_result") == "tool"
        assert normalize_role("tool_use") == "assistant"

    def test_leading_trailing_whitespace_stripped(self):
        assert normalize_role("  user  ") == "user"

    def test_unknown_role_raises(self):
        with pytest.raises(ValueError, match="unknown role"):
            normalize_role("mystery")

    def test_none_raises(self):
        with pytest.raises(ValueError, match="role is required"):
            normalize_role(None)

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="role is required"):
            normalize_role("")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="role is required"):
            normalize_role("   ")


class TestFlattenTextContent:
    def test_string_passthrough(self):
        assert flatten_text_content("hello") == "hello"

    def test_none(self):
        assert flatten_text_content(None) is None

    def test_joins_text_blocks(self):
        blocks = [
            {"type": "text", "text": "first"},
            {"type": "text", "text": "second"},
        ]
        assert flatten_text_content(blocks) == "first\nsecond"

    def test_string_blocks_in_list(self):
        assert flatten_text_content(["alpha", "beta"]) == "alpha\nbeta"

    def test_dict_with_text_key(self):
        assert flatten_text_content({"text": "hello"}) == "hello"

    def test_known_non_text_media_dropped(self):
        blocks = [
            {"type": "text", "text": "describe"},
            {"type": "image_url", "image_url": {"url": "http://..."}},
            {"type": "text", "text": "in detail"},
        ]
        assert flatten_text_content(blocks) == "describe\nin detail"

    def test_unknown_block_type_raises(self):
        blocks = [{"type": "mystery", "payload": 1}]
        with pytest.raises(ValueError, match="unsupported type"):
            flatten_text_content(blocks)

    def test_non_dict_non_str_block_raises(self):
        with pytest.raises(TypeError, match="must be str or dict"):
            flatten_text_content([42])

    def test_text_block_missing_text_key_raises(self):
        with pytest.raises(ValueError, match="'text' key is missing"):
            flatten_text_content([{"type": "text"}])

    def test_text_block_with_non_string_text_raises(self):
        with pytest.raises(ValueError, match="'text' key is missing"):
            flatten_text_content([{"type": "text", "text": 123}])

    def test_dict_without_text_key_raises(self):
        with pytest.raises(ValueError, match="must have a string 'text' key"):
            flatten_text_content({"foo": 1})

    def test_unsupported_type_raises(self):
        with pytest.raises(TypeError, match="unsupported content type"):
            flatten_text_content(42)


class TestParseToolArguments:
    def test_none_becomes_empty_dict(self):
        assert parse_tool_arguments(None) == {}

    def test_dict_passthrough(self):
        payload = {"x": 1}
        assert parse_tool_arguments(payload) is payload

    def test_json_string_to_dict(self):
        assert parse_tool_arguments('{"id": 42, "q": "cats"}') == {
            "id": 42,
            "q": "cats",
        }

    def test_unparseable_string_raises(self):
        with pytest.raises(ValueError, match="not valid JSON"):
            parse_tool_arguments("not json at all")

    def test_non_dict_json_raises(self):
        with pytest.raises(ValueError, match="must parse to a dict"):
            parse_tool_arguments("[1, 2, 3]")
        with pytest.raises(ValueError, match="must parse to a dict"):
            parse_tool_arguments('"just a string"')

    def test_non_string_non_dict_raises(self):
        with pytest.raises(TypeError, match="must be None / dict / JSON str"):
            parse_tool_arguments(42)
