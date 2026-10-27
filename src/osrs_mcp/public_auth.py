"""Static bearer tokens for the public MCP endpoint (PUBLIC_API_TOKENS).

The public endpoint serves bots such as a Discord bot, which can't do the browser sign-in the
owner's endpoint needs. Each consumer gets its own long random token, so one can be revoked
without touching the others. A bare TokenVerifier serves no OAuth metadata, so a 401 from the
public endpoint doesn't send clients to the Google sign-in.
"""

import hashlib
import hmac

from fastmcp.server.auth import AccessToken, TokenVerifier


class StaticBearerVerifier(TokenVerifier):
    def __init__(self, tokens: list[str]) -> None:
        super().__init__()
        self._tokens = [token.encode() for token in tokens if token]

    async def verify_token(self, token: str) -> AccessToken | None:
        presented = token.encode()
        # Compare against every token, so the time taken doesn't reveal which one nearly matched.
        matched = None
        for candidate in self._tokens:
            if hmac.compare_digest(presented, candidate):
                matched = candidate
        if matched is None:
            return None
        return AccessToken(token=token, client_id=token_id(matched), scopes=[])


def token_id(token: bytes) -> str:
    """A stable, non-secret name for a token, for logs: the start of its SHA-256."""
    return "public-" + hashlib.sha256(token).hexdigest()[:8]
