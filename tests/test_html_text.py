"""HTML to text, one piece at a time: the rules, and that splits change nothing."""

import pytest

from hermes_yandex_mail import html_text as module
from hermes_yandex_mail.html_text import HtmlText, html_text

NEWSLETTER = (
    "<html><head><title>Hidden title</title><style>p{color:red}</style></head>"
    "<body><p>Hello &amp; welcome</p><script>alert(1)</script>"
    "<div>Second</div></body></html>"
)


def convert(document: str, width: int | None = None) -> str:
    width = width or len(document) or 1
    return "".join(html_text(document[i : i + width] for i in range(0, len(document), width)))


def test_scripts_styles_and_head_are_dropped_and_entities_decoded():
    text = convert(NEWSLETTER)
    assert text == "Hello & welcome\nSecond"


@pytest.mark.parametrize(
    "document,expected",
    [
        ("<p>one</p><p>two</p>", "one\ntwo"),
        ("a<br>b<br/>c<BR />d", "a\nb\nc\nd"),
        ("<h1>Title</h1>text", "Title\ntext"),
        ("<table><tr><td>a</td><td>b</td></tr><tr><td>c</td></tr></table>", "ab\nc"),
        ("<p>x&nbsp;y</p>\n\n\n\n<p>z</p>", "x y\n\nz"),
        ("  <b>bold</b>   text  <br/>  next line \n  <br>", "bold   text\nnext line"),
        ("&#1049; &#x41; &copy; &amp", "Й A © &"),
        ("<!-- a comment > with a bracket --><p>after</p>", "after"),
        ("<!DOCTYPE html><?xml version='1.0'?>text", "text"),
        ("if a < b and c <= d", "if a < b and c <= d"),
        ("<header>kept</header><headline>kept too</headline>", "keptkept too"),
        ("<STYLE>x</STYLE><Script type=x>y</SCRIPT >Z", "Z"),
        ("one<script>x</script>two<style>y</style>three", "one two three"),
        ("<!-->shown<!--->also shown", "shownalso shown"),
    ],
)
def test_the_rules(document, expected):
    assert convert(document) == expected


@pytest.mark.parametrize("width", [1, 2, 3, 5, 7, 64])
@pytest.mark.parametrize(
    "document",
    [
        NEWSLETTER,
        "<p>A &amp; B &#1049;&nbsp;C</p><!--x--><br>D",
        "<p>Visible.</p><script>var x = 1;<p>After an unclosed script.</p>",
        "<!-->shown<!--->also shown",
    ],
)
def test_where_the_input_is_split_changes_nothing(document, width):
    assert convert(document, width) == convert(document)


def test_an_inline_image_of_any_size_is_skipped():
    """A data: URI is one long attribute; the tag must not have to fit in memory."""
    image = "A" * (3 * 1024 * 1024)
    document = f'<p>Before image.</p><img src="data:image/png;base64,{image}"><p>After image.</p>'
    assert convert(document, 49152) == "Before image.\nAfter image."


@pytest.mark.parametrize("element", ["script", "style", "head"])
def test_an_unclosed_element_does_not_hide_the_rest(element):
    document = f"<p>Start.</p><{element}>var x = 1;<p>Text after it.</p>"
    assert convert(document, 7) == "Start.\nvar x = 1;Text after it."


def test_body_ends_an_unclosed_head():
    document = "<html><head><title>T</title><style>p{}</style><body><p>Body text</p>"
    assert convert(document, 3) == "Body text"


def test_an_unclosed_comment_ends_at_its_first_bracket():
    assert convert("<p>a</p><!-- stray > b") == "a\nb"


def test_an_element_that_outgrows_the_limit_is_read_as_unclosed(monkeypatch):
    monkeypatch.setattr(module, "HIDDEN_LIMIT", 10)
    assert convert("<style>" + "x" * 20 + "</style>after", 4) == "x" * 20 + "after"


def test_a_lone_bracket_at_the_end_is_text():
    converter = HtmlText()
    assert converter.feed("a <") == "a"
    assert converter.close() == " <"


def test_a_tag_the_document_never_closes_is_dropped():
    assert convert('text<a href="x', 3) == "text"
