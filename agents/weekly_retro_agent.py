# === agents/weekly_retro_agent.py ===
"""
Weekly Retrospective Agent — auto-generates a narrative weekly review.

UNIQUE FEATURE: Every Friday (or on demand), this agent:
  1. Pulls the week's completed/overdue/pending tasks.
  2. Analyses calendar events attended.
  3. Counts notes created.
  4. Asks Gemini to write a personalised narrative retrospective:
     - What went well (wins)
     - What slipped (overdue analysis)
     - Patterns detected (time of day, tag clusters, priority drift)
     - One concrete suggestion for next week
     - A motivational closing line tailored to the data
  5. Saves the retro as a richly-formatted note AND emails it if requested.

This is NOT just a stats dump — it's a genuine narrative written by AI
that reads like a coach wrote it.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

from google import genai
from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from google.genai import types as genai_types

from config.settings import settings, LOCAL_TZ
from tools.analytics_tools import (
    get_task_completion_rate,
    get_task_stats_by_priority,
    get_weekly_trends,
)
from tools.calendar_tools import list_calendar_events
from tools.firestore_tools import create_note, list_tasks, list_notes

_client: Optional[genai.Client] = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.GOOGLE_API_KEY)
    return _client


RETRO_PROMPT = """You are a supportive productivity coach writing a personal weekly retrospective.

Write in second-person ("You completed...", "This week you...").
Tone: warm, honest, specific, motivating — like a trusted mentor who has seen the data.
Length: 500–700 words total. Be specific — cite actual numbers from the data.

Use this data to write a narrative retrospective with ALL six sections below:

## 🏆 This Week's Wins
Celebrate concrete achievements. Reference exact task counts, completion rate, and calendar events.

## 😬 What Slipped (and Why It Might Have)
Analyse overdue tasks by priority. Be empathetic — suggest a root cause, not blame.

## 🔍 Patterns I Noticed
Identify 2–3 observable patterns from the data (busy days, tag clusters, priority drift, note-taking habits).

## 💡 One Thing to Change Next Week
One specific, actionable change. Not generic advice — tailor it to the actual numbers.

## 🗓️ Next Week's Top 3 Priorities
Based on pending + overdue task data, list the 3 most important things to tackle first next week.
Format as a numbered list.

## 🚀 Your Motivational Closing
Two sentences max. Personal, specific, energising. Reference something from their actual week.

DATA:
{data_json}

