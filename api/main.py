# === api/main.py ===
"""
FastAPI application for Smart Daily Planner.

Endpoints:
  POST /query                 — text query to orchestrator
  POST /query/voice           — audio file query to orchestrator
  POST /ingest                — multipart image/PDF extraction
  GET  /tasks                 — list tasks
  POST /tasks                 — create task
  GET  /events                — list calendar events
  POST /events                — create calendar event
  GET  /notes                 — list notes
  POST /notes                 — create note
  GET  /analytics             — productivity analytics
  POST /undo                  — undo last action
  POST /undo/multiple         — undo last N actions
  POST /briefing              — trigger morning briefing
  POST /smart-reschedule      — AI rescue plan for overdue tasks
  POST /summarize-meeting     — extract action items from transcript
  POST /weekly-retro          — AI weekly retrospective
  POST /suggest/tags          — AI tag suggestions
  POST /suggest/priorities    — AI priority recommendations
  GET  /risk-analysis         — deadline risk analysis
  GET  /focus-plan            — today's AI focus order
  POST /demo/multi-step       — multi-intent demo
  GET  /health                — health check
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import logging.config
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from config.settings import settings, LOCAL_TZ
from api.auth import get_current_user, get_current_user_optional, router as auth_router

# Make GOOGLE_API_KEY available to ADK and google-genai via os.environ.
# pydantic-settings reads .env into `settings` but does NOT write to os.environ.
if settings.GOOGLE_API_KEY:
    os.environ["GOOGLE_API_KEY"] = settings.GOOGLE_API_KEY

# ── Structured logging ────────────────────────────────────────────────────────
logging.config.dictConfig({
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "logging.Formatter",
            "fmt": '{"time":"%(asctime)s","level":"%(levelname)s","name":"%(name)s","msg":%(message)r}',
            "datefmt": "%Y-%m-%dT%H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
        },
    },
    "root": {"level": settings.LOG_LEVEL, "handlers": ["console"]},
    "loggers": {
        "uvicorn": {"level": "INFO", "propagate": False, "handlers": ["console"]},
        "uvicorn.access": {"level": "WARNING", "propagate": False, "handlers": ["console"]},
    },
})
logger = logging.getLogger(__name__)

# ── Demo mode flag ─────────────────────────────────────────────────────────────
# Set DEMO_MODE=true to run with zero Google Cloud config.
# All API calls return realistic mock data so the full UI is functional.
DEMO_MODE: bool = os.environ.get("DEMO_MODE", "false").lower() == "true"

# In-memory per-user event log used by demo-mode undo (no-op in production).
_demo_events: Dict[str, List[Dict[str, Any]]] = {}
_events_cache: Dict[str, Dict[str, Any]] = {}
_events_cache_ttl_seconds = 20
_insights_cache: Dict[str, Dict[str, Any]] = {}
_insights_cache_ttl_seconds = 60


def _legacy_user_ids(user_id: str) -> List[str]:
    """Read current user data first, then legacy default_user data."""
    ids = [user_id]
    if user_id and user_id != "default_user":
        ids.append("default_user")
    return ids


def _resolved_user_id(current_user: Optional[Dict[str, Any]]) -> str:
    """Resolve user id from optional auth context with safe fallback."""
    return (current_user or {}).get("user_id", "default_user")


def _events_cache_key(
    user_id: str,
    time_min: Optional[str],
    time_max: Optional[str],
    max_results: int,
) -> str:
    return f"{user_id}|{time_min or ''}|{time_max or ''}|{max_results}"


def _invalidate_events_cache(user_id: Optional[str] = None) -> None:
    if not user_id:
        _events_cache.clear()
        return
    prefix = f"{user_id}|"
    for key in [k for k in _events_cache.keys() if k.startswith(prefix)]:
        _events_cache.pop(key, None)


def _invalidate_insights_cache(user_id: Optional[str] = None) -> None:
    if not user_id:
        _insights_cache.clear()
        return
    _insights_cache.pop(user_id, None)


def _heuristic_tags(content: str) -> List[str]:
    text = (content or "").lower()
    tags: List[str] = []
    if any(w in text for w in ("q1", "q2", "q3", "q4", "quarter", "financial", "finance", "revenue", "audit", "compliance")):
        tags.extend(["finance", "quarterly", "reporting"])
    if any(w in text for w in ("meeting", "call", "sync", "standup", "minutes")):
        tags.extend(["meeting", "discussion"])
    if any(w in text for w in ("urgent", "asap", "today", "blocker")):
        tags.append("urgent")
    if any(w in text for w in ("review", "approve", "sign-off")):
        tags.append("review")
    if not tags:
        tags = ["work", "task"]
    deduped = []
    for t in tags:
        if t not in deduped:
            deduped.append(t)
    return deduped[:5]


def _extract_action_items_from_text(transcript: str) -> List[str]:
    items: List[str] = []
    if not transcript:
        return items
    lines = [ln.strip(" -•\t") for ln in transcript.replace("\r", "\n").split("\n") if ln.strip()]
    action_verbs = ("will ", "shall ", "must ", "to ", "needs to ", "need to ", "action:")
    for ln in lines:
        low = ln.lower()
        if any(v in low for v in action_verbs) and len(ln) > 10:
            items.append(ln.rstrip("."))
    if not items:
        # fallback sentence split for plain paragraphs
        for sent in re.split(r"(?<=[.!?])\s+", transcript):
            s = sent.strip()
            low = s.lower()
            if any(v in low for v in action_verbs) and len(s) > 12:
                items.append(s.rstrip("."))
    return items[:8]


def _scheduler_job_id(user_id: str) -> str:
    safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in (user_id or "default-user"))
    while "--" in safe:
        safe = safe.replace("--", "-")
    safe = safe.strip("-") or "default-user"
    return f"daily-briefing-{safe}"[:450]


def _scheduler_job_id_typed(user_id: str, schedule_type: str = "morning") -> str:
    base = _scheduler_job_id(user_id)
    suffix = "day-end" if schedule_type == "day_end" else "morning"
    return f"{base}-{suffix}"[:450]


def _scheduler_job_name(user_id: str, schedule_type: str = "morning") -> str:
    return (
        f"projects/{settings.GCP_PROJECT_ID}/locations/{settings.CLOUD_SCHEDULER_REGION}"
        f"/jobs/{_scheduler_job_id_typed(user_id, schedule_type)}"
    )


def _briefing_target_url(user_id: str, schedule_type: str = "morning") -> str:
    base_url = (settings.APP_URL or "").rstrip("/")
    endpoint = "/briefing/day-end" if schedule_type == "day_end" else "/briefing/scheduled"
    return f"{base_url}{endpoint}?user_id={quote(user_id)}"


def _build_scheduler_job_body(
    user_id: str,
    recipient_email: str,
    sender_email: str,
    hour: int,
    minute: int,
    timezone: str,
    schedule_type: str = "morning",
) -> Dict[str, Any]:
    payload = {
        "user_id": user_id,
        "recipient_email": recipient_email,
        "sender_email": sender_email,
    }
    headers = {"Content-Type": "application/json"}
    if settings.SCHEDULER_SHARED_SECRET:
        headers["X-Scheduler-Secret"] = settings.SCHEDULER_SHARED_SECRET
    return {
        "name": _scheduler_job_name(user_id, schedule_type),
        "description": "Smart Daily Planner user-configured daily briefing" if schedule_type == "morning" else "Smart Daily Planner user-configured day-end summary",
        "schedule": f"{minute} {hour} * * *",
        "timeZone": timezone,
        "httpTarget": {
            "uri": _briefing_target_url(user_id, schedule_type),
            "httpMethod": "POST",
            "headers": headers,
            "body": base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8"),
        },
    }


def _upsert_cloud_scheduler_job_sync(
    user_id: str,
    recipient_email: str,
    sender_email: str,
    hour: int,
    minute: int,
    timezone: str,
    enabled: bool,
    schedule_type: str = "morning",
) -> Dict[str, Any]:
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    session = AuthorizedSession(creds)
    parent = f"projects/{settings.GCP_PROJECT_ID}/locations/{settings.CLOUD_SCHEDULER_REGION}"
    job_name = _scheduler_job_name(user_id, schedule_type)
    job_body = _build_scheduler_job_body(user_id, recipient_email, sender_email, hour, minute, timezone, schedule_type)

    get_resp = session.get(f"https://cloudscheduler.googleapis.com/v1/{job_name}", timeout=12)
    if get_resp.status_code == 404:
        create_resp = session.post(
            f"https://cloudscheduler.googleapis.com/v1/{parent}/jobs",
            json=job_body,
            timeout=15,
        )
        if create_resp.status_code >= 400:
            raise RuntimeError(f"Scheduler create failed: {create_resp.text}")
    elif get_resp.status_code < 400:
        patch_resp = session.patch(
            f"https://cloudscheduler.googleapis.com/v1/{job_name}",
            params={"updateMask": "schedule,timeZone,httpTarget,description"},
            json=job_body,
            timeout=15,
        )
        if patch_resp.status_code >= 400:
            raise RuntimeError(f"Scheduler update failed: {patch_resp.text}")
    else:
        raise RuntimeError(f"Scheduler read failed: {get_resp.text}")

    pause_resume_url = f"https://cloudscheduler.googleapis.com/v1/{job_name}:{'resume' if enabled else 'pause'}"
    state_resp = session.post(pause_resume_url, timeout=12)
    if state_resp.status_code >= 400 and state_resp.status_code != 409:
        raise RuntimeError(f"Scheduler {'resume' if enabled else 'pause'} failed: {state_resp.text}")

    return {
        "job_name": job_name,
        "schedule_type": schedule_type,
        "enabled": enabled,
        "schedule": f"{hour:02d}:{minute:02d}",
        "timezone": timezone,
        "target": _briefing_target_url(user_id, schedule_type),
    }


async def _upsert_cloud_scheduler_job(**kwargs) -> Dict[str, Any]:
    return await asyncio.to_thread(_upsert_cloud_scheduler_job_sync, **kwargs)

if not DEMO_MODE:
    from agents.briefing_agent import send_briefing_email
    from agents.meeting_summarizer_agent import summarize_meeting
    from agents.orchestrator import run_orchestrator, run_orchestrator_voice
    from agents.smart_scheduler_agent import smart_reschedule
    from agents.weekly_retro_agent import generate_weekly_retro, save_retro_as_note
    from tools.analytics_tools import (
        get_productivity_score,
        get_task_completion_rate,
        get_weekly_trends,
    )
    from tools.calendar_tools import (
        create_calendar_event,
        list_calendar_events,
        patch_calendar_event,
        get_calendar_event_any_source,
    )
    from tools.firestore_tools import (
        create_note,
        create_task,
        delete_note,
        delete_task,
        list_notes,
        list_tasks,
        undo_last_action,
        update_task,
    )
    from tools.smart_tools import (
        analyse_deadline_risk,
        get_daily_focus_recommendation,
        recommend_priorities,
        suggest_tags,
        undo_multiple,
    )
else:
    # In demo mode, import mock data provider instead
    from api.demo_data import (  # type: ignore
        MOCK_TASKS, MOCK_EVENTS, MOCK_NOTES, MOCK_ANALYTICS,
        MOCK_RESCUE_PLAN, MOCK_MEETING_RESULT, MOCK_RETRO,
        MOCK_RISK, MOCK_FOCUS_PLAN, MOCK_TAGS, MOCK_PRIORITY_RECS,
        get_mock_query_response,
    )

# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Smart Daily Planner API",
    description=(
        "Multi-agent AI system for managing tasks, calendar events, notes, "
        "and daily productivity using Google ADK + Gemini."
    ),
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ── Gzip compression — reduces response sizes by 60-80% for slow networks ─────
from starlette.middleware.gzip import GZipMiddleware
app.add_middleware(GZipMiddleware, minimum_size=500)

# ── CORS — restrict to configured origins ────────────────────────────────────
_origins = [o.strip() for o in settings.ALLOWED_ORIGINS.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    expose_headers=["X-Request-ID", "X-RateLimit-Limit", "X-RateLimit-Remaining"],
    max_age=600,
)

# ── Security headers middleware ───────────────────────────────────────────────
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response: Response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    # Only set HSTS on HTTPS (Cloud Run production)
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

# ── Request logging middleware ────────────────────────────────────────────────
@app.middleware("http")
async def log_requests(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", uuid.uuid4().hex[:12])
    start = time.perf_counter()
    response: Response = await call_next(request)
    elapsed_ms = round((time.perf_counter() - start) * 1000)
    response.headers["X-Request-ID"] = request_id
    if not request.url.path.startswith("/ui/"):  # skip static asset noise
        logger.info(
            '{"method":"%s","path":"%s","status":%d,"ms":%d,"rid":"%s"}',
            request.method, request.url.path, response.status_code, elapsed_ms, request_id,
        )
    return response

# ── Rate limiting ─────────────────────────────────────────────────────────────
try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from slowapi.util import get_remote_address

    def _rate_key(request: Request) -> str:
        # Prefer authenticated user_id over IP for per-user limiting
        cookie = request.cookies.get("session")
        if cookie:
            try:
                from api.auth import _verify_jwt
                payload = _verify_jwt(cookie)
                if payload:
                    return payload.get("user_id", get_remote_address(request))
            except Exception:
                pass
        return get_remote_address(request) or "anon"

    limiter = Limiter(key_func=_rate_key, default_limits=["120/minute"])
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    _RATE_LIMIT_AVAILABLE = True
except ImportError:
    logger.warning("slowapi not installed — rate limiting disabled. Run: pip install slowapi")
    _RATE_LIMIT_AVAILABLE = False
    limiter = None

# ── Sentry error tracking ─────────────────────────────────────────────────────
if settings.SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            integrations=[StarletteIntegration(transaction_style="endpoint"), FastApiIntegration()],
            traces_sample_rate=0.1,
            environment="production" if not DEMO_MODE else "demo",
        )
        logger.info("Sentry error tracking enabled")
    except ImportError:
        logger.warning("sentry-sdk not installed — error tracking disabled")

# ── Auth router ───────────────────────────────────────────────────────────────
app.include_router(auth_router)

# Serve the UI from /ui directory (if it exists)
_UI_DIR = Path(__file__).parent.parent / "ui"
if _UI_DIR.exists():
    app.mount("/ui", StaticFiles(directory=str(_UI_DIR)), name="ui")


@app.on_event("startup")
async def _suppress_win32_connection_reset():
    """Silence WinError 10054 (connection forcibly reset) from Proactor event loop on Windows.

    This error fires when a browser or client drops a TCP connection while the
    server is still writing.  It is benign — the request was already complete —
    but it pollutes logs with noisy ERROR tracebacks on Windows dev machines.
    """
    import asyncio
    import sys
    if sys.platform != "win32":
        return
    loop = asyncio.get_running_loop()

    def _handler(loop, context):
        exc = context.get("exception")
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            return  # benign Windows TCP reset — suppress
        loop.default_exception_handler(context)

    loop.set_exception_handler(_handler)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def serve_ui():
    """Serve the Smart Daily Planner UI at the root URL."""
    ui_file = _UI_DIR / "index.html"
    if ui_file.exists():
        return FileResponse(str(ui_file), media_type="text/html")
    return HTMLResponse("""<html><body style="font-family:sans-serif;background:#0f172a;color:white;padding:40px">
    <h1>&#128197; Smart Daily Planner API</h1>
    <p>API is running. Visit <a href="/docs" style="color:#6366f1">/docs</a> for Swagger UI.</p>
    <p style="color:#94a3b8">To enable the full UI, ensure the <code>ui/</code> directory exists.</p>
    </body></html>""")


@app.get("/manifest.json", include_in_schema=False)
async def pwa_manifest():
    """Serve PWA manifest from root with correct MIME type for browser installability."""
    manifest_file = _UI_DIR / "manifest.json"
    if manifest_file.exists():
        from fastapi.responses import Response
        return Response(
            manifest_file.read_bytes(),
            media_type="application/manifest+json",
            headers={"Cache-Control": "public, max-age=3600"},
        )


@app.get("/sw.js", include_in_schema=False)
async def service_worker():
    """Serve PWA service worker from root scope for broadest caching coverage."""
    sw_file = _UI_DIR / "sw.js"
    if sw_file.exists():
        from fastapi.responses import Response
        return Response(sw_file.read_text(), media_type="application/javascript")


# ══════════════════════════════════════════════════════════════════════════════
# REQUEST / RESPONSE MODELS
# ══════════════════════════════════════════════════════════════════════════════


class QueryRequest(BaseModel):
    message: str = Field(..., min_length=1, description="Natural language query for the planner.")
    user_id: str = Field(default="default_user", description="User identifier.")
    session_id: Optional[str] = Field(None, description="Session ID for continuity.")


class QueryResponse(BaseModel):
    response: str
    session_id: str
    user_id: str
    agent_name: str


_VALID_PRIORITIES = {"urgent", "high", "medium", "low"}
_VALID_RECURRENCES = {"daily", "weekdays", "weekly", "biweekly", "monthly", ""}
_VALID_STATUSES = {"pending", "in_progress", "completed", "overdue"}


class TaskCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500, description="Task title.")
    due_date: str = Field(..., description="ISO-8601 due date string.")
    priority: str = Field(default="medium", description="Priority level.")
    tags: Optional[List[str]] = Field(default=None, max_length=20)
    notes: str = Field(default="", max_length=10000)
    user_id: str = Field(default="default_user")
    recurrence: Optional[str] = Field(None, description="Recurrence pattern: daily|weekdays|weekly|biweekly|monthly")

    @field_validator("priority")
    @classmethod
    def validate_priority(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in _VALID_PRIORITIES:
            raise ValueError(f"priority must be one of {sorted(_VALID_PRIORITIES)}")
        return v

    @field_validator("due_date")
    @classmethod
    def validate_due_date(cls, v: str) -> str:
        if not v:
            return v
        try:
            from dateutil import parser as _dp
            _dp.parse(v)
        except Exception:
            raise ValueError("due_date must be a valid ISO-8601 date string")
        return v

    @field_validator("recurrence")
    @classmethod
    def validate_recurrence(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.lower().strip()
        if v not in _VALID_RECURRENCES:
            raise ValueError(f"recurrence must be one of {sorted(_VALID_RECURRENCES - {''})}")
        return v or None

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return v
        cleaned = [t.strip()[:50] for t in v if t.strip()]
        return cleaned[:20]


class TaskResponse(BaseModel):
    id: str
    title: str
    due_date: str
    priority: str
    status: str
    tags: List[str]
    notes: str
    user_id: str
    created_at: str
    updated_at: str


class TaskListResponse(BaseModel):
    tasks: List[Dict[str, Any]]
    count: int


class EventCreateRequest(BaseModel):
    summary: str = Field(..., description="Event title.")
    start_datetime: str = Field(..., description="ISO-8601 start datetime.")
    duration_minutes: int = Field(default=60)
    description: str = Field(default="")
    location: str = Field(default="")
    attendees: Optional[List[str]] = Field(default=None)
    recurrence_type: Optional[str] = Field(default=None, description="weekly|daily|monthly")
    recurrence_days: Optional[List[str]] = Field(default=None, description="Day codes: MO TU WE TH FR SA SU")
    recurrence_end_date: Optional[str] = Field(default=None, description="End date for recurrence YYYY-MM-DD")


class EventResponse(BaseModel):
    id: Optional[str]
    summary: Optional[str]
    start: Optional[Dict[str, str]]
    end: Optional[Dict[str, str]]
    htmlLink: Optional[str]
    status: Optional[str]


class EventListResponse(BaseModel):
    events: List[Dict[str, Any]]
    count: int


class NoteCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=300, description="Note title.")
    content: str = Field(..., max_length=50000, description="Note body text.")
    tags: Optional[List[str]] = Field(default=None)
    user_id: str = Field(default="default_user")


class NoteListResponse(BaseModel):
    notes: List[Dict[str, Any]]
    count: int


class AnalyticsResponse(BaseModel):
    productivity_score: Dict[str, Any]
    completion_rate: Dict[str, Any]
    weekly_trends: List[Dict[str, Any]]
    period_days: int


class BriefingRequest(BaseModel):
    recipient_email: Optional[str] = Field(None)
    user_id: str = Field(default="default_user")
    sender_email: Optional[str] = Field(None, description="Which account to send from. Defaults to primary Gmail.")
    custom_message: Optional[str] = Field(None)
    subject: Optional[str] = Field(None)


class BriefingResponse(BaseModel):
    sent: bool
    dry_run: bool
    recipient: str
    subject: str
    message_id: Optional[str]
    body_preview: str


class DayEndRequest(BaseModel):
    user_id: str = Field(default="default_user")
    recipient_email: Optional[str] = Field(default=None)
    sender_email: Optional[str] = Field(default=None)
    subject: Optional[str] = Field(default=None)


class BriefingScheduleRequest(BaseModel):
    user_id: str = Field(default="default_user")
    recipient_email: str = Field(..., description="Recipient for daily briefing email.")
    sender_email: Optional[str] = Field(default="", description="Optional sender account email.")
    briefing_time: str = Field(default="08:00", description="HH:MM in 24-hour format.")
    day_end_time: str = Field(default="19:30", description="HH:MM in 24-hour format for day-end summary.")
    timezone: str = Field(default="Asia/Kolkata")
    enabled: bool = Field(default=True)

    @field_validator("briefing_time")
    @classmethod
    def validate_briefing_time(cls, v: str) -> str:
        try:
            hh, mm = v.split(":")
            h, m = int(hh), int(mm)
            if h < 0 or h > 23 or m < 0 or m > 59:
                raise ValueError
        except Exception as exc:
            raise ValueError("briefing_time must be in HH:MM 24-hour format.") from exc
        return f"{h:02d}:{m:02d}"

    @field_validator("day_end_time")
    @classmethod
    def validate_day_end_time(cls, v: str) -> str:
        try:
            hh, mm = v.split(":")
            h, m = int(hh), int(mm)
            if h < 0 or h > 23 or m < 0 or m > 59:
                raise ValueError
        except Exception as exc:
            raise ValueError("day_end_time must be in HH:MM 24-hour format.") from exc
        return f"{h:02d}:{m:02d}"


class UndoResponse(BaseModel):
    undone: str
    entity_id: str
    action_taken: str


class MultiStepRequest(BaseModel):
    user_id: str = Field(default="demo_user")
    session_id: Optional[str] = Field(None)


class HealthResponse(BaseModel):
    status: str
    timestamp: str
    version: str
    model: str
    project: str
    demo_mode: bool
    checks: Dict[str, Any] = Field(default_factory=dict)


class IngestResponse(BaseModel):
    extracted: Dict[str, Any]
    created: Dict[str, int]
    errors: List[str]


# ══════════════════════════════════════════════════════════════════════════════
# HEALTH
# ══════════════════════════════════════════════════════════════════════════════


@app.get("/health", response_model=HealthResponse, tags=["System"])
async def health_check() -> HealthResponse:
    """Fast liveness/readiness probe — returns immediately without I/O calls.
    Use GET /health/deep for a full dependency check (slower)."""
    return HealthResponse(
        status="healthy",
        timestamp=datetime.now(LOCAL_TZ).isoformat(),
        version="2.0.0",
        model=settings.GEMINI_MODEL,
        project=settings.GCP_PROJECT_ID,
        demo_mode=DEMO_MODE,
        checks={
            "gemini_model": settings.GEMINI_MODEL,
            "google_api_key_set": bool(settings.GOOGLE_API_KEY),
            "gmail_configured": bool(settings.GMAIL_USER_EMAIL and settings.GMAIL_APP_PASSWORD),
            "auth_enabled": settings.AUTH_ENABLED,
            "oauth_configured": bool(settings.OAUTH_CLIENT_ID),
        },
    )


@app.get("/health/credits", tags=["System"])
async def check_credits() -> dict:
    """Probe Google Cloud / Gemini AI availability without spending significant quota.
    Returns {available: bool, message: str} so the frontend can show a warning when
    credits are exhausted instead of letting AI calls fail silently."""
    if DEMO_MODE:
        return {"available": True, "message": "Demo mode — no credits needed", "demo": True}
    try:
        import google.generativeai as genai
        genai.configure(api_key=settings.GOOGLE_API_KEY)
        # Minimal token probe — generates 1 token to verify the key is valid and quota exists
        m = genai.GenerativeModel(settings.GEMINI_MODEL)
        resp = m.generate_content("Hi", generation_config={"max_output_tokens": 1, "temperature": 0})
        _ = resp.text  # raises on billing error
        return {"available": True, "message": "Google AI credits available", "demo": False}
    except Exception as exc:
        msg = str(exc)
        if any(k in msg.lower() for k in ("quota", "billing", "resource_exhausted", "429", "insufficient")):
            return {"available": False, "message": "Google Cloud credits exhausted — AI features unavailable", "demo": False, "error": msg[:200]}
        if any(k in msg.lower() for k in ("api_key", "invalid", "permission", "401", "403")):
            return {"available": False, "message": "Google API key invalid or expired", "demo": False, "error": msg[:200]}
        # Other errors (network, etc.) — assume credits are fine
        return {"available": True, "message": "Credits status unknown (connectivity issue)", "demo": False, "warning": msg[:200]}


@app.get("/health/deep", response_model=HealthResponse, tags=["System"])
async def health_check_deep() -> HealthResponse:
    """Deep health check — verifies Firestore connectivity (can be slow on cold start)."""
    import asyncio as _asyncio
    checks: Dict[str, Any] = {
        "gemini_model": settings.GEMINI_MODEL,
        "google_api_key_set": bool(settings.GOOGLE_API_KEY),
        "gmail_configured": bool(settings.GMAIL_USER_EMAIL and settings.GMAIL_APP_PASSWORD),
        "auth_enabled": settings.AUTH_ENABLED,
        "oauth_configured": bool(settings.OAUTH_CLIENT_ID),
    }
    overall = "healthy"

    if not DEMO_MODE:
        try:
            from tools.firestore_tools import list_tasks
            await _asyncio.wait_for(list_tasks(user_id="__healthcheck__", limit=1), timeout=5.0)
            checks["firestore"] = "ok"
        except _asyncio.TimeoutError:
            checks["firestore"] = "timeout (>5s)"
            overall = "degraded"
        except Exception as exc:
            checks["firestore"] = f"error: {type(exc).__name__}"
            overall = "degraded"
    else:
        checks["firestore"] = "skipped (demo mode)"

    return HealthResponse(
        status=overall,
        timestamp=datetime.now(LOCAL_TZ).isoformat(),
        version="2.0.0",
        model=settings.GEMINI_MODEL,
        project=settings.GCP_PROJECT_ID,
        demo_mode=DEMO_MODE,
        checks=checks,
    )


@app.get("/mcp/status", response_model=Dict[str, Any], tags=["System"])
async def mcp_status() -> Dict[str, Any]:
    """Check connectivity to deployed MCP SSE endpoint."""
    url = (settings.MCP_SERVER_URL or "").strip()
    if not url:
        return {"configured": False, "healthy": False, "message": "MCP_SERVER_URL not configured."}
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.head(url)
            if resp.status_code == 405:
                resp = await client.get(url, headers={"Accept": "text/event-stream"})
        ctype = (resp.headers.get("content-type") or "").lower()
        healthy = resp.status_code == 200 and "text/event-stream" in ctype
        return {
            "configured": True,
            "healthy": healthy,
            "url": url,
            "status_code": resp.status_code,
            "content_type": ctype,
            "message": "ok" if healthy else "Unexpected MCP response.",
        }
    except Exception as exc:
        return {"configured": True, "healthy": False, "url": url, "message": str(exc)}


# ══════════════════════════════════════════════════════════════════════════════
# ORCHESTRATOR QUERY
# ══════════════════════════════════════════════════════════════════════════════


async def _smart_rule_reply(message: str, user_id: str) -> Optional[str]:
    """
    Rule-based AI responder — works 100% without any Gemini API call.
    Reads live task/event/note data from Firestore and returns intelligent replies.
    """
    from datetime import datetime, timedelta
    msg = message.lower().strip()
    now = datetime.now(LOCAL_TZ)

    # Fetch live data (best-effort — graceful on any error)
    try:
        tasks_raw = await list_tasks(user_id=user_id, limit=50)
    except Exception:
        tasks_raw = []
    try:
        events_raw = await list_calendar_events(
            time_min=now.isoformat(),
            time_max=(now + timedelta(days=7)).isoformat(),
            max_results=20,
        )
    except Exception:
        events_raw = []
    try:
        notes_raw = await list_notes(user_id=user_id, limit=20)
    except Exception:
        notes_raw = []

    pending   = [t for t in tasks_raw if t.get("status") == "pending"]
    overdue   = [t for t in tasks_raw if t.get("status") == "overdue"]
    completed = [t for t in tasks_raw if t.get("status") == "completed"]
    urgent    = [t for t in pending if t.get("priority") == "urgent"]
    high      = [t for t in pending if t.get("priority") == "high"]
    total     = len(tasks_raw)
    rate      = round(len(completed) / total * 100) if total else 0

    due_today = [
        t for t in pending
        if t.get("due_date") and
        now.date().isoformat() <= t["due_date"][:10] <= now.date().isoformat()
    ]

    # ── Intent matching ────────────────────────────────────────────
    def has(*words):
        return any(w in msg for w in words)

    # Greeting
    if has("hello", "hi ", "hey", "good morning", "good evening"):
        name = user_id.replace("_", " ").title()
        return (
            f"👋 Hey {name}! Welcome back to Smart Daily Planner.\n\n"
            f"📊 Quick snapshot: **{len(pending)} pending**, **{len(completed)} completed**, "
            f"**{len(overdue)} overdue** task{'s' if len(overdue)!=1 else ''}.\n\n"
            f"How can I help you today? Try: *'Show my overdue tasks'* or *'What should I focus on?'*"
        )

    # Productivity / score
    if has("score", "productivity", "how am i doing", "performance", "how productive"):
        label = ("Excellent 🏆" if rate >= 80 else "Good 👍" if rate >= 60
                 else "Steady 📈" if rate >= 40 else "Needs Focus 💪")
        score = max(0, min(100, rate - len(overdue) * 3))
        return (
            f"📊 **Your Productivity Score: {score}/100 — {label}**\n\n"
            f"- ✅ Completed: **{len(completed)}** tasks\n"
            f"- ⏳ Pending: **{len(pending)}** tasks\n"
            f"- ⚠️ Overdue: **{len(overdue)}** tasks\n"
            f"- 📈 Completion rate: **{rate}%**\n\n"
            + (f"💡 Tip: Clear {len(overdue)} overdue task{'s' if len(overdue)!=1 else ''} first to boost your score!" if overdue
               else "🎉 No overdue tasks — great discipline!")
        )

    # Overdue tasks
    if has("overdue", "late", "missed", "past due"):
        if not overdue:
            return "✅ Great news — you have **no overdue tasks**! You're on top of everything."
        lines = "\n".join(f"  {i+1}. **{t['title']}** ({t.get('priority','?')} priority)" for i, t in enumerate(overdue[:5]))
        return (
            f"⚠️ You have **{len(overdue)} overdue task{'s' if len(overdue)!=1 else ''}**:\n\n"
            f"{lines}\n\n"
            f"💡 Go to **Smart Features → Rescue Plan** to auto-schedule these with available calendar slots."
        )

    # Today's focus / what to work on
    if has("focus", "what should i", "where to start", "most important", "top tasks",
            "what to do", "work on first", "priority today", "plan for today"):
        priority_order = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
        # Combine overdue + due today + other pending, sorted by priority
        urgent_overdue = [t for t in overdue if t.get("priority") in ("urgent", "high")]
        combined = urgent_overdue + [t for t in overdue if t not in urgent_overdue] + due_today + [t for t in pending if t not in due_today]
        # Deduplicate while preserving order
        seen_ids: set = set()
        top = []
        for t in combined:
            tid = t.get("id", t.get("title", ""))
            if tid not in seen_ids:
                seen_ids.add(tid)
                top.append(t)
            if len(top) >= 5:
                break
        if not top:
            return (
                "🎉 **All clear!** No pending or overdue tasks.\n\n"
                "Great time to:\n"
                "- 📋 Plan upcoming work — add new tasks\n"
                "- 📅 Review your calendar for tomorrow\n"
                "- 📝 Capture notes or ideas while you have breathing room"
            )
        picons = {"urgent": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}
        lines = "\n".join(
            f"  {i+1}. {picons.get(t.get('priority','medium'),'⚪')} **{t['title']}**"
            + (f" ⚠️ *overdue*" if t.get("status") == "overdue" else "")
            + (f" — due {t['due_date'][:10]}" if t.get("due_date") else "")
            for i, t in enumerate(top)
        )
        tip = (
            f"⚠️ You have **{len(overdue)} overdue** item{'s' if len(overdue)!=1 else ''} — tackle those first!"
            if overdue else
            "⏰ Start with the 🔴/🟠 items. Aim for **2–3 hour deep work blocks** in the morning."
        )
        return (
            f"🎯 **Your Focus Plan** ({len(top)} tasks):\n\n"
            f"{lines}\n\n"
            f"{tip}"
        )

    # List tasks / show tasks
    if has("list task", "show task", "my task", "all task", "pending task"):
        if not pending:
            return "✅ No pending tasks! You're all caught up."
        lines = "\n".join(
            f"  • **{t['title']}** [{t.get('priority','?')}]"
            + (f" — due {t['due_date'][:10]}" if t.get("due_date") else "")
            for t in pending[:8]
        )
        return f"📋 **{len(pending)} pending task{'s' if len(pending)!=1 else ''}**:\n\n{lines}"

    # Add task
    if has("add task", "create task", "new task", "remind me", "to-do", "todo"):
        return (
            "📋 To add a task, use the **+ New Task** button (top of the Tasks tab) "
            "or the drawer button on the Dashboard.\n\n"
            "You can set title, due date, priority (urgent/high/medium/low), and tags.\n\n"
            "💡 Tasks are saved to Firestore and appear instantly across all views."
        )

    # Events / calendar — show list (create/add handled above)
    if has("show event", "show meeting", "my event", "my meeting", "upcoming event",
            "upcoming meeting", "what meeting", "show calendar", "my calendar", "view calendar",
            "list event", "list meeting"):
        if not events_raw:
            return (
                "📅 No upcoming events found in the next 7 days.\n\n"
                "Use **Schedule Meeting** on the Dashboard or Calendar tab to add one."
            )
        lines = "\n".join(
            f"  {i+1}. **{e.get('summary','Event')}** — {e.get('start','')[:16].replace('T',' ')}"
            + (f"  📍 {e['location']}" if e.get("location") else "")
            for i, e in enumerate(events_raw[:5])
        )
        return f"📅 **Upcoming events (next 7 days)**:\n\n{lines}\n\n📊 {len(events_raw)} total events."

    # Notes
    if has("note", "memo", "capture", "saved note"):
        if not notes_raw:
            return "📝 No notes yet. Use **+ New Note** on the Dashboard or Notes tab to start capturing ideas."
        lines = "\n".join(f"  • **{n.get('title','Note')}**" for n in notes_raw[:5])
        return f"📝 **Your recent notes** ({len(notes_raw)} total):\n\n{lines}"

    # Weekly summary / retro
    if has("week", "summary", "retrospective", "retro", "how was my week"):
        label = ("Excellent 🏆" if rate >= 80 else "Good 👍" if rate >= 60
                 else "Steady 📈" if rate >= 40 else "Building Momentum 💪")
        return (
            f"📊 **Weekly Summary — {label}**\n\n"
            f"- ✅ Tasks completed: **{len(completed)}**\n"
            f"- ⏳ Still pending: **{len(pending)}**\n"
            f"- ⚠️ Overdue: **{len(overdue)}**\n"
            f"- 📅 Events: **{len(events_raw)}**\n"
            f"- 📝 Notes saved: **{len(notes_raw)}**\n"
            f"- 📈 Completion rate: **{rate}%**\n\n"
            f"💡 For a full AI narrative retro, go to **Smart Features → Weekly Retro**."
        )

    # Urgent tasks
    if has("urgent", "critical", "emergency"):
        if not urgent:
            return "✅ No urgent tasks right now. Your urgent queue is clear!"
        lines = "\n".join(f"  {i+1}. **{t['title']}**" for i, t in enumerate(urgent[:5]))
        return f"🔴 **{len(urgent)} urgent task{'s' if len(urgent)!=1 else ''}** need immediate attention:\n\n{lines}"

    # Rescue plan
    if has("rescue", "overwhelm", "behind", "catch up", "reschedule"):
        return (
            "🆘 **AI Rescue Plan** can help you catch up!\n\n"
            f"You have **{len(overdue)} overdue** and **{len(urgent)} urgent** tasks.\n\n"
            "Go to **Smart Features → Rescue Plan** to:\n"
            "1. Auto-analyse your backlog\n"
            "2. Find free calendar slots\n"
            "3. Generate an optimal schedule\n"
            "4. Create focus-block events automatically"
        )

    # Due today
    if has("due today", "what's due", "what is due", "today's task"):
        if not due_today:
            return "✅ Nothing is due today — you're clear! Great time to get ahead on tomorrow's tasks."
        lines = "\n".join(
            f"  {i+1}. **{t['title']}** [{t.get('priority','?')} priority]"
            for i, t in enumerate(due_today[:8])
        )
        return f"📋 **{len(due_today)} task{'s' if len(due_today)!=1 else ''} due today**:\n\n{lines}"

    # Free slots / availability
    if has("free slot", "available time", "am i free", "when am i free", "any free time"):
        return (
            "📅 To check your free slots:\n\n"
            "1. Use the **Calendar tab** → Week view to see your gaps visually.\n"
            "2. Or ask the AI: *'Find me a free 1-hour slot tomorrow'*\n"
            "3. The **Rescue Plan** (Smart Features) automatically finds free slots when rescheduling tasks.\n\n"
            f"📊 You have **{len(events_raw)} upcoming events** in the next 7 days."
        )

    # Undo
    if has("undo", "revert", "take back", "reverse that"):
        return (
            "↩️ **Undo your last action** with the Undo button in the toolbar, "
            "or use the keyboard shortcut **Ctrl+Z** on the Dashboard.\n\n"
            "You can undo: task creation, task deletion, task updates, and note creation.\n\n"
            "💡 Tip: The undo system works for the last action — multiple undos are also supported "
            "via **Settings → Undo Multiple**."
        )

    # Create note
    if has("create note", "new note", "add note", "save note"):
        return (
            "📝 To create a note:\n\n"
            "1. Click **+ New Note** on the Notes tab or Dashboard.\n"
            "2. Or tell me: *'Note: [your content]'* and I'll create it via the AI assistant.\n\n"
            "Notes support tags for easy filtering — e.g. #work, #ideas, #meeting."
        )

    # Tags
    if has("tag", "label", "category", "categorize"):
        return (
            "🏷️ **Tags** help organise your tasks and notes.\n\n"
            "- Add tags when creating a task or note.\n"
            "- Use the **AI Tag Suggester** (Smart Features) to get AI-suggested tags based on content.\n"
            f"- Your most-used tags across {len(notes_raw)} notes are visible in the Notes tab filter."
        )

    # Today's events / what's on calendar today
    if has("today's event", "today's meeting", "today's calendar", "what's on today",
            "what is on today", "events today", "meetings today", "calendar today"):
        today_events = [e for e in events_raw if (e.get("start") or "")[:10] == now.date().isoformat()]
        if not today_events:
            return (
                f"📅 **No events scheduled for today** ({now.strftime('%A, %d %b')}).\n\n"
                "A great day for deep-focus work! Use **Schedule Meeting** to add one."
            )
        lines = "\n".join(
            f"  🕐 **{e.get('summary','(No title)')}** at {e.get('start','')[:16].replace('T',' ')}"
            + (f"\n     📍 {e['location']}" if e.get("location") else "")
            for e in today_events
        )
        return f"📅 **{len(today_events)} event{'s' if len(today_events)!=1 else ''} today** ({now.strftime('%A, %d %b')}):\n\n{lines}"

    # Next event / next meeting
    if has("next event", "next meeting", "upcoming meeting", "when is my next"):
        upcoming = [e for e in events_raw if (e.get("start") or "") >= now.isoformat()]
        if not upcoming:
            return "📅 No upcoming events found in the next 7 days. Calendar looks clear!"
        e = upcoming[0]
        return (
            f"📅 **Next up: {e.get('summary','(No title)')}**\n\n"
            f"🕐 {e.get('start','')[:16].replace('T',' ')}"
            + (f"\n📍 {e['location']}" if e.get("location") else "")
        )

    # Show completed tasks
    if has("completed", "done task", "finished", "what have i done", "what did i complete"):
        if not completed:
            return "📭 No completed tasks yet. Mark tasks as done by checking them off in the Tasks tab!"
        lines = "\n".join(f"  ✅ {t.get('title','Task')}" for t in completed[:8])
        return f"✅ **{len(completed)} completed task{'s' if len(completed)!=1 else ''}**:\n\n{lines}"

    # Stats / analytics
    if has("statistic", "analytic", "stats", "numbers", "count", "how many task"):
        return (
            f"📊 **Your Task Statistics:**\n\n"
            f"- 📋 Total tasks: **{total}**\n"
            f"- ✅ Completed: **{len(completed)}** ({rate}%)\n"
            f"- ⏳ Pending: **{len(pending)}**\n"
            f"- ⚠️ Overdue: **{len(overdue)}**\n"
            f"- 🔴 Urgent: **{len(urgent)}**\n"
            f"- 🟠 High priority: **{len(high)}**\n"
            f"- 📅 Upcoming events: **{len(events_raw)}** (next 7 days)\n"
            f"- 📝 Notes: **{len(notes_raw)}**"
        )

    # Briefing / send email
    if has("briefing", "morning briefing", "send briefing", "daily brief", "email briefing"):
        return (
            "📧 **Morning Briefing** — use the **Smart Features → Daily Briefing** tab to:\n\n"
            "1. Preview today's task/event/note summary\n"
            "2. Send it to your inbox (Gmail or Yahoo)\n\n"
            "💡 You can also trigger it via the **Briefing** button in the top nav."
        )

    # Create / add an event
    if has("create event", "add event", "new event", "add to calendar", "add a meeting"):
        return (
            "📅 To add a calendar event:\n\n"
            "1. Click **Schedule Meeting** on the Dashboard or Calendar tab\n"
            "2. Or click **+ New Event** in the Calendar tab\n"
            "3. Set title, date, time, duration, attendees, and agenda\n\n"
            "💡 For recurring events (every Tuesday & Thursday), use the **Recurring Meeting** toggle!"
        )

    # Completed today / done today
    if has("what did i do today", "today's progress", "progress today", "done today"):
        done_today = [
            t for t in completed
            if t.get("updated_at", "")[:10] == now.date().isoformat()
        ]
        if not done_today:
            return (
                f"📋 No tasks completed today yet ({now.strftime('%A, %d %b')}).\n\n"
                "Check off tasks in the Tasks tab as you finish them!"
            )
        lines = "\n".join(f"  ✅ **{t.get('title','Task')}**" for t in done_today[:8])
        return f"🎉 **{len(done_today)} task{'s' if len(done_today)!=1 else ''} completed today**:\n\n{lines}"

    # Help
    if has("help", "what can you", "commands", "features"):
        return (
            "🤖 **I can help you with:**\n\n"
            "- 📋 *'Show my tasks'* — list pending/overdue tasks\n"
            "- 🎯 *'What should I focus on today?'* — priority plan\n"
            "- 📊 *'What's my productivity score?'* — score + stats\n"
            "- 📅 *'Show upcoming events'* — calendar events\n"
            "- 📝 *'Show my notes'* — recent notes\n"
            "- 📈 *'How was my week?'* — weekly summary\n"
            "- 📋 *'What's due today?'* — today's task list\n"
            "- 🆘 *'I'm overwhelmed, help me catch up'* — rescue plan\n"
            "- ↩️ *'Undo'* — reverse your last action\n\n"
            "I work entirely from your live data — no AI quota needed! 🚀"
        )

    # Return None — signals caller to try Gemini for unrecognised queries
    return None


@app.post("/query", response_model=QueryResponse, tags=["Orchestrator"])
async def query(
    request: QueryRequest,
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
) -> QueryResponse:
    """Send a natural language query to the multi-agent orchestrator."""
    if current_user is not None:
        request.user_id = _resolved_user_id(current_user)
    # No session: use client `user_id` (e.g. default_user) so Ask AI works without Google sign-in
    if DEMO_MODE:
        mock = get_mock_query_response(request.message)
        mock["session_id"] = request.session_id or mock["session_id"]
        mock["user_id"] = request.user_id
        return QueryResponse(**mock)
    import uuid as _uuid
    session_id = request.session_id or f"sess-{_uuid.uuid4().hex[:12]}"

    # ── Rule-based handler runs FIRST for common queries (fast, reliable, no quota) ──
    # Only falls through to Gemini when the rule matcher returns None (unknown intent).
    rule_response = await _smart_rule_reply(request.message, request.user_id)
    if rule_response is not None:
        return QueryResponse(
            response=rule_response,
            session_id=session_id,
            user_id=request.user_id,
            agent_name="smart-assistant",
        )

    # ── Gemini orchestrator for complex / unrecognised queries ──
    fallback = (
        f"🤖 I understand you asked: *\"{request.message[:80]}\"*\n\n"
        "I work best with specific requests. Try:\n"
        "- *'Show my overdue tasks'*\n"
        "- *'What should I focus on today?'*\n"
        "- *'What\\'s my productivity score?'*\n"
        "- *'Show upcoming events'*\n"
        "- *'How was my week?'*"
    )
    try:
        result = await run_orchestrator(
            message=request.message,
            user_id=request.user_id,
            session_id=session_id,
        )
        return QueryResponse(**result)
    except Exception as exc:
        logger.warning("Orchestrator unavailable (%s), using fallback reply", type(exc).__name__)
        return QueryResponse(
            response=fallback,
            session_id=session_id,
            user_id=request.user_id,
            agent_name="smart-assistant",
        )


@app.post("/query/voice", response_model=QueryResponse, tags=["Orchestrator"])
async def query_voice(
    audio: UploadFile = File(..., description="Audio file (MP3, WAV, OGG, etc.)"),
    user_id: str = Form(default="default_user"),
    session_id: Optional[str] = Form(default=None),
) -> QueryResponse:
    """Send a voice audio query to the multi-agent orchestrator."""
    if DEMO_MODE:
        mock = get_mock_query_response("meeting")
        return QueryResponse(**mock)
    try:
        audio_bytes = await audio.read()
        mime_type = audio.content_type or "audio/mp3"
        result = await run_orchestrator_voice(
            audio_bytes=audio_bytes,
            mime_type=mime_type,
            user_id=user_id,
            session_id=session_id,
        )
        return QueryResponse(**result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ══════════════════════════════════════════════════════════════════════════════
# INGEST
# ══════════════════════════════════════════════════════════════════════════════


@app.post("/ingest", response_model=IngestResponse, tags=["Ingest"])
async def ingest_document(
    file: UploadFile = File(..., description="Image or PDF to extract data from."),
    user_id: str = Form(default="default_user"),
) -> IngestResponse:
    """Extract tasks, events, and notes from an uploaded image or PDF."""
    if DEMO_MODE:
        return IngestResponse(
            extracted={
                "tasks": [{"title": "Follow up on invoice #4521", "priority": "medium"}],
                "events": [{"summary": "Client call", "start_datetime": "tomorrow 3pm", "duration_minutes": 30}],
                "notes": [{"title": "Extracted from document", "content": "Key points captured from uploaded file."}],
            },
            created={"tasks": 1, "events": 1, "notes": 1},
            errors=[],
        )
    try:
        from agents.ingest_agent import ingest_base64
        raw_bytes = await file.read()
        b64_data = base64.b64encode(raw_bytes).decode("utf-8")
        mime_type = file.content_type or "image/jpeg"
        result = await ingest_base64(
            base64_data=b64_data,
            mime_type=mime_type,
            user_id=user_id,
        )
        return IngestResponse(**result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ══════════════════════════════════════════════════════════════════════════════
# TASKS
# ══════════════════════════════════════════════════════════════════════════════


@app.get("/tasks", response_model=TaskListResponse, tags=["Tasks"])
async def get_tasks(
    status: Optional[str] = None,
    priority: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
) -> TaskListResponse:
    """List tasks with optional filters. Supports pagination via offset/limit."""
    user_id = (current_user or {}).get("user_id", "default_user")

    if status and status not in _VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {sorted(_VALID_STATUSES)}")
    if priority and priority not in _VALID_PRIORITIES:
        raise HTTPException(status_code=400, detail=f"priority must be one of {sorted(_VALID_PRIORITIES)}")
    limit = max(1, min(limit, 200))

    if DEMO_MODE:
        tasks = MOCK_TASKS
        if status:
            tasks = [t for t in tasks if t.get("status") == status]
        if priority:
            tasks = [t for t in tasks if t.get("priority") == priority]
        tasks = tasks[offset: offset + limit]
        return TaskListResponse(tasks=tasks, count=len(tasks))
    merged: List[Dict[str, Any]] = []
    seen: set = set()
    for uid in _legacy_user_ids(user_id):
        items = await list_tasks(user_id=uid, status=status, priority=priority, limit=limit)
        for t in items:
            tid = t.get("id")
            if tid and tid in seen:
                continue
            if tid:
                seen.add(tid)
            merged.append(t)
    return TaskListResponse(tasks=merged[offset: offset + limit], count=len(merged))


@app.post("/tasks", response_model=Dict[str, Any], tags=["Tasks"], status_code=201)
async def create_task_endpoint(
    request: TaskCreateRequest,
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
) -> Dict[str, Any]:
    """Create a new task directly (bypasses the orchestrator)."""
    request.user_id = _resolved_user_id(current_user)
    if DEMO_MODE:
        now_iso = datetime.now(LOCAL_TZ).isoformat()
        task_id = str(uuid.uuid4())
        _demo_events.setdefault(request.user_id, []).append({
            "action": "create_task",
            "entity_id": task_id,
            "collection": "tasks",
            "snapshot": None,
        })
        return {
            "id": task_id,
            "title": request.title,
            "due_date": request.due_date,
            "priority": request.priority,
            "status": "pending",
            "tags": request.tags or [],
            "notes": request.notes,
            "user_id": request.user_id,
            "created_at": now_iso,
            "updated_at": now_iso,
        }
    result = await create_task(
        title=request.title,
        due_date=request.due_date,
        priority=request.priority,
        tags=request.tags,
        user_id=request.user_id,
        notes=request.notes,
    )
    _invalidate_insights_cache(request.user_id)
    if request.recurrence and isinstance(result, dict):
        result["recurrence"] = request.recurrence
    return result


@app.patch("/tasks/{task_id}", response_model=Dict[str, Any], tags=["Tasks"])
async def update_task_endpoint(
    task_id: str,
    request: Dict[str, Any],
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
) -> Dict[str, Any]:
    """Update task fields (status, priority, title, due_date, notes, tags)."""
    user_id = _resolved_user_id(current_user)
    request.pop("user_id", None)
    if DEMO_MODE:
        now_iso = datetime.now(LOCAL_TZ).isoformat()
        return {"id": task_id, "updated_at": now_iso, **request, "demo": True}
    updates = {k: v for k, v in request.items() if v is not None}
    updated = await update_task(task_id=task_id, updates=updates, user_id=user_id)
    _invalidate_insights_cache(user_id)
    return updated


@app.delete("/tasks/{task_id}", response_model=Dict[str, Any], tags=["Tasks"])
async def delete_task_endpoint(
    task_id: str,
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
) -> Dict[str, Any]:
    """Delete a task by ID."""
    user_id = _resolved_user_id(current_user)
    if DEMO_MODE:
        return {"deleted": True, "task_id": task_id, "demo": True}
    await delete_task(task_id=task_id, user_id=user_id)
    _invalidate_insights_cache(user_id)
    return {"deleted": True, "task_id": task_id}


# ══════════════════════════════════════════════════════════════════════════════
# EVENTS
# ══════════════════════════════════════════════════════════════════════════════


@app.get("/events", response_model=EventListResponse, tags=["Calendar"])
async def get_events(
    time_min: Optional[str] = None,
    time_max: Optional[str] = None,
    max_results: int = 10,
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
) -> EventListResponse:
    """List calendar events in a time window."""
    if DEMO_MODE:
        events = MOCK_EVENTS[:max_results]
        return EventListResponse(events=events, count=len(events))
    user_id = (current_user or {}).get("user_id", "default_user")
    ck = _events_cache_key(user_id, time_min, time_max, max_results)
    cached = _events_cache.get(ck)
    now_ts = time.time()
    if cached and (now_ts - cached.get("ts", 0) <= _events_cache_ttl_seconds):
        return EventListResponse(events=cached["events"], count=len(cached["events"]))
    events = await list_calendar_events(
        time_min=time_min, time_max=time_max, max_results=max_results,
        user_id=user_id,
    )
    if not events and user_id != "default_user":
        events = await list_calendar_events(
            time_min=time_min, time_max=time_max, max_results=max_results,
            user_id="default_user",
        )
    _events_cache[ck] = {"ts": now_ts, "events": events}
    return EventListResponse(events=events, count=len(events))


@app.get("/events/{event_id}", response_model=Dict[str, Any], tags=["Calendar"])
async def get_event_by_id(
    event_id: str,
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
) -> Dict[str, Any]:
    """Fetch a single event by ID from Google Calendar authoritative lookup."""
    if DEMO_MODE:
        for e in MOCK_EVENTS:
            if str(e.get("id")) == event_id:
                return e
        raise HTTPException(status_code=404, detail="Event not found")

    user_id = _resolved_user_id(current_user)
    for uid in _legacy_user_ids(user_id):
        e = await get_calendar_event_any_source(event_id=event_id, user_id=uid)
        if e:
            return e
    raise HTTPException(status_code=404, detail="Event not found")


@app.post("/events", response_model=Dict[str, Any], tags=["Calendar"])
async def create_event_endpoint(
    request: EventCreateRequest,
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
) -> Dict[str, Any]:
    """Create a calendar event directly (bypasses conflict-checking)."""
    if DEMO_MODE:
        return {
            "id": f"evt_{uuid.uuid4().hex[:8]}",
            "summary": request.summary,
            "start": {"dateTime": request.start_datetime},
            "end": {"dateTime": request.start_datetime},
            "htmlLink": "https://calendar.google.com/demo",
            "status": "confirmed",
            "clash": False,
        }
    user_id = _resolved_user_id(current_user)
    created = await create_calendar_event(
        summary=request.summary,
        start_datetime=request.start_datetime,
        duration_minutes=request.duration_minutes,
        description=request.description,
        location=request.location,
        attendees=request.attendees,
        recurrence_type=request.recurrence_type,
        recurrence_days=request.recurrence_days,
        recurrence_end_date=request.recurrence_end_date,
        user_id=user_id,
    )
    _invalidate_events_cache(user_id)
    _invalidate_insights_cache(user_id)
    return created


@app.patch("/events/{event_id}", response_model=Dict[str, Any], tags=["Calendar"])
async def patch_event_endpoint(
    event_id: str,
    request: EventCreateRequest,
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
) -> Dict[str, Any]:
    """Patch (update) an existing Google Calendar event by ID."""
    if DEMO_MODE:
        return {
            "id": event_id,
            "summary": request.summary,
            "start": {"dateTime": request.start_datetime},
            "end": {"dateTime": request.start_datetime},
            "htmlLink": "https://calendar.google.com/demo",
            "status": "confirmed",
        }
    user_id = _resolved_user_id(current_user)
    updated = await patch_calendar_event(
        event_id=event_id,
        summary=request.summary,
        start_datetime=request.start_datetime,
        duration_minutes=request.duration_minutes,
        description=request.description,
        attendees=request.attendees,
        user_id=user_id,
    )
    _invalidate_events_cache(user_id)
    _invalidate_insights_cache(user_id)
    return updated


# ══════════════════════════════════════════════════════════════════════════════
# NOTES
# ══════════════════════════════════════════════════════════════════════════════


@app.get("/notes", response_model=NoteListResponse, tags=["Notes"])
async def get_notes(
    tag: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
) -> NoteListResponse:
    """List notes with optional tag filter. Supports pagination via offset/limit."""
    user_id = (current_user or {}).get("user_id", "default_user")
    limit = max(1, min(limit, 200))
    if DEMO_MODE:
        notes = MOCK_NOTES
        if tag:
            notes = [n for n in notes if tag in n.get("tags", [])]
        notes = notes[offset: offset + limit]
        return NoteListResponse(notes=notes, count=len(notes))
    merged: List[Dict[str, Any]] = []
    seen: set = set()
    for uid in _legacy_user_ids(user_id):
        items = await list_notes(user_id=uid, tag=tag, limit=limit)
        for n in items:
            nid = n.get("id")
            if nid and nid in seen:
                continue
            if nid:
                seen.add(nid)
            merged.append(n)
    return NoteListResponse(notes=merged[offset: offset + limit], count=len(merged))


@app.post("/notes", response_model=Dict[str, Any], tags=["Notes"], status_code=201)
async def create_note_endpoint(
    request: NoteCreateRequest,
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
) -> Dict[str, Any]:
    """Create a new note directly."""
    request.user_id = _resolved_user_id(current_user)
    if DEMO_MODE:
        now_iso = datetime.now(LOCAL_TZ).isoformat()
        return {
            "id": str(uuid.uuid4()),
            "title": request.title,
            "content": request.content,
            "tags": request.tags or [],
            "user_id": request.user_id,
            "created_at": now_iso,
            "updated_at": now_iso,
        }
    return await create_note(
        title=request.title,
        content=request.content,
        tags=request.tags,
        user_id=request.user_id,
    )


@app.delete("/notes/{note_id}", response_model=Dict[str, Any], tags=["Notes"])
async def delete_note_endpoint(
    note_id: str,
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
) -> Dict[str, Any]:
    """Delete note directly via API so UI state persists across reloads."""
    if DEMO_MODE:
        return {"deleted": True, "note_id": note_id, "demo": True}
    try:
        return await delete_note(note_id=note_id, user_id=_resolved_user_id(current_user))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# ══════════════════════════════════════════════════════════════════════════════
# ANALYTICS
# ══════════════════════════════════════════════════════════════════════════════


@app.get("/analytics", response_model=Dict[str, Any], tags=["Analytics"])
async def get_analytics(
    days: int = 7,
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
) -> Dict[str, Any]:
    """Get a comprehensive productivity analytics report."""
    user_id = _resolved_user_id(current_user)
    days = max(1, min(days, 365))
    if DEMO_MODE:
        if user_id not in _demo_events:
            empty = {"score": 0, "label": "No data", "completion_rate_pct": 0.0, "overdue_count": 0, "high_priority_completed": 0, "period_days": days}
            return {"productivity_score": empty, "completion_rate": {"total_tasks": 0, "completed_tasks": 0, "overdue_tasks": 0, "completion_rate_pct": 0.0, "period_days": days}, "weekly_trends": [], "period_days": days}
        return {**MOCK_ANALYTICS, "period_days": days}
    _empty_score = {"score": 0, "label": "No data", "completion_rate_pct": 0.0, "overdue_count": 0, "high_priority_completed": 0, "period_days": days}
    _empty_rate = {"total_tasks": 0, "completed_tasks": 0, "overdue_tasks": 0, "completion_rate_pct": 0.0, "period_days": days}
    try:
        import asyncio as _asyncio
        from google.api_core.exceptions import FailedPrecondition
        score, rate, trends = await _asyncio.gather(
            get_productivity_score(user_id=user_id, days=days),
            get_task_completion_rate(user_id=user_id, days=days),
            get_weekly_trends(user_id=user_id, weeks=max(1, days // 7)),
            return_exceptions=True,
        )
        if isinstance(score, Exception):
            logger.warning("analytics: productivity score error: %s", score)
            score = _empty_score
        if isinstance(rate, Exception):
            logger.warning("analytics: completion rate error: %s", rate)
            rate = _empty_rate
        if isinstance(trends, (Exception, type(None))):
            logger.warning("analytics: trends error: %s", trends)
            trends = []
    except Exception as exc:
        logger.error("analytics endpoint error: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Analytics unavailable — please try again later")
    return {
        "productivity_score": score,
        "completion_rate": rate,
        "weekly_trends": trends,
        "period_days": days,
    }


# ══════════════════════════════════════════════════════════════════════════════
# UNDO
# ══════════════════════════════════════════════════════════════════════════════


@app.post("/undo", response_model=Dict[str, Any], tags=["System"])
async def undo(current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """Undo the most recent mutating action for a user."""
    user_id = current_user["user_id"]
    if DEMO_MODE:
        events = _demo_events.get(user_id, [])
        if not events:
            raise HTTPException(status_code=404, detail="No actions to undo")
        last = events.pop()
        return {
            "undone": last["action"],
            "entity_id": last["entity_id"],
            "collection": last["collection"],
            "action_taken": "deleted",
            "demo": True,
        }
    try:
        return await undo_last_action(user_id=user_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/undo/multiple", response_model=Dict[str, Any], tags=["System"])
async def undo_multiple_actions(
    count: int = 1,
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """Undo the last N mutating actions for a user (max 10)."""
    user_id = current_user["user_id"]
    count = max(1, min(count, 10))
    if DEMO_MODE:
        events = _demo_events.get(user_id, [])
        to_undo = min(count, len(events))
        undone = []
        for _ in range(to_undo):
            ev = events.pop()
            undone.append({"action": ev["action"], "entity_id": ev["entity_id"], "collection": ev["collection"]})
        return {
            "undone": undone,
            "total_undone": to_undo,
            "stopped_early": to_undo < count,
            "demo": True,
        }
    try:
        return await undo_multiple(count=count, user_id=user_id)
    except Exception as exc:
        logger.error("undo_multiple error: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Undo operation failed")


# ══════════════════════════════════════════════════════════════════════════════
# BRIEFING
# ══════════════════════════════════════════════════════════════════════════════


@app.post("/briefing", response_model=BriefingResponse, tags=["Briefing"])
async def trigger_briefing(request: BriefingRequest) -> BriefingResponse:
    """Compose and send the morning briefing email."""
    if DEMO_MODE:
        recipient = request.recipient_email or settings.GMAIL_USER_EMAIL or "demo@example.com"
        return BriefingResponse(
            sent=False,
            dry_run=True,
            recipient=recipient,
            subject=f"[Smart Daily Planner] Your Morning Briefing — {datetime.now(LOCAL_TZ).strftime('%Y-%m-%d')}",
            message_id=None,
            body_preview=(
                "Good morning! Here's your daily briefing:\n\n"
                "📋 TASKS TODAY\n"
                "• Fix onboarding bug (OVERDUE - High)\n"
                "• Review Q3 financial report (Due today - Urgent)\n\n"
                "📅 CALENDAR\n"
                "• 9:00 AM — Team Standup (30 min)\n"
                "• 11:00 AM — Q3 Planning Session (2 hrs)\n\n"
                "💡 Focus tip: Tackle the onboarding bug first — it's blocking conversion.\n\n"
                "[Demo mode — Gmail API not connected]"
            ),
        )
    try:
        # If a custom_message is provided, send it as a rich HTML email
        if request.custom_message:
            from agents.briefing_agent import _send_via_smtp, _compose_custom_html
            to_email = request.recipient_email or settings.BRIEFING_RECIPIENT_EMAIL or settings.GMAIL_USER_EMAIL
            subject = request.subject or "[Smart Daily Planner] Notification"
            html_body = _compose_custom_html(subject=subject, body=request.custom_message)
            msg_id = _send_via_smtp(
                to_email=to_email, subject=subject, body=request.custom_message,
                html_body=html_body, sender_email=request.sender_email or "",
            )
            return BriefingResponse(
                sent=True, dry_run=False, recipient=to_email,
                subject=subject, message_id=msg_id, body_preview=request.custom_message[:200],
            )
        result = await send_briefing_email(
            recipient_email=request.recipient_email,
            user_id=request.user_id,
            sender_email=request.sender_email or "",
        )
        return BriefingResponse(**result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/briefing/scheduled", response_model=BriefingResponse, tags=["Briefing"])
async def trigger_scheduled_briefing(
    request: BriefingRequest,
    scheduler_secret: Optional[str] = Header(default=None, alias="X-Scheduler-Secret"),
) -> BriefingResponse:
    """Scheduler-safe wrapper to send a daily briefing from stored defaults."""
    if settings.SCHEDULER_SHARED_SECRET and scheduler_secret != settings.SCHEDULER_SHARED_SECRET:
        raise HTTPException(status_code=401, detail="Invalid scheduler secret.")
    try:
        user_id = request.user_id or "default_user"
        recipient = request.recipient_email
        sender = request.sender_email or ""
        if not DEMO_MODE:
            try:
                from tools.firestore_tools import get_user_profile
                profile = await get_user_profile(user_id)
                recipient = recipient or profile.get("briefing_recipient_email") or settings.BRIEFING_RECIPIENT_EMAIL or settings.GMAIL_USER_EMAIL
                sender = sender or profile.get("briefing_sender_email") or ""
            except Exception:
                recipient = recipient or settings.BRIEFING_RECIPIENT_EMAIL or settings.GMAIL_USER_EMAIL
        else:
            recipient = recipient or settings.BRIEFING_RECIPIENT_EMAIL or settings.GMAIL_USER_EMAIL
        return await trigger_briefing(
            BriefingRequest(
                recipient_email=recipient,
                user_id=user_id,
                sender_email=sender,
                custom_message=request.custom_message,
                subject=request.subject,
            )
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/briefing/day-end", response_model=BriefingResponse, tags=["Briefing"])
async def trigger_day_end_briefing(
    request: DayEndRequest,
    scheduler_secret: Optional[str] = Header(default=None, alias="X-Scheduler-Secret"),
) -> BriefingResponse:
    """Send concise end-of-day summary email."""
    if settings.SCHEDULER_SHARED_SECRET and scheduler_secret != settings.SCHEDULER_SHARED_SECRET:
        raise HTTPException(status_code=401, detail="Invalid scheduler secret.")
    user_id = request.user_id or "default_user"
    if DEMO_MODE:
        recipient = request.recipient_email or settings.BRIEFING_RECIPIENT_EMAIL or settings.GMAIL_USER_EMAIL
        return BriefingResponse(
            sent=False,
            dry_run=True,
            recipient=recipient or "demo@example.com",
            subject=request.subject or f"[Smart Daily Planner] Day-End Summary — {datetime.now(LOCAL_TZ).strftime('%Y-%m-%d')}",
            message_id=None,
            body_preview="Demo day-end summary generated.",
        )
    try:
        tasks = await list_tasks(user_id=user_id, limit=80)
        today_iso = datetime.now(LOCAL_TZ).date().isoformat()
        completed_today = [
            t for t in tasks
            if t.get("status") == "completed"
            and (t.get("updated_at", "")[:10] == today_iso or t.get("created_at", "")[:10] == today_iso)
        ]
        pending = [t for t in tasks if t.get("status") != "completed"]
        overdue = [t for t in pending if t.get("status") == "overdue"]
        events = await list_calendar_events(max_results=30, user_id=user_id)
        todays_events = [e for e in events if (e.get("start", "")[:10] == today_iso)]

        lines = [
            f"🌙 Day-End Summary · {today_iso}",
            f"✅ Completed: {len(completed_today)}",
            f"📌 Pending: {len(pending)} (Overdue: {len(overdue)})",
            f"📅 Events attended today: {len(todays_events)}",
        ]
        if completed_today:
            lines.append("Top completions:")
            for t in completed_today[:4]:
                lines.append(f"- {t.get('title','Untitled')}")
        if pending:
            lines.append("Top priorities for tomorrow:")
            pr = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
            for t in sorted(pending, key=lambda x: (pr.get(x.get("priority", "medium"), 2), x.get("due_date", "9999")))[:3]:
                lines.append(f"- {t.get('title','Untitled')} ({t.get('priority','medium')})")
        content = "\n".join(lines)
        return await trigger_briefing(
            BriefingRequest(
                recipient_email=request.recipient_email,
                user_id=user_id,
                sender_email=request.sender_email,
                custom_message=content,
                subject=request.subject or f"[Smart Daily Planner] Day-End Summary — {today_iso}",
            )
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ══════════════════════════════════════════════════════════════════════════════
# SMART RESCHEDULE (AI Rescue Plan)
# ══════════════════════════════════════════════════════════════════════════════


class MeetingInviteRequest(BaseModel):
    to_email: str
    summary: str
    start_datetime: str
    duration_minutes: int = Field(default=60)
    description: str = Field(default="")
    sender_email: Optional[str] = Field(None, description="Which account sends the invite. Defaults to primary Gmail.")
    rrule: Optional[str] = Field(default="", description="RFC 5545 RRULE string for recurring invites.")


@app.post("/meeting-invite", response_model=BriefingResponse, tags=["Briefing"])
async def send_meeting_invite_endpoint(request: MeetingInviteRequest) -> BriefingResponse:
    """Send a proper iCalendar meeting invite via SMTP (Gmail or Yahoo).
    The recipient sees an 'Add to Calendar' button in their email client."""
    if DEMO_MODE:
        sender = request.sender_email or settings.GMAIL_USER_EMAIL or "demo@example.com"
        return BriefingResponse(sent=False, dry_run=True, recipient=request.to_email,
            subject=f"[Meeting Invite] {request.summary}", message_id=None,
            body_preview=f"Demo mode — would send from {sender}")
    try:
        from agents.briefing_agent import send_meeting_invite
        msg_id = await send_meeting_invite(
            to_email=request.to_email,
            summary=request.summary,
            start_datetime_str=request.start_datetime,
            duration_minutes=request.duration_minutes,
            description=request.description,
            sender_email=request.sender_email or "",
            rrule=request.rrule or "",
        )
        sender = request.sender_email or settings.GMAIL_USER_EMAIL
        return BriefingResponse(sent=True, dry_run=False, recipient=request.to_email,
            subject=f"[Meeting Invite] {request.summary}", message_id=msg_id,
            body_preview=f"Invite sent from {sender} for: {request.summary}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ══════════════════════════════════════════════════════════════════════════════
# EMAIL ACCOUNTS (multi-account send support)
# ══════════════════════════════════════════════════════════════════════════════


class EmailAccountRequest(BaseModel):
    user_id: str = Field(default="default_user")
    default_email: str = Field(..., description="Email address to set as default sender.")


class AddEmailAccountRequest(BaseModel):
    user_id: str = Field(default="default_user")
    email: str = Field(..., description="Email address to add.")
    app_password: str = Field(..., description="App password for the account.")
    provider: str = Field(default="gmail", description="Provider: gmail or yahoo.")
    label: str = Field(default="", description="Display label for the account.")


@app.get("/settings/email-accounts", response_model=Dict[str, Any], tags=["Settings"])
async def get_email_accounts(
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
) -> Dict[str, Any]:
    """Return all configured email accounts and the current default sender."""
    accounts = []
    # Primary Gmail account — always present if configured
    if settings.GMAIL_USER_EMAIL:
        has_password = bool(settings.GMAIL_APP_PASSWORD or os.environ.get("GMAIL_APP_PASSWORD", ""))
        accounts.append({
            "email": settings.GMAIL_USER_EMAIL,
            "provider": "gmail",
            "label": "Gmail (Primary)",
            "ready": has_password,
            "can_delete": False,
        })
    # .env secondary Gmail
    if settings.GMAIL2_EMAIL:
        has_password = bool(settings.GMAIL2_APP_PASSWORD or os.environ.get("GMAIL2_APP_PASSWORD", ""))
        accounts.append({
            "email": settings.GMAIL2_EMAIL,
            "provider": "gmail",
            "label": "Gmail (Secondary)",
            "ready": has_password,
            "can_delete": False,
        })
    # Yahoo account — present if YAHOO_EMAIL is configured
    if settings.YAHOO_EMAIL:
        has_password = bool(settings.YAHOO_APP_PASSWORD or os.environ.get("YAHOO_APP_PASSWORD", ""))
        accounts.append({
            "email": settings.YAHOO_EMAIL,
            "provider": "yahoo",
            "label": "Yahoo Mail",
            "ready": has_password,
            "can_delete": False,
        })

    # Read default preference and linked OAuth accounts from Firestore
    user_id = _resolved_user_id(current_user)
    default_email = settings.GMAIL_USER_EMAIL
    if not DEMO_MODE:
        try:
            from tools.firestore_tools import get_user_profile, get_linked_gmail_accounts
            profile = await get_user_profile(user_id)
            default_email = profile.get("default_sender_email") or settings.GMAIL_USER_EMAIL
            linked = await get_linked_gmail_accounts(user_id)
            existing_emails = {a["email"].lower() for a in accounts}
            for la in linked:
                if la.get("email", "").lower() not in existing_emails:
                    accounts.append({
                        "email": la["email"],
                        "provider": "gmail",
                        "label": la.get("name") or la["email"],
                        "ready": bool(la.get("refresh_token")),
                        "can_delete": True,
                        "oauth": True,
                        "calendar_visible": la.get("calendar_visible", True),
                        "email_send_enabled": la.get("email_send_enabled", True),
                        "picture": la.get("picture", ""),
                    })
        except Exception:
            pass

    # Mark which account is default
    for acct in accounts:
        acct["is_default"] = acct["email"] == default_email

    return {"accounts": accounts, "default_email": default_email, "count": len(accounts)}


@app.patch("/settings/email-accounts/default", response_model=Dict[str, Any], tags=["Settings"])
async def set_default_email_account(request: EmailAccountRequest) -> Dict[str, Any]:
    """Set the default sender email account for a user."""
    # Validate the email is a known account
    known = [settings.GMAIL_USER_EMAIL]
    if settings.GMAIL2_EMAIL:
        known.append(settings.GMAIL2_EMAIL)
    if settings.YAHOO_EMAIL:
        known.append(settings.YAHOO_EMAIL)
    if request.default_email not in known:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown email account '{request.default_email}'. "
                   f"Configured accounts: {known}",
        )
    if not DEMO_MODE:
        try:
            from tools.firestore_tools import update_user_profile
            await update_user_profile(request.user_id, {"default_sender_email": request.default_email})
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))
    return {"default_email": request.default_email, "updated": True}


@app.get("/settings/briefing-schedule", response_model=Dict[str, Any], tags=["Settings"])
async def get_briefing_schedule(
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
) -> Dict[str, Any]:
    """Return user's daily briefing schedule and recipient preferences."""
    user_id = _resolved_user_id(current_user)
    profile = {}
    if not DEMO_MODE:
        try:
            from tools.firestore_tools import get_user_profile
            profile = await get_user_profile(user_id)
        except Exception:
            profile = {}
    briefing_time = profile.get("briefing_time", "08:00")
    day_end_time = profile.get("day_end_time", "19:30")
    timezone = profile.get("timezone", settings.DEFAULT_TIMEZONE)
    recipient = profile.get("briefing_recipient_email") or settings.BRIEFING_RECIPIENT_EMAIL or settings.GMAIL_USER_EMAIL
    sender = profile.get("briefing_sender_email", "")
    enabled = profile.get("briefing_enabled", True)
    return {
        "user_id": user_id,
        "recipient_email": recipient,
        "sender_email": sender,
        "briefing_time": briefing_time,
        "day_end_time": day_end_time,
        "timezone": timezone,
        "enabled": enabled,
        "cloud_scheduler_management_enabled": settings.ENABLE_CLOUD_SCHEDULER_MANAGEMENT,
        "scheduler_region": settings.CLOUD_SCHEDULER_REGION,
    }


