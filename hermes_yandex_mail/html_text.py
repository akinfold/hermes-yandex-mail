"""HTML mail to readable text, fed a piece at a time.

The rules are the ones ``read_message`` has always applied to an HTML-only
message:

* ``<script>``, ``<style>`` and ``<head>`` elements are dropped with their
  content, and comments are removed;
* ``<br>`` and the ends of paragraphs, divisions, table rows and headings
  become line breaks, and every other tag is removed;
* character references are decoded, and a no-break space becomes a space;
* each line is stripped, runs of blank lines collapse to one, and the text as a
  whole is stripped.

Streaming changes only what has to be bounded. The result does not depend on
where the input is split — the tests check every split width — and nothing is
ever executed or fetched.

* A tag is skipped up to its ``>`` without being stored, so an inline
  ``data:`` image of any size costs nothing.
* A dropped element is held until its end tag. When the document ends first, or
  the element outgrows :data:`HIDDEN_LIMIT`, it was never closed, and its
  content is read as ordinary HTML instead of taking everything after it along.
  An unclosed comment ends at its first ``>``. A ``<body>`` tag ends an unclosed
  ``<head>``, as it does in a browser.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable, Iterator

__all__ = ["HIDDEN_LIMIT", "HtmlText", "html_text"]

#: The most of a dropped element held while waiting for its end tag.
HIDDEN_LIMIT = 1024 * 1024

_COMMENT = "!--"
_BREAKS = frozenset({"br", "/p", "/div", "/tr", "/h1", "/h2", "/h3", "/h4", "/h5", "/h6"})
_ENDS = {
    "script": re.compile(r"</script\s*>", re.IGNORECASE),
    "style": re.compile(r"</style\s*>", re.IGNORECASE),
    # The lookahead leaves <body> in place, to be removed like any other tag.
    "head": re.compile(r"</head\s*>|(?=<body[\s/>])", re.IGNORECASE),
    # "<!-->" and "<!--->" are complete, empty comments.
    _COMMENT: re.compile(r"\A-?>|-->"),
}
#: How far back to search again for an end tag that a split may have cut.
_END_OVERLAP = 256
_TAG_NAME = re.compile(r"/?[A-Za-z][A-Za-z0-9]*")
#: Enough of a tag to know its name, or that it opens a comment.
_TAG_HEAD = 32
#: A character reference that the next piece of input could still complete.
_OPEN_REFERENCE = re.compile(r"&#?[A-Za-z0-9]{0,32}\Z")
_LINE_TOKEN = re.compile(r"\n|[^\S\n]+|\S+")


class _Lines:
    """Strip every line, keep at most one blank line in a row, strip the whole."""

    def __init__(self) -> None:
        self._started = False
        self._on_line = False
        self._newlines = 0
        self._space = ""

    def feed(self, text: str) -> str:
        out: list[str] = []
        for match in _LINE_TOKEN.finditer(text):
            token = match.group()
            if token == "\n":
                self._newlines += 1
                self._on_line = False
                self._space = ""
            elif token[0].isspace():
                if self._on_line:
                    self._space += token
            else:
                if self._newlines and self._started:
                    out.append("\n" * min(self._newlines, 2))
                out.append(self._space)
                out.append(token)
                self._started = self._on_line = True
                self._newlines = 0
                self._space = ""
        return "".join(out)


class HtmlText:
    """Convert HTML to text incrementally: :meth:`feed` pieces, then :meth:`close`."""

    def __init__(self) -> None:
        self._input = ""
        self._tag: str | None = None
        self._hidden = ""
        self._held = ""
        self._searched = 0
        self._text = ""
        self._out: list[str] = []
        self._lines = _Lines()

    def feed(self, data: str) -> str:
        """Take the next piece of HTML; return the text it completes."""
        self._input += data
        return self._run(final=False)

    def close(self) -> str:
        """Finish the document; return the text still held back."""
        return self._run(final=True)

    def _run(self, final: bool) -> str:
        while self._step(final):
            pass
        self._flush_text(final)
        out, self._out = "".join(self._out), []
        return out

    def _step(self, final: bool) -> bool:
        """Consume what the current state can; say whether to go on."""
        if self._hidden:
            return self._step_hidden(final)
        if self._tag is not None:
            return self._step_tag(final)
        return self._step_text(final)

    def _step_text(self, final: bool) -> bool:
        data = self._input
        start = data.find("<")
        if start < 0:
            self._text += data
            self._input = ""
            return False
        self._text += data[:start]
        rest = data[start + 1 :]
        if not rest:
            # One more character decides whether this "<" opens a tag.
            if final:
                self._text += "<"
                self._input = ""
            else:
                self._input = data[start:]
            return False
        if rest[0].isascii() and (rest[0].isalpha() or rest[0] in "/!?"):
            self._flush_text(final=True)
            self._tag = ""
        else:
            self._text += "<"
        self._input = rest
        return True

    def _step_tag(self, final: bool) -> bool:
        data = self._input
        end = data.find(">")
        seen = self._tag or ""
        head = (seen + (data if end < 0 else data[:end]))[:_TAG_HEAD]
        if head.startswith(_COMMENT):
            self._tag = None
            self._hide(_COMMENT)
            self._input = data[len(_COMMENT) - len(seen) :]
            return True
        if end < 0:
            # A tag the document never closes is dropped with it.
            self._tag = None if final else head
            self._input = ""
            return False
        self._tag = None
        self._input = data[end + 1 :]
        name = _TAG_NAME.match(head)
        tag = name.group().lower() if name else ""
        if tag in _BREAKS:
            self._text += "\n"
        elif tag in _ENDS:
            self._hide(tag)
        return True

    def _hide(self, element: str) -> None:
        self._hidden = element
        self._held = ""
        self._searched = 0

    def _step_hidden(self, final: bool) -> bool:
        self._held += self._input
        self._input = ""
        found = _ENDS[self._hidden].search(self._held, max(0, self._searched - _END_OVERLAP))
        if found:
            if self._hidden != _COMMENT:
                self._text += " "
            self._input = self._held[found.end() :]
        elif final or len(self._held) > HIDDEN_LIMIT:
            # Never closed: read what it held as ordinary HTML.
            self._input = self._held
            if self._hidden == _COMMENT:
                self._input = self._held[self._held.find(">") + 1 :]
        else:
            self._searched = len(self._held)
            return False
        self._hidden = self._held = ""
        return True

    def _flush_text(self, final: bool) -> None:
        text = self._text
        cut = len(text)
        if not final:
            reference = _OPEN_REFERENCE.search(text)
            if reference:
                cut = reference.start()
        self._text = text[cut:]
        if cut:
            decoded = html.unescape(text[:cut]).replace("\xa0", " ")
            self._out.append(self._lines.feed(decoded))


def html_text(chunks: Iterable[str]) -> Iterator[str]:
    """The readable text of an HTML document that arrives in pieces."""
    converter = HtmlText()
    for chunk in chunks:
        yield converter.feed(chunk)
    yield converter.close()