Write the full retrospective now. Use markdown formatting. Be specific — mention actual numbers.
Do NOT add any text before the first ## heading.
"""


async def generate_weekly_retro(
    user_id: str = "default_user",
    week_offset: int = 0,
) -> Dict[str, Any]:
    """Generate a personalised narrative weekly retrospective.

    Combines task stats, calendar data, and notes activity for the target
    week, then uses Gemini to write a coach-style narrative retrospective.

    Args:
        user_id: Owner whose week to review.
        week_offset: 0 = current week, -1 = last week, etc. Defaults to 0.

    Returns:
        Dict with:
          - 'narrative' (str): Full AI-written retrospective in markdown.
          - 'stats' (dict): Raw stats used to generate the narrative.
          - 'week_label' (str): Human-readable week label (e.g. 'Week of Jun 10').
          - 'productivity_score' (int): 0–100 score for the week.
    """
    now = datetime.now(LOCAL_TZ)
    week_start = now - timedelta(days=now.weekday()) + timedelta(weeks=week_offset)
    week_end = week_start + timedelta(days=7)

    week_label = f"Week of {week_start.strftime('%b %d, %Y')}"

    # Gather data concurrently — handle missing Firestore indexes gracefully
    import asyncio
    from google.api_core.exceptions import FailedPrecondition

    _empty_completion = {"completion_rate_pct": 0.0, "total_tasks": 0, "completed_tasks": 0, "overdue_tasks": 0}
    _empty_priority = {}

    async def _safe(coro, fallback):
        try:
            return await coro
        except (FailedPrecondition, Exception):
            return fallback

    completion, priority_stats, all_tasks, events, notes = await asyncio.gather(
        _safe(get_task_completion_rate(user_id=user_id, days=7), _empty_completion),
        _safe(get_task_stats_by_priority(user_id=user_id, days=7), _empty_priority),
        _safe(list_tasks(user_id=user_id, limit=50), []),
        _safe(list_calendar_events(time_min=week_start.isoformat(), time_max=week_end.isoformat(), max_results=30), []),
        _safe(list_notes(user_id=user_id, limit=10), []),
    )

    # Build tag frequency map
    tag_counts: Dict[str, int] = {}
    for task in all_tasks:
        for tag in task.get("tags", []):
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
    top_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    # Build overdue analysis
    overdue_tasks = [t for t in all_tasks if t.get("status") == "overdue"]
    overdue_priorities = {}
    for t in overdue_tasks:
        p = t.get("priority", "medium")
        overdue_priorities[p] = overdue_priorities.get(p, 0) + 1

    data = {
        "week_label": week_label,
        "completion_rate_pct": completion["completion_rate_pct"],
        "total_tasks": completion["total_tasks"],
        "completed_tasks": completion["completed_tasks"],
        "overdue_tasks": completion["overdue_tasks"],
        "priority_breakdown": priority_stats,
        "top_tags": top_tags,
        "calendar_events_count": len(events),
        "calendar_events": [e["summary"] for e in events[:5]],
        "notes_created": len(notes),
        "overdue_by_priority": overdue_priorities,
        "busiest_day_events": _find_busiest_day(events),
    }

    # Calculate simple score
    rate = completion["completion_rate_pct"]
    score = max(0, min(100, int(rate - completion["overdue_tasks"] * 3)))

    # Try Gemini first; fall back to rich rule-based narrative on any error
    client = _get_client()
    narrative = None
    if settings.GOOGLE_API_KEY:
        try:
            _prompt_text = RETRO_PROMPT.format(data_json=json.dumps(data, indent=2))
            _contents = [
                genai_types.Content(
                    role="user",
                    parts=[genai_types.Part(text=_prompt_text)],
                )
            ]
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=settings.GEMINI_MODEL,
                contents=_contents,
            )
            txt = response.text or ""
            if txt and not txt.startswith("Retrospective generation failed"):
                narrative = txt
        except Exception:
            pass  # fall through to rule-based narrative

    if not narrative:
        narrative = _build_rule_narrative(data, score)

    return {
        "narrative": narrative,
        "stats": data,
        "week_label": week_label,
        "productivity_score": score,
    }


def _build_rule_narrative(data: Dict, score: int) -> str:
    """Build a rich coach-style weekly retrospective narrative from raw stats — no AI needed."""
    completed = data.get("completed_tasks", 0)
    total     = data.get("total_tasks", 0)
    overdue   = data.get("overdue_tasks", 0)
    pending   = max(0, total - completed - overdue)
    rate      = data.get("completion_rate_pct", 0.0)
    events    = data.get("calendar_events_count", 0)
    notes_n   = data.get("notes_created", 0)
    top_tags  = data.get("top_tags", [])
    busiest   = data.get("busiest_day_events", "N/A")
    week      = data.get("week_label", "This Week")
    overdue_by_p = data.get("overdue_by_priority", {})

    # Tone based on score
    if score >= 80:
        tone_open = "You absolutely crushed it"
        tone_close_emoji = "🏆"
        closing = "This week proved what you're capable of. Carry this momentum into next week and set even bigger targets. You've earned it!"
    elif score >= 60:
        tone_open = "You had a genuinely productive week"
        tone_close_emoji = "📈"
        closing = "Solid progress this week. You're building great habits — next week, try to protect your morning hours for deep focus and watch your completion rate climb further."
    elif score >= 40:
        tone_open = "You made steady progress this week"
        tone_close_emoji = "💪"
        closing = "Every completed task is a win. Don't let the overdue pile discourage you — tackle one item at a time. Small consistent actions compound into big results."
    else:
        tone_open = "This week was tough, but you're still in the game"
        tone_close_emoji = "🌱"
        closing = "A rough week is data, not failure. Look at what blocked you — was it unclear priorities, unexpected interruptions, or scope creep? Pick ONE thing to fix next week."

    # Wins section
    wins_parts = []
    if completed > 0:
        wins_parts.append(f"You completed **{completed} task{'s' if completed!=1 else ''}** — a {rate:.0f}% completion rate.")
    if events > 0:
        wins_parts.append(f"You attended or managed **{events} calendar event{'s' if events!=1 else ''}** with {busiest} being your busiest day.")
    if notes_n > 0:
        wins_parts.append(f"You captured **{notes_n} note{'s' if notes_n!=1 else ''}**, keeping your thinking organised.")
    if top_tags:
        tag_str = ", ".join(f"#{t[0]}" for t in top_tags[:3])
        wins_parts.append(f"Your most active areas: {tag_str}.")
    wins_text = " ".join(wins_parts) if wins_parts else "Even if the numbers are small, showing up and tracking your work is a win in itself."

    # Slipped section
    if overdue == 0:
        slipped_text = "Remarkably, nothing slipped through the cracks this week. Every task was either completed or intentionally deferred. That's excellent discipline."
    else:
        priority_detail = ""
        if overdue_by_p:
            p_parts = [f"{v} {k}-priority" for k, v in overdue_by_p.items()]
            priority_detail = f" ({', '.join(p_parts)})"
        slipped_text = (
            f"**{overdue} task{'s' if overdue!=1 else ''}** went overdue{priority_detail}. "
            + ("This often happens when urgent tasks crowd out high-priority planned work. "
               if overdue_by_p.get("high", 0) > 0 or overdue_by_p.get("urgent", 0) > 0
               else "Consider breaking large tasks into smaller sub-tasks to avoid deadline drift. ")
            + f"Use the AI Rescue Plan to auto-reschedule these into your next available slots."
        )

    # Patterns section
    pattern_parts = []
    if busiest != "N/A":
        pattern_parts.append(f"**{busiest}** was your busiest day for meetings/events.")
    if rate >= 70:
        pattern_parts.append("Your completion rate suggests you're doing well at realistic planning — keep setting achievable daily targets.")
    elif pending > completed:
        pattern_parts.append("More tasks are being created than completed — consider a weekly review to prune, defer, or delegate lower-priority items.")
    if top_tags:
        pattern_parts.append(f"Your work clusters around {', '.join(t[0] for t in top_tags[:2])} — consider whether these align with your key goals.")
    patterns_text = " ".join(pattern_parts) if pattern_parts else "Not enough data yet for strong pattern detection. Keep tracking for 2–3 more weeks."

    # Suggestion
    if overdue > 2:
        suggestion = f"Run the **AI Rescue Plan** at the start of next week to automatically slot your {overdue} overdue tasks into available calendar time."
    elif rate < 50:
        suggestion = "Try the **2-task rule**: each morning, identify just 2 tasks that must be done before anything else. This single habit consistently improves completion rates."
    elif events > 10:
        suggestion = "You have a lot of calendar events. Block at least **two 2-hour slots** next week as unscheduled focus time — protect them like meetings."
    else:
        suggestion = "Keep a **daily 5-minute review** at 5 PM: look at tomorrow's tasks, confirm priorities, and adjust due dates proactively. This alone prevents most overdue situations."

    # Next Week Priorities — derive from pending + overdue tasks
    priority_order = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
    all_pending = [
        t for t in data.get("top_tags", [])  # top_tags is available but not tasks list
    ]
    # Use overdue and rate data to generate priorities
    next_priorities = []
    if overdue > 0:
        next_priorities.append(f"Clear your **{overdue} overdue task{'s' if overdue!=1 else ''}** using the AI Rescue Plan")
    if rate < 60:
        next_priorities.append("Run a **daily 5-minute review** each morning — pick 3 tasks max per day")
    else:
        next_priorities.append("Maintain your completion streak — protect 2-hour focus blocks in the morning")
    if events > 8:
        next_priorities.append("Block at least **2 unscheduled focus hours** next week — you had a meeting-heavy week")
    elif notes_n == 0:
        next_priorities.append("Start capturing meeting notes and ideas — use the **+ New Note** feature")
    else:
        next_priorities.append("Review your top tags and align them with your key weekly goals")
    next_priorities_text = "\n".join(f"{i+1}. {p}" for i, p in enumerate(next_priorities[:3]))

    return f"""## 🏆 This Week's Wins
{tone_open} during **{week}**. {wins_text}

