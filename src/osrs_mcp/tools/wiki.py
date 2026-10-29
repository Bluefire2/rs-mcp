"""search_wiki and get_wiki_page: game knowledge from the OSRS Wiki, as plain text.

Pages are converted from the Wiki's rendered HTML rather than its wikitext, because drop tables,
infoboxes and quest details are templates that only exist as tables once rendered.
"""

import re
from html import unescape
from html.parser import HTMLParser
from typing import Annotated

from fastmcp import FastMCP
from pydantic import BaseModel, Field

from osrs_mcp.clients.wiki import PageMissingError, Section, page_url
from osrs_mcp.errors import SourceError
from osrs_mcp.sources import Sources
from osrs_mcp.tools import READ_ONLY

WIKI_SOURCE = "OSRS Wiki (oldschool.runescape.wiki; cached up to 1 hour)"
# Keeps a reply's context small; a long section is cut at a line break.
MAX_CHARS = 6000

# Elements dropped with everything inside them.
SKIP_TAGS = frozenset({"script", "style", "noscript", "sup"})
SKIP_CLASSES = frozenset(
    {"navbox", "mw-editsection", "reference", "references", "reflist", "toc", "mw-empty-elt"}
)
VOID_TAGS = frozenset({"area", "br", "col", "embed", "hr", "img", "input", "link", "meta", "wbr"})
BLOCK_TAGS = frozenset(
    {"p", "div", "ul", "ol", "dl", "dd", "dt", "table", "caption", "blockquote", "center"}
)
HEADINGS = {"h2": "## ", "h3": "### ", "h4": "#### ", "h5": "#### ", "h6": "#### "}


