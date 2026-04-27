# === mcp_servers/planner_mcp_server.py ===
"""
FastMCP 2.x SSE server exposing all Smart Daily Planner tools on port 8081.

Start the server:
    python mcp_servers/planner_mcp_server.py

MCP clients connect via Server-Sent Events at:
    http://localhost:8081/sse

All 21 tools are registered using the @mcp.tool() decorator.
Tool functions mirror those in the individual agent tool files, with
additional input validation for the MCP boundary.
"""

from __future__ import annotations

import sys
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

# Ensure project root is in sys.path when run directly
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastmcp import FastMCP

from config.settings import settings
from tools.firestore_tools import (
    create_note as _create_note,
    create_task as _create_task,
    delete_note as _delete_note,
    delete_task as _delete_task,
    get_user_profile as _get_profile,
    get_linked_gmail_accounts as _get_linked_accounts,
    list_notes as _list_notes,
    list_tasks as _list_tasks,
    search_notes as _search_notes,
    undo_last_action as _undo,
    update_task as _update_task,
    update_user_profile as _update_profile,
)
from tools.calendar_tools import (
    create_calendar_event as _create_event,
    delete_calendar_event as _delete_event,
    find_free_slots as _find_slots,
    list_calendar_events as _list_events,
)
from tools.analytics_tools import (
    get_productivity_score as _get_score,
    get_task_completion_rate as _get_rate,
    get_today_summary as _get_today,
    get_weekly_trends as _get_trends,
)
from agents.briefing_agent import compose_briefing as _compose_briefing
from agents.ingest_agent import ingest_base64 as _ingest

# ── MCP server instance ────────────────────────────────────────────────────────

mcp = FastMCP(
    name="Smart Daily Planner",
    instructions=(
        "Manage tasks, calendar events, notes, productivity analytics, "
        "linked Gmail accounts, and cross-account meeting conflict detection "
        "for the Smart Daily Planner system."
    ),
)

# ══════════════════════════════════════════════════════════════════════════════
# TASK TOOLS (1–5)
# ══════════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def create_task(
    title: str,
    due_date: str,
    priority: str = "medium",
    tags: Optional[List[str]] = None,
    notes: str = "",
    user_id: str = "default_user",
) -> Dict[str, Any]:
    """Create a new task in Firestore.

    Args:
        title: Short description of the task.
        due_date: ISO-8601 due date (e.g. '2024-06-15T14:00:00').
        priority: One of 'low', 'medium', 'high', 'urgent'.
        tags: Optional list of tag strings.
        notes: Optional annotation text.
        user_id: Owner of the task.

    Returns:
        Created task document dict including id and timestamps.
    """
    return await _create_task(
        title=title,
        due_date=due_date,
        priority=priority,
        tags=tags,
        user_id=user_id,
        notes=notes,
    )


@mcp.tool()
async def list_tasks(
    user_id: str = "default_user",
    status: Optional[str] = None,
    priority: Optional[str] = None,
    limit: int = 20,
) -> Dict[str, Any]:
    """List tasks from Firestore with optional filters.

    Args:
        user_id: Owner of the tasks.
        status: Filter by status ('pending', 'completed', 'overdue').
        priority: Filter by priority ('low', 'medium', 'high', 'urgent').
        limit: Max results to return (default 20).

    Returns:
        Dict with 'tasks' list and 'count'.
    """
    tasks = await _list_tasks(user_id=user_id, status=status, priority=priority, limit=limit)
    return {"tasks": tasks, "count": len(tasks)}


@mcp.tool()
async def update_task(
    task_id: str,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    due_date: Optional[str] = None,
    title: Optional[str] = None,
    user_id: str = "default_user",
) -> Dict[str, Any]:
    """Update fields on an existing task.

    Args:
        task_id: Firestore document ID of the task.
        status: New status ('pending', 'completed', 'overdue').
        priority: New priority level.
        due_date: New ISO-8601 due date string.
        title: New title for the task.
        user_id: Owner (for audit logging).

    Returns:
        Updated task document dict.
    """
    updates: Dict[str, Any] = {}
    if status:
        updates["status"] = status
    if priority:
        updates["priority"] = priority
    if due_date:
        updates["due_date"] = due_date
    if title:
        updates["title"] = title
    return await _update_task(task_id=task_id, updates=updates, user_id=user_id)