## 😬 What Slipped (and Why It Might Have)
{slipped_text}

## 🔍 Patterns I Noticed
{patterns_text}

## 💡 One Thing to Change Next Week
{suggestion}

## 🗓️ Next Week's Top 3 Priorities
{next_priorities_text}

## 🚀 Your Motivational Closing {tone_close_emoji}
{closing}"""


def _find_busiest_day(events: List[Dict]) -> str:
    """Find the day name with the most calendar events."""
    day_counts: Dict[str, int] = {}
    for e in events:
        start = e.get("start", "")
        if start:
            try:
                dt = __import__("dateutil.parser", fromlist=["parser"]).parse(start)
                day_name = dt.strftime("%A")
                day_counts[day_name] = day_counts.get(day_name, 0) + 1
            except Exception:
                pass
    if not day_counts:
        return "N/A"
    return max(day_counts, key=day_counts.get)


async def save_retro_as_note(
    user_id: str = "default_user",
    week_offset: int = 0,
) -> Dict[str, Any]:
    """Generate the weekly retrospective and save it as a Firestore note.

    Args:
        user_id: Owner of the retrospective.
        week_offset: 0 = current week, -1 = last week. Defaults to 0.

    Returns:
        Dict with the full retro result plus 'note_id' of the saved note.
    """
    retro = await generate_weekly_retro(user_id=user_id, week_offset=week_offset)
    note = await create_note(
        title=f"📊 Weekly Retro — {retro['week_label']} (Score: {retro['productivity_score']}/100)",
        content=retro["narrative"],
        tags=["retrospective", "weekly-review", "auto-generated"],
        user_id=user_id,
    )
    retro["note_id"] = note["id"]
    return retro


def _md_to_html(text: str) -> str:
    """Convert a limited subset of markdown to inline-safe HTML for email bodies."""
    import html as _html
    lines = text.split("\n")
    out: List[str] = []
    in_list = False
    for line in lines:
        stripped = line.strip()
        # numbered list item
        if re.match(r"^\d+\.\s", stripped):
            if not in_list:
                out.append('<ol style="margin:8px 0 8px 20px;padding:0">')
                in_list = True
            item = re.sub(r"^\d+\.\s", "", stripped)
            out.append(f'<li style="margin-bottom:4px">{_inline_md(_html.escape(item))}</li>')
        # bullet list item
        elif stripped.startswith("- ") or stripped.startswith("* "):
            if not in_list:
                out.append('<ul style="margin:8px 0 8px 20px;padding:0">')
                in_list = True
            item = stripped[2:]
            out.append(f'<li style="margin-bottom:4px">{_inline_md(_html.escape(item))}</li>')
        else:
            if in_list:
                tag = "ol" if re.match(r"^\d+\.", lines[max(0, lines.index(line) - 1)].strip()) else "ul"
                out.append(f"</{tag}>")
                in_list = False
            if stripped:
                out.append(f'<p style="margin:6px 0;line-height:1.6">{_inline_md(_html.escape(stripped))}</p>')
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def _inline_md(text: str) -> str:
    """Replace **bold** and *italic* markdown with HTML tags."""
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    return text


def _parse_sections(narrative: str) -> Dict[str, tuple]:
    """Split narrative into {key: (heading, body)} dict."""
    raw = re.split(r"(?m)^## ", narrative.strip())
    result: Dict[str, tuple] = {}
    for chunk in raw:
        if not chunk.strip():
            continue
        nl = chunk.find("\n")
        heading = chunk[:nl].strip() if nl != -1 else chunk.strip()
        body = chunk[nl:].strip() if nl != -1 else ""
        h = heading.lower()
        if "win" in h:
            result["wins"] = (heading, body)
        elif "slip" in h:
            result["slipped"] = (heading, body)
        elif "pattern" in h:
            result["patterns"] = (heading, body)
        elif "change" in h or "one thing" in h:
            result["change"] = (heading, body)
        elif "next" in h or "priorit" in h:
            result["next"] = (heading, body)
        elif "motiv" in h or "closing" in h:
            result["closing"] = (heading, body)
    return result


def _strip_md(text: str) -> str:
    """Strip markdown syntax for plain-text email fallback."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"^#{1,3}\s+", "", text, flags=re.MULTILINE)
    return text


