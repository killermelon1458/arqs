from __future__ import annotations

import hashlib
import secrets
import string

LINK_ALPHABET = string.ascii_uppercase + string.digits


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def generate_api_key() -> str:
    return secrets.token_urlsafe(32)


def generate_link_code(length: int = 6) -> str:
    return "".join(secrets.choice(LINK_ALPHABET) for _ in range(length))