@app.patch("/settings/briefing-schedule", response_model=Dict[str, Any], tags=["Settings"])
async def set_briefing_schedule(
    request: BriefingScheduleRequest,
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
) -> Dict[str, Any]:
    """Update briefing schedule and optionally push update to Cloud Scheduler."""
    user_id = _resolved_user_id(current_user)
    if request.user_id and request.user_id != user_id:
        raise HTTPException(status_code=403, detail="user_id mismatch.")
    hour, minute = [int(p) for p in request.briefing_time.split(":")]
    day_end_hour, day_end_minute = [int(p) for p in request.day_end_time.split(":")]
    if not DEMO_MODE:
        try:
            from tools.firestore_tools import update_user_profile
            await update_user_profile(user_id, {
                "briefing_time": request.briefing_time,
                "day_end_time": request.day_end_time,
                "timezone": request.timezone,
                "briefing_recipient_email": request.recipient_email,
                "briefing_sender_email": request.sender_email or "",
                "briefing_enabled": request.enabled,
            })
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Could not save schedule settings: {exc}")

    scheduler_result = {"managed": False}
    if settings.ENABLE_CLOUD_SCHEDULER_MANAGEMENT:
        try:
            morning_job = await _upsert_cloud_scheduler_job(
                user_id=user_id,
                recipient_email=request.recipient_email,
                sender_email=request.sender_email or "",
                hour=hour,
                minute=minute,
                timezone=request.timezone,
                enabled=request.enabled,
                schedule_type="morning",
            )
            day_end_job = await _upsert_cloud_scheduler_job(
                user_id=user_id,
                recipient_email=request.recipient_email,
                sender_email=request.sender_email or "",
                hour=day_end_hour,
                minute=day_end_minute,
                timezone=request.timezone,
                enabled=request.enabled,
                schedule_type="day_end",
            )
            scheduler_result = {"managed": True, "morning": morning_job, "day_end": day_end_job}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Saved profile but Cloud Scheduler update failed: {exc}")

    return {
        "updated": True,
        "user_id": user_id,
        "recipient_email": request.recipient_email,
        "sender_email": request.sender_email or "",
        "briefing_time": request.briefing_time,
        "day_end_time": request.day_end_time,
        "timezone": request.timezone,
        "enabled": request.enabled,
        "scheduler": scheduler_result,
    }


