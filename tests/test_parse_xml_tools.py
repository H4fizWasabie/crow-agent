"""Tests for XML/DSML text tool-call parsing."""

from __future__ import annotations

from crow_agent.providers import (
    normalize_model_text_tools,
    text_may_contain_tool_calls,
)

_DSML_OPEN = "<\uff5c\uff5cDSML\uff5c\uff5c"
_DSML_CLOSE = "\uff5c\uff5cDSML\uff5c\uff5c"


def test_text_may_contain_tool_calls_dsml():
    sample = f"{_DSML_OPEN}tool_calls>\n{_DSML_OPEN}invoke name=\"read_file\">"
    assert text_may_contain_tool_calls(sample)


def test_parse_standard_invoke():
    content = '<invoke name="read_file"><parameter name="limit">60</parameter></invoke>'
    cleaned, tools = normalize_model_text_tools(content, [])
    assert len(tools) == 1
    assert tools[0]["function"]["name"] == "read_file"
    assert "60" in tools[0]["function"]["arguments"]
    assert "invoke" not in cleaned


def test_parse_dsml_invoke():
    content = f"""{_DSML_OPEN}tool_calls>
{_DSML_OPEN}invoke name="run_cmd">
{_DSML_OPEN}parameter name="command" string="true">echo hi</{_DSML_CLOSE}parameter>
{_DSML_OPEN}parameter name="timeout" string="false">10</{_DSML_CLOSE}parameter>
</{_DSML_CLOSE}invoke>
</{_DSML_CLOSE}tool_calls>"""
    cleaned, tools = normalize_model_text_tools(content, [])
    assert len(tools) == 1
    assert tools[0]["function"]["name"] == "run_cmd"
    assert "echo hi" in tools[0]["function"]["arguments"]
    assert "DSML" not in cleaned


def test_parse_tool_call_tool_name_format():
    content = """<tool_call>
<tool_name>run_cmd</tool_name>
<param name="command">ssh host 'ls'</param>
<param name="timeout">10</param>
</tool_call>"""
    cleaned, tools = normalize_model_text_tools(content, [])
    assert len(tools) == 1
    assert tools[0]["function"]["name"] == "run_cmd"
    assert "ssh host" in tools[0]["function"]["arguments"]
    assert "tool_call" not in cleaned


def test_merge_with_existing_native_tool_calls():
    native = [{"id": "call_1", "type": "function", "function": {"name": "get_time", "arguments": "{}"}}]
    content = '<invoke name="grep_files"><parameter name="pattern">foo</parameter></invoke>'
    _, tools = normalize_model_text_tools(content, native)
    assert len(tools) == 2
    assert tools[0]["function"]["name"] == "get_time"
    assert tools[1]["function"]["name"] == "grep_files"
