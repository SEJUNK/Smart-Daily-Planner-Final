# === agents/analytics_agent.py ===
"""
Analytics Agent — answers productivity questions using ADK LlmAgent.

Tools exposed:
  - get_completion_rate
  - get_weekly_trends
  - get_priority_breakdown
  - get_productivity_score
  - get_today_summary
"""

from __future__ import annotations

from typing import Any, Dict, List

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from config.settings import settings
from tools.analytics_tools import (
    get_productivity_score as _get_score,
    get_task_completion_rate as _get_completion_rate,
    get_task_stats_by_priority as _get_priority_stats,
    get_today_summary as _get_today_summary,
    get_weekly_trends as _get_weekly_trends,
)


# ── Tool wrappers ─────────────────────────────────────────────────────────────


async def get_completion_rate(
    user_id: str = "default_user",
    days: int = 7,
) -> Dict[str, Any]:
    """Calculate the task completion rate over the last N days.

    Args:
        user_id: Owner of the tasks to analyse.
        days: Number of days to look back (default 7, max 90).

    Returns:
        Dict with 'total_tasks', 'completed_tasks', 'overdue_tasks',
        'completion_rate_pct' (float), and 'period_days'.
    """
    return await _get_completion_rate(user_id=user_id, days=days)


async def get_weekly_trends(
    user_id: str = "default_user",
    weeks: int = 4,
) -> Dict[str, Any]:
    """Return week-by-week productivity trends for the last N weeks.

    Args:
        user_id: Owner of the tasks to analyse.
        weeks: Number of complete weeks to include (default 4).

    Returns:
        Dict with 'trends' (list of weekly stats) and 'summary' string.
    """
    trends = await _get_weekly_trends(user_id=user_id, weeks=weeks)
    # Compute overall trend direction
    if len(trends) >= 2:
        first_rate = trends[0]["completion_rate_pct"]
        last_rate = trends[-1]["completion_rate_pct"]
        direction = "improving" if last_rate > first_rate else "declining" if last_rate < first_rate else "stable"
    else:
        direction = "insufficient data"

    return {
        "trends": trends,
        "weeks_analysed": len(trends),
        "trend_direction": direction,
    }


async def get_priority_breakdown(
    user_id: str = "default_user",
    days: int = 30,
) -> Dict[str, Any]:
    """Return task counts and completion rates grouped by priority level.

    Args:
        user_id: Owner of the tasks to analyse.
        days: Lookback window in days. Defaults to 30.

    Returns:
        Dict mapping each priority ('low', 'medium', 'high', 'urgent') to
        stats: 'total', 'completed', 'completion_rate_pct'.
    """
    return await _get_priority_stats(user_id=user_id, days=days)


async def get_productivity_score(
    user_id: str = "default_user",
    days: int = 7,
) -> Dict[str, Any]:
    """Compute a weekly productivity score on a 0–100 scale.

    The score weights: completion rate (60%), overdue penalty (−2 per task),
    and high-priority completions bonus (+1 each, capped at +10).

    Args:
        user_id: Owner whose productivity to score.
        days: Lookback window in days. Defaults to 7.

    Returns:
        Dict with 'score' (int 0–100), 'label' (str), 'completion_rate_pct',
        'overdue_count', and 'high_priority_completed'.
    """
    return await _get_score(user_id=user_id, days=days)


async def get_today_summary(user_id: str = "default_user") -> Dict[str, Any]:
    """Return a concise summary of today's task workload.

    Args:
        user_id: Owner of the tasks to summarise.

    Returns:
        Dict with 'pending_today', 'completed_today', 'overdue', and
        'upcoming_24h' counts.
    """
    return await _get_today_summary(user_id=user_id)


# ── ADK LlmAgent ──────────────────────────────────────────────────────────────

analytics_agent = LlmAgent(
    name="analytics_agent",
    model=settings.GEMINI_MODEL,
    description=(
        "Answers productivity questions: completion rates, weekly trends, "
        "priority breakdowns, and overall productivity scores."
    ),
    instruction="""You are the Analytics Coach for the Smart Daily Planner.

Your responsibilities:
1. Answer questions about productivity, task completion rates, and trends.
2. Provide weekly productivity scores and explain what drives them.
3. Break down performance by priority level (low/medium/high/urgent).
4. Give actionable, specific advice based on the actual data.

Guidelines:
- Always contextualise numbers: "72% is above the 65% weekly average — well done!"
- When trends are declining, identify the specific bottleneck: overloading, low-priority
  tasks crowding urgent ones, or lack of daily reviews.
- Use get_today_summary for: "how am I doing today?", "what did I complete?", "today's tasks".
- Use get_weekly_trends when asked about progress over time or week-on-week improvement.
- Use get_priority_breakdown when asked about focus area distribution.
- Format all percentages to one decimal place. Always show raw numbers alongside percentages.
- Always end your response with exactly ONE numbered action the user can take RIGHT NOW:
    Example: "1. Open your task list and mark any completed items to boost your score."
- All times are in Asia/Kolkata (IST, UTC+5:30).

When reporting a productivity score:
- 80–100: Excellent 🏆 — celebrate, then suggest one stretch goal.
- 60–79: Good 👍 — highlight what's working, name the one thing holding it back.
- 40–59: Fair 📈 — identify the single biggest bottleneck (usually overdue tasks).
- 0–39: Needs Work 💪 — suggest one tiny, immediate action (e.g. "Complete just 1 task now").
""",
    tools=[
        FunctionTool(get_completion_rate),
        FunctionTool(get_weekly_trends),
        FunctionTool(get_priority_breakdown),
        FunctionTool(get_productivity_score),
        FunctionTool(get_today_summary),
    ],
)