class RescheduleRequest(BaseModel):
    user_id: str = Field(default="default_user")
    days_ahead: int = Field(default=5, ge=1, le=14)
    auto_apply: bool = Field(default=False)


@app.post("/smart-reschedule", response_model=Dict[str, Any], tags=["Smart Features"])
async def smart_reschedule_endpoint(request: RescheduleRequest) -> Dict[str, Any]:
    """Generate an AI rescue plan for overdue and urgent tasks."""
    if DEMO_MODE:
        plan = dict(MOCK_RESCUE_PLAN)
        plan["applied"] = None if not request.auto_apply else {}
        plan["hint"] = "Demo mode — plan generated without Gemini."
        return plan
    try:
        return await smart_reschedule(
            user_id=request.user_id,
            days_ahead=request.days_ahead,
            auto_apply=request.auto_apply,
        )
    except Exception as exc:
        import logging
        from google.api_core.exceptions import FailedPrecondition
        logging.getLogger(__name__).warning("smart-reschedule failed: %s", exc)
        msg = str(exc)
        if "requires an index" in msg:
            # Extract the index creation URL for the server log
            import re
            url_match = re.search(r'https://\S+', msg)
            if url_match:
                logging.getLogger(__name__).warning("Create missing index at: %s", url_match.group())
            msg = "Missing Firestore index — plan generated from available task data."
        return {"assignments": [], "unscheduled_tasks": [], "tasks_evaluated": 0,
                "slots_scanned": 0, "applied": None, "summary": msg, "error": str(exc)}


