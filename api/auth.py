# === api/auth.py ===
"""
Google OAuth2 authentication + JWT session management.

Flow:
  1. GET  /auth/google      → redirect to Google consent screen
  2. GET  /auth/callback    → exchange code for tokens, set session cookie, redirect to /
  3. GET  /auth/logout      → clear session cookie, redirect to /
  4. GET  /auth/me          → return current user info (or 401)

All protected endpoints use Depends(get_current_user) which reads the JWT cookie.
When AUTH_ENABLED=false, get_current_user returns a synthetic "default_user" profile
so the app works without Google credentials configured (backward compatible).
"""

from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Cookie, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse

from config.settings import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Auth"])

# ── JWT helpers ───────────────────────────────────────────────────────────────

def _create_jwt(payload: Dict[str, Any]) -> str:
    """Create a signed JWT with HS256 using python-jose."""
    try:
        from jose import jwt
        now = int(time.time())
        payload = {
            **payload,
            "iat": now,
            "exp": now + settings.JWT_EXPIRE_HOURS * 3600,
        }
        return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    except ImportError:
        # Fallback: base64-encoded JSON signed with HMAC (no python-jose dependency)
        import base64
        import hashlib
        import hmac
        import json
        now = int(time.time())
        payload = {**payload, "iat": now, "exp": now + settings.JWT_EXPIRE_HOURS * 3600}
        body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        sig = hmac.new(settings.JWT_SECRET_KEY.encode(), body.encode(), hashlib.sha256).hexdigest()
        return f"{body}.{sig}"


def _verify_jwt(token: str) -> Optional[Dict[str, Any]]:
    """Verify and decode JWT. Returns payload dict or None on failure."""
    try:
        from jose import JWTError, jwt
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        if payload.get("exp", 0) < int(time.time()):
            return None
        return payload
    except Exception:
        pass

    # Fallback verifier
    try:
        import base64
        import hashlib
        import hmac
        import json
        parts = token.rsplit(".", 1)
        if len(parts) != 2:
            return None
        body, sig = parts
        expected = hmac.new(settings.JWT_SECRET_KEY.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not secrets.compare_digest(sig, expected):
            return None
        padding = 4 - len(body) % 4
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * padding))
        if payload.get("exp", 0) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


# ── FastAPI dependency ────────────────────────────────────────────────────────

_ANON_USER = {
    "user_id": "default_user",
    "email": "local@localhost",
    "name": "Local User",
    "picture": "",
}


async def get_current_user(
    session: Optional[str] = Cookie(default=None),
) -> Dict[str, Any]:
    """
    FastAPI dependency: extract and validate the current user from the session cookie.

    When AUTH_ENABLED=False → returns synthetic "default_user" (backward compatible).
    When AUTH_ENABLED=True  → raises 401 if cookie missing or token invalid.
    """
    if not settings.AUTH_ENABLED:
        return _ANON_USER

    if not session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated. Please sign in.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = _verify_jwt(session)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired or invalid. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload


async def get_current_user_optional(
    session: Optional[str] = Cookie(default=None),
) -> Optional[Dict[str, Any]]:
    """Like get_current_user but returns None instead of raising 401."""
    if not settings.AUTH_ENABLED:
        return _ANON_USER
    if not session:
        return None
    return _verify_jwt(session)


# ── OAuth2 endpoints ──────────────────────────────────────────────────────────

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

# In-memory CSRF state store (replace with Redis for multi-instance deployments)
_pending_states: Dict[str, Dict[str, Any]] = {}


def _redirect_uri() -> str:
    return f"{settings.APP_URL.rstrip('/')}/auth/callback"


@router.get("/google", include_in_schema=True, summary="Initiate Google OAuth2 login")
async def login_google() -> RedirectResponse:
    """Redirect user to Google consent screen."""
    if not settings.OAUTH_CLIENT_ID:
        raise HTTPException(
            status_code=501,
            detail=(
                "OAuth not configured. Set OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET "
                "in .env, then set AUTH_ENABLED=true."
            ),
        )

    state = secrets.token_urlsafe(32)
    _pending_states[state] = {"ts": time.time(), "mode": "login"}
    # Prune old states (> 10 min)
    cutoff = time.time() - 600
    expired = [k for k, v in _pending_states.items() if v.get("ts", 0) < cutoff]
    for k in expired:
        del _pending_states[k]

    params = {
        "client_id": settings.OAUTH_CLIENT_ID,
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        # Request calendar + gmail so each user gets their own integrations
        "scope": (
            "openid email profile "
            "https://www.googleapis.com/auth/calendar "
            "https://www.googleapis.com/auth/gmail.send"
        ),
        "state": state,
        "access_type": "offline",
        "prompt": "consent select_account",  # consent forces refresh_token to be issued
    }
    return RedirectResponse(f"{_GOOGLE_AUTH_URL}?{urlencode(params)}")


