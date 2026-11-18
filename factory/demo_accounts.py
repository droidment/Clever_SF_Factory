"""Explicit demo identities; one shared password, server-assigned roles, signed cookies."""

import asyncio
import hashlib
import hmac
import json
import secrets
import time
from collections import defaultdict, deque

import jwt
from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, Field, SecretStr

COOKIE = "factory_demo_session"
USERS = {
    "admin": ("Admin", frozenset({"admin", "viewer", "requester", "approver", "operator"})),
    "creator": ("Ticket Creator", frozenset({"viewer", "requester"})),
    "approver": ("Approver", frozenset({"viewer", "approver"})),
    "viewer": ("Viewer", frozenset({"viewer"})),
}


def make_credentials(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000).hex()
    return {"salt": salt, "password_hash": digest, "signing_key": secrets.token_urlsafe(48)}


def credentials(config):
    return json.loads(config.demo_credentials.get_secret_value())


def session_user(request):
    token = request.cookies.get(COOKIE)
    if not token or not request.app.state.config.demo_login_enabled:
        return None
    try:
        claims = jwt.decode(token, credentials(request.app.state.config)["signing_key"],
                            algorithms=["HS256"], audience="factory-demo", issuer="factory",
                            options={"require": ["exp", "iat", "sub", "aud", "iss"]})
        if claims["sub"] not in USERS:
            raise ValueError("Unknown account")
        return claims["sub"]
    except (jwt.PyJWTError, ValueError, KeyError):
        raise HTTPException(401, "Session expired. Sign in again or sign out.") from None


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: SecretStr = Field(min_length=1, max_length=200)


def register_sessions(app, config):
    attempts = defaultdict(deque)

    @app.post("/api/session/login")
    async def login(body: Login, request: Request, response: Response):
        if not config.demo_login_enabled:
            raise HTTPException(404, "Demo accounts are not enabled")
        username = body.username.strip().lower()
        bucket = attempts[username if username in USERS else "unknown"]
        current = time.monotonic()
        while bucket and bucket[0] < current - 60:
            bucket.popleft()
        if len(bucket) >= 8:
            raise HTTPException(429, "Too many sign-in attempts. Try again in a minute.", headers={"Retry-After": "60"})
        bucket.append(current)
        secret = credentials(config)
        digest = await asyncio.to_thread(hashlib.pbkdf2_hmac, "sha256", body.password.get_secret_value().encode(),
                                         bytes.fromhex(secret["salt"]), 600000)
        if not hmac.compare_digest(digest.hex(), secret["password_hash"]) or username not in USERS:
            raise HTTPException(401, "Incorrect user ID or password")
        now = int(time.time())
        token = jwt.encode({"sub": username, "iss": "factory", "aud": "factory-demo", "iat": now, "exp": now + 8 * 3600},
                           secret["signing_key"], algorithm="HS256")
        response.set_cookie(COOKIE, token, httponly=True, secure=request.url.scheme == "https",
                            samesite="strict", max_age=8 * 3600, path="/")
        response.headers["Cache-Control"] = "no-store"
        return {"username": username, "role": USERS[username][0]}

    @app.post("/api/session/logout")
    def logout(response: Response):
        response.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict")
        response.headers["Cache-Control"] = "no-store"
        return {"signed_out": True}
