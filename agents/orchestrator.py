# === agents/orchestrator.py ===
"""
Orchestrator — root ADK LlmAgent that routes requests to specialist agents.

Architecture:
  orchestrator (root)
    ├── task_agent       — task CRUD + auto-escalation
    ├── calendar_agent   — calendar + conflict-checking
    ├── notes_agent      — note CRUD + search
    ├── analytics_agent  — productivity stats + scoring
    ├── briefing_agent   — morning digest + Gmail send
    └── ingest_agent     — vision extraction from images/PDFs

Session management uses InMemorySessionService for stateless Cloud Run
deployment. For persistent sessions, swap for a Firestore-backed service.

Usage:
    from agents.orchestrator import run_orchestrator

    result = await run_orchestrator(
        user_id="user123",
        session_id="sess-abc",
        message="Add a task: Review PR by tomorrow at 5pm",
    )
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

from agents.analytics_agent import analytics_agent
from agents.briefing_agent import briefing_agent
from agents.calendar_agent import calendar_agent
from agents.ingest_agent import ingest_agent
from agents.meeting_summarizer_agent import meeting_summarizer_agent
from agents.notes_agent import notes_agent
from agents.smart_scheduler_agent import smart_scheduler_agent
from agents.task_agent import task_agent
from agents.weekly_retro_agent import weekly_retro_agent
from config.settings import settings

# ── Session service (shared across all requests) ───────────────────────────────

_session_service = InMemorySessionService()

APP_NAME = "smart_daily_planner"

# ── Root Orchestrator Agent ────────────────────────────────────────────────────

orchestrator = LlmAgent(
    name="orchestrator",
    model=settings.GEMINI_MODEL,
    description="Smart Daily Planner root orchestrator — routes requests to specialist agents.",
    instruction="""You are the Smart Daily Planner assistant — an intelligent productivity
companion that helps users manage their tasks, calendar, notes, and daily workflow.

You have access to the following specialist agents. Delegate to the most appropriate one:

  📋 task_agent               — Create, list, update, delete, and escalate tasks.
  📅 calendar_agent           — Create (conflict-checked), list, delete events; find free slots.
  📝 notes_agent              — Create, list, search, and delete notes.
  📊 analytics_agent          — Productivity scores, completion rates, weekly trends, today's summary.
  🌅 briefing_agent           — Morning digest composition and Gmail delivery.
  📄 ingest_agent             — Extract tasks/events/notes from images or PDFs.
  🤖 smart_scheduler_agent    — AI rescue plan for overdue/urgent task backlog.
  🎙️ meeting_summarizer_agent — Extract action items and decisions from meeting transcripts.
  📊 weekly_retro_agent       — AI-written personalised weekly retrospective.

Routing rules:
- "Add task / remind me / to-do / set a reminder / action item" → task_agent
- "Due today / what's due / overdue / show tasks / my tasks" → task_agent
- "Schedule / book / meeting / event / calendar / add to calendar" → calendar_agent
- "Am I free / any conflicts / free slot / available time / check my calendar" → calendar_agent
- "Note / memo / write down / capture / jot / save this" → notes_agent
- "Search notes / find note / show my notes" → notes_agent
- "How productive / stats / score / trends / how am I doing / what did I complete" → analytics_agent
- "Today's summary / how was today / tasks today / focus plan" → analytics_agent
- "Send briefing / morning digest / daily summary / daily email" → briefing_agent
- "Upload / extract / scan / image / PDF / document" → ingest_agent
- "I'm overwhelmed / rescue plan / reschedule overdue / catch up / behind" → smart_scheduler_agent
- "Meeting notes / transcript / summarise meeting / action items / decisions" → meeting_summarizer_agent
- "Weekly review / retrospective / how was my week / retro / email retro" → weekly_retro_agent
- Multi-intent requests (e.g. "add task AND schedule meeting") → delegate sequentially

Always:
1. Confirm completed actions with a concise summary (title + priority + due date for tasks).
2. For calendar events, always state whether a conflict was detected and confirm the IST time.
3. For tasks, echo back the priority level and due date in IST.
4. For analytics queries, end with one specific, numbered action the user can take right now.
5. Maintain a warm, concise, and proactive tone — like a smart colleague, not a bot.
6. If a request is ambiguous, make your best guess and mention which agent you used.

