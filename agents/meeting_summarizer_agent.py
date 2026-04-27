# === agents/meeting_summarizer_agent.py ===
"""
Meeting Summarizer Agent — transforms raw meeting transcripts into structured output.

UNIQUE FEATURE: Paste any meeting transcript (Teams, Zoom, Google Meet, manual notes)
and Gemini will extract:
  - Action items (auto-created as tasks in Firestore)
  - Decisions made (saved as a structured note)
  - Key discussion points (summary note)
  - Follow-up calendar event (if a next meeting is mentioned)
  - Meeting sentiment score (productive / neutral / unfocused)

This turns your messy meeting notes into a full productivity workflow automatically.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from google import genai
from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from google.genai import types as genai_types

from config.settings import settings, LOCAL_TZ
from tools.calendar_tools import create_calendar_event
from tools.firestore_tools import create_note, create_task

_client: Optional[genai.Client] = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.GOOGLE_API_KEY)
    return _client


MEETING_EXTRACTION_PROMPT = """You are an expert meeting analyst AI.

Analyse the following meeting transcript or notes and extract structured data.

IMPORTANT RULES FOR ACTION ITEMS:
- If explicit "Person X will do Y" assignments are stated, extract them directly.
- If the notes contain DECISIONS (e.g. "Finance team will close books in first week of July"),
  ALWAYS infer the required action items that must happen for that decision to be executed.
  Example: "Quarterly Financial Report to be published on July 7" → action item: "Publish Quarterly Financial Report to all stakeholders" (due: July 7, owner: Finance Team).
- A meeting with decisions ALWAYS has at least 2-3 action items. Never return an empty action_items array when decisions exist.
- For each decision/planned activity, derive WHO needs to do WHAT by WHEN.

Return ONLY valid JSON with this exact structure:
{
  "meeting_title": "inferred title if not given",
  "meeting_date": "ISO-8601 date string or null",
  "duration_minutes": estimated integer or null,
  "attendees": ["name or email list"],
  "sentiment": "productive | neutral | unfocused",
  "sentiment_score": 0-100 integer (100=extremely productive),
  "summary": "2-3 sentence executive summary",
  "key_decisions": ["Decision 1", "Decision 2"],
  "action_items": [
    {
      "title": "action item description (inferred from decisions if not explicitly stated)",
      "owner": "person/team name or null",
      "due_date": "ISO-8601 date or null",
      "priority": "low | medium | high | urgent"
    }
  ],
  "discussion_points": ["Point 1", "Point 2"],
  "next_meeting": {
    "suggested": true or false,
    "topic": "string or null",
    "start_datetime": "ISO-8601 or null",
    "duration_minutes": integer or null
  },
  "risks_flagged": ["Risk 1"]
}