def _action_items_html(body: str) -> str:
    """Render numbered action items as large visual cards."""
    import html as _html
    items = re.findall(r"^\d+\.\s+(.+)", body, re.MULTILINE)
    if not items:
        # fall back: treat whole body as one item
        items = [body.strip()]
    circle_colors = ["#4f46e5", "#0891b2", "#7c3aed"]
    rows = []
    for i, item in enumerate(items[:3]):
        col = circle_colors[i % len(circle_colors)]
        rows.append(f"""
        <tr>
          <td width="40" valign="top" style="padding:0 14px 0 0">
            <span style="display:inline-block;width:32px;height:32px;background:{col};color:#fff;border-radius:50%;text-align:center;line-height:32px;font-size:15px;font-weight:800">{i+1}</span>
          </td>
          <td valign="middle" style="font-size:14px;color:#1e293b;font-weight:600;padding:4px 0">{_inline_md(_html.escape(item))}</td>
        </tr>
        <tr><td colspan="2" style="height:10px"></td></tr>""")
    return f'<table width="100%" cellpadding="0" cellspacing="0">{"".join(rows)}</table>'


def _two_col_section(
    left_heading: str, left_body: str, left_color: str, left_bg: str,
    right_heading: str, right_body: str, right_color: str, right_bg: str,
) -> str:
    import html as _html
    def _card(heading, body, color, bg):
        return f"""<td width="50%" valign="top" style="padding:4px">
          <div style="background:{bg};border-top:3px solid {color};border-radius:8px;padding:14px 16px;height:100%">
            <div style="font-size:13px;font-weight:700;color:{color};text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px">{_html.escape(heading)}</div>
            <div style="font-size:13px;color:#374151;line-height:1.5">{_md_to_html(body)}</div>
          </div>
        </td>"""
    return f"""<tr>
      {_card(left_heading, left_body, left_color, left_bg)}
      {_card(right_heading, right_body, right_color, right_bg)}
    </tr>"""


