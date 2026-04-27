# === tools/smart_tools.py ===
"""
Smart AI Tools — Gemini-powered suggestions that go beyond CRUD.

UNIQUE FEATURES:
  1. Priority Recommender  — scans all tasks and suggests re-prioritisation.
  2. Auto Tag Suggester    — reads task/note content and recommends tags.
  3. Focus Score          — predicts your best focus time based on calendar patterns.
  4. Natural Language Undo — "Undo the last 3 task deletions" multi-step reversal.
  5. Deadline Risk Analyser — flags tasks likely to slip based on workload.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from google import genai
from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from google.genai import types as genai_types

from config.settings import settings, LOCAL_TZ
from tools.firestore_tools import (
    get_last_event,
    list_tasks,
    undo_last_action,
    update_task,
)

logger = logging.getLogger(__name__)
_client: Optional[genai.Client] = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.GOOGLE_API_KEY)
    return _client


def _gemini_model_candidates() -> List[str]:
    """Prefer configured model, then stable fallbacks when quota/routing differs by model."""
    primary = (settings.GEMINI_MODEL or "gemini-2.5-flash").strip()
    fallbacks = [
        "gemini-2.5-flash",
        "gemini-2.5-flash-preview-04-17",
        "gemini-1.5-flash",
        "gemini-1.5-flash-8b",
    ]
    out: List[str] = []
    seen: set[str] = set()
    for m in [primary] + fallbacks:
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


def _is_rate_limit_error(exc: BaseException) -> bool:
    s = str(exc).lower()
    return "429" in s or "resource_exhausted" in s or "quota" in s or "rate" in s and "limit" in s


def _looks_like_api_quota_message(text: str) -> bool:
    """True if a 'summary' field accidentally contains a raw API error (older builds)."""
    if not text or len(text) < 80:
        return False
    t = text.lower()
    if "resource_exhausted" in t or "rate-limit" in t or "quota exceeded" in t:
        return True
    if "429" in t and ("generativelanguage" in t or "gemini" in t or "free_tier" in t):
        return True
    if "analysis failed" in t and "429" in t:
        return True
    return False


async def _ask_gemini(prompt: str) -> str:
    """Fire a text-only Gemini call with model fallback and light retry on 429."""
    client = _get_client()
    last_exc: Optional[BaseException] = None
    for model in _gemini_model_candidates():
        for attempt in range(2):
            try:
                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model=model,
                    contents=[genai_types.Content(role="user", parts=[genai_types.Part(text=prompt)])],
                )
                return (response.text or "").strip()
            except Exception as exc:
                last_exc = exc
                if _is_rate_limit_error(exc) and attempt == 0:
                    await asyncio.sleep(min(8.0, 2.5 + random.random()))
                    continue
                if _is_rate_limit_error(exc):
                    break
                raise
    if last_exc:
        raise last_exc
    raise RuntimeError("Gemini generate_content returned no response")


def _heuristic_priority_recommendations(tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Rule-based priority suggestions when AI quota is exhausted."""
    recommendations: List[Dict[str, Any]] = []
    now = datetime.now(LOCAL_TZ)
    today: date = now.date()
    for t in tasks:
        tid = str(t.get("id", ""))
        title = str(t.get("title", "Task"))
        cur = (t.get("priority") or "medium").lower()
        if cur not in ("urgent", "high", "medium", "low"):
            cur = "medium"
        due_raw = t.get("due_date") or ""
        days = 999
        if due_raw:
            try:
                ds = str(due_raw).replace("Z", "")[:10]
                d_obj = datetime.strptime(ds, "%Y-%m-%d").date()
                days = (d_obj - today).days
            except Exception:
                pass
        rec = cur
        reason = ""
        if days <= 2 and cur != "urgent":
            rec = "urgent"
            reason = "Due within 2 days or overdue — escalate to urgent"
        elif 0 < days <= 7 and cur == "low":
            rec = "medium"
            reason = "Due within a week — low is too relaxed"
        elif days > 21 and cur == "urgent":
            rec = "high"
            reason = "Due more than three weeks away — urgent may be overstated"
        if rec != cur:
            recommendations.append(
                {
                    "task_id": tid,
                    "task_title": title,
                    "current_priority": cur,
                    "recommended_priority": rec,
                    "reasoning": reason,
                }
            )
    if recommendations:
        summary = (
            f"Rule-based suggestions ({len(recommendations)} change(s)) — "
            "AI was rate-limited; these follow simple due-date rules. You can apply them below."
        )
    else:
        summary = "No priority changes needed by simple due-date rules."
    return {
        "recommendations": recommendations,
        "summary": summary,
        "heuristic_fallback": True,
    }