TRANSCRIPT / NOTES:
{transcript}
"""


async def summarize_meeting(
    transcript: str,
    meeting_title: Optional[str] = None,
    user_id: str = "default_user",
    auto_create_tasks: bool = True,
    auto_create_note: bool = True,
    auto_schedule_followup: bool = False,
) -> Dict[str, Any]:
    """Analyse a meeting transcript and extract structured action items, decisions, and notes.

    Sends the transcript to Gemini for deep analysis, then optionally auto-creates
    tasks for each action item, saves a decisions note, and schedules a follow-up
    meeting if the transcript mentions one.

    Args:
        transcript: Raw meeting transcript text, meeting notes, or any free-form
            notes from a meeting session.
        meeting_title: Optional override title. Gemini will infer one if not provided.
        user_id: Owner for all created tasks and notes.
        auto_create_tasks: When True, automatically creates a Firestore task for
            each extracted action item. Defaults to True.
        auto_create_note: When True, saves a formatted meeting summary note to
            Firestore. Defaults to True.
        auto_schedule_followup: When True, creates a Google Calendar event if
            the transcript mentions a next meeting. Defaults to False.

    Returns:
        Dict with:
          - 'analysis' (dict): Full structured Gemini extraction result.
          - 'created_tasks' (list): Task IDs created from action items.
          - 'note_id' (str | None): ID of the created meeting note.
          - 'followup_event' (dict | None): Created follow-up calendar event.
          - 'sentiment' (str): Meeting sentiment label.
          - 'sentiment_score' (int): 0–100 productivity score.
    """
    client = _get_client()
    prompt = MEETING_EXTRACTION_PROMPT.format(transcript=transcript)

    try:
        _contents = [genai_types.Content(role="user", parts=[genai_types.Part(text=prompt)])]
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=settings.GEMINI_MODEL,
            contents=_contents,
        )
        raw = response.text or "{}"
        raw = re.sub(r"```(?:json)?\s*", "", raw)
        raw = re.sub(r"```\s*$", "", raw.strip()).strip()
        analysis = json.loads(raw)
    except Exception as exc:
        return {
            "analysis": {},
            "created_tasks": [],
            "note_id": None,
            "followup_event": None,
            "sentiment": "unknown",
            "sentiment_score": 0,
            "error": str(exc),
        }

    title = meeting_title or analysis.get("meeting_title", "Meeting Summary")
    created_task_ids: List[str] = []
    note_id: Optional[str] = None
    followup_event: Optional[Dict] = None

    # Create tasks for action items
    if auto_create_tasks:
        for item in analysis.get("action_items", []):
            try:
                task = await create_task(
                    title=item.get("title", "Action item"),
                    due_date=item.get("due_date") or _next_business_day(),
                    priority=item.get("priority", "medium"),
                    tags=["meeting", "action-item"],
                    user_id=user_id,
                    notes=f"Owner: {item.get('owner', 'Unassigned')} | From: {title}",
                )
                created_task_ids.append(task["id"])
            except Exception:
                pass

    # Save meeting summary note
    if auto_create_note:
        decisions = analysis.get("key_decisions", [])
        points = analysis.get("discussion_points", [])
        risks = analysis.get("risks_flagged", [])
        note_content = (
            f"## Summary\n{analysis.get('summary', '')}\n\n"
            f"## Key Decisions\n" + "\n".join(f"- {d}" for d in decisions) + "\n\n"
            f"## Discussion Points\n" + "\n".join(f"- {p}" for p in points) + "\n\n"
            f"## Risks Flagged\n" + "\n".join(f"⚠️ {r}" for r in risks) + "\n\n"
            f"## Action Items Created\n{len(created_task_ids)} tasks auto-created.\n\n"
            f"**Sentiment:** {analysis.get('sentiment', 'N/A')} "
            f"(score: {analysis.get('sentiment_score', 0)}/100)\n"
            f"**Attendees:** {', '.join(analysis.get('attendees', []))}"
        )
        try:
            note = await create_note(
                title=f"📋 {title}",
                content=note_content,
                tags=["meeting", "auto-summary"],
                user_id=user_id,
            )
            note_id = note["id"]
        except Exception:
            pass

    # Schedule follow-up if requested and suggested
    next_mtg = analysis.get("next_meeting", {})
    if auto_schedule_followup and next_mtg.get("suggested") and next_mtg.get("start_datetime"):
        try:
            followup_event = await create_calendar_event(
                summary=f"Follow-up: {next_mtg.get('topic', title)}",
                start_datetime=next_mtg["start_datetime"],
                duration_minutes=int(next_mtg.get("duration_minutes", 60)),
                description=f"Follow-up meeting scheduled from: {title}",
            )
        except Exception:
            pass

    return {
        "analysis": analysis,
        "created_tasks": created_task_ids,
        "note_id": note_id,
        "followup_event": followup_event,
        "sentiment": analysis.get("sentiment", "unknown"),
        "sentiment_score": analysis.get("sentiment_score", 0),
        "action_items_count": len(analysis.get("action_items", [])),
        "decisions_count": len(analysis.get("key_decisions", [])),
    }


def _next_business_day() -> str:
    """Return tomorrow's date as ISO-8601 string."""
    return (datetime.now(LOCAL_TZ) + __import__("datetime").timedelta(days=1)).strftime(
        "%Y-%m-%dT09:00:00"
    )


async def get_meeting_sentiment(transcript: str) -> Dict[str, Any]:
    """Quick sentiment-only analysis of a meeting transcript.

    Faster than full summarize_meeting — only returns the meeting's
    productivity sentiment without creating any tasks or notes.

    Args:
        transcript: Raw meeting transcript or notes.

    Returns:
        Dict with 'sentiment' (str), 'sentiment_score' (int 0–100),
        and 'one_line_assessment' (str).
    """
    client = _get_client()
    prompt = (
        "Rate the productivity of this meeting transcript on a scale of 0-100. "
        "Return JSON only: {\"sentiment\": \"productive|neutral|unfocused\", "
        "\"sentiment_score\": 0-100, \"one_line_assessment\": \"string\"}\n\n"
        f"TRANSCRIPT:\n{transcript[:2000]}"
    )
    try:
        _contents = [genai_types.Content(role="user", parts=[genai_types.Part(text=prompt)])]
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=settings.GEMINI_MODEL,
            contents=_contents,
        )
        raw = re.sub(r"```(?:json)?\s*", "", response.text or "{}").strip()
        return json.loads(raw)
    except Exception as exc:
        return {"sentiment": "unknown", "sentiment_score": 0, "one_line_assessment": str(exc)}


# ── ADK LlmAgent ──────────────────────────────────────────────────────────────

meeting_summarizer_agent = LlmAgent(
    name="meeting_summarizer_agent",
    model=settings.GEMINI_MODEL,
    description=(
        "Transforms meeting transcripts into structured output: action items "
        "(auto-created as tasks), decisions note, sentiment score, and optional "
        "follow-up event scheduling."
    ),
    instruction="""You are the Meeting Intelligence specialist for Smart Daily Planner.

When the user shares a meeting transcript or notes:
1. Call summarize_meeting(transcript, user_id) with auto_create_tasks=True.
2. Report back:
   - Meeting sentiment and score (e.g. "Productive — 78/100")
   - Number of action items created as tasks
   - Key decisions made
   - Whether a follow-up was suggested
3. Ask if they want to schedule the follow-up meeting (call with auto_schedule_followup=True).

For quick sentiment check: call get_meeting_sentiment(transcript).

Celebrate productive meetings! For unfocused ones, gently note what made it unproductive.
""",
    tools=[
        FunctionTool(summarize_meeting),
        FunctionTool(get_meeting_sentiment),
    ],
)