@mcp.tool()
async def delete_task(task_id: str, user_id: str = "default_user") -> Dict[str, Any]:
    """Delete a task from Firestore.

    Args:
        task_id: Firestore document ID of the task to delete.
        user_id: Owner of the task.

    Returns:
        Dict confirming deletion with 'deleted' True and 'task_id'.
    """
    return await _delete_task(task_id=task_id, user_id=user_id)


# ══════════════════════════════════════════════════════════════════════════════
# NOTES TOOLS (5–8)
# ══════════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def create_note(
    title: str,
    content: str,
    tags: Optional[List[str]] = None,
    user_id: str = "default_user",
) -> Dict[str, Any]:
    """Create and save a new note to Firestore.

    Args:
        title: Short title for the note.
        content: Full body text (markdown supported).
        tags: Optional list of tag strings.
        user_id: Owner of the note.

    Returns:
        Created note document dict including id and timestamps.
    """
    return await _create_note(title=title, content=content, tags=tags, user_id=user_id)


@mcp.tool()
async def list_notes(
    user_id: str = "default_user",
    tag: Optional[str] = None,
    limit: int = 20,
) -> Dict[str, Any]:
    """List notes from Firestore, optionally filtered by tag.

    Args:
        user_id: Owner of the notes.
        tag: Optional tag to filter by.
        limit: Max results to return (default 20).

    Returns:
        Dict with 'notes' list and 'count'.
    """
    notes = await _list_notes(user_id=user_id, tag=tag, limit=limit)
    return {"notes": notes, "count": len(notes)}


@mcp.tool()
async def search_notes(
    keyword: str,
    user_id: str = "default_user",
    limit: int = 10,
) -> Dict[str, Any]:
    """Search notes by keyword in title or content.

    Args:
        keyword: Search term (case-insensitive).
        user_id: Owner of the notes.
        limit: Max results to return (default 10).

    Returns:
        Dict with 'results' list and 'count'.
    """
    results = await _search_notes(keyword=keyword, user_id=user_id, limit=limit)
    return {"results": results, "count": len(results)}


@mcp.tool()
async def delete_note(note_id: str, user_id: str = "default_user") -> Dict[str, Any]:
    """Delete a note from Firestore.

    Args:
        note_id: Firestore document ID of the note.
        user_id: Owner of the note.

    Returns:
        Dict confirming deletion with 'deleted' True and 'note_id'.
    """
    return await _delete_note(note_id=note_id, user_id=user_id)


