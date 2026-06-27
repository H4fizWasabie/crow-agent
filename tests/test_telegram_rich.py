from crow_agent.telegram_rich import contains_pipe_table, format_telegram_html


def test_contains_pipe_table_detects_markdown_table() -> None:
    text = "| Name | Qty |\n|---|---|\n| Bolt | 4 |"
    assert contains_pipe_table(text)


def test_contains_pipe_table_ignores_plain_text() -> None:
    assert not contains_pipe_table("A | B\njust text")


def test_format_telegram_html_renders_table_as_pre() -> None:
    text = "| Name | Qty |\n|---|---|\n| Bolt | 4 |"
    html = format_telegram_html(text)
    assert "<pre>" in html
    # Never emit unsupported tags
    assert "<table" not in html
    assert "<th>" not in html


def test_format_telegram_html_strips_tags_in_pre_cells() -> None:
    text = "| Name | Note |\n|---|---|\n| Bolt | **hot** |"
    html = format_telegram_html(text)
    # <b>hot</b> should NOT appear inside <pre> — tags stripped
    assert "<b>hot</b>" not in html
    assert "hot" in html  # text preserved
    assert "<pre>" in html


def test_format_telegram_html_preserves_inline_outside_table() -> None:
    text = "**bold text**\n\n| a |\n|---|\n| x |"
    html = format_telegram_html(text)
    assert "<b>bold text</b>" in html
    assert "<pre>" in html


def test_format_telegram_html_handles_empty_table() -> None:
    assert "hello" in format_telegram_html("hello")
    assert format_telegram_html("") == ""
