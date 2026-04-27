# === agents/smart_scheduler_agent.py ===
"""
Smart Scheduler Agent — AI-powered optimal task rescheduler.

UNIQUE FEATURE: Instead of just listing overdue tasks, this agent:
  1. Fetches all overdue/high-priority tasks from Firestore.
  2. Queries Google Calendar for free slots over the next N days.
  3. Uses Gemini to intelligently assign each task to the best free slot
     based on priority, estimated effort, and preferred working hours.
  4. Creates calendar events for each task AND updates task due_date.
  5. Returns a structured "rescue plan" the user can approve.

Think of it as an AI that digs you out of a productivity hole automatically.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from google import genai
from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from google.genai import types as genai_types

from config.settings import settings, LOCAL_TZ
from tools.calendar_tools import (
    create_calendar_event,
    find_free_slots,
    list_calendar_events,
)
from tools.firestore_tools import list_tasks, update_task

_genai_client: Optional[genai.Client] = None


def _get_client() -> genai.Client:
    global _genai_client
    if _genai_client is None:
        _genai_client = genai.Client(api_key=settings.GOOGLE_API_KEY)
    return _genai_client


SCHEDULING_PROMPT = """You are a productivity expert AI helping a user in Asia/Kolkata (IST, UTC+5:30).

Given the following overdue/urgent tasks and available calendar free slots,
create an optimal RESCUE PLAN that assigns each task to the best time block.

Assignment Rules:
- Urgent tasks → earliest available morning slot (9 AM–12 PM IST preferred).
- High tasks → morning slots next; afternoon (2 PM–5 PM IST) if mornings full.
- Medium tasks → afternoon slots (2 PM–5 PM IST).
- Low tasks → end of day (5 PM–7 PM IST) or leave unscheduled if no room.
- Do NOT assign more than 3 focus blocks per day.
- Each focus block should be 45–90 minutes (match task complexity).
- Leave at least one free slot per day as a buffer for interruptions.
- Never schedule on Friday afternoon (leave as review/buffer time).
- All slot_start values must be valid ISO-8601 strings with timezone offset (+05:30).

Return ONLY valid JSON — no prose, no markdown fences:
{{
  "assignments": [
    {{
      "task_id": "...",
      "task_title": "...",
      "priority": "...",
      "slot_start": "ISO-8601 string with +05:30 offset",
      "duration_minutes": 60,
      "reasoning": "one sentence explaining this specific slot choice"
    }}
  ],
  "unscheduled_tasks": ["task_id_1", ...],
  "summary": "2-3 sentence rescue plan overview mentioning total tasks rescued and key strategy"
}}

TASKS TO SCHEDULE:
{tasks_json}

