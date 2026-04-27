# === api/demo_data.py ===
"""
Demo mode mock data provider.

When DEMO_MODE=true the API returns realistic pre-seeded data instead of
calling Firestore, Google Calendar, or Gemini. This lets evaluators and
developers run the full UI with ZERO external dependencies.

All mock AI responses simulate what Gemini would actually return so the
UI looks and feels completely real.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List

from config.settings import LOCAL_TZ

_now = datetime.now(LOCAL_TZ)
_uid = lambda: str(uuid.uuid4())


def _iso(dt): return dt.isoformat()
def _days(n): return _iso(_now + timedelta(days=n))
def _hrs(n): return _iso(_now + timedelta(hours=n))


# ── Mock Tasks ────────────────────────────────────────────────────────────────

MOCK_TASKS: List[Dict[str, Any]] = [
    {"id": _uid(), "title": "Review Q3 financial report", "due_date": _days(1), "priority": "urgent",
     "status": "pending", "tags": ["finance", "q3", "review"], "notes": "Focus on revenue section", "user_id": "default_user", "created_at": _days(-2), "updated_at": _days(-2)},
    {"id": _uid(), "title": "Fix onboarding bug in step 3", "due_date": _days(0), "priority": "high",
     "status": "overdue", "tags": ["tech", "bug", "onboarding"], "notes": "40% drop-off rate", "user_id": "default_user", "created_at": _days(-5), "updated_at": _days(-5)},
    {"id": _uid(), "title": "Prepare sprint planning slides", "due_date": _days(2), "priority": "high",
     "status": "pending", "tags": ["work", "meeting"], "notes": "", "user_id": "default_user", "created_at": _days(-1), "updated_at": _days(-1)},
    {"id": _uid(), "title": "Write product blog post", "due_date": _days(5), "priority": "medium",
     "status": "pending", "tags": ["content", "marketing"], "notes": "", "user_id": "default_user", "created_at": _days(-3), "updated_at": _days(-3)},
    {"id": _uid(), "title": "Update team documentation", "due_date": _days(7), "priority": "medium",
     "status": "pending", "tags": ["docs", "work"], "notes": "", "user_id": "default_user", "created_at": _days(-1), "updated_at": _days(-1)},
    {"id": _uid(), "title": "Sync with design team", "due_date": _days(1), "priority": "medium",
     "status": "completed", "tags": ["meeting", "design"], "notes": "", "user_id": "default_user", "created_at": _days(-4), "updated_at": _days(-1)},
    {"id": _uid(), "title": "Review pull requests", "due_date": _days(-1), "priority": "high",
     "status": "completed", "tags": ["code-review", "tech"], "notes": "", "user_id": "default_user", "created_at": _days(-3), "updated_at": _days(-1)},
    {"id": _uid(), "title": "Set up A/B test framework", "due_date": _days(10), "priority": "low",
     "status": "pending", "tags": ["experiment", "growth"], "notes": "Use feature flags", "user_id": "default_user", "created_at": _days(-2), "updated_at": _days(-2)},
]

# ── Mock Events ───────────────────────────────────────────────────────────────

MOCK_EVENTS: List[Dict[str, Any]] = [
    {"id": "evt_001", "summary": "Team Standup", "start": _hrs(9), "end": _hrs(9.5), "description": "Daily sync", "location": "Google Meet"},
    {"id": "evt_002", "summary": "Q3 Planning Session", "start": _hrs(11), "end": _hrs(13), "description": "Quarterly planning", "location": "Conference Room A"},
    {"id": "evt_003", "summary": "1:1 with Manager", "start": _days(1), "end": _iso(_now + timedelta(days=1, hours=1)), "description": "", "location": "Zoom"},
    {"id": "evt_004", "summary": "Sprint Review", "start": _days(3), "end": _iso(_now + timedelta(days=3, hours=2)), "description": "Demo sprint deliverables", "location": ""},
    {"id": "evt_005", "summary": "Product Roadmap Review", "start": _days(5), "end": _iso(_now + timedelta(days=5, hours=1.5)), "description": "H2 roadmap alignment", "location": "Google Meet"},
]

# ── Mock Notes ────────────────────────────────────────────────────────────────

MOCK_NOTES: List[Dict[str, Any]] = [
    {"id": _uid(), "title": "Q3 Planning Meeting Notes", "content": "## Decisions\n- Launch date: September 15\n- Budget approved: $50K\n- Team: 3 engineers + 1 designer\n\n## Action Items\n- Alice: revenue breakdown by Friday\n- Bob: fix onboarding bug by Wednesday", "tags": ["meeting", "q3", "planning"], "user_id": "default_user", "created_at": _days(-1), "updated_at": _days(-1)},
    {"id": _uid(), "title": "Product Ideas — Dark Mode", "content": "User research shows 67% of users prefer dark mode.\n\n**Implementation approach:**\n1. CSS variables for theme switching\n2. System preference detection\n3. Manual toggle in settings\n\nEstimated: 2 sprints", "tags": ["idea", "product", "design"], "user_id": "default_user", "created_at": _days(-3), "updated_at": _days(-3)},
    {"id": _uid(), "title": "Tech Debt Backlog", "content": "## High Priority\n- Migrate auth middleware (legal compliance)\n- Upgrade Postgres to 15.x\n\n## Medium Priority\n- Refactor payment service\n- Add distributed tracing", "tags": ["tech", "engineering", "backlog"], "user_id": "default_user", "created_at": _days(-5), "updated_at": _days(-5)},
    {"id": _uid(), "title": "Weekly Retrospective — Jun 10", "content": "## 🏆 Wins\nYou completed 8/10 tasks this week — your best rate in a month!\n\n## 😬 What Slipped\nThe onboarding bug slipped due to unexpected scope.\n\n## 💡 Next Week\nBlock 2 hours each morning for deep work before checking Slack.", "tags": ["retrospective", "weekly-review", "auto-generated"], "user_id": "default_user", "created_at": _days(-2), "updated_at": _days(-2)},
]

# ── Mock Analytics ────────────────────────────────────────────────────────────

MOCK_ANALYTICS: Dict[str, Any] = {
    "productivity_score": {"score": 72, "label": "Good", "completion_rate_pct": 78.0, "overdue_count": 2, "high_priority_completed": 3, "period_days": 7},
    "completion_rate": {"total_tasks": 9, "completed_tasks": 7, "overdue_tasks": 2, "completion_rate_pct": 77.8, "period_days": 7},
    "weekly_trends": [
        {"week_start": _days(-28)[:10], "total": 10, "completed": 6, "overdue": 3, "completion_rate_pct": 60.0},
        {"week_start": _days(-21)[:10], "total": 12, "completed": 8, "overdue": 2, "completion_rate_pct": 66.7},
        {"week_start": _days(-14)[:10], "total": 11, "completed": 8, "overdue": 1, "completion_rate_pct": 72.7},
        {"week_start": _days(-7)[:10], "total": 9, "completed": 7, "overdue": 2, "completion_rate_pct": 77.8},
    ],
    "period_days": 7,
}

# ── Mock Orchestrator responses ────────────────────────────────────────────────

MOCK_QUERY_RESPONSES: Dict[str, Dict[str, Any]] = {
    "default": {
        "response": "I've processed your request. How can I help you further?",
        "agent_name": "orchestrator",
    },
    "task": {
        "response": "✅ I've created your task with the specified priority and due date. You'll find it in your task list. Would you like me to also block time for it in your calendar?",
        "agent_name": "task_agent",
    },
    "event": {
        "response": "📅 I've checked your calendar — no conflicts found! Your meeting has been scheduled. I've added it to Google Calendar with a reminder 15 minutes before.",
        "agent_name": "calendar_agent",
    },
    "note": {
        "response": "📝 Note saved! I've tagged it with relevant labels. You can find it in your Notes section and search for it anytime.",
        "agent_name": "notes_agent",
    },
    "analytics": {
        "response": "📊 Your productivity score this week is **72/100 (Good)**.\n\n**Highlights:**\n- ✅ 7/9 tasks completed (77.8% rate)\n- ⚠️ 2 tasks overdue (onboarding bug & Q3 report)\n- 🏆 3 high-priority tasks finished\n\n**Trend:** Improving — up from 60% four weeks ago!\n\n**Tip:** Block 2 hours of deep work each morning before checking Slack.",
        "agent_name": "analytics_agent",
    },
    "overdue": {
        "response": "⚠️ You have **2 overdue tasks**:\n1. 🔴 **Fix onboarding bug** (High priority — 1 day overdue)\n2. 🟠 **Update API docs** (Medium — 2 days overdue)\n\nWould you like me to generate an AI rescue plan to schedule these into your week?",
        "agent_name": "task_agent",
    },
    "rescue": {
        "response": "🆘 I've analysed your backlog! Here's your **AI Rescue Plan**:\n\n**2 tasks scheduled across 2 days:**\n1. 🎯 *Fix onboarding bug* → Tomorrow 9:00–10:30 AM\n2. 🎯 *Update API docs* → Thursday 2:00–3:00 PM\n\nFocus blocks added to your calendar. Your completion rate should recover to 85% by Friday!",
        "agent_name": "smart_scheduler_agent",
    },
    "meeting": {
        "response": "🎙️ Meeting analysed! **Score: 74/100 (Productive)**\n\n**4 action items extracted and created as tasks:**\n- Alice: Prepare revenue breakdown (Due: Friday, High)\n- Bob: Fix onboarding step 3 bug (Due: Wednesday, High)\n- Carol: Set up A/B test framework (Due: Monday, Medium)\n- Team: Schedule Q3 mid-sprint review (Due: Next week, Medium)\n\n**Key decisions:** Feature freeze until Q3 end, $50K budget approved.\n\nMeeting summary saved as a note! 📝",
        "agent_name": "meeting_summarizer_agent",
    },
    "weekly": {
        "response": "📊 **Weekly Retrospective — Week of Jun 10**\n\n## 🏆 This Week's Wins\nYou completed 7 out of 9 tasks — a 78% completion rate, your best in 4 weeks!\n\n## 😬 What Slipped\nThe onboarding bug took longer due to scope creep.\n\n## 🔍 Patterns I Noticed\nYou're most productive on Tuesday and Thursday mornings.\n\n## 💡 One Thing to Change\nBlock 2 hours before 11am for deep work — no meetings.\n\n## 🚀 You're on an upward trend! Keep it going. 🎯",
        "agent_name": "weekly_retro_agent",
    },
}


def get_mock_query_response(message: str) -> Dict[str, Any]:
    """Return a mock orchestrator response based on message keywords."""
    msg = message.lower()
    sess = f"demo-sess-{uuid.uuid4().hex[:8]}"

    if any(w in msg for w in ["task", "add", "create", "remind", "todo", "to-do"]):
        r = MOCK_QUERY_RESPONSES["task"]
    elif any(w in msg for w in ["schedule", "event", "meeting", "book", "calendar"]):
        r = MOCK_QUERY_RESPONSES["event"]
    elif any(w in msg for w in ["note", "write", "capture", "memo"]):
        r = MOCK_QUERY_RESPONSES["note"]
    elif any(w in msg for w in ["score", "productivity", "analytics", "stats", "how am i"]):
        r = MOCK_QUERY_RESPONSES["analytics"]
    elif any(w in msg for w in ["overdue", "behind", "missed", "late"]):
        r = MOCK_QUERY_RESPONSES["overdue"]
    elif any(w in msg for w in ["overwhelm", "rescue", "catch up", "reschedule", "help me"]):
        r = MOCK_QUERY_RESPONSES["rescue"]
    elif any(w in msg for w in ["transcript", "summarise", "summarize", "meeting notes", "action items"]):
        r = MOCK_QUERY_RESPONSES["meeting"]
    elif any(w in msg for w in ["retro", "weekly", "week", "review"]):
        r = MOCK_QUERY_RESPONSES["weekly"]
    else:
        r = MOCK_QUERY_RESPONSES["default"]

    return {**r, "session_id": sess, "user_id": "default_user"}


# ── Mock Smart features ────────────────────────────────────────────────────────

MOCK_RESCUE_PLAN: Dict[str, Any] = {
    "assignments": [
        {"task_id": "t1", "task_title": "Fix onboarding bug", "priority": "high",
         "slot_start": _hrs(25), "duration_minutes": 90,
         "reasoning": "Earliest available slot — critical bug affecting conversion."},
        {"task_id": "t2", "task_title": "Review Q3 report", "priority": "urgent",
         "slot_start": _days(2), "duration_minutes": 60,
         "reasoning": "Morning slot tomorrow — peak focus time for analysis work."},
    ],
    "unscheduled_tasks": [],
    "summary": "2 overdue tasks scheduled into the next 2 days. Focus blocks created in calendar. Expected completion rate improvement: +15%.",
    "slots_scanned": 8,
    "tasks_evaluated": 2,
    "applied": None,
    "hint": "Set auto_apply=true to activate this plan.",
}

MOCK_MEETING_RESULT: Dict[str, Any] = {
    "analysis": {
        "meeting_title": "Q3 Planning Session",
        "sentiment": "productive",
        "sentiment_score": 74,
        "summary": "Productive Q3 planning session. Revenue 12% behind target. Key decisions around feature freeze and A/B testing approved.",
        "key_decisions": ["Feature freeze until Q3 end", "$50K budget approved", "A/B test to be launched"],
        "action_items": [
            {"title": "Prepare revenue breakdown", "owner": "Alice", "due_date": _days(2), "priority": "high"},
            {"title": "Fix onboarding step 3 bug", "owner": "Bob", "due_date": _days(1), "priority": "high"},
            {"title": "Set up A/B test framework", "owner": "Carol", "due_date": _days(7), "priority": "medium"},
        ],
        "attendees": ["Alice", "Bob", "Carol"],
        "risks_flagged": ["Revenue 12% behind target", "Onboarding drop-off at step 3"],
    },
    "created_tasks": [_uid(), _uid(), _uid()],
    "note_id": _uid(),
    "followup_event": None,
    "sentiment": "productive",
    "sentiment_score": 74,
    "action_items_count": 3,
    "decisions_count": 3,
}

MOCK_RETRO: Dict[str, Any] = {
    "narrative": """## 🏆 This Week's Wins

