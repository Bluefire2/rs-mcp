"""One place that owns the shared HTTP client and every data-source client."""

from dataclasses import dataclass

from osrs_mcp.clients.hiscores import HiscoresClient
from osrs_mcp.clients.prices import PricesClient
from osrs_mcp.clients.wikisync import WikiSyncClient
from osrs_mcp.clients.wom import WomClient
from osrs_mcp.config import AccountType, Settings
from osrs_mcp.errors import SourceError
from osrs_mcp.http import HttpClient


@dataclass
class Sources:
    settings: Settings
    hiscores: HiscoresClient
    wikisync: WikiSyncClient
    wom: WomClient
    prices: PricesClient

    @classmethod
    def create(cls, settings: Settings, http: HttpClient | None = None) -> Sources:
        http = http or HttpClient(settings.full_user_agent)
        return cls(
            settings=settings,
            hiscores=HiscoresClient(http),
            wikisync=WikiSyncClient(http),
            wom=WomClient(http, settings.wom_api_key),
            prices=PricesClient(http),
        )

    def rsn(self, rsn: str | None) -> str:
        name = (rsn or self.settings.default_rsn).strip()
        if not name:
            raise SourceError(
                "No player name given and DEFAULT_RSN isn't set on the server. Pass rsn."
            )
        return name

    def account_type(self, account_type: AccountType | None) -> AccountType:
        return account_type or self.settings.default_account_type

    @property
    def is_ironman(self) -> bool:
        return self.settings.default_account_type in ("ironman", "hardcore_ironman", "ultimate")
