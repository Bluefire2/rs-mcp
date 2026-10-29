"""The OSRS Wiki's MediaWiki API (oldschool.runescape.wiki/api.php): search and page text.

The Wiki asks API users for a descriptive User-Agent (HttpClient sends one) and modest request
rates; pages are cached for an hour.
"""

from pydantic import BaseModel

from osrs_mcp.errors import SourceError
from osrs_mcp.http import CachedResponse, HttpClient

API = "https://oldschool.runescape.wiki/api.php"
PAGE_URL = "https://oldschool.runescape.wiki/w/"
TTL = 3600


class SearchHit(BaseModel):
    title: str
    snippet: str  # HTML, with <span class="searchmatch"> around matched words


class Section(BaseModel):
    index: str  # "1", "2", ... or "T-1" for a section from a transcluded page
    line: str  # the heading, as HTML
    level: str


class PageMissingError(SourceError):
    """No page with this title (after redirects)."""


def page_url(title: str) -> str:
    return PAGE_URL + title.replace(" ", "_")


class WikiClient:
    def __init__(self, http: HttpClient) -> None:
        self._http = http

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        data = await self._get(
            {
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": str(limit),
                "srnamespace": "0",
                "srprop": "snippet",
            }
        )
        return [SearchHit.model_validate(hit) for hit in data["query"]["search"]]

    async def sections(self, title: str) -> tuple[str, list[Section]]:
        """The page's title after redirects, and its sections."""
        data = await self._get({"action": "parse", "page": title, "prop": "sections"})
        parsed = data["parse"]
        return parsed["title"], [Section.model_validate(s) for s in parsed["sections"]]

    async def section_html(self, title: str, index: str) -> str:
        """Rendered HTML of one section and its subsections ("0" is the part before the first
        heading, including the infobox)."""
        data = await self._get(
            {
                "action": "parse",
                "page": title,
                "prop": "text",
                "section": index,
                "disableeditsection": "1",
            }
        )
        return data["parse"]["text"]

    async def _get(self, params: dict[str, str]) -> dict:
        query = params | {"format": "json", "formatversion": "2", "redirects": "1"}
        response: CachedResponse = await self._http.get(API, params=query, ttl=TTL)
        if response.status_code != 200:
            raise SourceError(f"The OSRS Wiki returned HTTP {response.status_code}; try again.")
        data = response.json()
        if "error" in data:
            if data["error"].get("code") == "missingtitle":
                raise PageMissingError(f"No OSRS Wiki page is called '{params.get('page')}'.")
            raise SourceError(f"The OSRS Wiki rejected the request: {data['error'].get('info')}")
        return data
