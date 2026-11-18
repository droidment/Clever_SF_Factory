import asyncio
from dataclasses import dataclass

import jwt
from fastapi import HTTPException, Request

from factory.demo_accounts import USERS, session_user


@dataclass(frozen=True)
class Principal:
    tenant: str
    subject: str
    roles: frozenset[str]
    repositories: frozenset[str]

    def require(self, role):
        if role not in self.roles and "admin" not in self.roles:
            raise HTTPException(403, f"Role required: {role}")


async def principal(request: Request) -> Principal:
    config = request.app.state.config
    username = session_user(request)
    if username:
        return Principal(config.google_tenant, f"demo:{username}", USERS[username][1], frozenset(config.repositories))
    if config.auth_mode == "demo":
        if request.client and request.client.host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
            raise HTTPException(403, "Demo authentication only accepts loopback connections")
        return Principal(
            "demo",
            "local-operator",
            frozenset({"viewer", "requester", "approver", "operator"}),
            frozenset(config.repositories),
        )
    auth = request.headers.get("Authorization", "")
    if config.auth_mode == "google":
        # Cloud Run consumes its IAM header. This separate header still requires
        # full Google signature/issuer/audience/email verification below.
        auth = request.headers.get("X-Factory-Authorization", auth)
        if not auth and config.public_view:
            return Principal(
                config.google_tenant,
                "Public visitor",
                frozenset({"viewer"}),
                frozenset(config.repositories),
            )
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Bearer token required", headers={"WWW-Authenticate": "Bearer"})
    token = auth[7:]
    try:
        signing_key = await asyncio.to_thread(request.app.state.jwks.get_signing_key_from_jwt, token)
        if config.auth_mode == "google":
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=config.google_audience,
                issuer=["https://accounts.google.com", "accounts.google.com"],
                options={"require": ["exp", "iat", "sub", "aud", "iss", "email", "email_verified"]},
            )
            allowed = {email.strip().lower() for email in config.google_allowed_emails.split(",")}
            if claims["email_verified"] is not True or not isinstance(claims["email"], str):
                raise ValueError("Verified Google email required")
            if claims["email"].lower() not in allowed:
                raise HTTPException(403, "Google account is not authorized for this workspace")
            return Principal(
                config.google_tenant,
                claims["email"],
                frozenset({"viewer", "requester", "approver", "operator"}),
                frozenset(config.repositories),
            )
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=config.oidc_audience,
            issuer=config.oidc_issuer,
            options={"require": ["exp", "iat", "sub", "tid", "aud", "iss"]},
        )
        if not isinstance(claims["tid"], str) or not claims["tid"] or len(claims["tid"]) > 100:
            raise ValueError("Invalid tenant claim")
        roles, repos = claims.get("roles", []), claims.get("repositories", [])
        if (
            not isinstance(roles, list)
            or not isinstance(repos, list)
            or not all(isinstance(x, str) for x in roles + repos)
        ):
            raise ValueError("Invalid entitlement claims")
        return Principal(claims["tid"], claims["sub"], frozenset(roles), frozenset(repos))
    except (jwt.PyJWTError, ValueError, KeyError):
        raise HTTPException(401, "Invalid or expired access token") from None