def _build_html_email(retro: Dict[str, Any]) -> str:
    """Build an action-first, infographic-style HTML email for the weekly retro."""
    import html as _html
    stats     = retro.get("stats", {})
    week_label = retro.get("week_label", "This Week")
    score     = retro.get("productivity_score", 0)
    narrative = retro.get("narrative", "")

    completed = stats.get("completed_tasks", 0)
    total     = stats.get("total_tasks", 0)
    overdue   = stats.get("overdue_tasks", 0)
    events    = stats.get("calendar_events_count", 0)
    notes_n   = stats.get("notes_created", 0)
    rate      = stats.get("completion_rate_pct", 0.0)
    bar_pct   = min(100, int(rate))

    # Score badge
    if score >= 80:   score_color, score_emoji, score_label = "#16a34a", "&#127942;", "Excellent"
    elif score >= 60: score_color, score_emoji, score_label = "#2563eb", "&#128200;", "Good"
    elif score >= 40: score_color, score_emoji, score_label = "#d97706", "&#128170;", "Steady"
    else:             score_color, score_emoji, score_label = "#dc2626", "&#127807;", "Needs Focus"

    bar_color = "#16a34a" if bar_pct >= 70 else "#d97706" if bar_pct >= 40 else "#dc2626"

    # Parse all six sections
    sections = _parse_sections(narrative)
    wins_h,    wins_b    = sections.get("wins",     ("This Week's Wins",          ""))
    slip_h,    slip_b    = sections.get("slipped",  ("What Slipped",              ""))
    pat_h,     pat_b     = sections.get("patterns", ("Patterns I Noticed",        ""))
    change_h,  change_b  = sections.get("change",   ("One Thing to Change",       ""))
    next_h,    next_b    = sections.get("next",     ("Next Week's Top 3",         ""))
    close_h,   close_b   = sections.get("closing",  ("Your Motivational Closing", ""))

    action_html = _action_items_html(next_b)

    # Completion bar: use a table cell trick for email-safe progress bar
    bar_empty = 100 - bar_pct
    bar_row = f"""<tr>
      <td width="{bar_pct}%" height="10" style="background:linear-gradient(90deg,{bar_color},{bar_color}bb);border-radius:5px 0 0 5px;font-size:0">&nbsp;</td>
      <td width="{bar_empty}%" height="10" style="background:#e5e7eb;border-radius:0 5px 5px 0;font-size:0">&nbsp;</td>
    </tr>""" if bar_pct > 0 and bar_pct < 100 else (
        f'<tr><td width="100%" height="10" style="background:{bar_color};border-radius:5px;font-size:0">&nbsp;</td></tr>' if bar_pct >= 100
        else '<tr><td width="100%" height="10" style="background:#e5e7eb;border-radius:5px;font-size:0">&nbsp;</td></tr>'
    )

    overdue_bg    = "#fff7ed" if overdue > 0 else "#f0fdf4"
    overdue_border= "#fed7aa" if overdue > 0 else "#bbf7d0"
    overdue_color = "#ea580c" if overdue > 0 else "#16a34a"

    closing_text = _strip_md(close_b).strip()

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Weekly Retro</title></head>
<body style="margin:0;padding:0;background:#eef2f7;font-family:Arial,Helvetica,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#eef2f7">
<tr><td align="center" style="padding:20px 12px">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.10)">

