"""
Authentication module for GTA6-ZeroHour.
Enforces single-key access control for UI, video clips, and API endpoints.
Default key: "Javelloui*~*.Yrs->2004" (overridable via ACCESS_KEY env var).
"""

import hashlib
import hmac
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

STATIC_DIR = Path(__file__).resolve().parent / "static"
LOGIN_HTML_PATH = STATIC_DIR / "login.html"
INDEX_HTML_PATH = STATIC_DIR / "index.html"

# Default access key as specified by user
DEFAULT_KEY = "Javelloui*~*.Yrs->2004"
ACCESS_KEY = os.getenv("ACCESS_KEY", DEFAULT_KEY).strip() or DEFAULT_KEY

# Derive deterministic session token using HMAC-SHA256
TOKEN_SALT = b"gta6-zerohour-session-token-salt"
SESSION_TOKEN = hmac.new(TOKEN_SALT, ACCESS_KEY.encode("utf-8"), hashlib.sha256).hexdigest()
COOKIE_NAME = "gta6_session"

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    key: str


def is_authenticated(request: Request) -> bool:
    """Check if request is authenticated via cookie, X-Access-Key header, or Bearer token."""
    # 1. Cookie check
    cookie_val = request.cookies.get(COOKIE_NAME)
    if cookie_val and hmac.compare_digest(cookie_val, SESSION_TOKEN):
        return True

    # 2. X-Access-Key header check (raw key or session token)
    header_key = request.headers.get("x-access-key")
    if header_key:
        if hmac.compare_digest(header_key, ACCESS_KEY) or hmac.compare_digest(header_key, SESSION_TOKEN):
            return True

    # 3. Authorization Bearer header check
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if hmac.compare_digest(token, SESSION_TOKEN) or hmac.compare_digest(token, ACCESS_KEY):
            return True

    return False


@router.post("/login")
async def login(body: LoginRequest, response: Response):
    input_key = body.key.strip() if body.key else ""
    if not hmac.compare_digest(input_key, ACCESS_KEY):
        raise HTTPException(status_code=401, detail="Invalid Key. Access denied.")

    # Set HttpOnly session cookie valid across the whole domain
    response.set_cookie(
        key=COOKIE_NAME,
        value=SESSION_TOKEN,
        max_age=30 * 86400,  # 30 days
        httponly=True,
        samesite="lax",
        path="/",
    )
    return {"status": "ok", "token": SESSION_TOKEN}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"status": "ok", "message": "Logged out"}


@router.get("/status")
async def auth_status(request: Request):
    return {"authenticated": is_authenticated(request)}


def setup_auth(app):
    """Register HTTP auth middleware on the FastAPI app."""

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path

        # 1. Allow security middleware to handle forbidden hosts/origins first
        allowed_hosts = getattr(app.state, "allowed_hosts", None)
        if allowed_hosts and "*" not in allowed_hosts:
            host = request.headers.get("host")
            if host and (host not in allowed_hosts and host.split(":")[0] not in allowed_hosts):
                return await call_next(request)

        # 2. Whitelisted public endpoints & assets
        if (
            path in ("/api/auth/login", "/api/auth/status", "/login.html", "/favicon.ico")
            or path.startswith("/static/login.html")
        ):
            return await call_next(request)

        # 3. Root or /index.html delivery check
        if path in ("/", "/index.html"):
            if not is_authenticated(request):
                if LOGIN_HTML_PATH.exists():
                    return FileResponse(
                        str(LOGIN_HTML_PATH),
                        media_type="text/html; charset=utf-8",
                        status_code=200,
                    )
            return await call_next(request)

        # 4. Protected API and video routes
        if path.startswith(("/api/", "/clips/")):
            if not is_authenticated(request):
                return JSONResponse(
                    {"detail": "Authentication required. Key missing or invalid."},
                    status_code=401,
                )

        # 5. Fallback for unauthenticated static requests
        if not is_authenticated(request) and not path.endswith((".css", ".js", ".png", ".jpg", ".svg", ".woff2")):
            if LOGIN_HTML_PATH.exists():
                return FileResponse(
                    str(LOGIN_HTML_PATH),
                    media_type="text/html; charset=utf-8",
                    status_code=200,
                )

        return await call_next(request)