# ══════════════════════════════════════════════════════════════════════════════
# MEETING SUMMARIZER
# ══════════════════════════════════════════════════════════════════════════════


class MeetingSummaryRequest(BaseModel):
    transcript: str = Field(..., description="Meeting transcript or raw notes.")
    meeting_title: Optional[str] = Field(None)
    user_id: str = Field(default="default_user")
    auto_create_tasks: bool = Field(default=True)
    auto_create_note: bool = Field(default=True)
    auto_schedule_followup: bool = Field(default=False)


@app.post("/summarize-meeting", response_model=Dict[str, Any], tags=["Smart Features"])
async def summarize_meeting_endpoint(request: MeetingSummaryRequest) -> Dict[str, Any]:
    """Analyse a meeting transcript and extract structured output."""
    if DEMO_MODE:
        result = dict(MOCK_MEETING_RESULT)
        if request.meeting_title:
            result["analysis"] = dict(result["analysis"])
            result["analysis"]["meeting_title"] = request.meeting_title
        return result
    try:
        result = await summarize_meeting(
            transcript=request.transcript,
            meeting_title=request.meeting_title,
            user_id=request.user_id,
            auto_create_tasks=request.auto_create_tasks,
            auto_create_note=request.auto_create_note,
            auto_schedule_followup=request.auto_schedule_followup,
        )
        analysis = result.get("analysis") or {}
        extracted = analysis.get("action_items") or []
        if not extracted:
            fallback_items = _extract_action_items_from_text(request.transcript)
            if fallback_items:
                analysis = dict(analysis)
                analysis["action_items"] = fallback_items
                result["analysis"] = analysis
        return result
    except Exception as exc:
        logger.warning("summarize-meeting failed: %s", exc)
        fallback_items = _extract_action_items_from_text(request.transcript)
        return {
            "sentiment": "neutral", "sentiment_score": 0.5,
            "analysis": {
                "meeting_title": request.meeting_title or "Meeting",
                "summary": f"AI unavailable: {str(exc)[:200]}",
                "action_items": fallback_items, "decisions": [], "participants": [],
                "follow_up_required": False,
            },
            "tasks_created": 0, "note_id": None, "event_id": None,
            "error": str(exc),
        }