<!-- ═══ HEADER ═══ -->
<tr><td style="background:linear-gradient(135deg,#4f46e5,#7c3aed);padding:0">
  <table width="100%" cellpadding="0" cellspacing="0">
    <tr>
      <td style="padding:28px 28px 0;text-align:left">
        <div style="font-size:10px;color:#c7d2fe;text-transform:uppercase;letter-spacing:3px">Smart Daily Planner</div>
        <div style="font-size:22px;font-weight:800;color:#fff;margin-top:4px">&#128202; Weekly Retrospective</div>
        <div style="font-size:13px;color:#c7d2fe;margin-top:2px">{_html.escape(week_label)}</div>
      </td>
      <td style="padding:28px 28px 0;text-align:right;vertical-align:top">
        <div style="display:inline-block;background:rgba(255,255,255,0.12);border:1px solid rgba(255,255,255,0.25);border-radius:12px;padding:10px 18px;text-align:center">
          <div style="font-size:36px;font-weight:900;color:{score_color};line-height:1">{score}</div>
          <div style="font-size:10px;color:#a5b4fc;margin-top:2px">/100 &nbsp;{score_emoji}&nbsp; {score_label}</div>
        </div>
      </td>
    </tr>
  </table>
  <!-- wave divider -->
  <div style="height:18px;background:linear-gradient(to bottom right,#7c3aed,#4f46e5);margin-top:20px"></div>
</td></tr>