You completed 7 out of 9 tasks this week — a **77.8% completion rate**, your best performance in 4 weeks! You closed 3 high-priority items including the auth refactor and the payment service migration, both of which had been sitting in the backlog for over a month.

## 😬 What Slipped (and Why It Might Have)

Two tasks slipped into overdue territory: the onboarding bug fix and the API documentation update. The onboarding bug likely slipped due to unexpected scope — what looked like a 2-hour fix turned into a 3-day investigation. The docs update simply got deprioritised when urgent items took over.

## 🔍 Patterns I Noticed

Your most productive days this week were Tuesday and Thursday — you completed 5 tasks on those two days alone. You tend to tackle high-priority items in the morning and defer medium tasks to afternoon. Your "meeting" and "tech" tagged tasks consistently get done faster than "docs" tasks.

## 💡 One Thing to Change Next Week

**Block 2 uninterrupted hours before 11am every day** — no Slack, no meetings. Use it only for your top-priority task. Even blocking just Mon/Wed/Fri would give you 6 extra deep-work hours per week.

## 🚀 You're on an upward trend — 60% four weeks ago, 77% today. Keep this momentum going!""",
    "week_label": f"Week of {(_now - timedelta(days=_now.weekday())).strftime('%b %d, %Y')}",
    "productivity_score": 72,
    "stats": {"completion_rate_pct": 77.8, "total_tasks": 9, "completed_tasks": 7},
    "note_id": _uid(),
}

