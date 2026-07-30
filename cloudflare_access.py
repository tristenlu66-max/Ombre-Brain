"""Cloudflare Access JWT validation for the private TE House todo API."""

import json
import os
import time
from urllib.request import urlopen

import jwt

_CACHE_SECONDS = 3600
_jwks_cache = {"expires_at": 0.0, "keys": []}


def _team_domain() -> str:
    value = os.environ.get("CF_ACCESS_TEAM_DOMAIN", "").strip().rstrip("/")
    if value and not value.startswith("https://"):
        value = "https://" + value
    return value


def _allowed_emails() -> set[str]:
    return {
        email.strip().lower()
        for email in os.environ.get("CF_ACCESS_ALLOWED_EMAILS", "").split(",")
        if email.strip()
    }


def _jwks(team_domain: str) -> list[dict]:
    now = time.time()
    if _jwks_cache["keys"] and now < _jwks_cache["expires_at"]:
        return _jwks_cache["keys"]
    with urlopen(team_domain + "/cdn-cgi/access/certs", timeout=5) as response:
        keys = json.loads(response.read().decode("utf-8")).get("keys", [])
    _jwks_cache["keys"] = keys
    _jwks_cache["expires_at"] = now + _CACHE_SECONDS
    return keys


def verify_request(request) -> bool:
    """Return True only for a valid, allowed Cloudflare Access identity."""
    team_domain = _team_domain()
    audience = os.environ.get("CF_ACCESS_AUD", "").strip()
    token = request.headers.get("cf-access-jwt-assertion") or request.cookies.get("CF_Authorization")
    if not team_domain or not audience or not token:
        return False
    try:
        kid = jwt.get_unverified_header(token).get("kid")
        jwk = next(key for key in _jwks(team_domain) if key.get("kid") == kid)
        key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
        payload = jwt.decode(
            token,
            key=key,
            algorithms=["RS256"],
            audience=audience,
            issuer=team_domain,
        )
        allowed = _allowed_emails()
        return bool(payload.get("email")) and (not allowed or payload["email"].lower() in allowed)
    except Exception:
        return False