def _parse_json(raw: str) -> Any:
    raw = re.sub(r"```(?:json)?\s*", "", raw)
    raw = re.sub(r"```\s*$", "", raw.strip()).strip()
    return json.loads(raw)


# ── 1. Priority Recommender ───────────────────────────────────────────────────


async def recommend_priorities(
    user_id: str = "default_user",
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Analyse all pending tasks and recommend priority adjustments using AI.

    Gemini looks at due dates, current priorities, and task titles to identify:
    - Tasks that are under-prioritised (low/medium but due soon)
    - Tasks that are over-prioritised (urgent/high but far out)
    - Tasks that should be dropped or delegated

    Args:
        user_id: Owner whose tasks to analyse.
        dry_run: When True (default), returns recommendations without applying
            them. Set False to automatically update task priorities in Firestore.

    Returns:
        Dict with:
          - 'recommendations' (list): Each has task_id, task_title, current_priority,
            recommended_priority, and reasoning.
          - 'applied' (int): Number of changes applied (0 if dry_run=True).
          - 'summary' (str): One-paragraph overview of the analysis.
    """
    tasks = await list_tasks(user_id=user_id, status="pending", limit=50)
    if not tasks:
        return {"recommendations": [], "applied": 0, "summary": "No pending tasks to analyse."}

    now_iso = datetime.now(LOCAL_TZ).isoformat()
    prompt = (
        "You are a productivity analyst. Analyse these tasks and recommend priority changes.\n"
        "Rules:\n"
        "- Due within 2 days AND not urgent → recommend 'urgent'\n"
        "- Due within 1 week AND low → recommend 'medium'\n"
        "- Due more than 3 weeks away AND urgent → recommend 'high'\n"
        "- Identify any tasks to 'drop' (mark as note instead of task)\n\n"
        "Return JSON only:\n"
        '{"recommendations": [{"task_id": "...", "task_title": "...", "current_priority": "...",'
        ' "recommended_priority": "...", "reasoning": "..."}], "summary": "..."}\n\n'
        f"Current time: {now_iso}\n\nTASKS:\n{json.dumps(tasks, indent=2, default=str)}"
    )

    try:
        raw = await _ask_gemini(prompt)
        result = _parse_json(raw)
        # Some API failure modes return parseable text that is not task JSON; fall back
        if not isinstance(result, dict) or "recommendations" not in result:
            raise ValueError("invalid_recommendation_shape")
    except Exception as exc:
        logger.warning("recommend_priorities: AI failed, using heuristics: %s", exc)
        return await _apply_priority_result(_heuristic_priority_recommendations(tasks), user_id, dry_run)

    _sum = (result.get("summary") or "")
    if _looks_like_api_quota_message(_sum):
        logger.warning("recommend_priorities: discarding error-like summary, using heuristics")
        return await _apply_priority_result(_heuristic_priority_recommendations(tasks), user_id, dry_run)

    applied = 0
    if not dry_run:
        for rec in result.get("recommendations", []):
            if rec.get("recommended_priority") != rec.get("current_priority"):
                try:
                    await update_task(
                        task_id=rec["task_id"],
                        updates={"priority": rec["recommended_priority"]},
                        user_id=user_id,
                    )
                    applied += 1
                except Exception:
                    pass

    return {**result, "applied": applied, "dry_run": dry_run}


async def _apply_priority_result(
    result: Dict[str, Any], user_id: str, dry_run: bool
) -> Dict[str, Any]:
    """Merge heuristic/AI result and optionally write priorities to Firestore."""
    base = {k: v for k, v in result.items() if k != "heuristic_fallback"}
    applied = 0
    if not dry_run:
        for rec in result.get("recommendations", []):
            if rec.get("recommended_priority") != rec.get("current_priority"):
                try:
                    await update_task(
                        task_id=rec["task_id"],
                        updates={"priority": rec["recommended_priority"]},
                        user_id=user_id,
                    )
                    applied += 1
                except Exception:
                    pass
    return {**base, "applied": applied, "dry_run": dry_run}


# ── 2. Auto Tag Suggester ─────────────────────────────────────────────────────


async def suggest_tags(
    content: str,
    content_type: str = "task",
) -> Dict[str, Any]:
    """Suggest relevant tags for a task title or note content using AI.

    Uses Gemini to analyse the content and recommend 2–5 appropriate tags
    from a predefined taxonomy plus custom ones it infers from context.

    Args:
        content: Task title or note body text to analyse.
        content_type: Either 'task' or 'note'. Affects tag recommendations.

    Returns:
        Dict with:
          - 'suggested_tags' (list): Recommended tag strings.
          - 'confidence' (str): 'high', 'medium', or 'low'.
          - 'reasoning' (str): Brief explanation of tag choices.
    """
    prompt = (
        f"Suggest 2-5 relevant tags for this {content_type}. "
        "Choose from: work, personal, meeting, urgent, idea, reference, project, "
        "finance, health, learning, communication, review, design, tech, admin. "
        "Add 1-2 custom tags if they fit better.\n"
        'Return JSON only: {"suggested_tags": [...], "confidence": "high|medium|low", '
        '"reasoning": "one sentence"}\n\n'
        f"CONTENT: {content[:500]}"
    )
    try:
        raw = await _ask_gemini(prompt)
        return _parse_json(raw)
    except Exception as exc:
        if _is_rate_limit_error(exc):
            return {
                "suggested_tags": [],
                "confidence": "low",
                "reasoning": "ai_quota: rate_limited",
            }
        return {"suggested_tags": [], "confidence": "low", "reasoning": str(exc)}


# ── 3. Deadline Risk Analyser ─────────────────────────────────────────────────


async def analyse_deadline_risk(
    user_id: str = "default_user",
) -> Dict[str, Any]:
    """Identify tasks at risk of being missed based on workload and deadlines.

    Analyses the ratio of tasks due per day vs available working hours, flags
    any days that appear overloaded, and estimates which tasks are likely to slip.

    Args:
        user_id: Owner whose task portfolio to analyse.

    Returns:
        Dict with:
          - 'at_risk_tasks' (list): Tasks likely to slip, with risk_level and reason.
          - 'overloaded_days' (list): Dates with too many tasks due.
          - 'risk_score' (int): 0–100 overall portfolio risk score.
          - 'recommendation' (str): One concrete mitigation suggestion.
    """
    tasks = await list_tasks(user_id=user_id, status="pending", limit=100)
    if not tasks:
        return {
            "at_risk_tasks": [],
            "overloaded_days": [],
            "risk_score": 0,
            "recommendation": "No pending tasks — risk is zero!",
        }

    # Group tasks by due date
    day_map: Dict[str, int] = {}
    for t in tasks:
        due = t.get("due_date", "")[:10]  # date part only
        if due:
            day_map[due] = day_map.get(due, 0) + 1

    overloaded = [d for d, count in day_map.items() if count >= 4]
    near_term = [
        t for t in tasks
        if t.get("due_date", "") <= (datetime.now(LOCAL_TZ) + timedelta(days=3)).isoformat()
    ]

    prompt = (
        "Analyse this task list for deadline risk. Identify tasks likely to be missed.\n"
        'Return JSON: {"at_risk_tasks": [{"task_id":"...","task_title":"...","risk_level":"high|medium|low","reason":"..."}],'
        '"risk_score": 0-100, "recommendation": "string"}\n\n'
        f"OVERLOADED DAYS: {overloaded}\n"
        f"NEAR-TERM TASKS (next 3 days): {json.dumps(near_term, indent=2, default=str)}\n"
        f"ALL TASKS COUNT: {len(tasks)}"
    )
    try:
        raw = await _ask_gemini(prompt)
        result = _parse_json(raw)
    except Exception as exc:
        msg = "AI rate-limited — showing structural signals only" if _is_rate_limit_error(exc) else str(exc)
        result = {"at_risk_tasks": [], "risk_score": 50, "recommendation": msg}

    return {**result, "overloaded_days": overloaded}


# ── 4. Multi-step Natural Language Undo ───────────────────────────────────────


async def undo_multiple(
    count: int = 1,
    user_id: str = "default_user",
) -> Dict[str, Any]:
    """Undo the last N mutating actions for a user.

    Calls undo_last_action() repeatedly up to count times, stopping early
    if there are no more events to undo.

    Args:
        count: Number of actions to undo. Must be between 1 and 10.
        user_id: Owner whose actions to reverse.

    Returns:
        Dict with:
          - 'undone' (list): Each reversal result.
          - 'total_undone' (int): How many were successfully reversed.
          - 'stopped_early' (bool): True if the event log ran out before count.
    """
    count = max(1, min(count, 10))
    results = []
    stopped_early = False

    for _ in range(count):
        try:
            result = await undo_last_action(user_id=user_id)
            results.append(result)
        except ValueError:
            stopped_early = True
            break

    return {
        "undone": results,
        "total_undone": len(results),
        "stopped_early": stopped_early,
    }


# ── 5. Daily Focus Score ──────────────────────────────────────────────────────


async def get_daily_focus_recommendation(
    user_id: str = "default_user",
) -> Dict[str, Any]:
    """Recommend the optimal focus block for today based on task urgency and calendar.

    Analyses today's pending tasks and provides a prioritised focus order
    with time estimates for each item.

    Args:
        user_id: Owner to advise.

    Returns:
        Dict with:
          - 'focus_order' (list): Tasks in recommended focus sequence with
            estimated_minutes and why_first reasoning.
          - 'total_estimated_minutes' (int): Total focus time needed today.
          - 'motivational_message' (str): AI-personalised motivation line.
    """
    from tools.analytics_tools import get_today_summary
    summary = await get_today_summary(user_id=user_id)
    pending = await list_tasks(user_id=user_id, status="pending", limit=20)

    # Sort by priority + due date proximity
    priority_order = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
    now_iso = datetime.now(LOCAL_TZ).isoformat()
    scored = []
    for t in pending:
        p_score = priority_order.get(t.get("priority", "medium"), 2)
        due = t.get("due_date", "9999")
        scored.append((p_score, due, t))
    scored.sort(key=lambda x: (x[0], x[1]))
    ordered_tasks = [t for _, _, t in scored[:10]]

    prompt = (
        "Create a prioritised focus order for today's tasks. "
        "Estimate how long each task takes (30, 60, 90, or 120 minutes). "
        "Write a personalised motivational message based on the workload.\n"
        "Return JSON only:\n"
        '{"focus_order": [{"task_id":"...","task_title":"...","estimated_minutes":60,"why_first":"..."}],'
        '"total_estimated_minutes": 0, "motivational_message": "..."}\n\n'
        f"TODAY SUMMARY: {json.dumps(summary)}\n"
        f"TODAY'S TASKS: {json.dumps(ordered_tasks, indent=2, default=str)}"
    )
    try:
        raw = await _ask_gemini(prompt)
        return _parse_json(raw)
    except Exception as exc:
        return {
            "focus_order": ordered_tasks[:5],
            "total_estimated_minutes": len(ordered_tasks) * 60,
            "motivational_message": "You've got this! One task at a time."
            if not _is_rate_limit_error(exc)
            else "Focus on your top tasks by due date while AI is rate-limited.",
            "error": str(exc) if not _is_rate_limit_error(exc) else "rate_limited",
        }