MOCK_RISK: Dict[str, Any] = {
    "at_risk_tasks": [
        {"task_id": "t1", "task_title": "Fix onboarding bug", "risk_level": "high", "reason": "Already overdue + high business impact"},
        {"task_id": "t2", "task_title": "Review Q3 report", "risk_level": "medium", "reason": "Due tomorrow with no progress logged"},
    ],
    "overloaded_days": [_days(1)[:10]],
    "risk_score": 45,
    "recommendation": "Defer 'Set up A/B test' to next week and focus on the onboarding bug first — it's blocking conversion.",
}

MOCK_FOCUS_PLAN: Dict[str, Any] = {
    "focus_order": [
        {"task_id": "t1", "task_title": "Fix onboarding bug in step 3", "estimated_minutes": 90, "why_first": "Overdue + high impact on conversion rate"},
        {"task_id": "t2", "task_title": "Review Q3 financial report", "estimated_minutes": 60, "why_first": "Due tomorrow — urgent priority"},
        {"task_id": "t3", "task_title": "Prepare sprint planning slides", "estimated_minutes": 45, "why_first": "Meeting at 3pm today needs preparation"},
        {"task_id": "t4", "task_title": "Write product blog post", "estimated_minutes": 60, "why_first": "Medium priority — good for afternoon"},
    ],
    "total_estimated_minutes": 255,
    "motivational_message": "You've got 4 focused tasks today. Tackle the bug first — unblocking it will give you momentum for everything else! 🚀",
}

MOCK_TAGS: Dict[str, Any] = {
    "suggested_tags": ["work", "finance", "review", "q3"],
    "confidence": "high",
    "reasoning": "Content is about a quarterly financial review task.",
}

MOCK_PRIORITY_RECS: Dict[str, Any] = {
    "recommendations": [
        {"task_id": "t4", "task_title": "Update API docs", "current_priority": "low", "recommended_priority": "medium", "reasoning": "Due in 3 days — needs bump from low."},
        {"task_id": "t5", "task_title": "Write blog post", "current_priority": "urgent", "recommended_priority": "high", "reasoning": "2 weeks out — urgent is too high for this timeline."},
    ],
    "summary": "2 tasks need priority adjustments to better reflect actual urgency and deadlines.",
    "applied": 0,
    "dry_run": True,
}
