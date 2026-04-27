# === agents/memory_agent.py ===
"""
Memory Agent — reads and writes user preferences stored in Firestore.

This agent maintains the user_profile document which tracks:
  - Default meeting duration and preferred working hours
  - Priority style preference (deadline-driven vs importance-driven)
  - Favourite tags for tasks/notes
  - Preferred briefing time

The agent is used by the Orchestrator to personalise all other agents.
It does NOT use LlmAgent directly — it exposes async helper functions that
are called programmatically by the orchestrator and other agents.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from tools.firestore_tools import get_user_profile, update_user_profile


async def load_user_memory(user_id: str = "default_user") -> Dict[str, Any]:
    """Load the full user preference profile from Firestore.

    Args:
        user_id: Unique identifier for the user whose profile to load.

    Returns:
        Dict containing all user preference fields. Returns safe defaults
        when the profile document does not yet exist.
    """
    return await get_user_profile(user_id)


async def save_user_preference(
    user_id: str = "default_user",
    preference_key: str = "",
    preference_value: Any = None,
) -> Dict[str, Any]:
    """Persist a single user preference field to Firestore.

    Args:
        user_id: Unique identifier for the user.
        preference_key: Name of the preference field to update (e.g.
            'default_meeting_duration_minutes', 'priority_style').
        preference_value: New value for the preference field.

    Returns:
        Updated complete user profile dict after the change is applied.

    Raises:
        ValueError: If preference_key is empty.
    """
    if not preference_key:
        raise ValueError("preference_key must not be empty.")
    return await update_user_profile(user_id, {preference_key: preference_value})


async def update_preferred_tags(
    user_id: str = "default_user",
    tags: Optional[list] = None,
) -> Dict[str, Any]:
    """Replace the user's preferred tag list in Firestore.

    Args:
        user_id: Unique identifier for the user.
        tags: New list of preferred tag strings to save.

    Returns:
        Updated user profile dict.
    """
    return await update_user_profile(user_id, {"preferred_tags": tags or []})


async def get_meeting_defaults(user_id: str = "default_user") -> Dict[str, Any]:
    """Retrieve meeting scheduling defaults from the user profile.

    Args:
        user_id: Unique identifier for the user.

    Returns:
        Dict with keys:
          - 'default_meeting_duration_minutes' (int)
          - 'preferred_meeting_start_hour' (int)
          - 'preferred_meeting_end_hour' (int)
          - 'timezone' (str)
    """
    profile = await get_user_profile(user_id)
    return {
        "default_meeting_duration_minutes": profile.get("default_meeting_duration_minutes", 30),
        "preferred_meeting_start_hour": profile.get("preferred_meeting_start_hour", 9),
        "preferred_meeting_end_hour": profile.get("preferred_meeting_end_hour", 18),
        "timezone": profile.get("timezone", "Asia/Kolkata"),
    }
