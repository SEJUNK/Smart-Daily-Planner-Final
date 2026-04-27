# === tools/calendar_tools.py ===
"""
Google Calendar API helpers for Smart Daily Planner.

Auth strategy:
  Local dev  — reads token.json (created by running: python auth_calendar.py)
  Cloud Run  — uses attached service account via google.auth.default()

Functions exposed as ADK tools must have full Google-style docstrings.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import google.auth
from dateutil import parser as dateutil_parser

from config.settings import settings, LOCAL_TZ

# ── Google Calendar via AuthorizedSession (requests-based) ────────────────────
# We intentionally bypass googleapiclient / httplib2 because google-auth 2.49+
# breaks their auto-refresh path (reauth.refresh_grant → invalid_scope error).
# Using AuthorizedSession (requests-based) gives us reliable token refresh with
# the same credentials object.

_CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"

CALENDAR_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.send",
]

_TOKEN_FILE = Path(__file__).parent.parent / "token.json"
_session = None


async def _get_session_for_user(user_id: Optional[str] = None):
    """Return an AuthorizedSession for the given user.

    Priority:
      1. Per-user OAuth tokens stored in Firestore (when AUTH_ENABLED=true and user signed in).
      2. token.json file (local dev / single-user deployment).
      3. google.auth.default() service account (Cloud Run).
    """
    from google.auth.transport.requests import AuthorizedSession, Request
    from google.oauth2.credentials import Credentials

    # ── Per-user path: look up stored OAuth token in Firestore ────────────────
    if user_id and user_id not in ("default_user", "local@localhost", ""):
        try:
            from google.cloud import firestore as _fs
            _db = _fs.AsyncClient()
            doc = await _db.collection("user_oauth_tokens").document(user_id).get()
            if doc.exists:
                data = doc.to_dict()
                creds = Credentials(
                    token=data.get("access_token"),
                    refresh_token=data.get("refresh_token") or None,
                    token_uri="https://oauth2.googleapis.com/token",
                    client_id=settings.OAUTH_CLIENT_ID,
                    client_secret=settings.OAUTH_CLIENT_SECRET,
                    scopes=CALENDAR_SCOPES,
                )
                # Refresh if expired
                if creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                    await _db.collection("user_oauth_tokens").document(user_id).update({
                        "access_token": creds.token,
                        "token_expiry": __import__("time").time() + 3600,
                    })
                return AuthorizedSession(creds)
        except Exception:
            pass  # fall through to shared credentials

    # ── Shared / fallback path ────────────────────────────────────────────────
    return _get_session()


async def _get_session_for_linked_account(account: dict):
    """Build an AuthorizedSession for a linked Gmail account using stored OAuth tokens."""
    from google.auth.transport.requests import AuthorizedSession, Request
    from google.oauth2.credentials import Credentials
    try:
        creds = Credentials(
            token=account.get("access_token"),
            refresh_token=account.get("refresh_token") or None,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.OAUTH_CLIENT_ID,
            client_secret=settings.OAUTH_CLIENT_SECRET,
            scopes=CALENDAR_SCOPES,
        )
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return AuthorizedSession(creds)
    except Exception:
        return None


def _get_session():
    """Return an AuthorizedSession using the shared token.json or service account."""
    from google.auth.transport.requests import AuthorizedSession, Request
    from google.oauth2.credentials import Credentials

    if _TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(_TOKEN_FILE))
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _TOKEN_FILE.write_text(creds.to_json())
    else:
        creds, _ = google.auth.default(scopes=CALENDAR_SCOPES)

    return AuthorizedSession(creds)


def _parse_dt(dt_str: str) -> datetime:
    """Parse an ISO-8601 or natural datetime string to an aware datetime.

    Args:
        dt_str: Date/datetime string in any dateutil-parseable format.

    Returns:
        Timezone-aware datetime in LOCAL_TZ if no tzinfo provided.
    """
    dt = dateutil_parser.parse(dt_str)
    if dt.tzinfo is None:
        dt = LOCAL_TZ.localize(dt)
    return dt


def _dt_to_rfc3339(dt: datetime) -> str:
    """Format a datetime as RFC-3339 string required by the Calendar API."""
    return dt.isoformat()


# ── Core CRUD ─────────────────────────────────────────────────────────────────


def _raise_for_status(resp) -> None:
    """Raise a RuntimeError with the API error body if status >= 400."""
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("error", {}).get("message", resp.text)
        except Exception:
            detail = resp.text
        raise RuntimeError(f"Calendar API {resp.status_code}: {detail}")


async def create_calendar_event(
    summary: str,
    start_datetime: str,
    duration_minutes: int = 60,
    description: str = "",
    location: str = "",
    attendees: Optional[List[str]] = None,
    calendar_id: Optional[str] = None,
    user_id: Optional[str] = None,
    recurrence_type: Optional[str] = None,
    recurrence_days: Optional[List[str]] = None,
    recurrence_end_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new event in Google Calendar.

    Args:
        summary: Title / name of the calendar event.
        start_datetime: ISO-8601 string for the event start time
            (e.g. '2024-06-15T10:00:00'). Localised to DEFAULT_TIMEZONE
            if no tzinfo is present.
        duration_minutes: Length of the event in minutes. Defaults to 60.
        description: Optional free-text description or agenda.
        location: Optional physical or virtual location string.
        attendees: Optional list of attendee email addresses.
        calendar_id: Target calendar ID. Defaults to GOOGLE_CALENDAR_ID.
        recurrence_type: RFC 5545 frequency: 'weekly', 'daily', 'monthly'.
        recurrence_days: Day codes for weekly recurrence e.g. ['TU', 'TH'].
        recurrence_end_date: Recurrence end date as YYYY-MM-DD string.

    Returns:
        Dict representing the created Google Calendar event resource,
        including 'id', 'htmlLink', 'start', 'end', and 'recurrence'.
    """
    session = await _get_session_for_user(locals().get("user_id"))
    cal_id = calendar_id or settings.GOOGLE_CALENDAR_ID

    start_dt = _parse_dt(start_datetime)
    end_dt = start_dt + timedelta(minutes=duration_minutes)

    event_body: Dict[str, Any] = {
        "summary": summary,
        "description": description,
        "location": location,
        "start": {"dateTime": _dt_to_rfc3339(start_dt), "timeZone": settings.DEFAULT_TIMEZONE},
        "end": {"dateTime": _dt_to_rfc3339(end_dt), "timeZone": settings.DEFAULT_TIMEZONE},
    }
    if attendees:
        event_body["attendees"] = [{"email": e} for e in attendees if e]
        event_body["description"] = (event_body.get("description") or "") + f"\n\nAttendees: {', '.join(attendees)}"

    # Build RRULE for recurring events (RFC 5545)
    if recurrence_type:
        freq = recurrence_type.upper()
        rrule_parts = [f"FREQ={freq}"]
        if recurrence_days:
            rrule_parts.append(f"BYDAY={','.join(d.upper() for d in recurrence_days)}")
        if recurrence_end_date:
            end_recur = _parse_dt(recurrence_end_date).replace(hour=23, minute=59, second=59)
            rrule_parts.append(f"UNTIL={end_recur.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
        event_body["recurrence"] = [f"RRULE:{';'.join(rrule_parts)}"]

    url = f"{_CALENDAR_BASE}/calendars/{cal_id}/events"
    resp = session.post(url, json=event_body, params={"sendUpdates": "none"})
    if resp.status_code >= 400 and attendees:
        # Personal/service-account calendars often reject attendees unless
        # domain-wide delegation is configured. Retry without attendees and rely
        # on explicit invite mail flow handled by the app.
        try:
            detail = resp.text or ""
        except Exception:
            detail = ""
        if "Service accounts cannot invite attendees" in detail:
            event_body.pop("attendees", None)
            resp = session.post(url, json=event_body, params={"sendUpdates": "none"})
    _raise_for_status(resp)
    result = resp.json()
    return {
        "id": result.get("id"),
        "summary": result.get("summary"),
        "start": result.get("start"),
        "end": result.get("end"),
        "htmlLink": result.get("htmlLink"),
        "status": result.get("status"),
    }


async def patch_calendar_event(
    event_id: str,
    summary: Optional[str] = None,
    start_datetime: Optional[str] = None,
    duration_minutes: Optional[int] = None,
    description: Optional[str] = None,
    attendees: Optional[List[str]] = None,
    calendar_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Update (patch) an existing Google Calendar event.

    Args:
        event_id: Google Calendar event ID to update.
        summary: New title (optional).
        start_datetime: New ISO-8601 start datetime (optional).
        duration_minutes: New duration in minutes (optional).
        description: New description (optional).
        calendar_id: Target calendar ID. Defaults to GOOGLE_CALENDAR_ID.

    Returns:
        Updated event resource dict.
    """
    session = await _get_session_for_user(locals().get("user_id"))
    cal_id = calendar_id or settings.GOOGLE_CALENDAR_ID

    body: Dict[str, Any] = {}
    if summary:
        body["summary"] = summary
    if description is not None:
        body["description"] = description
    if attendees is not None:
        body["attendees"] = [{"email": e} for e in attendees if e]
        if description is not None:
            body["description"] = (description or "") + (f"\n\nAttendees: {', '.join(attendees)}" if attendees else "")
    if start_datetime:
        start_dt = _parse_dt(start_datetime)
        end_dt = start_dt + timedelta(minutes=duration_minutes or 60)
        body["start"] = {"dateTime": _dt_to_rfc3339(start_dt), "timeZone": settings.DEFAULT_TIMEZONE}
        body["end"] = {"dateTime": _dt_to_rfc3339(end_dt), "timeZone": settings.DEFAULT_TIMEZONE}

    url = f"{_CALENDAR_BASE}/calendars/{cal_id}/events/{event_id}"
    resp = session.patch(url, json=body, params={"sendUpdates": "none"})
    if resp.status_code >= 400 and "attendees" in body:
        try:
            detail = resp.text or ""
        except Exception:
            detail = ""
        if "Service accounts cannot invite attendees" in detail:
            body.pop("attendees", None)
            resp = session.patch(url, json=body, params={"sendUpdates": "none"})
    _raise_for_status(resp)
    result = resp.json()
    return {
        "id": result.get("id"),
        "summary": result.get("summary"),
        "start": result.get("start"),
        "end": result.get("end"),
        "htmlLink": result.get("htmlLink"),
        "status": result.get("status"),
    }


async def list_calendar_events(
    time_min: Optional[str] = None,
    time_max: Optional[str] = None,
    max_results: int = 20,
    calendar_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List upcoming events from Google Calendar within a time window.

    Args:
        time_min: ISO-8601 datetime string for window start. Defaults to now.
        time_max: ISO-8601 datetime string for window end. Defaults to 7 days
            from now.
        max_results: Maximum number of events to return. Defaults to 20.
        calendar_id: Target calendar ID. Defaults to GOOGLE_CALENDAR_ID.

    Returns:
        List of simplified event dicts with keys:
        'id', 'summary', 'start', 'end', 'description', 'location'.
    """
    cal_id = calendar_id or settings.GOOGLE_CALENDAR_ID

    now = datetime.now(timezone.utc)
    time_min_dt = _parse_dt(time_min).astimezone(timezone.utc) if time_min else now
    time_max_dt = _parse_dt(time_max).astimezone(timezone.utc) if time_max else now + timedelta(days=7)

    # Cap per-source pulls so one heavy account cannot stall initial app load,
    # while still allowing month views to include dense recurring schedules.
    per_source_max = max(25, min(int(max_results or 20), 300))
    query_params = {
        "timeMin": _dt_to_rfc3339(time_min_dt),
        "timeMax": _dt_to_rfc3339(time_max_dt),
        "maxResults": per_source_max,
        "singleEvents": "true",
        "orderBy": "startTime",
    }

    def _normalize(item: Dict[str, Any], source_cal: str = "", account_email: str = "") -> Dict[str, Any]:
        attendees = []
        for a in (item.get("attendees") or []):
            email = (a or {}).get("email")
            if email:
                attendees.append(email)
        if not attendees:
            desc = (item.get("description") or "")
            m = desc and __import__("re").search(r"Attendees:\s*([^\n]+)", desc, __import__("re").IGNORECASE)
            if m and m.group(1):
                attendees = [e.strip() for e in m.group(1).split(",") if e.strip()]
        return {
            "id": item.get("id"),
            "summary": item.get("summary", "(No title)"),
            "start": item.get("start", {}).get("dateTime") or item.get("start", {}).get("date"),
            "end": item.get("end", {}).get("dateTime") or item.get("end", {}).get("date"),
            "description": item.get("description", ""),
            "location": item.get("location", ""),
            "attendees": attendees,
            "calendar_id": source_cal,
            "account_email": account_email,
        }

    # Build primary session from shared app credentials first so the configured
    # primary account calendar (e.g. env/default account) is always available.
    # This preserves the expected "Primary" calendar behavior in the UI.
    _primary_session = None
    try:
        _primary_session = _get_session()
    except Exception:
        # Fallback to user OAuth token if shared credentials are unavailable.
        try:
            _primary_session = await _get_session_for_user(locals().get("user_id"))
        except Exception:
            pass  # no token/session — linked accounts below may still load

    async def _fetch_events_async(session, cid: str) -> List[Dict[str, Any]]:
        if session is None:
            return []
        url = f"{_CALENDAR_BASE}/calendars/{cid}/events"
        try:
            # Run blocking HTTP in a worker thread so one account cannot block
            # the whole async request path.
            resp = await asyncio.to_thread(session.get, url, params=query_params, timeout=6)
            if resp.status_code >= 400:
                return []
            return resp.json().get("items", [])
        except Exception:
            return []

    all_items: List[Dict[str, Any]] = []
    try:
        primary_items = await _fetch_events_async(_primary_session, cal_id)
        all_items = [_normalize(i, cal_id) for i in primary_items]
    except Exception:
        pass

    # Aggregate from additional calendars configured in settings
    extra_ids = [c.strip() for c in settings.ADDITIONAL_CALENDAR_IDS.split(",") if c.strip()]
    seen_ids: set = {i["id"] for i in all_items}
    if extra_ids and _primary_session is not None:
        extra_results = await asyncio.gather(
            *[_fetch_events_async(_primary_session, extra_id) for extra_id in extra_ids],
            return_exceptions=True,
        )
        for extra_id, items in zip(extra_ids, extra_results):
            if isinstance(items, Exception):
                continue
            for item in items:
                ev = _normalize(item, extra_id)
                ev_id = ev.get("id")
                if ev_id and ev_id not in seen_ids:
                    seen_ids.add(ev_id)
                    all_items.append(ev)

    # Aggregate from OAuth-linked accounts (calendar_visible=true)
    # Include "default_user" — when AUTH_ENABLED=false, linked accounts are stored under that key
    if user_id:
        try:
            from tools.firestore_tools import get_linked_gmail_accounts
            linked_accounts = await get_linked_gmail_accounts(user_id)
            linked_sources = []
            for acct in linked_accounts:
                if not acct.get("calendar_visible", True):
                    continue
                if not acct.get("refresh_token") and not acct.get("access_token"):
                    continue
                try:
                    linked_session = await _get_session_for_linked_account(acct)
                    if not linked_session:
                        continue
                    linked_sources.append((acct.get("email", ""), linked_session))
                except Exception:
                    pass
            if linked_sources:
                linked_results = await asyncio.gather(
                    *[_fetch_events_async(session, "primary") for _, session in linked_sources],
                    return_exceptions=True,
                )
                for (acct_email, _), items in zip(linked_sources, linked_results):
                    if isinstance(items, Exception):
                        continue
                    for item in items:
                        ev = _normalize(item, "primary", acct_email)
                        ev_id = ev.get("id")
                        dedup_key = f"{ev_id}::{acct_email}" if ev_id else f"__noid__::{acct_email}"
                        if dedup_key not in seen_ids:
                            seen_ids.add(dedup_key)
                            all_items.append(ev)
        except Exception:
            pass

    # Sort by start time and cap at max_results
    all_items.sort(key=lambda e: e.get("start") or "")
    return all_items[:max_results]


async def delete_calendar_event(
    event_id: str,
    calendar_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Delete a Google Calendar event by its event ID.

    Args:
        event_id: Google Calendar event ID to delete.
        calendar_id: Target calendar ID. Defaults to GOOGLE_CALENDAR_ID.

    Returns:
        Dict with 'deleted' True and the 'event_id' that was removed.
    """
    session = await _get_session_for_user(locals().get("user_id"))
    cal_id = calendar_id or settings.GOOGLE_CALENDAR_ID
    url = f"{_CALENDAR_BASE}/calendars/{cal_id}/events/{event_id}"
    resp = session.delete(url)
    if resp.status_code not in (200, 204):
        _raise_for_status(resp)
    return {"deleted": True, "event_id": event_id}


async def get_calendar_event(
    event_id: str,
    calendar_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Retrieve a single Google Calendar event by ID.

    Args:
        event_id: Google Calendar event ID.
        calendar_id: Target calendar ID. Defaults to GOOGLE_CALENDAR_ID.

    Returns:
        Full event resource dict from Google Calendar API.
    """
    session = await _get_session_for_user(locals().get("user_id"))
    cal_id = calendar_id or settings.GOOGLE_CALENDAR_ID
    url = f"{_CALENDAR_BASE}/calendars/{cal_id}/events/{event_id}"
    resp = session.get(url)
    _raise_for_status(resp)
    return resp.json()


def _normalize_full_event(item: Dict[str, Any], source_cal: str = "", account_email: str = "") -> Dict[str, Any]:
    attendees = []
    for a in (item.get("attendees") or []):
        email = (a or {}).get("email")
        if email:
            attendees.append(email)
    return {
        "id": item.get("id"),
        "summary": item.get("summary", "(No title)"),
        "start": item.get("start", {}).get("dateTime") or item.get("start", {}).get("date"),
        "end": item.get("end", {}).get("dateTime") or item.get("end", {}).get("date"),
        "description": item.get("description", ""),
        "location": item.get("location", ""),
        "attendees": attendees,
        "calendar_id": source_cal,
        "account_email": account_email,
        "status": item.get("status"),
        "htmlLink": item.get("htmlLink"),
    }


async def get_calendar_event_any_source(
    event_id: str,
    user_id: Optional[str] = None,
    calendar_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch event by ID from primary + extra + linked calendars."""
    cal_id = calendar_id or settings.GOOGLE_CALENDAR_ID

    async def _fetch_one(session, cid: str, account_email: str = "") -> Optional[Dict[str, Any]]:
        if session is None:
            return None
        url = f"{_CALENDAR_BASE}/calendars/{cid}/events/{event_id}"
        try:
            resp = await asyncio.to_thread(
                session.get,
                url,
                params={"alwaysIncludeEmail": "true"},
                timeout=8,
            )
            if resp.status_code == 404:
                return None
            if resp.status_code >= 400:
                return None
            return _normalize_full_event(resp.json(), cid, account_email)
        except Exception:
            return None

    primary_session = None
    try:
        primary_session = _get_session()
    except Exception:
        try:
            primary_session = await _get_session_for_user(user_id)
        except Exception:
            primary_session = None

    # Try primary/default calendar first.
    ev = await _fetch_one(primary_session, cal_id, "")
    if ev:
        return ev

    # Try configured additional calendars.
    extra_ids = [c.strip() for c in settings.ADDITIONAL_CALENDAR_IDS.split(",") if c.strip()]
    for extra_id in extra_ids:
        ev = await _fetch_one(primary_session, extra_id, "")
        if ev:
            return ev

    # Try linked accounts (primary calendar in each linked account).
    if user_id:
        try:
            from tools.firestore_tools import get_linked_gmail_accounts
            linked_accounts = await get_linked_gmail_accounts(user_id)
            for acct in linked_accounts:
                if not acct.get("calendar_visible", True):
                    continue
                sess = await _get_session_for_linked_account(acct)
                ev = await _fetch_one(sess, "primary", acct.get("email", ""))
                if ev:
                    return ev
        except Exception:
            pass

    return None


# ── Free-slot Finder ──────────────────────────────────────────────────────────


async def find_free_slots(
    date: str,
    duration_minutes: int = 60,
    working_start_hour: int = 9,
    working_end_hour: int = 18,
    calendar_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> List[Dict[str, str]]:
    """Find available time slots on a given day that fit the requested duration.

    Uses the Free/Busy query to identify blocked intervals, then computes
    gaps within working hours that are long enough for the requested duration.

    Args:
        date: Date string in any parseable format (e.g. '2024-06-15').
        duration_minutes: Required slot length in minutes. Defaults to 60.
        working_start_hour: Start of working day (24-h). Defaults to 9.
        working_end_hour: End of working day (24-h). Defaults to 18.
        calendar_id: Target calendar ID. Defaults to GOOGLE_CALENDAR_ID.

    Returns:
        List of dicts each with 'start' and 'end' ISO-8601 strings representing
        free slots. Returns an empty list if no suitable slot exists.
    """
    session = await _get_session_for_user(locals().get("user_id"))
    cal_id = calendar_id or settings.GOOGLE_CALENDAR_ID

    day_start_naive = dateutil_parser.parse(date).replace(
        hour=working_start_hour, minute=0, second=0, microsecond=0
    )
    day_end_naive = day_start_naive.replace(hour=working_end_hour)
    day_start = LOCAL_TZ.localize(day_start_naive) if day_start_naive.tzinfo is None else day_start_naive
    day_end = LOCAL_TZ.localize(day_end_naive) if day_end_naive.tzinfo is None else day_end_naive

    url = f"{_CALENDAR_BASE}/freeBusy"
    body = {
        "timeMin": _dt_to_rfc3339(day_start.astimezone(timezone.utc)),
        "timeMax": _dt_to_rfc3339(day_end.astimezone(timezone.utc)),
        "items": [{"id": cal_id}],
    }
    resp = session.post(url, json=body)
    _raise_for_status(resp)
    busy_periods = resp.json().get("calendars", {}).get(cal_id, {}).get("busy", [])

    busy: List[tuple[datetime, datetime]] = sorted(
        [(dateutil_parser.parse(p["start"]), dateutil_parser.parse(p["end"])) for p in busy_periods],
        key=lambda x: x[0],
    )

    free_slots: List[Dict[str, str]] = []
    cursor = day_start
    delta = timedelta(minutes=duration_minutes)

    for b_start, b_end in busy:
        if cursor + delta <= b_start:
            free_slots.append({"start": _dt_to_rfc3339(cursor), "end": _dt_to_rfc3339(cursor + delta)})
        cursor = max(cursor, b_end)

    if cursor + delta <= day_end:
        free_slots.append({"start": _dt_to_rfc3339(cursor), "end": _dt_to_rfc3339(cursor + delta)})

    return free_slots


# ── Conflict Checker ──────────────────────────────────────────────────────────


async def check_calendar_conflict(
    start_datetime: str,
    duration_minutes: int = 60,
    calendar_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Check whether a proposed time slot conflicts with existing calendar events.

    Args:
        start_datetime: ISO-8601 string for the proposed event start.
        duration_minutes: Proposed event length in minutes. Defaults to 60.
        calendar_id: Target calendar ID. Defaults to GOOGLE_CALENDAR_ID.

    Returns:
        Dict with:
          - 'clash' (bool): True if an overlapping event exists.
          - 'conflicting_events' (list): Events that overlap the proposed slot.
          - 'suggested_slot' (dict | None): Next free slot on the same day if
            a clash was detected; None if no suitable alternative found.
    """
    start_dt = _parse_dt(start_datetime)
    end_dt = start_dt + timedelta(minutes=duration_minutes)

    # List events in the proposed window
    events = await list_calendar_events(
        time_min=_dt_to_rfc3339(start_dt),
        time_max=_dt_to_rfc3339(end_dt),
        calendar_id=calendar_id,
    )

    conflicting = [e for e in events if e.get("id")]  # any event in window = conflict

    if not conflicting:
        return {
            "clash": False,
            "conflicting_events": [],
            "suggested_slot": None,
        }

    # Suggest next free slot on the same day
    free_slots = await find_free_slots(
        date=start_datetime,
        duration_minutes=duration_minutes,
        calendar_id=calendar_id,
    )
    # Pick the first slot that starts after the proposed start
    suggested = None
    for slot in free_slots:
        slot_start = _parse_dt(slot["start"])
        if slot_start >= end_dt:
            suggested = slot
            break

    return {
        "clash": True,
        "conflicting_events": conflicting,
        "suggested_slot": suggested,
    }