Timezone: All times are in Asia/Kolkata (IST, UTC+5:30) unless the user specifies otherwise.
""",
    sub_agents=[
        task_agent,
        calendar_agent,
        notes_agent,
        analytics_agent,
        briefing_agent,
        ingest_agent,
        smart_scheduler_agent,
        meeting_summarizer_agent,
        weekly_retro_agent,
    ],
)

# ── Runner ────────────────────────────────────────────────────────────────────

_runner = Runner(
    agent=orchestrator,
    app_name=APP_NAME,
    session_service=_session_service,
)


# ── Public API ────────────────────────────────────────────────────────────────


async def run_orchestrator(
    message: str,
    user_id: str = "default_user",
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the orchestrator with a user message and return the agent's response.

    Creates a new session if session_id is not provided. The session persists
    in memory for the lifetime of the process, allowing multi-turn conversation.

    Args:
        message: Natural language user message to process.
        user_id: Unique identifier for the user. Used for data isolation.
        session_id: Optional session ID for conversation continuity.
            Auto-generated if not supplied.

    Returns:
        Dict with:
          - 'response' (str): Agent's text response.
          - 'session_id' (str): Session ID for follow-up messages.
          - 'user_id' (str): Echo of the user_id.
          - 'agent_name' (str): Name of the agent that handled the request.
    """
    if not session_id:
        session_id = f"sess-{uuid.uuid4().hex[:12]}"

    # Ensure session exists
    existing = await _session_service.get_session(
        app_name=APP_NAME, user_id=user_id, session_id=session_id
    )
    if existing is None:
        await _session_service.create_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )

    content = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=message)],
    )

    final_response = ""
    responding_agent = "orchestrator"
    last_error: Optional[Exception] = None

    try:
        async for event in _runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=content,
        ):
            if event.is_final_response():
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        # Skip thought/reasoning parts — only take text output
                        if getattr(part, "thought", False):
                            continue
                        if part.text:
                            final_response = part.text
                            break
                if hasattr(event, "author") and event.author:
                    responding_agent = event.author
    except Exception as exc:
        last_error = exc

    if last_error is not None:
        raise last_error

    if not final_response:
        raise RuntimeError(
            "AI model returned no response. This is usually caused by free-tier "
            "quota exhaustion (limit: 20 req/day for gemini-2.5-flash). "
            "Enable billing on your GCP project at console.cloud.google.com/billing "
            "to unlock higher quotas, then retry."
        )

    return {
        "response": final_response,
        "session_id": session_id,
        "user_id": user_id,
        "agent_name": responding_agent,
    }


async def run_orchestrator_voice(
    audio_bytes: bytes,
    mime_type: str = "audio/mp3",
    user_id: str = "default_user",
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Process a voice (audio) query through the orchestrator.

    Sends raw audio bytes to Gemini as a multimodal input. Gemini transcribes
    and processes the audio, then routes to the appropriate sub-agent.

    Args:
        audio_bytes: Raw audio file bytes (MP3, WAV, OGG, etc.).
        mime_type: MIME type of the audio data. Defaults to 'audio/mp3'.
        user_id: Unique identifier for the user.
        session_id: Optional session ID for conversation continuity.

    Returns:
        Same dict schema as run_orchestrator, plus 'audio_processed' True.
    """
    if not session_id:
        session_id = f"sess-{uuid.uuid4().hex[:12]}"

    existing = await _session_service.get_session(
        app_name=APP_NAME, user_id=user_id, session_id=session_id
    )
    if existing is None:
        await _session_service.create_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )

    content = genai_types.Content(
        role="user",
        parts=[
            genai_types.Part(
                inline_data=genai_types.Blob(
                    mime_type=mime_type,
                    data=audio_bytes,
                )
            ),
            genai_types.Part(
                text="Please process this voice message and help me accordingly."
            ),
        ],
    )

    final_response = ""
    responding_agent = "orchestrator"
    last_error: Optional[Exception] = None

    try:
        async for event in _runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=content,
        ):
            if event.is_final_response():
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        if getattr(part, "thought", False):
                            continue
                        if part.text:
                            final_response = part.text
                            break
                if hasattr(event, "author") and event.author:
                    responding_agent = event.author
    except Exception as exc:
        last_error = exc

    if last_error is not None:
        raise last_error

    if not final_response:
        raise RuntimeError(
            "AI model returned no response (voice query). "
            "This may be due to quota exhaustion — enable billing on GCP to unlock higher quotas."
        )

    return {
        "response": final_response,
        "session_id": session_id,
        "user_id": user_id,
        "agent_name": responding_agent,
        "audio_processed": True,
    }
