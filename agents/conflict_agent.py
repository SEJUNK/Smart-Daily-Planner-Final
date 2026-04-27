# === agents/conflict_agent.py ===
"""
Conflict Agent — checks Google Calendar for scheduling conflicts.

This agent is called by the Calendar Agent before every event creation.
It is a pure logic module (not an LlmAgent) that wraps the calendar
conflict-check tool and returns structured results.

If a clash is detected it suggests the next available free slot on the
same day so the Calendar Agent can offer the user an alternative.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from tools.calendar_tools import check_calendar_conflict, find_free_slots


async def check_and_suggest(
    start_datetime: str,
    duration_minutes: int = 60,
    calendar_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Check for calendar conflicts and suggest a free slot if a clash is found.

    Calls check_calendar_conflict() to detect overlapping events and then
    find_free_slots() on the same day if needed.

    Args:
        start_datetime: ISO-8601 string for the proposed event start time.
        duration_minutes: Length of the proposed event in minutes.
        calendar_id: Google Calendar ID to check. Defaults to primary calendar.

    Returns:
        Dict with:
          - 'clash' (bool): True when a conflicting event was found.
          - 'conflicting_events' (list): Events that overlap the proposed slot.
          - 'suggested_slot' (dict | None): First available alternative slot
            dict with 'start' and 'end' ISO strings, or None if the
            proposed slot is free.
          - 'message' (str): Human-readable summary of the conflict check.
    """
    result = await check_calendar_conflict(
        start_datetime=start_datetime,
        duration_minutes=duration_minutes,
        calendar_id=calendar_id,
    )

    if not result["clash"]:
        result["message"] = (
            f"No conflicts found. The slot starting at {start_datetime} "
            f"for {duration_minutes} minutes is available."
        )
        return result

    conflicting_names = [
        e.get("summary", "Unknown event")
        for e in result.get("conflicting_events", [])
    ]
    suggestion = result.get("suggested_slot")
    if suggestion:
        suggestion_msg = (
            f" Suggested alternative: {suggestion['start']} to {suggestion['end']}."
        )
    else:
        suggestion_msg = " No free alternative found on the same day."

    result["message"] = (
        f"Conflict detected with: {', '.join(conflicting_names)}.{suggestion_msg}"
    )
    return result


async def get_day_availability(
    date: str,
    duration_minutes: int = 60,
    calendar_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Return all free slots on a given day for a requested duration.

    Args:
        date: Date string in any parseable format (e.g. '2024-06-15').
        duration_minutes: Required slot length in minutes. Defaults to 60.
        calendar_id: Google Calendar ID to check. Defaults to primary.

    Returns:
        Dict with:
          - 'date' (str): The queried date.
          - 'duration_minutes' (int): The requested slot length.
          - 'free_slots' (list): List of dicts with 'start' and 'end' strings.
          - 'slot_count' (int): Number of available slots.
    """
    slots = await find_free_slots(
        date=date,
        duration_minutes=duration_minutes,
        calendar_id=calendar_id,
    )
    return {
        "date": date,
        "duration_minutes": duration_minutes,
        "free_slots": slots,
        "slot_count": len(slots),
    }