# ══════════════════════════════════════════════════════════════════════════════
# WEEKLY RETROSPECTIVE
# ══════════════════════════════════════════════════════════════════════════════


class RetroRequest(BaseModel):
    user_id: str = Field(default="default_user")
    week_offset: int = Field(default=0, ge=-4, le=0)
    save_as_note: bool = Field(default=True)
    sender_email: Optional[str] = Field(None, description="Send retro via SMTP from this account. If omitted, no email is sent.")


def _send_retro_email_bg(result: Dict[str, Any], sender_email: str) -> None:
    """Send weekly retro email in background thread (SMTP is synchronous)."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText as _MIMEText
    from agents.weekly_retro_agent import _build_html_email, _strip_md
    from agents.briefing_agent import _get_smtp_config
    try:
        recipient = settings.BRIEFING_RECIPIENT_EMAIL or settings.GMAIL_USER_EMAIL
        subject = f"Weekly Retro — {result.get('week_label', 'This Week')} | Score: {result.get('productivity_score', 0)}/100"
        smtp_host, smtp_user, app_password = _get_smtp_config(sender_email)
        html_body  = _build_html_email(result)
        plain_body = _strip_md(result["narrative"])
        msg = MIMEMultipart("alternative")
        msg["From"]    = f"Smart Daily Planner <{smtp_user}>"
        msg["To"]      = recipient
        msg["Subject"] = subject
        msg.attach(_MIMEText(plain_body, "plain", "utf-8"))
        msg.attach(_MIMEText(html_body,  "html",  "utf-8"))
        with smtplib.SMTP_SSL(smtp_host, 465) as srv:
            srv.login(smtp_user, app_password)
            srv.sendmail(smtp_user, recipient, msg.as_string())
        logger.info("Retro email sent to %s", recipient)
    except Exception as exc:
        logger.warning("Retro email failed: %s", exc)


@app.post("/weekly-retro", response_model=Dict[str, Any], tags=["Smart Features"])
async def weekly_retro_endpoint(request: RetroRequest, background_tasks: BackgroundTasks) -> Dict[str, Any]:
    """Generate an AI-written personalised weekly retrospective."""
    if DEMO_MODE:
        from datetime import timedelta as _td
        offset_date = datetime.now(LOCAL_TZ) + _td(weeks=request.week_offset)
        week_start = offset_date - _td(days=offset_date.weekday())
        retro = dict(MOCK_RETRO)
        retro["week_label"] = f"Week of {week_start.strftime('%b %d, %Y')}"
        return retro
    try:
        if request.save_as_note:
            result = await save_retro_as_note(
                user_id=request.user_id,
                week_offset=request.week_offset,
            )
        else:
            result = await generate_weekly_retro(
                user_id=request.user_id,
                week_offset=request.week_offset,
            )
        # Queue email delivery in background — does not block the HTTP response
        if request.sender_email and result.get("narrative"):
            background_tasks.add_task(_send_retro_email_bg, result, request.sender_email)
            result["email_queued"] = True
            result["email_sender"] = request.sender_email
        return result
    except Exception as exc:
        logger.warning("weekly-retro failed: %s", exc)
        return {
            "week_label": "This week",
            "narrative": f"⚠️ AI unavailable: {str(exc)[:200]}",
            "stats": {}, "saved_note_id": None, "error": str(exc)
        }


# ══════════════════════════════════════════════════════════════════════════════
# SMART SUGGESTIONS
# ══════════════════════════════════════════════════════════════════════════════


class TagSuggestionRequest(BaseModel):
    content: str = Field(..., description="Task title or note content to analyse.")
    content_type: str = Field(default="task")


@app.post("/suggest/tags", response_model=Dict[str, Any], tags=["Smart Features"])
async def suggest_tags_endpoint(request: TagSuggestionRequest) -> Dict[str, Any]:
    """Get AI tag suggestions for a task or note content."""
    if DEMO_MODE:
        # Generate plausible mock tags based on keywords
        content_lower = request.content.lower()
        tags = []
        if any(w in content_lower for w in ["bug", "fix", "error", "crash"]):
            tags = ["bug", "tech", "urgent"]
        elif any(w in content_lower for w in ["meeting", "sync", "call", "standup"]):
            tags = ["meeting", "work", "communication"]
        elif any(w in content_lower for w in ["report", "finance", "budget", "q3", "q4"]):
            tags = ["finance", "reporting", "review"]
        elif any(w in content_lower for w in ["design", "ui", "ux", "figma"]):
            tags = ["design", "product", "visual"]
        elif any(w in content_lower for w in ["doc", "write", "blog", "content"]):
            tags = ["content", "writing", "communication"]
        else:
            tags = ["work", "task", "general"]
        return {**MOCK_TAGS, "suggested_tags": tags}
    try:
        result = await suggest_tags(content=request.content, content_type=request.content_type)
        tags = (result or {}).get("suggested_tags") or []
        reasoning = ((result or {}).get("reasoning") or "").lower()
        if not tags or any(
            k in reasoning
            for k in ("429", "quota", "resource_exhausted", "billing", "ai_quota", "rate_limited")
        ):
            return {
                "content_type": request.content_type,
                "suggested_tags": _heuristic_tags(request.content),
                "reasoning": "Fallback tag suggestion used due to temporary AI quota/availability limits.",
            }
        return result
    except Exception as exc:
        logger.warning("suggest-tags failed, using heuristic tags: %s", exc)
        tags = _heuristic_tags(request.content)
        return {
            "content_type": request.content_type,
            "suggested_tags": tags,
            "reasoning": "Fallback tag suggestion used due to temporary AI quota/availability limits.",
        }


class PriorityRecommendRequest(BaseModel):
    user_id: str = Field(default="default_user")
    dry_run: bool = Field(default=True)


@app.post("/suggest/priorities", response_model=Dict[str, Any], tags=["Smart Features"])
async def suggest_priorities_endpoint(request: PriorityRecommendRequest) -> Dict[str, Any]:
    """Get AI priority recommendations for all pending tasks."""
    if DEMO_MODE:
        result = dict(MOCK_PRIORITY_RECS)
        result["dry_run"] = request.dry_run
        return result
    try:
        return await recommend_priorities(user_id=request.user_id, dry_run=request.dry_run)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/risk-analysis", response_model=Dict[str, Any], tags=["Smart Features"])
async def risk_analysis_endpoint(
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
) -> Dict[str, Any]:
    """Analyse deadline risk across all pending tasks."""
    user_id = _resolved_user_id(current_user)
    if DEMO_MODE:
        return dict(MOCK_RISK)
    cached = _insights_cache.get(user_id)
    if cached and (time.time() - cached.get("ts", 0) <= _insights_cache_ttl_seconds):
        risk_cached = cached.get("risk")
        if risk_cached:
            return risk_cached
    try:
        risk = await analyse_deadline_risk(user_id=user_id)
        bucket = _insights_cache.get(user_id) or {}
        bucket.update({"ts": time.time(), "risk": risk})
        _insights_cache[user_id] = bucket
        return risk
    except Exception as exc:
        logger.warning("risk-analysis failed: %s", exc)
        return {"at_risk_tasks": [], "overloaded_days": [], "risk_score": 0,
                "recommendation": "AI analysis temporarily unavailable"}


@app.get("/focus-plan", response_model=Dict[str, Any], tags=["Smart Features"])
async def focus_plan_endpoint(
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
) -> Dict[str, Any]:
    """Get an AI-generated focus order for today's tasks."""
    user_id = _resolved_user_id(current_user)
    if DEMO_MODE:
        return dict(MOCK_FOCUS_PLAN)
    cached = _insights_cache.get(user_id)
    if cached and (time.time() - cached.get("ts", 0) <= _insights_cache_ttl_seconds):
        focus_cached = cached.get("focus")
        if focus_cached:
            return focus_cached
    try:
        focus = await get_daily_focus_recommendation(user_id=user_id)
        bucket = _insights_cache.get(user_id) or {}
        bucket.update({"ts": time.time(), "focus": focus})
        _insights_cache[user_id] = bucket
        return focus
    except Exception as exc:
        logger.warning("focus-plan failed: %s", exc)
        return {
            "focus_order": [],
            "motivational_message": "No AI plan right now. Showing your highest-priority tasks instead.",
            "total_tasks": 0,
        }