@router.get("/callback", include_in_schema=True, summary="OAuth2 callback handler")
async def auth_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
) -> RedirectResponse:
    """Exchange Google authorization code for a session JWT cookie."""
    is_link_state = bool(state and state in _pending_link_states)
    if error:
        logger.warning("OAuth2 error from Google: %s", error)
        return RedirectResponse(f"/?{'link_error' if is_link_state else 'auth_error'}={error}")

    if not code or not state:
        return RedirectResponse("/?auth_error=missing_params")

    # Validate CSRF state (supports both login and link-account flow)
    if state not in _pending_states and state not in _pending_link_states:
        return RedirectResponse("/?auth_error=invalid_state")
    is_link_flow = state in _pending_link_states
    link_user_id = None
    if is_link_flow:
        link_state = _pending_link_states.pop(state)
        link_user_id = link_state.get("user_id", "default_user")
    else:
        _pending_states.pop(state, None)

    # Exchange code for tokens
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            callback_uri = _link_redirect_uri() if is_link_flow else _redirect_uri()
            token_resp = await client.post(
                _GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": settings.OAUTH_CLIENT_ID,
                    "client_secret": settings.OAUTH_CLIENT_SECRET,
                    "redirect_uri": callback_uri,
                    "grant_type": "authorization_code",
                },
            )
            token_resp.raise_for_status()
            tokens = token_resp.json()

            userinfo_resp = await client.get(
                _GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {tokens['access_token']}"},
            )
            userinfo_resp.raise_for_status()
            user = userinfo_resp.json()
    except httpx.HTTPStatusError as exc:
        logger.error("OAuth token exchange failed: %s", exc.response.text)
        return RedirectResponse(f"/?{'link_error' if is_link_flow else 'auth_error'}=token_exchange_failed")
    except Exception as exc:
        logger.error("OAuth callback error: %s", exc)
        return RedirectResponse(f"/?{'link_error' if is_link_flow else 'auth_error'}=server_error")

    user_email = user.get("email", "")

    # Persist per-user OAuth tokens so calendar_tools can use each user's own calendar
    if user_email and tokens.get("access_token"):
        try:
            from google.cloud import firestore as _fs
            _db = _fs.AsyncClient()
            await _db.collection("user_oauth_tokens").document(user_email).set({
                "access_token":  tokens["access_token"],
                "refresh_token": tokens.get("refresh_token", ""),
                "token_expiry":  time.time() + tokens.get("expires_in", 3600),
                "email":         user_email,
                "updated_at":    time.time(),
            })
        except Exception as exc:
            logger.warning("Could not store OAuth tokens for %s: %s", user_email, exc)

    if is_link_flow:
        # Prevent linking the primary account as a secondary account
        primary_email = settings.GMAIL_USER_EMAIL or ""
        if user_email.lower() == primary_email.lower():
            return RedirectResponse("/?link_error=already_primary")
        try:
            from tools.firestore_tools import save_linked_gmail_account
            await save_linked_gmail_account(
                user_id=link_user_id or "default_user",
                email=user_email,
                name=user.get("name", user_email),
                picture=user.get("picture", ""),
                refresh_token=tokens.get("refresh_token", ""),
                access_token=tokens["access_token"],
                token_expiry=time.time() + tokens.get("expires_in", 3600),
            )
            logger.info("Linked account %s for user %s", user_email, link_user_id)
        except Exception as exc:
            logger.error("Failed to save linked account: %s", exc)
            return RedirectResponse("/?link_error=save_failed")
        return RedirectResponse("/?account_linked=1")

    # Build session JWT (primary sign-in flow)
    jwt_payload = {
        "sub":      user.get("sub", ""),
        "user_id":  user_email,
        "email":    user_email,
        "name":     user.get("name", ""),
        "picture":  user.get("picture", ""),
    }
    token = _create_jwt(jwt_payload)

    # Set httpOnly cookie and redirect to app
    response = RedirectResponse("/", status_code=302)
    is_prod = not settings.APP_URL.startswith("http://localhost")
    response.set_cookie(
        key="session",
        value=token,
        httponly=True,
        secure=is_prod,
        samesite="lax",
        max_age=settings.JWT_EXPIRE_HOURS * 3600,
        path="/",
    )
    logger.info("User signed in: %s", user_email)
    return response


# ── Secondary account linking ─────────────────────────────────────────────────

_pending_link_states: Dict[str, Dict] = {}  # state → {user_id, ts}


def _link_redirect_uri() -> str:
    # Reuse the primary callback URI to avoid Google OAuth redirect mismatch.
    return _redirect_uri()


