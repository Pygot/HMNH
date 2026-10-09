# src/agent/html_text.py
from html.parser import HTMLParser

SKIPPED_TAGS = frozenset(
    {"script", "style", "noscript", "template", "svg", "iframe", "object", "canvas", "nav"}
)
BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dt",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "p",
        "section",
        "td",
        "th",
        "tr",
    }
)
DESCRIPTION_KEYS = frozenset({"description", "og:description"})


class _Extractor(HTMLParser):
    """An HTML parser that collects the title, meta descriptions and visible text.

    Text inside non-content tags such as script, style and nav is left out.
    """

    def __init__(self) -> None:
        """Initialize the parser with empty collectors and no skipped section open."""
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.descriptions: list[str] = []
        self.parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Handle an opening tag by updating the skip state or collecting metadata.

        Block tags add a line break to the text, and meta description tags are recorded.

        Args:
            tag: the tag name.
            attrs: the attribute name and value pairs.
        """
        if tag in SKIPPED_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            fields = dict(attrs)
            key = (fields.get("name") or fields.get("property") or "").lower()
            if key in DESCRIPTION_KEYS and fields.get("content"):
                self.descriptions.append(fields["content"] or "")
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        """Handle a closing tag by leaving a skipped section or ending the title.

        Args:
            tag: the tag name.
        """
        if tag in SKIPPED_TAGS:
            # Stray closing tags must not push the depth below zero and hide the rest of the page.
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        """Collect text into the title or the body unless inside a skipped section.

        Args:
            data: the text found between tags.
        """
        if self._in_title:
            self.title_parts.append(data)
        elif not self._skip_depth:
            self.parts.append(data)


def extract_text(markup: str, max_chars: int) -> tuple[str, str]:
    """Extract the title and readable text from HTML markup.

    Args:
        markup: the HTML source.
        max_chars: largest length of the returned text. The title is not cut.

    Returns:
        A pair of the title and the text. The text starts with the meta description,
        continues with one line per non-empty block, has whitespace collapsed and is
        cut to max_chars.
    """
    extractor = _Extractor()
    extractor.feed(markup)
    extractor.close()
    title = " ".join("".join(extractor.title_parts).split())
    body_lines = ("".join(extractor.parts)).splitlines()
    lines = [" ".join(line.split()) for line in body_lines]
    description = " ".join(" ".join(extractor.descriptions).split())
    text = "\n".join(line for line in [description, *lines] if line)
    return title, text[:max_chars]