class _TextExtractor(HTMLParser):
    """Rendered wiki HTML to plain text: headings as '## ', list items as '- ', and table rows
    as cells joined by ' | '."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._text: list[str] = []
        self._prefix = ""
        self._skip_depth = 0  # >0 while inside a skipped element
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in VOID_TAGS:
            if tag == "br" and not self._skip_depth:
                self._space()
            return
        if self._skip_depth:
            self._skip_depth += 1
            return
        classes = set((dict(attrs).get("class") or "").split())
        if tag in SKIP_TAGS or classes & SKIP_CLASSES:
            self._skip_depth = 1
            return
        if tag == "tr":
            self._flush()
            self._row = []
        elif tag in ("td", "th"):
            self._end_cell()
            self._cell = []
        elif tag == "li":
            self._flush()
            self._prefix = "- "
        elif tag in HEADINGS:
            self._flush()
            self._prefix = HEADINGS[tag]
        elif tag in BLOCK_TAGS and self._cell is None:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_TAGS:
            return
        if self._skip_depth:
            self._skip_depth -= 1
            return
        if tag in ("td", "th"):
            self._end_cell()
        elif tag == "tr":
            self._end_cell()
            row = self._row or []
            # Image-only columns (item icons) leave empty cells at the ends of a row.
            while row and not row[0]:
                row.pop(0)
            while row and not row[-1]:
                row.pop()
            if row:
                self.lines.append(" | ".join(row))
            self._row = None
        elif tag == "li" or tag in HEADINGS or (tag in BLOCK_TAGS and self._cell is None):
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        (self._cell if self._cell is not None else self._text).append(data)

    def close(self) -> None:
        super().close()
        self._flush()

    def _space(self) -> None:
        (self._cell if self._cell is not None else self._text).append(" ")

    def _end_cell(self) -> None:
        if self._cell is not None and self._row is not None:
            self._row.append(_squash("".join(self._cell)))
        self._cell = None

    def _flush(self) -> None:
        text = _squash("".join(self._text))
        if text:
            self.lines.append(self._prefix + text)
        self._text = []
        self._prefix = ""


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return "\n".join(parser.lines)


def strip_tags(html: str) -> str:
    return _squash(unescape(re.sub(r"<[^>]+>", "", html)))


def truncate(text: str, limit: int = MAX_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    cut = text.rfind("\n", 0, limit)
    return text[: cut if cut > limit // 2 else limit].rstrip(), True


class WikiSearchResult(BaseModel):
    title: str
    snippet: str
    url: str


class WikiSearch(BaseModel):
    query: str
    source: str
    results: list[WikiSearchResult]


class WikiPage(BaseModel):
    title: str
    url: str
    source: str
    section: str | None = Field(description="The section shown; null for the page's intro")
    text: str = Field(description="Plain text; table rows are cells joined by ' | '")
    truncated: bool = Field(description="True if text was cut short; ask for a section instead")
    sections: list[str] = Field(description="The page's section headings, for `section`")


def find_section(sections: list[Section], wanted: str) -> Section | None:
    """Exact heading (ignoring case), then the first heading that starts with or contains it."""
    w = wanted.strip().lower()
    names = [(s, strip_tags(s.line).lower()) for s in sections]
    for test in (lambda n: n == w, lambda n: n.startswith(w), lambda n: w in n):
        for section, name in names:
            if test(name):
                return section
    return None


async def search_wiki(sources: Sources, query: str, limit: int) -> WikiSearch:
    hits = await sources.wiki.search(query, limit)
    return WikiSearch(
        query=query,
        source=WIKI_SOURCE,
        results=[
            WikiSearchResult(title=h.title, snippet=strip_tags(h.snippet), url=page_url(h.title))
            for h in hits
        ],
    )


async def get_wiki_page(sources: Sources, title: str, section: str | None) -> WikiPage:
    try:
        resolved, sections = await sources.wiki.sections(title)
    except PageMissingError:
        hits = await sources.wiki.search(title, 5)
        suggestions = ", ".join(f"'{h.title}'" for h in hits)
        raise SourceError(
            f"No OSRS Wiki page is called '{title}'."
            + (f" Closest pages: {suggestions}." if suggestions else " Try search_wiki.")
        ) from None

    index, heading = "0", None
    if section:
        match = find_section(sections, section)
        if match is None:
            names = ", ".join(strip_tags(s.line) for s in sections)
            raise SourceError(f"'{resolved}' has no section '{section}'. Its sections: {names}.")
        index, heading = match.index, strip_tags(match.line)

    text, truncated = truncate(html_to_text(await sources.wiki.section_html(resolved, index)))
    return WikiPage(
        title=resolved,
        url=page_url(resolved),
        source=WIKI_SOURCE,
        section=heading,
        text=text,
        truncated=truncated,
        sections=[strip_tags(s.line) for s in sections],
    )


def register(mcp: FastMCP, sources: Sources) -> None:
    @mcp.tool(name="search_wiki", annotations=READ_ONLY)
    async def search_wiki_tool(
        query: Annotated[str, Field(min_length=1, description="Words to search for")],
        limit: Annotated[int, Field(ge=1, le=10, description="Max results")] = 5,
    ) -> WikiSearch:
        """Search the Old School RuneScape Wiki. Returns page titles with a snippet of matching
        text. Search for the thing itself ('Dragon warhammer', 'Song of the Elves'), not a whole
        question; then read the page with get_wiki_page."""
        return await search_wiki(sources, query, limit)

    @mcp.tool(name="get_wiki_page", annotations=READ_ONLY)
    async def get_wiki_page_tool(
        title: Annotated[str, Field(min_length=1, description="Page title; follows redirects")],
        section: Annotated[
            str | None,
            Field(description="A section heading, e.g. 'Drops', 'Item sources', 'Details'"),
        ] = None,
    ) -> WikiPage:
        """Read an Old School RuneScape Wiki page as plain text, for game knowledge: items,
        monsters and drop rates, quests and their requirements, skills and training methods.
        Without `section` returns the intro and infobox plus the list of section headings; pass
        a heading to read that section (drop tables are usually under 'Drops', quest
        requirements under 'Details', item sources under 'Item sources')."""
        return await get_wiki_page(sources, title, section)