<!-- ═══ KPI STRIP ═══ -->
<tr><td style="padding:20px 20px 8px">
  <table width="100%" cellpadding="4" cellspacing="4">
    <tr>
      <td width="25%" style="text-align:center;background:#f0fdf4;border-radius:10px;padding:14px 6px;border-bottom:3px solid #16a34a">
        <div style="font-size:26px;font-weight:800;color:#16a34a">{completed}</div>
        <div style="font-size:10px;color:#6b7280;text-transform:uppercase;letter-spacing:0.5px;margin-top:2px">&#9989; Done</div>
      </td>
      <td width="25%" style="text-align:center;background:{overdue_bg};border-radius:10px;padding:14px 6px;border-bottom:3px solid {overdue_color}">
        <div style="font-size:26px;font-weight:800;color:{overdue_color}">{overdue}</div>
        <div style="font-size:10px;color:#6b7280;text-transform:uppercase;letter-spacing:0.5px;margin-top:2px">&#9888; Overdue</div>
      </td>
      <td width="25%" style="text-align:center;background:#eff6ff;border-radius:10px;padding:14px 6px;border-bottom:3px solid #2563eb">
        <div style="font-size:26px;font-weight:800;color:#2563eb">{events}</div>
        <div style="font-size:10px;color:#6b7280;text-transform:uppercase;letter-spacing:0.5px;margin-top:2px">&#128197; Events</div>
      </td>
      <td width="25%" style="text-align:center;background:#faf5ff;border-radius:10px;padding:14px 6px;border-bottom:3px solid #7c3aed">
        <div style="font-size:26px;font-weight:800;color:#7c3aed">{notes_n}</div>
        <div style="font-size:10px;color:#6b7280;text-transform:uppercase;letter-spacing:0.5px;margin-top:2px">&#128221; Notes</div>
      </td>
    </tr>
  </table>
</td></tr>

<!-- ═══ COMPLETION BAR ═══ -->
<tr><td style="padding:4px 20px 16px">
  <table width="100%" cellpadding="0" cellspacing="0">
    <tr>
      <td style="font-size:11px;font-weight:700;color:#374151">TASK COMPLETION</td>
      <td style="font-size:11px;font-weight:800;color:{bar_color};text-align:right">{rate:.0f}% &nbsp;({completed}/{total} tasks)</td>
    </tr>
    <tr><td colspan="2" height="6"></td></tr>
    <tr>
      <td colspan="2">
        <table width="100%" cellpadding="0" cellspacing="0" style="border-radius:6px;overflow:hidden">
          {bar_row}
        </table>
      </td>
    </tr>
  </table>
</td></tr>

<!-- ═══ ACTION PLAN (most prominent) ═══ -->
<tr><td style="padding:4px 20px 20px">
  <div style="background:linear-gradient(135deg,#f0f9ff,#e0f2fe);border:1px solid #bae6fd;border-radius:12px;padding:20px 20px 12px">
    <div style="font-size:11px;font-weight:800;color:#0369a1;text-transform:uppercase;letter-spacing:2px;margin-bottom:14px">&#127919; &nbsp;YOUR ACTION PLAN FOR NEXT WEEK</div>
    {action_html}
  </div>
</td></tr>

<!-- ═══ WINS + SLIPPED (2-col) ═══ -->
<tr><td style="padding:0 20px 12px">
  <table width="100%" cellpadding="0" cellspacing="0">
    {_two_col_section(
        wins_h,  wins_b,  "#16a34a", "#f0fdf4",
        slip_h,  slip_b,  "#ea580c", "#fff7ed",
    )}
  </table>
</td></tr>

<!-- ═══ PATTERNS + CHANGE (2-col) ═══ -->
<tr><td style="padding:0 20px 16px">
  <table width="100%" cellpadding="0" cellspacing="0">
    {_two_col_section(
        pat_h,    pat_b,    "#2563eb", "#eff6ff",
        change_h, change_b, "#7c3aed", "#faf5ff",
    )}
  </table>
</td></tr>

