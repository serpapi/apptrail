from __future__ import annotations

import hashlib
import ipaddress
import os
import secrets
import threading
from pathlib import Path
from unicodedata import category

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import delete, text
from starlette.concurrency import run_in_threadpool

from .db import AuthLimit, LoginSession, Owner, now

SESSION_SECONDS = 24 * 3600
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
PUBLIC_API = {"/api/auth/status", "/api/auth/setup", "/api/auth/login", "/api/auth/restore"}
PUBLIC_FILES = {
    "/login",
    "/healthz",
    "/static/auth.js",
    "/static/loading.js",
    "/static/auth.css",
    "/static/style.css",
    "/static/favicon.svg",
    "/static/vendor/lucide-icons.js",
}
# Limit simultaneous password hashing to bound memory use under login floods.
HASH_SLOTS = threading.BoundedSemaphore(2)
HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=1)
DUMMY_HASH = HASHER.hash(secrets.token_urlsafe(32))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def hash_password(password):
    with HASH_SLOTS:
        return HASHER.hash(password)


def matches(encoded, password):
    with HASH_SLOTS:
        try:
            return HASHER.verify(encoded, password)
        except (VerificationError, InvalidHashError):
            return False


def valid_new_password(password):
    if not 8 <= len(password) <= 128:
        raise HTTPException(422, "Use a password with 8 to 128 characters.")
    if not any("0" <= char <= "9" for char in password) or not any(
        category(char)[0] in {"P", "S"} for char in password
    ):
        raise HTTPException(422, "Include at least one number and one special character.")


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=1, max_length=128)


class SetupInput(LoginInput):
    username: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$")
    setup_token: SecretStr = Field(min_length=1, max_length=200)


class PasswordInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: SecretStr = Field(min_length=1, max_length=128)
    new_password: SecretStr = Field(min_length=8, max_length=128)


