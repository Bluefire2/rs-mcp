"""Print fresh values for JWT_SIGNING_KEY and STORAGE_ENCRYPTION_KEY.

python -m uv run python -m osrs_mcp.keys >> .env
"""

import secrets

from cryptography.fernet import Fernet

if __name__ == "__main__":
    print(f"JWT_SIGNING_KEY={secrets.token_urlsafe(48)}")
    print(f"STORAGE_ENCRYPTION_KEY={Fernet.generate_key().decode()}")
