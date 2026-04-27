# === tools/analytics_tools.py ===
"""
Analytics helpers that query Firestore to compute productivity statistics.

Functions are exposed as ADK tools via FunctionTool wrappers in the
analytics_agent. All docstrings follow Google style so the ADK framework
can use them as tool descriptions.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import google.auth
from google.cloud import firestore
from google.cloud.firestore_v1.async_client import AsyncClient
from google.cloud.firestore_v1.base_query import FieldFilter

from config.settings import settings, LOCAL_TZ

# ── Firestore client ──────────────────────────────────────────────────────────

_db: AsyncClient | None = None


def _get_db() -> AsyncClient:
    global _db
    if _db is None:
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        _db = AsyncClient(
            project=settings.GCP_PROJECT_ID,
            credentials=credentials,
            database=settings.FIRESTORE_DATABASE,
        )
    return _db


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ── Completion Rate ───────────────────────────────────────────────────────────


async def get_task_completion_rate(
    user_id: str = "default_user",
    days: int = 7,
) -> Dict[str, Any]:
    """Calculate the task completion rate over the last N days.

    Args:
        user_id: Owner of the tasks to analyse.
        days: Number of days to look back. Defaults to 7.

    Returns:
        Dict with:
          - 'total_tasks' (int): Tasks created in the period.
          - 'completed_tasks' (int): Tasks marked completed.
          - 'overdue_tasks' (int): Tasks in 'overdue' status.
          - 'completion_rate_pct' (float): completed / total * 100.
          - 'period_days' (int): The analysed period length.
    """
    db = _get_db()
    since = _iso(_now() - timedelta(days=days))

    query = (
        db.collection("tasks")
        .where(filter=FieldFilter("user_id", "==", user_id))
        .where(filter=FieldFilter("created_at", ">=", since))
    )
    docs = await query.get()
    tasks = [d.to_dict() for d in docs]

    total = len(tasks)
    completed = sum(1 for t in tasks if t.get("status") == "completed")
    overdue = sum(1 for t in tasks if t.get("status") == "overdue")

    rate = round((completed / total * 100) if total else 0.0, 1)
    return {
        "total_tasks": total,
        "completed_tasks": completed,
        "overdue_tasks": overdue,
        "completion_rate_pct": rate,
        "period_days": days,
    }


# ── Weekly Trends ─────────────────────────────────────────────────────────────


async def get_weekly_trends(
    user_id: str = "default_user",
    weeks: int = 4,
) -> List[Dict[str, Any]]:
    """Return week-by-week task completion trends for the last N weeks.

    Args:
        user_id: Owner of the tasks to analyse.
        weeks: Number of complete weeks to analyse. Defaults to 4.

    Returns:
        List of dicts (one per week), each with:
          - 'week_start' (str): ISO date of Monday of that week.
          - 'total' (int): Tasks created that week.
          - 'completed' (int): Tasks completed that week.
          - 'overdue' (int): Tasks overdue that week.
          - 'completion_rate_pct' (float): Completion percentage.
    """
    db = _get_db()
    since = _iso(_now() - timedelta(weeks=weeks))
    query = (
        db.collection("tasks")
        .where(filter=FieldFilter("user_id", "==", user_id))
        .where(filter=FieldFilter("created_at", ">=", since))
        .order_by("created_at")
    )
    docs = await query.get()
    tasks = [d.to_dict() for d in docs]

    # Bucket by ISO calendar week
    week_buckets: dict[str, dict] = defaultdict(
        lambda: {"total": 0, "completed": 0, "overdue": 0}
    )
    for task in tasks:
        try:
            dt = datetime.fromisoformat(task["created_at"])
        except (KeyError, ValueError):
            continue
        week_key = dt.strftime("%G-W%V")  # e.g. '2024-W23'
        monday = dt - timedelta(days=dt.weekday())
        bucket = week_buckets[week_key]
        bucket["week_start"] = monday.date().isoformat()
        bucket["total"] += 1
        if task.get("status") == "completed":
            bucket["completed"] += 1
        if task.get("status") == "overdue":
            bucket["overdue"] += 1

    result = []
    for week_key in sorted(week_buckets.keys()):
        b = week_buckets[week_key]
        rate = round((b["completed"] / b["total"] * 100) if b["total"] else 0.0, 1)
        result.append({
            "week_start": b.get("week_start", week_key),
            "total": b["total"],
            "completed": b["completed"],
            "overdue": b["overdue"],
            "completion_rate_pct": rate,
        })
    return result


# ── Priority Distribution ─────────────────────────────────────────────────────


async def get_task_stats_by_priority(
    user_id: str = "default_user",
    days: int = 30,
) -> Dict[str, Any]:
    """Breakdown of task counts and completion rates grouped by priority.

    Args:
        user_id: Owner of the tasks to analyse.
        days: Number of days to look back. Defaults to 30.

    Returns:
        Dict mapping each priority level ('low', 'medium', 'high', 'urgent')
        to a sub-dict with 'total', 'completed', and 'completion_rate_pct'.
    """
    db = _get_db()
    since = _iso(_now() - timedelta(days=days))
    query = (
        db.collection("tasks")
        .where(filter=FieldFilter("user_id", "==", user_id))
        .where(filter=FieldFilter("created_at", ">=", since))
    )
    docs = await query.get()
    tasks = [d.to_dict() for d in docs]

    buckets: dict[str, dict] = {
        p: {"total": 0, "completed": 0}
        for p in ("low", "medium", "high", "urgent")
    }
    for task in tasks:
        p = task.get("priority", "medium")
        if p in buckets:
            buckets[p]["total"] += 1
            if task.get("status") == "completed":
                buckets[p]["completed"] += 1

    result = {}
    for p, b in buckets.items():
        rate = round((b["completed"] / b["total"] * 100) if b["total"] else 0.0, 1)
        result[p] = {
            "total": b["total"],
            "completed": b["completed"],
            "completion_rate_pct": rate,
        }
    return result


# ── Productivity Score ────────────────────────────────────────────────────────


async def get_productivity_score(
    user_id: str = "default_user",
    days: int = 7,
) -> Dict[str, Any]:
    """Compute a weekly productivity score on a 0–100 scale.

    Scoring formula:
      - Completion rate contributes 60 points (60 × completion_rate / 100).
      - Overdue penalty: subtract 2 points per overdue task (capped at −20).
      - High/urgent task bonus: +1 per completed high/urgent task (capped at +10).

    Args:
        user_id: Owner whose productivity to score.
        days: Lookback window in days. Defaults to 7.

    Returns:
        Dict with:
          - 'score' (int): Final productivity score 0–100.
          - 'completion_rate_pct' (float): Raw completion rate.
          - 'overdue_count' (int): Number of overdue tasks.
          - 'high_priority_completed' (int): Completed high/urgent tasks.
          - 'label' (str): Qualitative label ('Excellent', 'Good', 'Fair', 'Needs Work').
    """
    db = _get_db()
    since = _iso(_now() - timedelta(days=days))
    query = (
        db.collection("tasks")
        .where(filter=FieldFilter("user_id", "==", user_id))
        .where(filter=FieldFilter("created_at", ">=", since))
    )
    docs = await query.get()
    tasks = [d.to_dict() for d in docs]

    total = len(tasks)
    completed = sum(1 for t in tasks if t.get("status") == "completed")
    overdue = sum(1 for t in tasks if t.get("status") == "overdue")
    high_done = sum(
        1 for t in tasks
        if t.get("status") == "completed" and t.get("priority") in ("high", "urgent")
    )

    rate = (completed / total * 100) if total else 0.0
    score = (0.60 * rate) - min(overdue * 2, 20) + min(high_done, 10)
    score = max(0, min(100, round(score)))

    if score >= 80:
        label = "Excellent"
    elif score >= 60:
        label = "Good"
    elif score >= 40:
        label = "Fair"
    else:
        label = "Needs Work"

    return {
        "score": score,
        "completion_rate_pct": round(rate, 1),
        "overdue_count": overdue,
        "high_priority_completed": high_done,
        "label": label,
        "period_days": days,
    }


# ── Today's Summary ───────────────────────────────────────────────────────────


async def get_today_summary(user_id: str = "default_user") -> Dict[str, Any]:
    """Return a concise summary of today's task workload.

    Args:
        user_id: Owner of the tasks to summarise.

    Returns:
        Dict with:
          - 'pending_today' (int): Tasks due today that are still pending.
          - 'completed_today' (int): Tasks completed today.
          - 'overdue' (int): All overdue tasks (past due, not completed).
          - 'upcoming_24h' (int): Tasks due in the next 24 hours.
    """
    db = _get_db()
    now = _now()
    today_start = _iso(now.replace(hour=0, minute=0, second=0, microsecond=0))
    today_end = _iso(now.replace(hour=23, minute=59, second=59, microsecond=0))
    tomorrow = _iso(now + timedelta(days=1))

    # Pending tasks due today
    q_today = (
        db.collection("tasks")
        .where(filter=FieldFilter("user_id", "==", user_id))
        .where(filter=FieldFilter("status", "==", "pending"))
        .where(filter=FieldFilter("due_date", ">=", today_start))
        .where(filter=FieldFilter("due_date", "<=", today_end))
    )
    # Overdue tasks
    q_overdue = (
        db.collection("tasks")
        .where(filter=FieldFilter("user_id", "==", user_id))
        .where(filter=FieldFilter("status", "in", ["pending", "overdue"]))
        .where(filter=FieldFilter("due_date", "<", today_start))
    )
    # Completed today
    q_completed = (
        db.collection("tasks")
        .where(filter=FieldFilter("user_id", "==", user_id))
        .where(filter=FieldFilter("status", "==", "completed"))
        .where(filter=FieldFilter("updated_at", ">=", today_start))
        .where(filter=FieldFilter("updated_at", "<=", today_end))
    )
    # Upcoming in next 24h
    q_upcoming = (
        db.collection("tasks")
        .where(filter=FieldFilter("user_id", "==", user_id))
        .where(filter=FieldFilter("status", "==", "pending"))
        .where(filter=FieldFilter("due_date", ">", today_end))
        .where(filter=FieldFilter("due_date", "<=", tomorrow))
    )

    today_docs, overdue_docs, completed_docs, upcoming_docs = await __import__(
        "asyncio"
    ).gather(
        q_today.get(),
        q_overdue.get(),
        q_completed.get(),
        q_upcoming.get(),
    )

    return {
        "pending_today": len(today_docs),
        "completed_today": len(completed_docs),
        "overdue": len(overdue_docs),
        "upcoming_24h": len(upcoming_docs),
    }