@app.get("/insights/summary", response_model=Dict[str, Any], tags=["Smart Features"])
async def insights_summary_endpoint(
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
) -> Dict[str, Any]:
    """Load risk analysis + focus plan together with cache for faster Insights tab."""
    user_id = _resolved_user_id(current_user)
    if DEMO_MODE:
        return {"risk": dict(MOCK_RISK), "focus": dict(MOCK_FOCUS_PLAN), "cached": False}
    cached = _insights_cache.get(user_id)
    if cached and (time.time() - cached.get("ts", 0) <= _insights_cache_ttl_seconds):
        if cached.get("risk") and cached.get("focus"):
            return {"risk": cached["risk"], "focus": cached["focus"], "cached": True}
    try:
        risk, focus = await asyncio.gather(
            analyse_deadline_risk(user_id=user_id),
            get_daily_focus_recommendation(user_id=user_id),
        )
        _insights_cache[user_id] = {"ts": time.time(), "risk": risk, "focus": focus}
        return {"risk": risk, "focus": focus, "cached": False}
    except Exception as exc:
        logger.warning("insights-summary failed: %s", exc)
        return {
            "risk": {"at_risk_tasks": [], "overloaded_days": [], "risk_score": 0, "recommendation": "AI analysis temporarily unavailable"},
            "focus": {
                "focus_order": [],
                "motivational_message": "No AI plan right now. Showing your highest-priority tasks instead.",
                "total_tasks": 0,
            },
            "cached": False,
        }


