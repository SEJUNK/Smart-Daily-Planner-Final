# === agents/ingest_agent.py ===
"""
Ingest Agent — processes images and PDFs using Gemini Vision.

Accepts base64-encoded image or PDF data, sends it to Gemini multimodal
model with a structured extraction prompt, then routes the extracted items
to the appropriate agent tools:
  - Tasks → Firestore tasks collection
  - Calendar events → Google Calendar
  - Notes → Firestore notes collection

All extraction and routing is handled synchronously via direct SDK calls
rather than sub-agent delegation, for deterministic structured output.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from google import genai
from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from google.genai import types as genai_types

from config.settings import settings
from tools.calendar_tools import create_calendar_event
from tools.firestore_tools import create_note, create_task

# ── Gemini client ─────────────────────────────────────────────────────────────

_genai_client: Optional[genai.Client] = None


def _get_genai_client() -> genai.Client:
    """Return a shared google.genai Client configured with the API key."""
    global _genai_client
    if _genai_client is None:
        _genai_client = genai.Client(api_key=settings.GOOGLE_API_KEY)
    return _genai_client


EXTRACTION_PROMPT = (
    "Carefully analyse this document/image and extract ALL of the following:\n\n"
    "1. TASKS: Items that need to be done. For each task extract:\n"
    "   - title (string, required)\n"
    "   - due_date (ISO-8601 string, or null)\n"
    "   - priority: 'low', 'medium', 'high', or 'urgent' (infer from language)\n\n"
    "2. CALENDAR EVENTS: Meetings, appointments, or scheduled activities. For each:\n"
    "   - summary (string, required)\n"
    "   - start_datetime (ISO-8601 string, required)\n"
    "   - duration_minutes (integer, default 60)\n"
    "   - location (string or null)\n\n"
    "3. NOTES: Informational content, ideas, or reference material. For each:\n"
    "   - title (string, required)\n"
    "   - content (string, required)\n"
    "   - tags (list of strings)\n\n"
    "Return ONLY valid JSON in this exact structure, no prose:\n"
    '{"tasks": [...], "events": [...], "notes": [...]}'
)


def _clean_json(text: str) -> str:
    """Strip markdown code fences and extra whitespace from a JSON string.

    Args:
        text: Raw model output that may contain ```json ... ``` fences.

    Returns:
        Clean JSON string ready for json.loads().
    """
    # Remove ```json ... ``` or ``` ... ``` fences
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```\s*$", "", text.strip())
    return text.strip()


# ── Core extraction ───────────────────────────────────────────────────────────


async def extract_from_image(
    base64_data: str,
    mime_type: str = "image/jpeg",
    user_id: str = "default_user",
) -> Dict[str, Any]:
    """Extract tasks, events, and notes from a base64-encoded image using Gemini Vision.

    The image is sent to the Gemini multimodal model with a structured JSON
    extraction prompt. Extracted items are persisted to Firestore and
    Google Calendar automatically.

    Args:
        base64_data: Base64-encoded image data (without data URI prefix).
        mime_type: MIME type of the image (e.g. 'image/jpeg', 'image/png',
            'image/webp'). Defaults to 'image/jpeg'.
        user_id: Owner for all created items. Defaults to 'default_user'.

    Returns:
        Dict with:
          - 'extracted' (dict): Raw extracted JSON with 'tasks', 'events', 'notes'.
          - 'created' (dict): Counts of successfully persisted items.
          - 'errors' (list): Any errors encountered during item creation.
    """
    client = _get_genai_client()

    import base64 as b64_lib
    raw_bytes = b64_lib.b64decode(base64_data)

    contents = [
        genai_types.Content(
            role="user",
            parts=[
                genai_types.Part(
                    inline_data=genai_types.Blob(
                        mime_type=mime_type,
                        data=raw_bytes,
                    )
                ),
                genai_types.Part(text=EXTRACTION_PROMPT),
            ],
        )
    ]

    response = client.models.generate_content(
        model=settings.GEMINI_MODEL,
        contents=contents,
    )

    raw_text = response.text or "{}"
    clean = _clean_json(raw_text)

    try:
        extracted = json.loads(clean)
    except json.JSONDecodeError as exc:
        return {
            "extracted": {},
            "created": {"tasks": 0, "events": 0, "notes": 0},
            "errors": [f"JSON parse error: {exc}. Raw: {raw_text[:300]}"],
        }

    return await _persist_extracted(extracted, user_id)


async def extract_from_pdf(
    base64_data: str,
    user_id: str = "default_user",
) -> Dict[str, Any]:
    """Extract tasks, events, and notes from a base64-encoded PDF using Gemini.

    Args:
        base64_data: Base64-encoded PDF file data.
        user_id: Owner for all created items. Defaults to 'default_user'.

    Returns:
        Dict with 'extracted', 'created' counts, and 'errors' list.
        See extract_from_image for full schema.
    """
    return await extract_from_image(
        base64_data=base64_data,
        mime_type="application/pdf",
        user_id=user_id,
    )


async def _persist_extracted(
    extracted: Dict[str, Any],
    user_id: str,
) -> Dict[str, Any]:
    """Persist all extracted items to their respective stores.

    Args:
        extracted: Dict with optional 'tasks', 'events', 'notes' lists.
        user_id: Owner for all created items.

    Returns:
        Dict with 'extracted' (original data), 'created' counts, and 'errors'.
    """
    errors: List[str] = []
    created = {"tasks": 0, "events": 0, "notes": 0}

    # Persist tasks
    for task_data in extracted.get("tasks", []):
        try:
            await create_task(
                title=task_data.get("title", "Extracted Task"),
                due_date=task_data.get("due_date") or _default_due_date(),
                priority=task_data.get("priority", "medium"),
                user_id=user_id,
                notes="Auto-extracted from document",
            )
            created["tasks"] += 1
        except Exception as exc:
            errors.append(f"Task '{task_data.get('title', '?')}': {exc}")

    # Persist calendar events
    for event_data in extracted.get("events", []):
        try:
            await create_calendar_event(
                summary=event_data.get("summary", "Extracted Event"),
                start_datetime=event_data.get("start_datetime", _default_due_date()),
                duration_minutes=int(event_data.get("duration_minutes", 60)),
                location=event_data.get("location", ""),
                description="Auto-extracted from document",
            )
            created["events"] += 1
        except Exception as exc:
            errors.append(f"Event '{event_data.get('summary', '?')}': {exc}")

    # Persist notes
    for note_data in extracted.get("notes", []):
        try:
            await create_note(
                title=note_data.get("title", "Extracted Note"),
                content=note_data.get("content", ""),
                tags=note_data.get("tags", []) + ["auto-extracted"],
                user_id=user_id,
            )
            created["notes"] += 1
        except Exception as exc:
            errors.append(f"Note '{note_data.get('title', '?')}': {exc}")

    return {
        "extracted": extracted,
        "created": created,
        "errors": errors,
    }


def _default_due_date() -> str:
    """Return tomorrow's date as ISO-8601 string for items with no due date."""
    from datetime import datetime, timedelta
    return (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT09:00:00")


async def ingest_base64(
    base64_data: str,
    mime_type: str = "image/jpeg",
    user_id: str = "default_user",
) -> Dict[str, Any]:
    """Ingest a base64-encoded document and extract structured data.

    Accepts images (JPEG, PNG, WebP) or PDFs, runs them through Gemini Vision,
    and persists all extracted tasks, events, and notes.

    Args:
        base64_data: Base64-encoded file content (no data URI prefix).
        mime_type: MIME type — 'image/jpeg', 'image/png', 'image/webp',
            or 'application/pdf'.
        user_id: Owner for all created items.

    Returns:
        Dict with 'extracted' (raw JSON), 'created' (counts per type),
        and 'errors' (list of any failures).
    """
    if mime_type == "application/pdf":
        return await extract_from_pdf(base64_data=base64_data, user_id=user_id)
    return await extract_from_image(
        base64_data=base64_data, mime_type=mime_type, user_id=user_id
    )


# ── ADK LlmAgent ──────────────────────────────────────────────────────────────

ingest_agent = LlmAgent(
    name="ingest_agent",
    model=settings.GEMINI_MODEL,
    description=(
        "Processes base64-encoded images and PDFs using Gemini Vision to extract "
        "tasks, calendar events, and notes, then persists them automatically."
    ),
    instruction="""You are the Document Ingestion specialist for Smart Daily Planner.

Your responsibilities:
1. Accept base64-encoded images or PDFs from the user.
2. Use Gemini Vision to extract structured tasks, calendar events, and notes.
3. Automatically persist all extracted items to their respective stores.
4. Report back a summary of what was extracted and created.

Guidelines:
- Call ingest_base64 with the provided data and appropriate mime_type.
- Always report the count of created items (tasks, events, notes).
- If errors occurred, list them clearly so the user can fix them manually.
- For ambiguous dates, default to tomorrow at 9am IST.
""",
    tools=[
        FunctionTool(ingest_base64),
        FunctionTool(extract_from_image),
        FunctionTool(extract_from_pdf),
    ],
)
