# === tools/firestore_tools.py ===
"""
Async Firestore CRUD helpers for all collections used by Smart Daily Planner.

Collections:
  tasks        — user task items
  notes        — free-form notes / memos
  user_profile — per-user preferences (singleton doc per user)
  event_log    — immutable audit log used by the undo system

All public functions have full Google-style docstrings because the ADK
framework uses them as tool descriptions when wrapping with FunctionTool.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import google.auth
from google.cloud import firestore
from google.cloud.firestore_v1.async_client import AsyncClient
from google.cloud.firestore_v1.base_query import FieldFilter

from config.settings import settings, LOCAL_TZ

# ── Firestore client (lazy singleton) ────────────────────────────────────────

_db: Optional[AsyncClient] = None


def _get_db() -> AsyncClient:
    """Return a shared async Firestore client, creating it on first call.

    Uses google.auth.default() (ADC) — no key files needed.
    """
    global _db
    if _db is None:
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        _db = firestore.AsyncClient(
            project=settings.GCP_PROJECT_ID,
            credentials=credentials,
            database=settings.FIRESTORE_DATABASE,
        )
    return _db


def _now_iso() -> str:
    """Return current UTC timestamp as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


# ── Tasks ─────────────────────────────────────────────────────────────────────


async def create_task(
    title: str,
    due_date: str,
    priority: str = "medium",
    tags: Optional[List[str]] = None,
    user_id: str = "default_user",
    notes: str = "",
) -> Dict[str, Any]:
    """Create a new task in the Firestore tasks collection.

    Args:
        title: Short description of the task.
        due_date: ISO-8601 date/datetime string (e.g. '2024-06-15T09:00:00').
        priority: One of 'low', 'medium', 'high', 'urgent'. Defaults to 'medium'.
        tags: Optional list of string labels for categorisation.
        user_id: Owner of the task. Defaults to 'default_user'.
        notes: Additional free-text notes attached to the task.

    Returns:
        Dict containing the newly created task document with its generated id.
    """
    db = _get_db()
    task_id = str(uuid.uuid4())
    doc_data: Dict[str, Any] = {
        "id": task_id,
        "title": title,
        "due_date": due_date,
        "priority": priority,
        "tags": tags or [],
        "notes": notes,
        "status": "pending",
        "user_id": user_id,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    await db.collection("tasks").document(task_id).set(doc_data)
    await _log_event(
        action="create_task",
        entity="task",
        entity_id=task_id,
        payload=doc_data,
        undo_data=None,
        user_id=user_id,
    )
    return doc_data


async def list_tasks(
    user_id: str = "default_user",
    status: Optional[str] = None,
    priority: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """List tasks from Firestore, optionally filtered by status and priority.

    Args:
        user_id: Filter tasks to this owner.
        status: Optional status filter — 'pending', 'completed', 'overdue'.
        priority: Optional priority filter — 'low', 'medium', 'high', 'urgent'.
        limit: Maximum number of tasks to return. Defaults to 50.

    Returns:
        List of task dicts ordered by due_date ascending.
    """
    db = _get_db()
    query = db.collection("tasks").where(filter=FieldFilter("user_id", "==", user_id))
    if status:
        query = query.where(filter=FieldFilter("status", "==", status))
    if priority:
        query = query.where(filter=FieldFilter("priority", "==", priority))
    query = query.order_by("due_date").limit(limit)
    docs = await query.get()
    return [doc.to_dict() for doc in docs]


async def update_task(
    task_id: str,
    updates: Dict[str, Any],
    user_id: str = "default_user",
) -> Dict[str, Any]:
    """Update an existing task document with the provided fields.

    Args:
        task_id: Firestore document ID of the task to update.
        updates: Dictionary of fields to update (e.g. {'status': 'completed'}).
        user_id: Owner of the task (used for audit logging).

    Returns:
        Updated task document dict.

    Raises:
        ValueError: If the task document does not exist.
    """
    db = _get_db()
    ref = db.collection("tasks").document(task_id)
    snap = await ref.get()
    if not snap.exists:
        raise ValueError(f"Task '{task_id}' not found.")
    original = snap.to_dict()
    updates["updated_at"] = _now_iso()
    await ref.update(updates)
    updated = {**original, **updates}
    await _log_event(
        action="update_task",
        entity="task",
        entity_id=task_id,
        payload=updates,
        undo_data=original,
        user_id=user_id,
    )
    return updated


async def delete_task(task_id: str, user_id: str = "default_user") -> Dict[str, Any]:
    """Permanently delete a task document from Firestore.

    Args:
        task_id: Firestore document ID of the task to delete.
        user_id: Owner of the task (used for audit logging).

    Returns:
        Dict with keys 'deleted' (bool) and 'task_id' confirming deletion.

    Raises:
        ValueError: If the task document does not exist.
    """
    db = _get_db()
    ref = db.collection("tasks").document(task_id)
    snap = await ref.get()
    if not snap.exists:
        raise ValueError(f"Task '{task_id}' not found.")
    original = snap.to_dict()
    await ref.delete()
    await _log_event(
        action="delete_task",
        entity="task",
        entity_id=task_id,
        payload=None,
        undo_data=original,
        user_id=user_id,
    )
    return {"deleted": True, "task_id": task_id}


async def get_overdue_tasks(user_id: str = "default_user") -> List[Dict[str, Any]]:
    """Retrieve all pending tasks whose due_date is in the past.

    Args:
        user_id: Owner of the tasks to check.

    Returns:
        List of overdue task dicts. Each dict includes the original due_date.
    """
    db = _get_db()
    now_iso = _now_iso()
    query = (
        db.collection("tasks")
        .where(filter=FieldFilter("user_id", "==", user_id))
        .where(filter=FieldFilter("status", "==", "pending"))
        .where(filter=FieldFilter("due_date", "<", now_iso))
    )
    docs = await query.get()
    return [doc.to_dict() for doc in docs]


async def escalate_overdue_tasks(user_id: str = "default_user") -> List[Dict[str, Any]]:
    """Mark all overdue pending tasks as 'overdue' and escalate priority.

    For each overdue task:
    - Sets status to 'overdue'.
    - If priority is 'low' or 'medium', bumps it to 'high'.

    Args:
        user_id: Owner of the tasks to escalate.

    Returns:
        List of updated task dicts after escalation.
    """
    overdue = await get_overdue_tasks(user_id)
    escalated = []
    priority_bump = {"low": "high", "medium": "high"}
    for task in overdue:
        new_priority = priority_bump.get(task.get("priority", "medium"), task.get("priority"))
        updated = await update_task(
            task["id"],
            {"status": "overdue", "priority": new_priority},
            user_id,
        )
        escalated.append(updated)
    return escalated


# ── Notes ─────────────────────────────────────────────────────────────────────


async def create_note(
    title: str,
    content: str,
    tags: Optional[List[str]] = None,
    user_id: str = "default_user",
) -> Dict[str, Any]:
    """Create a new note in the Firestore notes collection.

    Args:
        title: Short title or subject of the note.
        content: Full markdown or plain-text body of the note.
        tags: Optional list of string labels for categorisation.
        user_id: Owner of the note.

    Returns:
        Dict containing the newly created note document with its generated id.
    """
    db = _get_db()
    note_id = str(uuid.uuid4())
    doc_data: Dict[str, Any] = {
        "id": note_id,
        "title": title,
        "content": content,
        "tags": tags or [],
        "user_id": user_id,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    await db.collection("notes").document(note_id).set(doc_data)
    await _log_event(
        action="create_note",
        entity="note",
        entity_id=note_id,
        payload=doc_data,
        undo_data=None,
        user_id=user_id,
    )
    return doc_data


async def list_notes(
    user_id: str = "default_user",
    tag: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """List notes from Firestore, optionally filtered by tag.

    Args:
        user_id: Owner of the notes to list.
        tag: Optional tag to filter by (uses array-contains query).
        limit: Maximum number of notes to return. Defaults to 50.

    Returns:
        List of note dicts ordered by created_at descending.
    """
    db = _get_db()
    query = db.collection("notes").where(filter=FieldFilter("user_id", "==", user_id))
    if tag:
        query = query.where(filter=FieldFilter("tags", "array_contains", tag))
    query = query.order_by("created_at", direction=firestore.Query.DESCENDING).limit(limit)
    docs = await query.get()
    return [doc.to_dict() for doc in docs]


async def search_notes(
    keyword: str,
    user_id: str = "default_user",
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Search notes by keyword match in title or content (client-side filter).

    Args:
        keyword: Search term to look for in note title and content.
        user_id: Owner of the notes to search.
        limit: Maximum number of results to return. Defaults to 20.

    Returns:
        List of note dicts whose title or content contains the keyword
        (case-insensitive).
    """
    all_notes = await list_notes(user_id=user_id, limit=200)
    kw = keyword.lower()
    matches = [
        n for n in all_notes
        if kw in n.get("title", "").lower() or kw in n.get("content", "").lower()
    ]
    return matches[:limit]


async def delete_note(note_id: str, user_id: str = "default_user") -> Dict[str, Any]:
    """Permanently delete a note document from Firestore.

    Args:
        note_id: Firestore document ID of the note to delete.
        user_id: Owner of the note (used for audit logging).

    Returns:
        Dict with keys 'deleted' (bool) and 'note_id' confirming deletion.

    Raises:
        ValueError: If the note document does not exist.
    """
    db = _get_db()
    ref = db.collection("notes").document(note_id)
    snap = await ref.get()
    if not snap.exists:
        raise ValueError(f"Note '{note_id}' not found.")
    original = snap.to_dict()
    await ref.delete()
    await _log_event(
        action="delete_note",
        entity="note",
        entity_id=note_id,
        payload=None,
        undo_data=original,
        user_id=user_id,
    )
    return {"deleted": True, "note_id": note_id}


# ── User Profile ──────────────────────────────────────────────────────────────


async def get_user_profile(user_id: str = "default_user") -> Dict[str, Any]:
    """Retrieve the user preference profile from Firestore.

    Args:
        user_id: Unique identifier for the user.

    Returns:
        Dict containing user preferences. Returns default profile if no doc exists.
    """
    db = _get_db()
    ref = db.collection("user_profile").document(user_id)
    snap = await ref.get()
    if snap.exists:
        return snap.to_dict()
    # Return sensible defaults on first access
    return {
        "user_id": user_id,
        "default_meeting_duration_minutes": 30,
        "preferred_meeting_start_hour": 9,
        "preferred_meeting_end_hour": 18,
        "priority_style": "deadline",  # 'deadline' | 'importance'
        "preferred_tags": [],
        "briefing_time": "07:30",
        "timezone": settings.DEFAULT_TIMEZONE,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }


async def update_user_profile(
    user_id: str = "default_user",
    updates: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Update user preference profile in Firestore (upsert).

    Args:
        user_id: Unique identifier for the user.
        updates: Dictionary of preference fields to update.

    Returns:
        Complete updated user profile dict.
    """
    if updates is None:
        updates = {}
    db = _get_db()
    ref = db.collection("user_profile").document(user_id)
    updates["user_id"] = user_id
    updates["updated_at"] = _now_iso()
    await ref.set(updates, merge=True)
    snap = await ref.get()
    return snap.to_dict()


# ── Extra Email Accounts ──────────────────────────────────────────────────────


async def get_extra_email_accounts(user_id: str = "default_user") -> List[Dict[str, Any]]:
    """Return the list of extra email accounts saved by the user.

    Stored under user_profile/{user_id}.extra_email_accounts.
    Each entry: {email, app_password, provider, label}.
    """
    profile = await get_user_profile(user_id)
    return profile.get("extra_email_accounts", [])


async def save_extra_email_account(
    user_id: str,
    email: str,
    app_password: str,
    provider: str = "gmail",
    label: str = "",
) -> List[Dict[str, Any]]:
    """Add or update an extra email account for a user.

    If an entry with the same email already exists it is replaced.
    Returns the updated list.
    """
    accounts = await get_extra_email_accounts(user_id)
    accounts = [a for a in accounts if a.get("email", "").lower() != email.lower()]
    accounts.append({
        "email": email,
        "app_password": app_password,
        "provider": provider,
        "label": label or email,
    })
    await update_user_profile(user_id, {"extra_email_accounts": accounts})
    return accounts


async def delete_extra_email_account(user_id: str, email: str) -> List[Dict[str, Any]]:
    """Remove an extra email account by address.

    Returns the updated list.
    """
    accounts = await get_extra_email_accounts(user_id)
    accounts = [a for a in accounts if a.get("email", "").lower() != email.lower()]
    await update_user_profile(user_id, {"extra_email_accounts": accounts})
    return accounts


# ── Event Log (Undo Support) ───────────────────────────────────────────────────


# ── Linked Gmail Accounts (OAuth — no passwords stored) ───────────────────────


async def get_linked_gmail_accounts(user_id: str = "default_user") -> List[Dict[str, Any]]:
    """Return list of OAuth-linked Gmail accounts for a user (excluding primary).

    Stored in linked_gmail_accounts/{user_id}.accounts.
    Each entry: {email, name, picture, refresh_token, access_token, token_expiry,
                 linked_at, calendar_visible, email_send_enabled}.
    """
    db = _get_db()
    ref = db.collection("linked_gmail_accounts").document(user_id)
    snap = await ref.get()
    if snap.exists:
        return snap.to_dict().get("accounts", [])
    return []


async def save_linked_gmail_account(
    user_id: str,
    email: str,
    name: str = "",
    picture: str = "",
    refresh_token: str = "",
    access_token: str = "",
    token_expiry: float = 0.0,
) -> List[Dict[str, Any]]:
    """Add or update a linked Gmail account (upsert by email).

    Returns the updated accounts list.
    """
    db = _get_db()
    ref = db.collection("linked_gmail_accounts").document(user_id)
    accounts = await get_linked_gmail_accounts(user_id)
    # Preserve prefs if re-linking same account
    existing = next((a for a in accounts if a.get("email", "").lower() == email.lower()), {})
    accounts = [a for a in accounts if a.get("email", "").lower() != email.lower()]
    accounts.append({
        "email": email,
        "name": name or email,
        "picture": picture,
        "refresh_token": refresh_token,
        "access_token": access_token,
        "token_expiry": token_expiry,
        "linked_at": existing.get("linked_at") or _now_iso(),
        "calendar_visible": existing.get("calendar_visible", True),
        "email_send_enabled": existing.get("email_send_enabled", True),
    })
    await ref.set({"user_id": user_id, "accounts": accounts, "updated_at": _now_iso()}, merge=True)
    return accounts


async def delete_linked_gmail_account(user_id: str, email: str) -> List[Dict[str, Any]]:
    """Remove a linked Gmail account by email address.

    Returns the updated accounts list.
    """
    db = _get_db()
    ref = db.collection("linked_gmail_accounts").document(user_id)
    accounts = await get_linked_gmail_accounts(user_id)
    accounts = [a for a in accounts if a.get("email", "").lower() != email.lower()]
    await ref.set({"user_id": user_id, "accounts": accounts, "updated_at": _now_iso()}, merge=True)
    return accounts


async def update_linked_account_prefs(
    user_id: str,
    email: str,
    calendar_visible: Optional[bool] = None,
    email_send_enabled: Optional[bool] = None,
) -> List[Dict[str, Any]]:
    """Toggle calendar visibility or email-send permission for a linked account.

    Returns the updated accounts list.
    """
    db = _get_db()
    ref = db.collection("linked_gmail_accounts").document(user_id)
    accounts = await get_linked_gmail_accounts(user_id)
    for a in accounts:
        if a.get("email", "").lower() == email.lower():
            if calendar_visible is not None:
                a["calendar_visible"] = calendar_visible
            if email_send_enabled is not None:
                a["email_send_enabled"] = email_send_enabled
    await ref.set({"user_id": user_id, "accounts": accounts, "updated_at": _now_iso()}, merge=True)
    return accounts


async def _log_event(
    action: str,
    entity: str,
    entity_id: str,
    payload: Optional[Dict[str, Any]],
    undo_data: Optional[Dict[str, Any]],
    user_id: str = "default_user",
) -> str:
    """Write an immutable audit event to the event_log collection.

    Args:
        action: Name of the action (e.g. 'create_task', 'delete_note').
        entity: Collection name of the affected document ('task', 'note', etc.).
        entity_id: ID of the affected document.
        payload: Data that was written / updated.
        undo_data: Original data snapshot that allows reverting the change.
        user_id: Who performed the action.

    Returns:
        ID of the new event_log document.
    """
    db = _get_db()
    event_id = str(uuid.uuid4())
    event: Dict[str, Any] = {
        "id": event_id,
        "action": action,
        "entity": entity,
        "entity_id": entity_id,
        "payload": payload,
        "undo_data": undo_data,
        "user_id": user_id,
        "timestamp": _now_iso(),
    }
    await db.collection("event_log").document(event_id).set(event)
    return event_id


async def get_last_event(user_id: str = "default_user") -> Optional[Dict[str, Any]]:
    """Retrieve the most recent event from the event_log for undo purposes.

    Args:
        user_id: Filter events to this user.

    Returns:
        Most recent event dict, or None if the log is empty.
    """
    db = _get_db()
    query = (
        db.collection("event_log")
        .where(filter=FieldFilter("user_id", "==", user_id))
        .order_by("timestamp", direction=firestore.Query.DESCENDING)
        .limit(1)
    )
    docs = await query.get()
    if docs:
        return docs[0].to_dict()
    return None


async def undo_last_action(user_id: str = "default_user") -> Dict[str, Any]:
    """Reverse the most recent mutating action for the given user.

    Supported reversals:
    - create_task / create_note   → deletes the created document.
    - delete_task / delete_note   → restores the original document.
    - update_task                 → restores the previous field values.

    Args:
        user_id: Owner whose last action should be undone.

    Returns:
        Dict describing what was reversed, including 'action' and 'entity_id'.

    Raises:
        ValueError: If there is no event to undo or the action is not reversible.
    """
    event = await get_last_event(user_id)
    if not event:
        raise ValueError("No events found to undo.")

    db = _get_db()
    action = event["action"]
    entity = event["entity"]
    entity_id = event["entity_id"]
    undo_data = event.get("undo_data")

    collection = f"{entity}s"  # 'task' → 'tasks', 'note' → 'notes'

    if action in ("create_task", "create_note"):
        await db.collection(collection).document(entity_id).delete()
        result = {"undone": action, "entity_id": entity_id, "action_taken": "deleted"}

    elif action in ("delete_task", "delete_note"):
        if not undo_data:
            raise ValueError("No undo data available for delete reversal.")
        await db.collection(collection).document(entity_id).set(undo_data)
        result = {"undone": action, "entity_id": entity_id, "action_taken": "restored"}

    elif action == "update_task":
        if not undo_data:
            raise ValueError("No undo data available for update reversal.")
        await db.collection(collection).document(entity_id).set(undo_data)
        result = {"undone": action, "entity_id": entity_id, "action_taken": "reverted"}

    else:
        raise ValueError(f"Action '{action}' is not reversible.")

    # Remove the event log entry so it cannot be undone twice
    await db.collection("event_log").document(event["id"]).delete()
    return result
