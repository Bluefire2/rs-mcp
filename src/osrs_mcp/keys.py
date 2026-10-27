"""Print fresh values for JWT_SIGNING_KEY, STORAGE_ENCRYPTION_KEY, INGEST_TOKEN and
PUBLIC_API_TOKENS.

python -m uv run python -m osrs_mcp.keys >> .env
"""

import secrets

from cryptography.fernet import Fernet

if __name__ == "__main__":
    print(f"JWT_SIGNING_KEY={secrets.token_urlsafe(48)}")
    print(f"STORAGE_ENCRYPTION_KEY={Fernet.generate_key().decode()}")
    # Pasted into the RuneLite plugin's "Group Token" field. URL-safe, and never starts with
    # "Bearer ", so the OAuth middleware ignores it rather than trying to verify it with Google.
    print(f"INGEST_TOKEN={secrets.token_urlsafe(32)}")
    # Bearer token for the public endpoint (<PUBLIC_BASE_URL>/public/mcp), given to a bot.
    # Comma-separate several to give each consumer its own.
    print(f"PUBLIC_API_TOKENS={secrets.token_urlsafe(32)}")
