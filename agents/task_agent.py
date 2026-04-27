# === agents/task_agent.py ===
"""
Task Agent — manages user tasks using Google ADK LlmAgent.

Tools exposed:
  - create_task
  - list_tasks
  - update_task
  - delete_task
  - escalate_overdue_tasks

The agent automatically escalates overdue tasks when listing, giving the
user a proactive reminder of items that need attention.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from config.settings import settings
from tools.firestore_tools import (
    create_task as _create_task,
    delete_task as _delete_task,
    escalate_overdue_tasks as _escalate_overdue,
    list_tasks as _list_tasks,
    update_task as _update_task,
)


# ── Tool wrapper functions ─────────────────────────────────────────────────────
# Each function is a thin, well-documented wrapper so ADK can auto-generate
# tool descriptions from the docstrings.


async def create_task(
    title: str,
    due_date: str,
    priority: str = "medium",
    tags: Optional[List[str]] = None,
    notes: str = "",
    user_id: str = "default_user",
) -> Dict[str, Any]:
    """Create a new task and persist it to Firestore.

    Args:
        title: Short description of the task (required).
        due_date: ISO-8601 date/datetime when the task is due (required).
            Example: '2024-06-15' or '2024-06-15T14:00:00'.
        priority: Task urgency — 'low', 'medium', 'high', or 'urgent'.
            Defaults to 'medium'.
        tags: Optional list of label strings for categorisation
            (e.g. ['work', 'project-alpha']).
        notes: Optional free-text annotation attached to the task.
        user_id: Owner of the task. Defaults to 'default_user'.

    Returns:
        Dict representing the newly created task document including its
        auto-generated 'id', 'status' (pending), and timestamps.
    """
    return await _create_task(
        title=title,
        due_date=due_date,
        priority=priority,
        tags=tags,
        user_id=user_id,
        notes=notes,
    )


async def list_tasks(
    user_id: str = "default_user",
    status: Optional[str] = None,
    priority: Optional[str] = None,
    limit: int = 20,
) -> Dict[str, Any]:
    """List tasks from Firestore with optional filters, auto-escalating overdue items.

    Before returning results this function checks for overdue tasks and
    escalates their priority automatically (pending → overdue, low/medium → high).

    Args:
        user_id: Owner of the tasks to list.
        status: Optional status filter ('pending', 'completed', 'overdue').
        priority: Optional priority filter ('low', 'medium', 'high', 'urgent').
        limit: Maximum number of tasks to return. Defaults to 20.

    Returns:
        Dict with:
          - 'tasks' (list): Task dicts matching the filters.
          - 'count' (int): Number of tasks returned.
          - 'escalated' (int): Number of tasks auto-escalated during this call.
    """
    escalated = await _escalate_overdue(user_id)
    tasks = await _list_tasks(user_id=user_id, status=status, priority=priority, limit=limit)
    return {
        "tasks": tasks,
        "count": len(tasks),
        "escalated": len(escalated),
    }


async def update_task(
    task_id: str,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    due_date: Optional[str] = None,
    title: Optional[str] = None,
    notes: Optional[str] = None,
    user_id: str = "default_user",
) -> Dict[str, Any]:
    """Update fields on an existing task.

    Supply only the fields you wish to change; others are left intact.

    Args:
        task_id: Firestore document ID of the task to update.
        status: New status — 'pending', 'completed', or 'overdue'.
        priority: New priority — 'low', 'medium', 'high', 'urgent'.
        due_date: New ISO-8601 due date/datetime string.
        title: New task title.
        notes: New annotation text.
        user_id: Owner of the task (for audit logging).

    Returns:
        Updated task document dict with all current field values.
    """
    updates: Dict[str, Any] = {}
    if status is not None:
        updates["status"] = status
    if priority is not None:
        updates["priority"] = priority
    if due_date is not None:
        updates["due_date"] = due_date
    if title is not None:
        updates["title"] = title
    if notes is not None:
        updates["notes"] = notes

    if not updates:
        raise ValueError("At least one field must be provided to update.")

    return await _update_task(task_id=task_id, updates=updates, user_id=user_id)


async def delete_task(
    task_id: str,
    user_id: str = "default_user",
) -> Dict[str, Any]:
    """Permanently delete a task from Firestore.

    Args:
        task_id: Firestore document ID of the task to delete.
        user_id: Owner of the task (for audit logging).

    Returns:
        Dict with 'deleted' True and 'task_id' confirming the deletion.
    """
    return await _delete_task(task_id=task_id, user_id=user_id)


async def escalate_overdue_tasks(
    user_id: str = "default_user",
) -> Dict[str, Any]:
    """Manually trigger overdue task escalation for the given user.

    Marks pending tasks past their due_date as 'overdue' and bumps low/medium
    priority tasks to 'high' priority.

    Args:
        user_id: Owner whose overdue tasks to escalate.

    Returns:
        Dict with 'escalated_count' (int) and 'escalated_tasks' (list).
    """
    escalated = await _escalate_overdue(user_id)
    return {
        "escalated_count": len(escalated),
        "escalated_tasks": escalated,
    }


# ── ADK LlmAgent ──────────────────────────────────────────────────────────────

task_agent = LlmAgent(
    name="task_agent",
    model=settings.GEMINI_MODEL,
    description=(
        "Manages user tasks: create, list, update, delete, and escalate overdue tasks. "
        "Use this agent for all task-related requests."
    ),
    instruction="""You are the Task Manager for the Smart Daily Planner.

Your responsibilities:
1. Create tasks when the user describes work items, action items, reminders, or to-dos.
2. List and filter tasks by status or priority when asked.
3. Update task status (e.g. mark complete), priority, or due date.
4. Delete tasks when explicitly requested.
5. Escalate overdue tasks proactively — call escalate_overdue_tasks at the
   start of every list_tasks call or when the user asks about their workload.

Guidelines:
- Always confirm task creation with: title, due date (IST), priority, and any tags.
- When marking tasks complete, briefly celebrate — "Great work! ✅ [task] is done."
- If the user mentions being overwhelmed, list urgent/high items first, max 5.
- Parse natural language dates into ISO-8601 before calling create_task:
    "tomorrow" → next calendar day at 09:00 IST
    "next Friday" → the coming Friday at 17:00 IST
    "end of week" → Friday 18:00 IST
    "tonight" → today at 20:00 IST
- Default priority is 'medium' unless the user says urgent/important/ASAP (→ high/urgent).
- Triggers like "remind me", "don't forget", "set a reminder", "I need to" → create_task.
- After creating a task with no tags, ask once: "Any tags for this? (e.g. work, personal)"
""",
    tools=[
        FunctionTool(create_task),
        FunctionTool(list_tasks),
        FunctionTool(update_task),
        FunctionTool(delete_task),
        FunctionTool(escalate_overdue_tasks),
    ],
)