FREE SLOTS (next {days} days, IST):
{slots_json}
"""


async def generate_rescue_plan(
    user_id: str = "default_user",
    days_ahead: int = 5,
    max_tasks_per_day: int = 3,
) -> Dict[str, Any]:
    """Generate an AI-powered rescue plan for overdue and urgent tasks.

    Analyses overdue and urgent pending tasks, finds calendar free slots
    over the next N days, and uses Gemini to intelligently assign each task
    to an optimal time block — creating a personalised rescue schedule.

    Args:
        user_id: Owner whose tasks to rescue.
        days_ahead: How many days ahead to look for slots. Defaults to 5.
        max_tasks_per_day: Maximum focus blocks to assign per day. Defaults to 3.

    Returns:
        Dict with:
          - 'assignments' (list): Each has task_id, task_title, slot_start,
            duration_minutes, and reasoning.
          - 'unscheduled_tasks' (list): Task IDs that could not be assigned.
          - 'summary' (str): AI narrative summary of the rescue plan.
          - 'slots_scanned' (int): Total free slots evaluated.
    """
    # 1. Fetch overdue + urgent tasks
    overdue = await list_tasks(user_id=user_id, status="overdue", limit=20)
    urgent = await list_tasks(user_id=user_id, status="pending", priority="urgent", limit=10)
    high = await list_tasks(user_id=user_id, status="pending", priority="high", limit=10)
    all_tasks = {t["id"]: t for t in (overdue + urgent + high)}
    unique_tasks = list(all_tasks.values())

    if not unique_tasks:
        return {
            "assignments": [],
            "unscheduled_tasks": [],
            "summary": "No overdue or urgent tasks found. You're on top of things!",
            "slots_scanned": 0,
        }

    # 2. Gather free slots for each day
    all_slots: List[Dict] = []
    now = datetime.now(LOCAL_TZ)
    for i in range(days_ahead):
        day = (now + timedelta(days=i + 1)).strftime("%Y-%m-%d")
        try:
            slots = await find_free_slots(date=day, duration_minutes=60)
            for s in slots:
                s["date"] = day
            all_slots.extend(slots[:max_tasks_per_day])
        except Exception:
            continue

    # 3. Call Gemini for optimal assignment
    client = _get_client()
    prompt = SCHEDULING_PROMPT.format(
        tasks_json=json.dumps(unique_tasks, indent=2, default=str),
        slots_json=json.dumps(all_slots, indent=2),
        days=days_ahead,
    )

    try:
        response = client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=[genai_types.Content(role="user", parts=[genai_types.Part(text=prompt)])],
        )
        raw = response.text or "{}"
        import re
        raw = re.sub(r"```(?:json)?\s*", "", raw)
        raw = re.sub(r"```\s*$", "", raw.strip()).strip()
        plan = json.loads(raw)
    except Exception as exc:
        return {
            "assignments": [],
            "unscheduled_tasks": [t["id"] for t in unique_tasks],
            "summary": f"AI planning failed: {exc}. Please reschedule manually.",
            "slots_scanned": len(all_slots),
        }

    return {**plan, "slots_scanned": len(all_slots), "tasks_evaluated": len(unique_tasks)}


async def apply_rescue_plan(
    assignments: List[Dict[str, Any]],
    user_id: str = "default_user",
    create_events: bool = True,
) -> Dict[str, Any]:
    """Apply an AI rescue plan by creating calendar events and updating task due dates.

    Takes the output of generate_rescue_plan and materialises it:
    creates focus-block calendar events and updates each task's due_date
    to match the assigned slot.

    Args:
        assignments: List of assignment dicts from generate_rescue_plan,
            each with task_id, slot_start, duration_minutes, task_title.
        user_id: Owner of the tasks.
        create_events: When True, creates Google Calendar events for each
            assignment. Set False to update tasks only. Defaults to True.

    Returns:
        Dict with:
          - 'applied' (int): Number of assignments successfully applied.
          - 'errors' (list): Any failures during application.
          - 'calendar_events' (list): Created event IDs.
    """
    applied = 0
    errors = []
    event_ids = []

    for assignment in assignments:
        task_id = assignment.get("task_id")
        slot_start = assignment.get("slot_start")
        duration = int(assignment.get("duration_minutes", 60))
        title = assignment.get("task_title", "Focus Block")

        try:
            # Update task due_date
            await update_task(
                task_id=task_id,
                updates={"due_date": slot_start, "status": "pending"},
                user_id=user_id,
            )

            # Create calendar focus block
            if create_events:
                event = await create_calendar_event(
                    summary=f"🎯 Focus: {title}",
                    start_datetime=slot_start,
                    duration_minutes=duration,
                    description=f"AI-scheduled focus block.\nReasoning: {assignment.get('reasoning', '')}",
                )
                event_ids.append(event.get("id"))

            applied += 1
        except Exception as exc:
            errors.append(f"Task {task_id}: {exc}")

    return {
        "applied": applied,
        "errors": errors,
        "calendar_events": event_ids,
    }


async def smart_reschedule(
    user_id: str = "default_user",
    days_ahead: int = 5,
    auto_apply: bool = False,
) -> Dict[str, Any]:
    """Generate and optionally apply an AI rescue plan in one call.

    This is the primary entry point for the smart reschedule feature.
    It generates the optimal plan and, if auto_apply is True, immediately
    creates calendar events and updates all task due dates.

    Args:
        user_id: Owner whose tasks to rescue.
        days_ahead: Days ahead to search for free slots. Defaults to 5.
        auto_apply: When True, automatically apply the plan without waiting
            for user confirmation. Defaults to False (preview only).

    Returns:
        Dict with the rescue plan (from generate_rescue_plan) plus, if
        auto_apply is True, an 'applied' field with the application result.
    """
    plan = await generate_rescue_plan(user_id=user_id, days_ahead=days_ahead)

    if auto_apply and plan.get("assignments"):
        application = await apply_rescue_plan(
            assignments=plan["assignments"],
            user_id=user_id,
        )
        plan["applied"] = application
    else:
        plan["applied"] = None
        plan["hint"] = "Call apply_rescue_plan with the assignments list to activate this plan."

    return plan


# ── ADK LlmAgent ──────────────────────────────────────────────────────────────

smart_scheduler_agent = LlmAgent(
    name="smart_scheduler_agent",
    model=settings.GEMINI_MODEL,
    description=(
        "AI-powered rescue planner: finds overdue/urgent tasks, locates free "
        "calendar slots, and assigns each task to an optimal focus block."
    ),
    instruction="""You are the Smart Scheduler — an AI productivity rescue expert.

When the user is overwhelmed, overdue, or asks 'help me catch up':
1. Call smart_reschedule(user_id, days_ahead=5) to generate the rescue plan.
2. Present the assignments clearly: task name, assigned date/time, duration, reasoning.
3. Ask the user if they want to apply the plan (this creates calendar events).
4. If they say yes, call apply_rescue_plan(assignments, user_id).
5. Confirm how many tasks were scheduled and show the calendar event summary.

Always mention:
- Total tasks rescued vs unscheduled
- The AI's reasoning for key assignments
- Whether calendar events were created
""",
    tools=[
        FunctionTool(smart_reschedule),
        FunctionTool(generate_rescue_plan),
        FunctionTool(apply_rescue_plan),
    ],
)