@router.get("/link-account", summary="Link an additional Gmail account (calendar + email)")
async def link_account(
    user_id: str = "default_user",
    session: Optional[str] = Cookie(default=None),
) -> RedirectResponse:
    """Start a fresh Google consent screen to add a secondary Gmail account."""
    if not settings.OAUTH_CLIENT_ID:
        raise HTTPException(status_code=501, detail="OAuth not configured (OAUTH_CLIENT_ID missing).")

    # Prefer authenticated identity for linking so accounts do not get saved
    # under a stale/default user key when session exists.
    current_user = await get_current_user_optional(session=session)
    resolved_user_id = (current_user or {}).get("user_id") or user_id or "default_user"

    state = secrets.token_urlsafe(32)
    _pending_link_states[state] = {"user_id": resolved_user_id, "ts": time.time()}
    cutoff = time.time() - 600
    for k in list(_pending_link_states):
        if _pending_link_states[k]["ts"] < cutoff:
            del _pending_link_states[k]

    params = {
        "client_id": settings.OAUTH_CLIENT_ID,
        "redirect_uri": _link_redirect_uri(),
        "response_type": "code",
        "scope": (
            "openid email profile "
            "https://www.googleapis.com/auth/calendar "
            "https://www.googleapis.com/auth/gmail.send"
        ),
        "state": state,
        "access_type": "offline",
        # Force Google account chooser so user can enter/select any Gmail account.
        "prompt": "select_account consent",
    }
    return RedirectResponse(f"{_GOOGLE_AUTH_URL}?{urlencode(params)}")


@router.get("/link-account/callback", summary="OAuth2 callback for linked account")
async def link_account_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
) -> RedirectResponse:
    """Backward-compatible route; delegates to unified /auth/callback handler."""
    return await auth_callback(code=code, state=state, error=error)


@router.get("/linked-accounts", summary="List all linked Gmail accounts for the current user")
async def list_linked_accounts(
    session: Optional[str] = Cookie(default=None),
) -> Dict[str, Any]:
    """Return linked accounts stripped of sensitive token data."""
    user = await get_current_user_optional(session=session)
    user_id = user.get("user_id", "default_user") if user else "default_user"
    try:
        from tools.firestore_tools import get_linked_gmail_accounts
        accounts = await get_linked_gmail_accounts(user_id)
        # Backward compatibility: include legacy bucket if current user has none.
        if not accounts and user_id != "default_user":
            accounts = await get_linked_gmail_accounts("default_user")
        # Merge unique accounts across current + default buckets when both exist.
        if user_id != "default_user":
            try:
                legacy_accounts = await get_linked_gmail_accounts("default_user")
                by_email = {(a.get("email") or "").lower(): a for a in accounts if a.get("email")}
                for a in legacy_accounts:
                    e = (a.get("email") or "").lower()
                    if e and e not in by_email:
                        by_email[e] = a
                accounts = list(by_email.values())
            except Exception:
                pass
        safe = [
            {
                "email": a["email"],
                "name": a.get("name", a["email"]),
                "picture": a.get("picture", ""),
                "linked_at": a.get("linked_at", ""),
                "calendar_visible": a.get("calendar_visible", True),
                "email_send_enabled": a.get("email_send_enabled", True),
                "has_token": bool(a.get("refresh_token")),
            }
            for a in accounts
        ]
        return {"accounts": safe, "count": len(safe)}
    except Exception as exc:
        logger.error("list_linked_accounts error: %s", exc)
        return {"accounts": [], "count": 0}


@router.delete("/unlink-account/{email}", summary="Unlink a secondary Gmail account")
async def unlink_account(
    email: str,
    session: Optional[str] = Cookie(default=None),
) -> Dict[str, Any]:
    """Remove the OAuth tokens and unlink a secondary Gmail account."""
    user = await get_current_user_optional(session=session)
    user_id = user.get("user_id", "default_user") if user else "default_user"
    try:
        from tools.firestore_tools import delete_linked_gmail_account
        remaining = await delete_linked_gmail_account(user_id, email)
        return {"unlinked": email, "remaining_count": len(remaining)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.patch("/linked-account-prefs", summary="Toggle calendar/email visibility for a linked account")
async def update_linked_account_prefs_endpoint(
    request: Request,
    session: Optional[str] = Cookie(default=None),
) -> Dict[str, Any]:
    """Update calendar_visible and/or email_send_enabled for a linked account."""
    user = await get_current_user_optional(session=session)
    user_id = user.get("user_id", "default_user") if user else "default_user"
    body = await request.json()
    email = body.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="email is required")
    try:
        from tools.firestore_tools import update_linked_account_prefs
        updated = await update_linked_account_prefs(
            user_id=user_id,
            email=email,
            calendar_visible=body.get("calendar_visible"),
            email_send_enabled=body.get("email_send_enabled"),
        )
        return {"updated": email, "accounts": [{"email": a["email"], "calendar_visible": a.get("calendar_visible", True), "email_send_enabled": a.get("email_send_enabled", True)} for a in updated]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/logout", summary="Sign out and clear session")
async def logout() -> RedirectResponse:
    """Clear the session cookie."""
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie("session", path="/")
    return response


@router.get("/me", summary="Current user profile")
async def get_me(session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    """Return the current user's profile, or 401 if not signed in."""
    user = await get_current_user_optional(session=session)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {
        "authenticated": True,
        "user_id": user.get("user_id", "default_user"),
        "email": user.get("email", ""),
        "name": user.get("name", ""),
        "picture": user.get("picture", ""),
        "auth_enabled": settings.AUTH_ENABLED,
    }