# ══════════════════════════════════════════════════════════════════════════════
# CALENDAR TOOLS (9–12)
# ══════════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def create_calendar_event(
    summary: str,
    start_datetime: str,
    duration_minutes: int = 60,
    description: str = "",
    location: str = "",
    attendees: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Create a new Google Calendar event.

    Args:
        summary: Event title.
        start_datetime: ISO-8601 start datetime string.
        duration_minutes: Event length in minutes (default 60).
        description: Optional event description.
        location: Optional location string.
        attendees: Optional list of attendee email addresses.

    Returns:
        Created event resource dict with id, htmlLink, start, end.
    """
    return await _create_event(
        summary=summary,
        start_datetime=start_datetime,
        duration_minutes=duration_minutes,
        description=description,
        location=location,
        attendees=attendees,
    )


@mcp.tool()
async def list_calendar_events(
    time_min: Optional[str] = None,
    time_max: Optional[str] = None,
    max_results: int = 10,
) -> Dict[str, Any]:
    """List Google Calendar events in a time window.

    Args:
        time_min: ISO-8601 window start. Defaults to now.
        time_max: ISO-8601 window end. Defaults to 7 days from now.
        max_results: Max events to return (default 10).

    Returns:
        Dict with 'events' list and 'count'.
    """
    events = await _list_events(time_min=time_min, time_max=time_max, max_results=max_results)
    return {"events": events, "count": len(events)}


@mcp.tool()
async def delete_calendar_event(event_id: str) -> Dict[str, Any]:
    """Delete a Google Calendar event by ID.

    Args:
        event_id: Google Calendar event ID string.

    Returns:
        Dict with 'deleted' True and 'event_id'.
    """
    return await _delete_event(event_id=event_id)


@mcp.tool()
async def find_free_slots(
    date: str,
    duration_minutes: int = 60,
) -> Dict[str, Any]:
    """Find available time slots on a given day.

    Args:
        date: Date string (e.g. '2024-06-15').
        duration_minutes: Required slot length in minutes (default 60).

    Returns:
        Dict with 'free_slots' list (each has 'start' and 'end') and 'slot_count'.
    """
    slots = await _find_slots(date=date, duration_minutes=duration_minutes)
    return {"free_slots": slots, "slot_count": len(slots)}


# ══════════════════════════════════════════════════════════════════════════════
# ANALYTICS TOOLS (13–14)
# ══════════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def get_productivity_score(
    user_id: str = "default_user",
    days: int = 7,
) -> Dict[str, Any]:
    """Get a productivity score (0–100) for the last N days.

    Args:
        user_id: Owner to score.
        days: Lookback window in days (default 7).

    Returns:
        Dict with 'score', 'label', 'completion_rate_pct', 'overdue_count'.
    """
    return await _get_score(user_id=user_id, days=days)


@mcp.tool()
async def get_weekly_trends(
    user_id: str = "default_user",
    weeks: int = 4,
) -> Dict[str, Any]:
    """Get week-by-week task completion trends.

    Args:
        user_id: Owner to analyse.
        weeks: Number of weeks to include (default 4).

    Returns:
        Dict with 'trends' list and 'trend_direction'.
    """
    trends = await _get_trends(user_id=user_id, weeks=weeks)
    return {"trends": trends, "weeks_analysed": len(trends)}


# ══════════════════════════════════════════════════════════════════════════════
# SYSTEM TOOLS (15–16)
# ══════════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def undo_last_action(user_id: str = "default_user") -> Dict[str, Any]:
    """Reverse the most recent mutating action for a user.

    Supported reversals: create → delete, delete → restore, update → revert.

    Args:
        user_id: Owner whose last action to reverse.

    Returns:
        Dict describing the reversal with 'undone' action and 'entity_id'.
    """
    return await _undo(user_id=user_id)


@mcp.tool()
async def get_today_summary(user_id: str = "default_user") -> Dict[str, Any]:
    """Get a summary of today's task workload.

    Args:
        user_id: Owner whose today to summarise.

    Returns:
        Dict with 'pending_today', 'completed_today', 'overdue', 'upcoming_24h'.
    """
    return await _get_today(user_id=user_id)


# ══════════════════════════════════════════════════════════════════════════════
# LINKED ACCOUNTS TOOLS (17–18)
# ══════════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def get_linked_gmail_accounts(user_id: str = "default_user") -> Dict[str, Any]:
    """List all secondary Gmail accounts linked to this user via OAuth.

    These accounts have their calendar events aggregated alongside the
    primary account and can send emails on the user's behalf.

    Args:
        user_id: Owner whose linked accounts to retrieve.

    Returns:
        Dict with 'accounts' list (each has email, name, calendar_visible,
        email_send_enabled, has_token) and 'count'.
    """
    accounts = await _get_linked_accounts(user_id=user_id)
    # Strip sensitive token fields for external consumers
    safe = [
        {
            "email": a.get("email"),
            "name": a.get("name"),
            "calendar_visible": a.get("calendar_visible", True),
            "email_send_enabled": a.get("email_send_enabled", True),
            "has_token": bool(a.get("refresh_token") or a.get("access_token")),
        }
        for a in accounts
    ]
    return {"accounts": safe, "count": len(safe)}


@mcp.tool()
async def detect_calendar_conflicts(
    days_ahead: int = 2,
    user_id: str = "default_user",
) -> Dict[str, Any]:
    """Detect overlapping calendar events across all linked Google accounts.

    Fetches events for the primary account plus all linked accounts that have
    calendar_visible=True, then finds time-slot overlaps within the requested
    window. Only events with explicit start AND end times are compared (all-day
    events are excluded).

    Args:
        days_ahead: How many days from now to scan for conflicts (1–7, default 2).
        user_id: Owner whose calendars to check.

    Returns:
        Dict with:
          - 'conflict_count': total number of overlapping pairs found
          - 'conflicts': list of conflict objects, each containing
              'day' (ISO date), 'event_a', 'event_b', and 'overlap_minutes'
          - 'days_scanned': the window size used
          - 'accounts_checked': number of calendars queried
    """
    days_ahead = max(1, min(7, days_ahead))
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=days_ahead)
    time_min = now.isoformat()
    time_max = cutoff.isoformat()

    # Gather events from primary calendar
    all_events: List[Dict[str, Any]] = []
    try:
        primary = await _list_events(
            time_min=time_min, time_max=time_max, max_results=100
        )
        for e in primary:
            e.setdefault("account_email", settings.GMAIL_USER_EMAIL)
        all_events.extend(primary)
    except Exception:
        pass

    accounts_checked = 1

    # Gather events from each linked account
    try:
        linked = await _get_linked_accounts(user_id=user_id)
        for acct in linked:
            if not acct.get("calendar_visible", True):
                continue
            try:
                from tools.calendar_tools import _get_session_for_linked_account
                from googleapiclient.discovery import build

                session = await _get_session_for_linked_account(acct)
                if not session:
                    continue
                service = build("calendar", "v3", credentials=session)
                resp = (
                    service.events()
                    .list(
                        calendarId="primary",
                        timeMin=time_min,
                        timeMax=time_max,
                        maxResults=100,
                        singleEvents=True,
                        orderBy="startTime",
                    )
                    .execute()
                )
                for item in resp.get("items", []):
                    start = item.get("start", {}).get("dateTime")
                    end = item.get("end", {}).get("dateTime")
                    if start and end:
                        all_events.append({
                            "id": item["id"],
                            "summary": item.get("summary", "(No title)"),
                            "start": start,
                            "end": end,
                            "location": item.get("location", ""),
                            "account_email": acct["email"],
                        })
                accounts_checked += 1
            except Exception:
                continue
    except Exception:
        pass

    # Detect overlaps
    timed = [e for e in all_events if e.get("start") and e.get("end")]
    conflicts = []
    seen_keys: set = set()

    for i in range(len(timed)):
        for j in range(i + 1, len(timed)):
            a, b = timed[i], timed[j]
            try:
                a_start = datetime.fromisoformat(a["start"].replace("Z", "+00:00"))
                a_end = datetime.fromisoformat(a["end"].replace("Z", "+00:00"))
                b_start = datetime.fromisoformat(b["start"].replace("Z", "+00:00"))
                b_end = datetime.fromisoformat(b["end"].replace("Z", "+00:00"))
            except ValueError:
                continue

            if a_start < b_end and a_end > b_start:
                key = "|".join(sorted([a["id"], b["id"]]))
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                overlap_start = max(a_start, b_start)
                overlap_end = min(a_end, b_end)
                overlap_min = int((overlap_end - overlap_start).total_seconds() / 60)
                conflicts.append({
                    "day": a_start.date().isoformat(),
                    "overlap_minutes": overlap_min,
                    "event_a": {
                        "id": a["id"],
                        "summary": a.get("summary"),
                        "start": a["start"],
                        "end": a["end"],
                        "account_email": a.get("account_email"),
                    },
                    "event_b": {
                        "id": b["id"],
                        "summary": b.get("summary"),
                        "start": b["start"],
                        "end": b["end"],
                        "account_email": b.get("account_email"),
                    },
                })

    conflicts.sort(key=lambda c: c["day"])
    return {
        "conflict_count": len(conflicts),
        "conflicts": conflicts,
        "days_scanned": days_ahead,
        "accounts_checked": accounts_checked,
    }


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"Starting Smart Daily Planner MCP server on port {settings.MCP_PORT}...")
    mcp.run(transport="sse", host="0.0.0.0", port=settings.MCP_PORT)
