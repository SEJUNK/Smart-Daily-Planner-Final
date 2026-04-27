# === agents/calendar_agent.py ===
"""
Calendar Agent — manages Google Calendar events using ADK LlmAgent.

IMPORTANT: Every create_event call goes through the conflict_agent first.
If a clash is found the agent offers the suggested alternative slot instead
of blindly creating an overlapping event.

Tools exposed:
  - create_event (conflict-checked)
  - list_events
  - delete_event
  - find_free_slots
  - check_conflict
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from agents.conflict_agent import check_and_suggest, get_day_availability
from config.settings import settings
from tools.calendar_tools import (
    create_calendar_event,
    delete_calendar_event,
    find_free_slots as _find_free_slots,
    list_calendar_events,
)


# ── Tool wrappers ─────────────────────────────────────────────────────────────


async def create_event(
    summary: str,
    start_datetime: str,
    duration_minutes: int = 60,
    description: str = "",
    location: str = "",
    attendees: Optional[List[str]] = None,
    force_create: bool = False,
) -> Dict[str, Any]:
    """Create a Google Calendar event after checking for conflicts.

    Before creating the event this function checks for scheduling conflicts.
    If a clash is detected and force_create is False, the event is NOT created
    and the suggested alternative slot is returned instead.

    Args:
        summary: Title of the calendar event.
        start_datetime: ISO-8601 string for event start (e.g. '2024-06-15T10:00:00').
        duration_minutes: Event length in minutes. Defaults to 60.
        description: Optional agenda or description text.
        location: Optional physical or virtual location.
        attendees: Optional list of attendee email addresses.
        force_create: If True, create the event even when conflicts exist.
            Use with caution. Defaults to False.

    Returns:
        Dict with either:
          - Created event fields ('id', 'summary', 'start', 'end', 'htmlLink')
            and 'conflict_check' showing no clash, OR
          - 'clash' True and 'suggested_slot' when a conflict blocks creation.
    """
    conflict = await check_and_suggest(
        start_datetime=start_datetime,
        duration_minutes=duration_minutes,
    )

    if conflict["clash"] and not force_create:
        return {
            "created": False,
            "clash": True,
            "message": conflict["message"],
            "conflicting_events": conflict.get("conflicting_events", []),
            "suggested_slot": conflict.get("suggested_slot"),
        }

    event = await create_calendar_event(
        summary=summary,
        start_datetime=start_datetime,
        duration_minutes=duration_minutes,
        description=description,
        location=location,
        attendees=attendees,
    )
    return {
        "created": True,
        "clash": conflict["clash"],
        **event,
    }


async def list_events(
    time_min: Optional[str] = None,
    time_max: Optional[str] = None,
    max_results: int = 10,
) -> Dict[str, Any]:
    """List upcoming Google Calendar events within an optional time window.

    Args:
        time_min: ISO-8601 start of the query window. Defaults to now.
        time_max: ISO-8601 end of the query window. Defaults to 7 days from now.
        max_results: Maximum number of events to return. Defaults to 10.

    Returns:
        Dict with 'events' (list) and 'count' (int).
    """
    events = await list_calendar_events(
        time_min=time_min,
        time_max=time_max,
        max_results=max_results,
    )
    return {"events": events, "count": len(events)}


async def delete_event(event_id: str) -> Dict[str, Any]:
    """Delete a Google Calendar event by its event ID.

    Args:
        event_id: Google Calendar event ID string (not the event title).

    Returns:
        Dict with 'deleted' True and 'event_id'.
    """
    return await delete_calendar_event(event_id=event_id)


async def find_free_slots(
    date: str,
    duration_minutes: int = 60,
) -> Dict[str, Any]:
    """Find available time slots on a specific day.

    Args:
        date: Date string in any parseable format (e.g. '2024-06-15', 'tomorrow').
        duration_minutes: Required slot length in minutes. Defaults to 60.

    Returns:
        Dict with 'free_slots' (list of dicts with 'start' and 'end') and
        'slot_count' (int).
    """
    return await get_day_availability(date=date, duration_minutes=duration_minutes)


async def check_conflict(
    start_datetime: str,
    duration_minutes: int = 60,
) -> Dict[str, Any]:
    """Check whether a proposed time slot conflicts with calendar events.

    Args:
        start_datetime: ISO-8601 string for the proposed start time.
        duration_minutes: Proposed event length in minutes. Defaults to 60.

    Returns:
        Dict with 'clash' (bool), 'conflicting_events', 'suggested_slot',
        and a human-readable 'message'.
    """
    return await check_and_suggest(
        start_datetime=start_datetime,
        duration_minutes=duration_minutes,
    )


# ── ADK LlmAgent ──────────────────────────────────────────────────────────────

calendar_agent = LlmAgent(
    name="calendar_agent",
    model=settings.GEMINI_MODEL,
    description=(
        "Manages Google Calendar events: create (with conflict-checking), "
        "list, delete events, and find free time slots."
    ),
    instruction="""You are the Calendar Manager for the Smart Daily Planner.

Your responsibilities:
1. Schedule new events — ALWAYS run check_conflict first, then create_event.
2. List upcoming events within a date range the user specifies.
3. Delete events when explicitly requested (confirm the event title first).
4. Find free slots when the user asks 'when am I free?' or similar.

Conflict policy:
- If create_event returns clash=True, show the user the conflicting events
  and the suggested_slot. Ask whether to book the suggested slot instead.
- NEVER create events without confirming the suggested slot with the user
  when a conflict exists (unless force_create=True is explicitly requested).

Date parsing:
- Convert natural language dates/times ('tomorrow at 2pm', 'next Monday 10am')
  to ISO-8601 format (e.g. '2024-06-15T14:00:00') before calling tools.
- Default timezone is Asia/Kolkata (IST).

Always echo back the confirmed event title, date, time, and duration.
""",
    tools=[
        FunctionTool(create_event),
        FunctionTool(list_events),
        FunctionTool(delete_event),
        FunctionTool(find_free_slots),
        FunctionTool(check_conflict),
    ],
)
