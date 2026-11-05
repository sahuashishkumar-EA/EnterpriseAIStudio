"""Authentication modes.

  dev        trusted headers (local only): X-Dev-User, X-Dev-Roles ("bu:role,..."), X-Dev-Kind
  iap        Identity-Aware Proxy assertion (x-goog-iap-jwt-assertion, ES256) - console users
  google_id  Google-signed ID tokens (Authorization: Bearer) - service-to-service on Cloud Run
             and Pub/Sub / Cloud Tasks push
  oidc       any OIDC issuer (Authorization: Bearer) - end users calling the runtime API

Several modes can be enabled at once (EAP_AUTH_MODES="iap,google_id"). The authenticator only
establishes identity; authorization (per-BU roles) is decided by each service.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

import httpx
import jwt

IAP_JWKS = "https://www.gstatic.com/iap/verify/public_key-jwk"
GOOGLE_JWKS = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
IAP_ISSUER = "https://cloud.google.com/iap"


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


@dataclass
class Principal:
    sub: str
    email: str | None = None
    kind: str = "user"  # user | service
    mode: str = "dev"
    roles: list[str] = field(default_factory=list)  # "bu:role" from token claims / dev headers
    claims: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.email or self.sub

    def bu_roles(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for r in self.roles:
            bu, _, role = r.partition(":")
            if role:
                out.setdefault(bu, set()).add(role)
        return out


@dataclass
class AuthConfig:
    modes: list[str] = field(default_factory=lambda: ["dev"])
    iap_audience: str | None = None
    google_audiences: list[str] = field(default_factory=list)
    service_accounts: dict[str, str] = field(default_factory=dict)  # email -> logical name
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_roles_claim: str = "eap_roles"
    jwks_override: dict[str, dict[str, Any]] = field(default_factory=dict)  # url -> JWKS (tests)

    @staticmethod
    def from_env(prefix: str = "EAP_") -> "AuthConfig":
        e = os.environ
        sas = {}
        for item in filter(None, e.get(f"{prefix}SERVICE_ACCOUNTS", "").split(",")):
            name, _, email = item.partition("=")
            sas[email.strip()] = name.strip()
        return AuthConfig(
            modes=[m.strip() for m in e.get(f"{prefix}AUTH_MODES", "dev").split(",") if m.strip()],
            iap_audience=e.get(f"{prefix}IAP_AUDIENCE"),
            google_audiences=[a for a in e.get(f"{prefix}GOOGLE_AUDIENCES", "").split(",") if a],
            service_accounts=sas,
            oidc_issuer=e.get(f"{prefix}OIDC_ISSUER"),
            oidc_audience=e.get(f"{prefix}OIDC_AUDIENCE"),
            oidc_roles_claim=e.get(f"{prefix}OIDC_ROLES_CLAIM", "eap_roles"),
        )


class _JWKSCache:
    def __init__(self, overrides: dict[str, dict[str, Any]], ttl_s: int = 3600):
        self._overrides = overrides
        self._ttl = ttl_s
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    async def get(self, url: str) -> dict[str, Any]:
        if url in self._overrides:
            return self._overrides[url]
        hit = self._cache.get(url)
        if hit and time.time() - hit[0] < self._ttl:
            return hit[1]
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(url)
            r.raise_for_status()
            data = r.json()
        self._cache[url] = (time.time(), data)
        return data

    async def key_for(self, url: str, kid: str | None):
        jwks = await self.get(url)
        for k in jwks.get("keys", []):
            if kid is None or k.get("kid") == kid:
                return jwt.PyJWK(k).key
        # key rotation: refetch once
        self._cache.pop(url, None)
        jwks = await self.get(url)
        for k in jwks.get("keys", []):
            if k.get("kid") == kid:
                return jwt.PyJWK(k).key
        raise AuthError("signing key not found")


class Authenticator:
    def __init__(self, config: AuthConfig):
        self.config = config
        self._jwks = _JWKSCache(config.jwks_override)
        self._oidc_jwks_uri: str | None = None

    async def authenticate(self, headers: Mapping[str, str]) -> Principal:
        h = {k.lower(): v for k, v in headers.items()}
        modes = self.config.modes
        if "iap" in modes and "x-goog-iap-jwt-assertion" in h:
            return await self._iap(h["x-goog-iap-jwt-assertion"])
        auth = h.get("authorization", "")
        if auth.lower().startswith("bearer ") and ("google_id" in modes or "oidc" in modes):
            token = auth[7:].strip()
            try:
                iss = jwt.decode(token, options={"verify_signature": False}).get("iss")
            except jwt.PyJWTError as e:
                raise AuthError(f"malformed token: {e}") from e
            if iss in GOOGLE_ISSUERS and "google_id" in modes:
                return await self._google_id(token)
            if "oidc" in modes and self.config.oidc_issuer and iss == self.config.oidc_issuer:
                return await self._oidc(token)
            raise AuthError("token issuer not accepted")
        if "dev" in modes and "x-dev-user" in h:
            return self._dev(h)
        raise AuthError("authentication required")

    # ---------------------------------------------------------------------- modes

    def _dev(self, h: dict[str, str]) -> Principal:
        user = h["x-dev-user"]
        roles = [r.strip() for r in h.get("x-dev-roles", "").split(",") if r.strip()]
        kind = h.get("x-dev-kind", "service" if user in self.config.service_accounts else "user")
        return Principal(sub=user, email=user if "@" in user else None, kind=kind, mode="dev", roles=roles)

    async def _iap(self, token: str) -> Principal:
        header = jwt.get_unverified_header(token)
        key = await self._jwks.key_for(IAP_JWKS, header.get("kid"))
        try:
            claims = jwt.decode(
                token, key, algorithms=["ES256"], audience=self.config.iap_audience, issuer=IAP_ISSUER
            )
        except jwt.PyJWTError as e:
            aud = jwt.decode(token, options={"verify_signature": False}).get("aud")
            raise AuthError(f"invalid IAP assertion: {e} (token aud={aud!r}, expected {self.config.iap_audience!r})") from e
        return Principal(sub=claims["sub"], email=claims.get("email"), kind="user", mode="iap", claims=claims)

    async def _google_id(self, token: str) -> Principal:
        header = jwt.get_unverified_header(token)
        key = await self._jwks.key_for(GOOGLE_JWKS, header.get("kid"))
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self.config.google_audiences or None,
                options={"verify_aud": bool(self.config.google_audiences)},
            )
        except jwt.PyJWTError as e:
            raise AuthError(f"invalid Google ID token: {e}") from e
        if claims.get("iss") not in GOOGLE_ISSUERS:
            raise AuthError("bad issuer")
        email = claims.get("email")
        if not email or not claims.get("email_verified", False):
            raise AuthError("ID token has no verified email")
        is_sa = email.endswith(".gserviceaccount.com")
        return Principal(
            sub=claims["sub"],
            email=email,
            kind="service" if is_sa else "user",
            mode="google_id",
            claims=claims,
        )

    async def _oidc(self, token: str) -> Principal:
        cfg = self.config
        if self._oidc_jwks_uri is None:
            disc = await self._jwks.get(cfg.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration")
            self._oidc_jwks_uri = disc["jwks_uri"]
        header = jwt.get_unverified_header(token)
        key = await self._jwks.key_for(self._oidc_jwks_uri, header.get("kid"))
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256", "ES256"],
                audience=cfg.oidc_audience,
                issuer=cfg.oidc_issuer,
            )
        except jwt.PyJWTError as e:
            raise AuthError(f"invalid OIDC token: {e}") from e
        roles = claims.get(cfg.oidc_roles_claim) or []
        if isinstance(roles, str):
            roles = [r for r in roles.replace(" ", ",").split(",") if r]
        return Principal(
            sub=claims["sub"], email=claims.get("email"), kind="user", mode="oidc", roles=list(roles), claims=claims
        )

    def service_name(self, p: Principal) -> str | None:
        """Logical name ('governor', 'runtime', 'pubsub-push', ...) for a known service account."""
        if p.kind != "service":
            return None
        return self.config.service_accounts.get(p.id)


def dev_headers(user: str, roles: list[str] | None = None, kind: str | None = None) -> dict[str, str]:
    h = {"X-Dev-User": user}
    if roles:
        h["X-Dev-Roles"] = ",".join(roles)
    if kind:
        h["X-Dev-Kind"] = kind
    return h


def json_claims(p: Principal) -> str:
    return json.dumps({"sub": p.sub, "email": p.email, "kind": p.kind, "roles": p.roles})