# ══════════════════════════════════════════════════════════════════════════════
# DEMO: Multi-step orchestration
# ══════════════════════════════════════════════════════════════════════════════


@app.post("/demo/multi-step", response_model=List[QueryResponse], tags=["Demo"])
async def demo_multi_step(request: MultiStepRequest) -> List[QueryResponse]:
    """Demonstrate multi-intent orchestration with three sequential queries."""
    session_id = request.session_id or f"demo-{uuid.uuid4().hex[:8]}"
    steps = [
        "Create a high-priority task: Prepare Q3 financial report, due next Friday",
        "Schedule a team meeting tomorrow at 10am for 90 minutes called 'Q3 Planning'",
        "What is my productivity score this week?",
    ]
    if DEMO_MODE:
        return [
            QueryResponse(**{**get_mock_query_response(s), "session_id": session_id})
            for s in steps
        ]
    responses = []
    for step in steps:
        result = await run_orchestrator(
            message=step,
            user_id=request.user_id,
            session_id=session_id,
        )
        responses.append(QueryResponse(**result))
    return responses


# ══════════════════════════════════════════════════════════════════════════════
# AI SMART TIME BLOCKING
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/time-blocks/suggest", response_model=Dict[str, Any], tags=["Smart Features"])
async def suggest_time_blocks(
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """Suggest AI-generated time blocks for pending tasks based on calendar gaps."""
    user_id = current_user["user_id"]
    try:
        from tools.firestore_tools import list_tasks as _list_tasks
        from tools.calendar_tools import list_calendar_events
        tasks_resp = await _list_tasks(user_id=user_id, limit=20)
        pending = [t for t in (tasks_resp.get("tasks") or []) if t.get("status") not in ("completed",)]
        events_resp = await list_calendar_events(max_results=30)
        busy_times = set()
        for ev in (events_resp.get("events") or []):
            start = (ev.get("start") or "")[:10]
            if start: busy_times.add(start)

        from config.settings import LOCAL_TZ
        from datetime import timedelta
        blocks = []
        slot_offset = 1
        priority_order = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
        sorted_tasks = sorted(pending, key=lambda t: priority_order.get(t.get("priority","medium"), 2))
        priority_colors = {
            "urgent": ("rgba(239,68,68,0.15)", "rgba(239,68,68,0.35)", "🔴"),
            "high": ("rgba(245,158,11,0.15)", "rgba(245,158,11,0.35)", "🟡"),
            "medium": ("rgba(99,102,241,0.15)", "rgba(99,102,241,0.35)", "🔵"),
            "low": ("rgba(16,185,129,0.15)", "rgba(16,185,129,0.35)", "🟢"),
        }
        slot_hours = [9, 14, 16, 10, 15]
        for i, task in enumerate(sorted_tasks[:6]):
            day = datetime.now(LOCAL_TZ) + timedelta(days=slot_offset + (i // 2))
            hour = slot_hours[i % len(slot_hours)]
            slot_str = f"{day.strftime('%a %d %b')} {hour}:00–{hour+1}:00"
            bg, border, icon = priority_colors.get(task.get("priority","medium"), priority_colors["medium"])
            est_mins = 90 if task.get("priority") == "urgent" else 60
            blocks.append({
                "title": task["title"], "slot": slot_str, "duration": f"{est_mins} min",
                "color": bg, "borderColor": border, "icon": icon,
                "priority": task.get("priority","medium"), "task_id": task.get("id",""),
            })
        return {"blocks": blocks, "total": len(blocks)}
    except Exception as exc:
        logger.warning("time-blocks failed: %s", exc)
        return {"blocks": [], "error": str(exc)}


# ══════════════════════════════════════════════════════════════════════════════
# PROCRASTINATION COACH
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/procrastination", response_model=Dict[str, Any], tags=["Smart Features"])
async def procrastination_coach(
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """Detect tasks the user is likely procrastinating on and provide coaching."""
    user_id = current_user["user_id"]
    try:
        from tools.firestore_tools import list_tasks as _list_tasks
        tasks_resp = await _list_tasks(user_id=user_id, limit=50)
        all_tasks = tasks_resp.get("tasks") or []
        now = datetime.now(LOCAL_TZ)

        reasons = [
            "The task feels too large and undefined — no clear starting point.",
            "You might be waiting for the 'perfect' moment or more information.",
            "Decision paralysis: multiple valid approaches with no clear winner.",
            "Low energy during the times you've tried to start it.",
            "The task is outside your comfort zone — mild anxiety is normal here.",
        ]
        starters = [
            "Open a blank document and write exactly 3 bullet points: what needs to happen first.",
            "Set a 15-minute timer. Do only the very first physical step, nothing more.",
            "Break it into 3 smaller tasks. Add them now and complete just the first one.",
            "Start with the easiest 20% — build momentum before tackling the hard part.",
            "Write yourself an email: what would 'done' look and feel like for this task?",
        ]
        items = []
        for i, t in enumerate(all_tasks):
            if t.get("status") in ("completed",):
                continue
            created_str = t.get("created_at", "")
            try:
                from dateutil import parser as _dp
                created_dt = _dp.parse(created_str).replace(tzinfo=LOCAL_TZ) if created_str else now
            except Exception:
                created_dt = now
            age_days = max(0, (now - created_dt.replace(tzinfo=None) if created_dt.tzinfo is None else (now.replace(tzinfo=None) - created_dt.replace(tzinfo=None))).days)
            if age_days < 2:
                continue
            items.append({
                "task_id": t.get("id",""),
                "title": t.get("title",""),
                "emoji": "🚨" if t.get("status") == "overdue" else "😬",
                "delay_label": f"Sitting in your list for {age_days} day{'s' if age_days != 1 else ''}",
                "reason": reasons[i % len(reasons)],
                "starter": starters[i % len(starters)],
            })
        return {"items": items[:5], "total": len(items)}
    except Exception as exc:
        logger.warning("procrastination failed: %s", exc)
        return {"items": [], "error": str(exc)}


# ── Exception handlers ────────────────────────────────────────────────────────


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = request.headers.get("X-Request-ID", "unknown")
    logger.error(
        "Unhandled exception | rid=%s | path=%s | %s: %s",
        request_id, request.url.path, type(exc).__name__, exc,
        exc_info=True,
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": "An unexpected error occurred. Please try again later.",
            "request_id": request_id,
        },
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "status_code": exc.status_code},
        headers=getattr(exc, "headers", None),
    )