class Auth:
    def __init__(self, db, config):
        self.db, self.config = db, config
        self.process_id = secrets.token_hex(32)
        self.setup_path = config.directory / "setup-token"
        self.cookie = "apptrail_session_" + digest(str(config.directory))[:12]
        with FileLock(config.directory / "setup.lock"):
            if not self.has_owner():
                if not self.setup_path.exists():
                    fd = os.open(self.setup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w") as handle:
                        handle.write(secrets.token_urlsafe(32))
                self.setup_path.chmod(0o600)
            else:
                self.setup_path.unlink(missing_ok=True)

    def has_owner(self):
        with self.db.session() as session:
            return session.get(Owner, 1) is not None

    def setup_token(self):
        return self.setup_path.read_text().strip() if self.setup_path.exists() else ""

    def require_setup_token(self, supplied):
        expected = self.setup_token()
        if not expected or not secrets.compare_digest(expected.encode(), supplied.encode()):
            raise HTTPException(
                403, "The setup code is incorrect. Use the code from the server terminal."
            )

    def authorize_setup_restore(self, request, *, throttle=True):
        if throttle:
            self.limit(request)
        if self.has_owner():
            raise HTTPException(
                409, "An account already exists. Sign in and restore from Settings."
            )
        self.require_setup_token(request.headers.get("x-apptrail-setup-token", ""))

    def limit(self, request):
        ip = request.client.host if request.client else "unknown"
        try:
            address = ipaddress.ip_address(ip)
            if isinstance(address, ipaddress.IPv6Address):
                ip = str(address.ipv4_mapped or ipaddress.ip_network(f"{address}/64", strict=False))
            else:
                ip = str(address)
        except ValueError:
            pass
        current = now()
        blocked = False
        with self.db.session.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            session.execute(delete(AuthLimit).where(AuthLimit.started_at < current - 3600))
            for key, maximum, window in [("ip:" + digest(ip), 10, 300), ("global", 100, 60)]:
                item = session.get(AuthLimit, key)
                if not item:
                    item = AuthLimit(key=key, started_at=current, attempts=0)
                    session.add(item)
                elif current - item.started_at >= window:
                    item.started_at, item.attempts = current, 0
                item.attempts += 1
                if item.attempts > maximum:
                    blocked = True
                    break
        if blocked:
            raise HTTPException(
                429,
                "Too many attempts. Wait a few minutes and try again.",
                headers={"Retry-After": "300"},
            )

    def origin(self, request):
        return f"{request.url.scheme}://{request.headers.get('host', '')}"

    def cookie_name(self, request):
        prefix = "__Host-" if request.url.scheme == "https" else ""
        return prefix + self.cookie

    def identify(self, request):
        token = request.cookies.get(self.cookie_name(request), "")
        if not token or len(token) > 128:
            return None
        with self.db.session.begin() as session:
            item = session.get(LoginSession, digest(token))
            if not item or item.origin != self.origin(request) or item.expires_at <= now():
                return None
            owner = session.get(Owner, 1)
            if not owner:
                return None
            if now() - item.last_seen > 60:
                item.last_seen = now()
            return {
                "username": owner.username,
                "csrf_token": digest("csrf:" + token),
                "token_hash": item.token_hash,
            }

    def issue(self, request, session, username):
        token = secrets.token_urlsafe(32)
        current = now()
        cookie = self.cookie_name(request)
        # A fresh login replaces this browser's old session, never promotes a supplied cookie.
        session.execute(
            delete(LoginSession).where(
                (LoginSession.token_hash == digest(request.cookies.get(cookie, "")))
                | (LoginSession.expires_at <= current)
            )
        )
        session.add(
            LoginSession(
                token_hash=digest(token),
                process_id=self.process_id,
                origin=self.origin(request),
                expires_at=current + SESSION_SECONDS,
                last_seen=current,
            )
        )
        response = JSONResponse(
            {"authenticated": True, "username": username, "csrf_token": digest("csrf:" + token)}
        )
        response.set_cookie(
            cookie,
            token,
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            path="/",
        )
        return response

    def reset_password(self, password):
        valid_new_password(password)
        encoded = hash_password(password)
        with self.db.session.begin() as session:
            owner = session.get(Owner, 1)
            if not owner:
                raise ValueError("No account exists yet. Create it in the web interface.")
            owner.password_hash = encoded
            session.execute(delete(LoginSession))
            session.execute(delete(AuthLimit))
            return owner.username

    async def guard(self, request, call_next):
        path = request.url.path
        if path.startswith("/api/") and request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse(
                {"detail": "Cross-site browser requests are not allowed."}, status_code=403
            )
        request.state.auth = await run_in_threadpool(self.identify, request)
        public = path in PUBLIC_API or path in PUBLIC_FILES
        if not public and not request.state.auth:
            if path == "/":
                return RedirectResponse("/login", status_code=303)
            return JSONResponse({"detail": "Sign in to AppTrail."}, status_code=401)
        if request.method not in SAFE_METHODS:
            content_type = request.headers.get("content-type", "").split(";")[0]
            allowed_type = content_type == "application/json" or (
                path in {"/api/restore", "/api/auth/restore"}
                and content_type == "application/vnd.sqlite3"
            )
            if request.headers.get("x-apptrail-request") != "1" or not allowed_type:
                return JSONResponse(
                    {"detail": "Use the AppTrail interface or authenticated API requests."},
                    status_code=403,
                )
            if path not in {"/api/auth/setup", "/api/auth/login", "/api/auth/restore"}:
                supplied = request.headers.get("x-csrf-token", "")
                expected = (request.state.auth or {}).get("csrf_token", "")
                if not expected or not secrets.compare_digest(supplied.encode(), expected.encode()):
                    return JSONResponse(
                        {
                            "detail": "Your security token is missing or expired. Reload and try again."
                        },
                        status_code=403,
                    )
        return await call_next(request)

    def routes(self, app, static: Path):
        @app.get("/login")
        def login_page(request: Request):
            from fastapi.responses import FileResponse

            if request.state.auth:
                return RedirectResponse("/", status_code=303)
            return FileResponse(static / "auth.html")

        @app.get("/api/auth/status")
        def status(request: Request):
            identity = request.state.auth
            return {
                "setup_required": not self.has_owner(),
                "authenticated": bool(identity),
                **(
                    {"username": identity["username"], "csrf_token": identity["csrf_token"]}
                    if identity
                    else {}
                ),
            }

        @app.post("/api/auth/setup")
        def setup(payload: SetupInput, request: Request):
            self.limit(request)
            valid_new_password(payload.password.get_secret_value())
            with self.db.session.begin() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                if session.get(Owner, 1):
                    raise HTTPException(409, "An account already exists. Sign in instead.")
                self.require_setup_token(payload.setup_token.get_secret_value())
                owner = Owner(
                    id=1,
                    username=payload.username.lower(),
                    password_hash=hash_password(payload.password.get_secret_value()),
                )
                session.add(owner)
                response = self.issue(request, session, owner.username)
            self.setup_path.unlink(missing_ok=True)
            return response

        @app.post("/api/auth/login")
        def login(payload: LoginInput, request: Request):
            self.limit(request)
            with self.db.session() as session:
                owner = session.get(Owner, 1)
            password = payload.password.get_secret_value()
            valid = matches(owner.password_hash if owner else DUMMY_HASH, password)
            if not owner or not valid or payload.username.lower() != owner.username:
                raise HTTPException(401, "Incorrect username or password.")
            with self.db.session.begin() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                current = session.get(Owner, 1)
                if current.password_hash != owner.password_hash:
                    raise HTTPException(401, "Credentials changed. Sign in again.")
                if HASHER.check_needs_rehash(current.password_hash):
                    current.password_hash = hash_password(password)
                return self.issue(request, session, owner.username)

        @app.post("/api/auth/logout")
        def logout(request: Request):
            with self.db.session.begin() as session:
                session.execute(
                    delete(LoginSession).where(
                        LoginSession.token_hash == request.state.auth["token_hash"]
                    )
                )
            response = JSONResponse({"ok": True})
            response.delete_cookie(
                self.cookie_name(request),
                path="/",
                secure=request.url.scheme == "https",
                httponly=True,
                samesite="strict",
            )
            return response

        @app.post("/api/auth/password")
        def change_password(payload: PasswordInput, request: Request):
            self.limit(request)
            valid_new_password(payload.new_password.get_secret_value())
            with self.db.session.begin() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                owner = session.get(Owner, 1)
                if not matches(owner.password_hash, payload.current_password.get_secret_value()):
                    raise HTTPException(401, "Current password is incorrect.")
                owner.password_hash = hash_password(payload.new_password.get_secret_value())
                session.execute(delete(LoginSession))
                return self.issue(request, session, owner.username)