<!-- ═══ MOTIVATIONAL CLOSING ═══ -->
<tr><td style="padding:0 20px 20px">
  <div style="background:linear-gradient(135deg,#fdf4ff,#fce7f3);border-left:4px solid #db2777;border-radius:0 10px 10px 0;padding:16px 20px">
    <div style="font-size:11px;font-weight:800;color:#db2777;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:6px">&#128640; &nbsp;{_html.escape(close_h)}</div>
    <div style="font-size:14px;color:#374151;font-style:italic;line-height:1.6">{closing_text}</div>
  </div>
</td></tr>

<!-- ═══ FOOTER ═══ -->
<tr><td style="background:#f8fafc;border-top:1px solid #e2e8f0;padding:16px 20px;text-align:center">
  <span style="font-size:12px;font-weight:700;color:#6366f1">&#128197; Smart Daily Planner</span>
  <span style="font-size:11px;color:#94a3b8;margin-left:12px">&#8226; Auto-generated weekly coach report &nbsp;&#8226;&nbsp; {_html.escape(week_label)}</span>
</td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""


async def email_weekly_retro(
    user_id: str = "default_user",
    recipient_email: Optional[str] = None,
    week_offset: int = 0,
) -> Dict[str, Any]:
    """Generate the weekly retrospective and email it via Gmail SMTP.

    Args:
        user_id: Owner of the retrospective.
        recipient_email: Email to send to. Defaults to GMAIL_USER_EMAIL.
        week_offset: 0 = current week, -1 = last week. Defaults to 0.

    Returns:
        Dict with retro data plus 'sent' bool and 'message_id'.
    """
    import smtplib

    retro = await save_retro_as_note(user_id=user_id, week_offset=week_offset)

    to_email = recipient_email or settings.GMAIL_USER_EMAIL
    subject = f"📊 Weekly Retro — {retro['week_label']} | Score: {retro['productivity_score']}/100"

    if not settings.ENABLE_GMAIL_SEND:
        retro["sent"] = False
        retro["dry_run"] = True
        return retro

    app_password = os.environ.get("GMAIL_APP_PASSWORD", "") or getattr(settings, "GMAIL_APP_PASSWORD", "")
    sender_email = settings.GMAIL_USER_EMAIL

    if not app_password:
        retro["sent"] = False
        retro["error"] = "GMAIL_APP_PASSWORD not set — generate one at myaccount.google.com/apppasswords"
        return retro

    try:
        html_body = _build_html_email(retro)
        plain_body = retro["narrative"]

        msg = MIMEMultipart("alternative")
        msg["From"] = f"Smart Daily Planner <{sender_email}>"
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(plain_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender_email, app_password)
            server.sendmail(sender_email, to_email, msg.as_string())

        retro["sent"] = True
        retro["message_id"] = f"smtp-retro-{retro['week_label'].replace(' ', '-')}"
    except Exception as exc:
        retro["sent"] = False
        retro["error"] = str(exc)

    return retro


# ── ADK LlmAgent ──────────────────────────────────────────────────────────────

weekly_retro_agent = LlmAgent(
    name="weekly_retro_agent",
    model=settings.GEMINI_MODEL,
    description=(
        "Generates a personalised AI-written narrative weekly retrospective "
        "combining task stats, calendar, and notes data. Saves as note and "
        "optionally emails it."
    ),
    instruction="""You are the Weekly Retrospective Coach for Smart Daily Planner.

When the user asks for a weekly review or retrospective:
1. Call generate_weekly_retro(user_id) to create the narrative.
2. Show the week label and productivity score prominently.
3. Present the narrative — it's AI-written so just relay it.
4. Offer to save it as a note (call save_retro_as_note).
5. Offer to email it (call email_weekly_retro).

For last week: use week_offset=-1.
Trigger automatically every Friday (configured via Cloud Scheduler).
""",
    tools=[
        FunctionTool(generate_weekly_retro),
        FunctionTool(save_retro_as_note),
        FunctionTool(email_weekly_retro),
    ],
)
