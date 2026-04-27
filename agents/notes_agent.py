# === agents/notes_agent.py ===
"""
Notes Agent — manages free-form notes using ADK LlmAgent.

Tools exposed:
  - create_note
  - list_notes
  - search_notes
  - delete_note
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from config.settings import settings
from tools.firestore_tools import (
    create_note as _create_note,
    delete_note as _delete_note,
    list_notes as _list_notes,
    search_notes as _search_notes,
)


# ── Tool wrappers ─────────────────────────────────────────────────────────────


async def create_note(
    title: str,
    content: str,
    tags: Optional[List[str]] = None,
    user_id: str = "default_user",
) -> Dict[str, Any]:
    """Create and save a new note to Firestore.

    Args:
        title: Short title or subject line for the note.
        content: Full body text of the note. Supports markdown formatting.
        tags: Optional list of label strings for categorisation
            (e.g. ['meeting', 'project-alpha']).
        user_id: Owner of the note. Defaults to 'default_user'.

    Returns:
        Dict with the created note document including its auto-generated 'id'
        and creation timestamps.
    """
    return await _create_note(
        title=title,
        content=content,
        tags=tags,
        user_id=user_id,
    )


async def list_notes(
    user_id: str = "default_user",
    tag: Optional[str] = None,
    limit: int = 20,
) -> Dict[str, Any]:
    """List notes from Firestore, optionally filtered by a tag.

    Args:
        user_id: Owner of the notes to list.
        tag: Optional tag to filter by. Returns only notes that include
            this tag in their tags list.
        limit: Maximum number of notes to return. Defaults to 20.

    Returns:
        Dict with 'notes' (list of note dicts) and 'count' (int).
    """
    notes = await _list_notes(user_id=user_id, tag=tag, limit=limit)
    return {"notes": notes, "count": len(notes)}


async def search_notes(
    keyword: str,
    user_id: str = "default_user",
    limit: int = 10,
) -> Dict[str, Any]:
    """Search notes by keyword match in title or content.

    Performs a case-insensitive substring search across note titles and
    body content. Returns the top matches ordered by creation date.

    Args:
        keyword: Search term to find in note title or content.
        user_id: Owner of the notes to search.
        limit: Maximum number of results to return. Defaults to 10.

    Returns:
        Dict with 'results' (list of matching note dicts) and 'count' (int).
    """
    results = await _search_notes(keyword=keyword, user_id=user_id, limit=limit)
    return {"results": results, "count": len(results)}


async def delete_note(
    note_id: str,
    user_id: str = "default_user",
) -> Dict[str, Any]:
    """Permanently delete a note from Firestore.

    Args:
        note_id: Firestore document ID of the note to delete.
        user_id: Owner of the note (for audit logging).

    Returns:
        Dict with 'deleted' True and 'note_id' confirming the deletion.
    """
    return await _delete_note(note_id=note_id, user_id=user_id)


# ── ADK LlmAgent ──────────────────────────────────────────────────────────────

notes_agent = LlmAgent(
    name="notes_agent",
    model=settings.GEMINI_MODEL,
    description=(
        "Manages user notes: create, list, search, and delete notes stored "
        "in Firestore. Use for memos, meeting notes, ideas, and reference material."
    ),
    instruction="""You are the Notes Manager for the Smart Daily Planner.

Your responsibilities:
1. Create notes when the user wants to capture ideas, memos, or meeting notes.
2. List notes, optionally filtered by tag.
3. Search notes by keyword when the user asks to find something they wrote.
4. Delete notes when explicitly requested (confirm the note title first).

Guidelines:
- When creating notes, suggest relevant tags based on content (e.g. 'meeting',
  'idea', 'reference', 'personal', 'work').
- For search, if no results are found, ask the user to try a different keyword.
- Keep note content faithful to what the user dictates — don't summarise
  or paraphrase unless asked.
- When listing, show title, tags, and the first 100 characters of content.
""",
    tools=[
        FunctionTool(create_note),
        FunctionTool(list_notes),
        FunctionTool(search_notes),
        FunctionTool(delete_note),
    ],
)
