# === agents/briefing_agent.py ===
"""
Briefing Agent — composes and sends a morning digest email via Gmail API.

The agent:
1. Queries today's tasks from Firestore.
2. Fetches today's calendar events.
3. Pulls the most recent notes (up to 5).
4. Composes a structured morning briefing.
5. Sends it via Gmail API using google.auth.default() credentials.

The send step can be disabled by setting ENABLE_GMAIL_SEND=False in the
environment (useful for development/testing).
"""

from __future__ import annotations

import os
import smtplib
import uuid
from datetime import datetime, timedelta, timezone
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from typing import Any, Dict, List, Optional

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from config.settings import settings, LOCAL_TZ
from tools.calendar_tools import list_calendar_events
from tools.firestore_tools import list_notes, list_tasks


def _build_ics(
    summary: str,
    start_dt: datetime,
    end_dt: datetime,
    description: str,
    organizer_email: str,
    attendee_email: str,
    uid: str,
    rrule: str = "",
) -> str:
    """Build an iCalendar (ICS) string for a meeting invite."""
    fmt = "%Y%m%dT%H%M%SZ"
    now = datetime.utcnow().strftime(fmt)
    start_utc = start_dt.astimezone(timezone.utc).strftime(fmt)
    end_utc = end_dt.astimezone(timezone.utc).strftime(fmt)
    desc_safe = description.replace("\n", "\\n").replace(",", "\\,")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Smart Daily Planner//EN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{now}",
        f"DTSTART:{start_utc}",
        f"DTEND:{end_utc}",
        f"SUMMARY:{summary}",
        f"DESCRIPTION:{desc_safe}",
        f"ORGANIZER;CN=Smart Daily Planner:mailto:{organizer_email}",
        f"ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE;CN={attendee_email}:mailto:{attendee_email}",
        "STATUS:CONFIRMED",
        "SEQUENCE:0",
    ]
    if rrule:
        lines.append(rrule)
    lines += ["END:VEVENT", "END:VCALENDAR", ""]
    return "\r\n".join(lines)


def _get_smtp_config(
    sender_email: str,
    known_passwords: Optional[dict] = None,
) -> tuple[str, str, str]:
    """Return (smtp_host, smtp_user, app_password) for the given sender address.

    known_passwords: optional {email: app_password} dict for UI-saved accounts.
    Supports Gmail (primary + UI-added) and Yahoo Mail.
    """
    email_lower = sender_email.lower()

    # UI-saved accounts take highest priority
    if known_passwords:
        for addr, pwd in known_passwords.items():
            if addr.lower() == email_lower and pwd:
                smtp_host = "smtp.mail.yahoo.com" if ("@yahoo" in email_lower or "@ymail" in email_lower) else "smtp.gmail.com"
                return smtp_host, sender_email, pwd

    if "@yahoo" in email_lower or "@ymail" in email_lower:
        app_password = settings.YAHOO_APP_PASSWORD or os.environ.get("YAHOO_APP_PASSWORD", "")
        if not app_password:
            raise ValueError(
                f"YAHOO_APP_PASSWORD not configured for {sender_email}. "
                "Add YAHOO_APP_PASSWORD to .env or add the account via Settings → Connected Accounts."
            )
        return "smtp.mail.yahoo.com", sender_email, app_password

    # .env-based secondary Gmail
    if settings.GMAIL2_EMAIL and email_lower == settings.GMAIL2_EMAIL.lower():
        app_password = settings.GMAIL2_APP_PASSWORD or os.environ.get("GMAIL2_APP_PASSWORD", "")
        if not app_password:
            raise ValueError(
                f"GMAIL2_APP_PASSWORD not configured for {sender_email}."
            )
        return "smtp.gmail.com", sender_email, app_password

    # Default: primary Gmail
    app_password = settings.GMAIL_APP_PASSWORD or os.environ.get("GMAIL_APP_PASSWORD", "")
    if not app_password:
        raise ValueError(
            f"GMAIL_APP_PASSWORD not configured for {sender_email}. "
            "Add GMAIL_APP_PASSWORD to .env or add the account via Settings → Connected Accounts."
        )
    return "smtp.gmail.com", sender_email, app_password


def _send_via_smtp(
    to_email: str,
    subject: str,
    body: str,
    html_body: str = "",
    ics_content: str = "",
    sender_email: str = "",
    known_passwords: Optional[dict] = None,
) -> str:
    """Send email via SMTP (Gmail or Yahoo) using an App Password.

    Automatically selects the correct SMTP server based on the sender domain.
    If html_body is provided, sends multipart/alternative with HTML + plain text.
    If ics_content is provided, attaches it as a calendar invite.

    Args:
        to_email: Recipient email address.
        subject: Email subject line.
        body: Plain-text email body.
        html_body: Optional HTML version of the body.
        ics_content: Optional iCalendar (.ics) string for meeting invites.
        sender_email: Override which account sends the email.
        known_passwords: Optional {email: app_password} map for UI-saved accounts.
    """
    from_email = sender_email or settings.GMAIL_USER_EMAIL
    smtp_host, smtp_user, app_password = _get_smtp_config(from_email, known_passwords)

    if ics_content:
        # mixed → alternative (text+html) + calendar attachment
        msg = MIMEMultipart("mixed")
        msg["From"] = f"Smart Daily Planner <{from_email}>"
        msg["To"] = to_email
        msg["Subject"] = subject

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body, "plain", "utf-8"))
        if html_body:
            alt.attach(MIMEText(html_body, "html", "utf-8"))
        msg.attach(alt)

        ics_part = MIMEBase("text", "calendar", method="REQUEST", charset="utf-8")
        ics_part.set_payload(ics_content.encode("utf-8"))
        encoders.encode_base64(ics_part)
        ics_part.add_header("Content-Disposition", "attachment", filename="invite.ics")
        ics_part.add_header("Content-Transfer-Encoding", "base64")
        msg.attach(ics_part)
    elif html_body:
        msg = MIMEMultipart("alternative")
        msg["From"] = f"Smart Daily Planner <{from_email}>"
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    else:
        msg = MIMEMultipart("alternative")
        msg["From"] = f"Smart Daily Planner <{from_email}>"
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP_SSL(smtp_host, 465) as server:
        server.login(smtp_user, app_password)
        server.sendmail(from_email, to_email, msg.as_string())
    return f"smtp-{from_email}-{to_email}"


def _send_via_gmail_api(
    to_email: str,
    subject: str,
    body: str,
    html_body: str = "",
    ics_content: str = "",
    sender_email: str = "",
    refresh_token: str = "",
    access_token: str = "",
) -> str:
    """Send email via Gmail REST API using an OAuth2 refresh token.

    Used for linked accounts where no app password is available.
    """
    import base64
    from google.auth.transport.requests import AuthorizedSession, Request
    from google.oauth2.credentials import Credentials

    creds = Credentials(
        token=access_token or None,
        refresh_token=refresh_token or None,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.OAUTH_CLIENT_ID,
        client_secret=settings.OAUTH_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/gmail.send"],
    )
    if (not access_token or creds.expired) and creds.refresh_token:
        creds.refresh(Request())

    from_email = sender_email or settings.GMAIL_USER_EMAIL

    if ics_content:
        msg = MIMEMultipart("mixed")
        msg["From"] = f"Smart Daily Planner <{from_email}>"
        msg["To"] = to_email
        msg["Subject"] = subject
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body, "plain", "utf-8"))
        if html_body:
            alt.attach(MIMEText(html_body, "html", "utf-8"))
        msg.attach(alt)
        ics_part = MIMEBase("text", "calendar", method="REQUEST", charset="utf-8")
        ics_part.set_payload(ics_content.encode("utf-8"))
        encoders.encode_base64(ics_part)
        ics_part.add_header("Content-Disposition", "attachment", filename="invite.ics")
        msg.attach(ics_part)
    elif html_body:
        msg = MIMEMultipart("alternative")
        msg["From"] = f"Smart Daily Planner <{from_email}>"
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    else:
        msg = MIMEMultipart("alternative")
        msg["From"] = f"Smart Daily Planner <{from_email}>"
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    session = AuthorizedSession(creds)
    resp = session.post(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        json={"raw": raw},
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Gmail API error {resp.status_code}: {resp.text}")
    return resp.json().get("id", f"gmail-api-{from_email}")


async def _send_email_smart(
    to_email: str,
    subject: str,
    body: str,
    html_body: str = "",
    ics_content: str = "",
    sender_email: str = "",
    user_id: str = "default_user",
    known_passwords: Optional[dict] = None,
) -> str:
    """Route email through Gmail API (OAuth) for linked accounts, SMTP for others."""
    from_email = sender_email or settings.GMAIL_USER_EMAIL

    # Check if sender is an OAuth-linked account — use Gmail API (no password needed)
    try:
        from tools.firestore_tools import get_linked_gmail_accounts
        linked = await get_linked_gmail_accounts(user_id)
        for acct in linked:
            if acct.get("email", "").lower() == from_email.lower():
                if acct.get("email_send_enabled", True) and acct.get("refresh_token"):
                    return _send_via_gmail_api(
                        to_email=to_email, subject=subject, body=body,
                        html_body=html_body, ics_content=ics_content,
                        sender_email=from_email,
                        refresh_token=acct["refresh_token"],
                        access_token=acct.get("access_token", ""),
                    )
    except Exception:
        pass

    # Fall back to SMTP for primary account / Yahoo
    return _send_via_smtp(
        to_email=to_email, subject=subject, body=body,
        html_body=html_body, ics_content=ics_content,
        sender_email=from_email, known_passwords=known_passwords,
    )


async def send_meeting_invite(
    to_email: str,
    summary: str,
    start_datetime_str: str,
    duration_minutes: int = 60,
    description: str = "",
    organizer_email: str = "",
    sender_email: str = "",
    rrule: str = "",
    known_passwords: Optional[dict] = None,
) -> str:
    """Send a proper calendar invite (ICS) via SMTP (Gmail or Yahoo).

    The recipient will see an 'Add to Calendar' button in their email client.

    Args:
        to_email: Recipient email address.
        summary: Meeting title.
        start_datetime_str: ISO-8601 start datetime string.
        duration_minutes: Duration in minutes.
        description: Meeting description / agenda.
        organizer_email: Organizer display email (defaults to sender).
        sender_email: Which account to send from. Defaults to GMAIL_USER_EMAIL.
        rrule: Optional RFC 5545 RRULE string for recurring invites.

    Returns:
        Message ID string.
    """
    from dateutil import parser as dateutil_parser
    organizer = sender_email or organizer_email or settings.GMAIL_USER_EMAIL
    start_dt = dateutil_parser.parse(start_datetime_str)
    if start_dt.tzinfo is None:
        import pytz
        start_dt = pytz.timezone(settings.DEFAULT_TIMEZONE).localize(start_dt)
    end_dt = start_dt + timedelta(minutes=duration_minutes)
    invite_uid = str(uuid.uuid4())

    ics = _build_ics(
        summary=summary,
        start_dt=start_dt,
        end_dt=end_dt,
        description=description,
        organizer_email=organizer,
        attendee_email=to_email,
        uid=invite_uid,
        rrule=rrule,
    )

    date_str = start_dt.strftime("%A, %d %B %Y")
    time_str = start_dt.strftime("%I:%M %p")
    dur_str = f"{duration_minutes} minutes" if duration_minutes < 60 else f"{duration_minutes // 60}h {duration_minutes % 60}m".rstrip("m 0") or f"{duration_minutes // 60}h"

    recur_label = ""
    if rrule:
        recur_label = f"<p style='margin:4px 0 0;color:#a78bfa;font-size:13px'>🔁 {rrule.replace('RRULE:','').replace(';',' · ')}</p>"

    plain_body = (
        f"Hi,\n\nYou have been invited to the following meeting:\n\n"
        f"Meeting: {summary}\n"
        f"Date: {date_str}\n"
        f"Time: {time_str} ({settings.DEFAULT_TIMEZONE})\n"
        f"Duration: {dur_str}\n"
        f"{('Agenda: ' + description + chr(10)) if description else ''}"
        f"{('Recurrence: ' + rrule.replace('RRULE:','') + chr(10)) if rrule else ''}\n"
        f"Please open the attached .ics file to accept this invite.\n\n"
        f"Regards,\nSmart Daily Planner"
    )

    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#0f172a;font-family:'Segoe UI',Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#0f172a;padding:30px 0">
  <tr><td align="center">
    <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#1e293b;border-radius:16px;overflow:hidden;border:1px solid rgba(99,102,241,0.25)">
      <!-- Header -->
      <tr><td style="background:linear-gradient(135deg,#4f46e5,#0ea5e9);padding:30px 36px">
        <table width="100%"><tr>
          <td><p style="margin:0;color:rgba(255,255,255,0.7);font-size:12px;text-transform:uppercase;letter-spacing:1px">Calendar Invite</p>
              <h1 style="margin:8px 0 0;color:#fff;font-size:22px;font-weight:700">{summary}</h1></td>
          <td align="right"><div style="background:rgba(255,255,255,0.15);border-radius:50%;width:52px;height:52px;display:inline-flex;align-items:center;justify-content:center;font-size:24px">📅</div></td>
        </tr></table>
      </td></tr>
      <!-- Details card -->
      <tr><td style="padding:28px 36px">
        <table width="100%" cellpadding="0" cellspacing="0" style="background:#0f172a;border-radius:12px;overflow:hidden">
          <tr>
            <td width="50%" style="padding:18px 20px;border-right:1px solid rgba(99,102,241,0.15)">
              <p style="margin:0;color:#64748b;font-size:11px;text-transform:uppercase;letter-spacing:0.8px">Date</p>
              <p style="margin:4px 0 0;color:#e2e8f0;font-size:15px;font-weight:600">{date_str}</p>
            </td>
            <td width="50%" style="padding:18px 20px">
              <p style="margin:0;color:#64748b;font-size:11px;text-transform:uppercase;letter-spacing:0.8px">Time</p>
              <p style="margin:4px 0 0;color:#e2e8f0;font-size:15px;font-weight:600">{time_str}</p>
              <p style="margin:2px 0 0;color:#64748b;font-size:11px">{settings.DEFAULT_TIMEZONE}</p>
            </td>
          </tr>
          <tr style="border-top:1px solid rgba(99,102,241,0.1)">
            <td colspan="2" style="padding:18px 20px">
              <p style="margin:0;color:#64748b;font-size:11px;text-transform:uppercase;letter-spacing:0.8px">Duration</p>
              <p style="margin:4px 0 0;color:#e2e8f0;font-size:15px;font-weight:600">⏱ {dur_str}</p>
              {recur_label}
            </td>
          </tr>
        </table>
        {f'<div style="margin-top:20px;padding:16px 20px;background:#0f172a;border-radius:12px;border-left:3px solid #6366f1"><p style="margin:0;color:#64748b;font-size:11px;text-transform:uppercase;letter-spacing:0.8px">Agenda</p><p style="margin:8px 0 0;color:#cbd5e1;font-size:14px;line-height:1.6">' + description.replace(chr(10),'<br>') + '</p></div>' if description else ''}
        <div style="margin-top:24px;padding:16px;background:rgba(99,102,241,0.08);border-radius:12px;text-align:center;border:1px solid rgba(99,102,241,0.2)">
          <p style="margin:0;color:#a5b4fc;font-size:13px">📎 A calendar file (.ics) is attached — open it to add this event to your calendar.</p>
        </div>
      </td></tr>
      <!-- Footer -->
      <tr><td style="padding:20px 36px;border-top:1px solid rgba(255,255,255,0.06);text-align:center">
        <p style="margin:0;color:#475569;font-size:12px">Sent by <strong style="color:#6366f1">Smart Daily Planner</strong> · Organizer: {organizer}</p>
      </td></tr>
    </table>
  </td></tr>
</table>
</body></html>"""

    subject = f"[Meeting Invite] {summary}"
    from_acct = sender_email or organizer
    return await _send_email_smart(
        to_email=to_email, subject=subject, body=plain_body,
        html_body=html_body, ics_content=ics, sender_email=from_acct,
    )


# ── Helper: compose briefing text ─────────────────────────────────────────────


_DAILY_TIPS = [
    "Start with your #1 hardest task in the first 90 minutes — that's when willpower is highest.",
    "Before each meeting, write one sentence: what outcome do I need from this?",
    "The 2-minute rule: if a task takes less than 2 minutes, do it right now.",
    "Block 'deep work' time on your calendar — treat it like a meeting you can't skip.",
    "End each day by writing tomorrow's top 3 tasks. It primes your brain overnight.",
    "Batch similar tasks (emails, calls) into one time block to cut context-switching cost.",
    "A 5-minute daily review of your task list prevents overdue tasks from piling up.",
]


def _compose_briefing(
    tasks: List[Dict],
    events: List[Dict],
    notes: List[Dict],
    date_str: str,
) -> str:
    """Compose a structured morning briefing string from agent data.

    Args:
        tasks: List of task dicts for today (pending/overdue).
        events: List of calendar event dicts for today.
        notes: List of recent note dicts.
        date_str: Human-readable date string for the briefing header.

    Returns:
        Formatted plain-text briefing string ready for email body.
    """
    from datetime import date as _date
    today_tip = _DAILY_TIPS[_date.today().weekday() % len(_DAILY_TIPS)]

    lines = [f"🌅 Daily Brief — {date_str}", ""]

    # Tasks section
    overdue_count = sum(1 for t in tasks if t.get("status") == "overdue")
    urgent_count = sum(1 for t in tasks if t.get("priority") in ("urgent", "high") and t.get("status") != "completed")
    lines.append(f"📋 Tasks: {len(tasks)} total · {urgent_count} urgent/high · {overdue_count} overdue")
    if tasks:
        # Sort: overdue first, then by priority
        priority_rank = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
        sorted_tasks = sorted(
            tasks,
            key=lambda t: (0 if t.get("status") == "overdue" else 1,
                           priority_rank.get(t.get("priority", "medium"), 2))
        )
        for t in sorted_tasks[:5]:
            priority_emoji = {"urgent": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}.get(
                t.get("priority", "medium"), "⚪"
            )
            status = t.get("status", "pending")
            status_marker = "✅" if status == "completed" else ("⚠️ OVERDUE" if status == "overdue" else "◯")
            due = t.get("due_date", "N/A")
            if due and "T" in due:
                try:
                    from dateutil import parser as dp
                    due_dt = dp.parse(due).astimezone(LOCAL_TZ)
                    due = due_dt.strftime("%d %b %Y, %I:%M %p IST")
                except Exception:
                    pass
            lines.append(f"  {status_marker} {priority_emoji} {t.get('title','Untitled')} ({t.get('priority','medium')}) · {due}")
        if len(sorted_tasks) > 5:
            lines.append(f"  … +{len(sorted_tasks)-5} more task(s)")
    else:
        lines.append("  ✨ No tasks due today — great discipline!")
    lines.append("")

    # Events section
    lines.append(f"📅 Calendar: {len(events)} event(s)")
    if events:
        for e in events[:4]:
            start_raw = e.get("start", "?")
            if start_raw and "T" in str(start_raw):
                try:
                    from dateutil import parser as dp
                    start_dt = dp.parse(str(start_raw)).astimezone(LOCAL_TZ)
                    start_fmt = start_dt.strftime("%I:%M %p IST")
                except Exception:
                    start_fmt = str(start_raw)[:16].replace("T", " ")
            else:
                start_fmt = str(start_raw)[:16].replace("T", " ")
            lines.append(f"  🕐 {start_fmt} — {e.get('summary','(No title)')}")
        if len(events) > 4:
            lines.append(f"  … +{len(events)-4} more event(s)")
    else:
        lines.append("  📭 No events scheduled today. A great day for deep work!")
    lines.append("")

    # Keep mail concise — omit notes block unless no tasks and no events.
    if not tasks and not events and notes:
        lines.append(f"📝 Notes: {len(notes)} recent")
        lines.append(f"  📌 {notes[0].get('title','Untitled')}")
        lines.append("")

    # Daily tip
    lines.append(f"💡 Tip: {today_tip}")
    lines.append("")
    lines.append("Have a focused day! — Smart Daily Planner")
    return "\n".join(lines)


def _compose_briefing_html(
    tasks: List[Dict],
    events: List[Dict],
    notes: List[Dict],
    date_str: str,
) -> str:
    """Compose an HTML morning briefing email."""
    from datetime import date as _date
    today_tip = _DAILY_TIPS[_date.today().weekday() % len(_DAILY_TIPS)]

    overdue_count = sum(1 for t in tasks if t.get("status") == "overdue")
    urgent_count = sum(1 for t in tasks if t.get("priority") in ("urgent", "high") and t.get("status") != "completed")
    pending_count = sum(1 for t in tasks if t.get("status") != "completed")

    priority_rank = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
    sorted_tasks = sorted(
        tasks,
        key=lambda t: (0 if t.get("status") == "overdue" else 1, priority_rank.get(t.get("priority", "medium"), 2))
    )

    pcolors = {"urgent": "#f87171", "high": "#fb923c", "medium": "#fbbf24", "low": "#34d399"}
    pbadge = lambda t: f'<span style="display:inline-block;padding:2px 8px;border-radius:20px;font-size:10px;font-weight:700;text-transform:uppercase;background:{pcolors.get(t.get("priority","medium"),"#64748b")}22;color:{pcolors.get(t.get("priority","medium"),"#94a3b8")};border:1px solid {pcolors.get(t.get("priority","medium"),"#64748b")}44">{t.get("priority","med").upper()}</span>'

    def fmt_due(t):
        due = t.get("due_date", "")
        if due and "T" in due:
            try:
                from dateutil import parser as dp
                return dp.parse(due).astimezone(LOCAL_TZ).strftime("%d %b %I:%M %p")
            except Exception:
                pass
        return due[:10] if due else "N/A"

    def fmt_time(e):
        start_raw = e.get("start", "")
        if start_raw and "T" in str(start_raw):
            try:
                from dateutil import parser as dp
                return dp.parse(str(start_raw)).astimezone(LOCAL_TZ).strftime("%I:%M %p")
            except Exception:
                pass
        return str(start_raw)[:16].replace("T", " ")


    task_rows = ""
    for t in sorted_tasks[:5]:
        is_overdue = t.get("status") == "overdue"
        is_done = t.get("status") == "completed"
        icon = "✅" if is_done else ("🔴" if is_overdue else "⬜")
        left_bar = "#ef4444" if is_overdue else ("#10b981" if is_done else "#475569")
        title_style = "text-decoration:line-through;color:#475569" if is_done else (
            "color:#f87171;font-weight:600" if is_overdue else "color:#e2e8f0"
        )
        task_rows += f"""
        <tr>
          <td style="padding:0"><div style="padding:10px 14px 10px 16px;margin:2px 0;background:#0f172a;border-radius:8px;border-left:3px solid {left_bar};display:flex;align-items:center;gap:10px">
            <span style="font-size:13px">{icon}</span>
            <div style="flex:1;min-width:0">
              <div style="{title_style};font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{t.get("title","Untitled")}</div>
              {"<div style='color:#f87171;font-size:10px;font-weight:700;margin-top:2px'>⚠ OVERDUE</div>" if is_overdue else ""}
            </div>
            <div style="text-align:right;flex-shrink:0">{pbadge(t)}<div style="color:#64748b;font-size:10px;margin-top:2px">{fmt_due(t)}</div></div>
          </div></td>
        </tr>"""

    event_rows = ""
    for e in events[:4]:
        event_rows += f"""
        <tr style="border-bottom:1px solid rgba(255,255,255,0.04)">
          <td style="padding:10px 14px;color:#22d3ee;font-size:14px">🕐</td>
          <td style="padding:10px 4px"><span style="color:#e2e8f0;font-size:13px;font-weight:500">{e.get("summary","(No title)")}</span>
            {"<br><span style='color:#94a3b8;font-size:11px'>📍 " + e.get("location","") + "</span>" if e.get("location") else ""}</td>
          <td style="padding:10px 14px;color:#22d3ee;font-size:12px;font-weight:600;white-space:nowrap">{fmt_time(e)}</td>
        </tr>"""

    note_items = ""
    for n in notes[:4]:
        preview = (n.get("content", "") or "")[:90].replace("<", "&lt;").replace(">", "&gt;")
        note_items += f'<div style="padding:8px 12px;margin-bottom:6px;background:rgba(255,255,255,0.03);border-radius:8px;border-left:2px solid #6366f1"><p style="margin:0;color:#e2e8f0;font-size:12px;font-weight:600">{n.get("title","Untitled")}</p><p style="margin:3px 0 0;color:#64748b;font-size:11px">{preview}…</p></div>'

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#0f172a;font-family:'Segoe UI',Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#0f172a;padding:24px 0">
<tr><td align="center">
<table width="620" cellpadding="0" cellspacing="0" style="max-width:620px;width:100%;background:#1e293b;border-radius:16px;overflow:hidden;border:1px solid rgba(99,102,241,0.2)">

  <!-- Header -->
  <tr><td style="background:linear-gradient(135deg,#312e81 0%,#1e3a5f 100%);padding:28px 32px">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      <td><p style="margin:0 0 4px;color:rgba(255,255,255,0.5);font-size:11px;text-transform:uppercase;letter-spacing:1px">📅 Smart Daily Planner</p>
        <h1 style="margin:0;color:#fff;font-size:22px;font-weight:700">🌅 Good Morning!</h1>
        <p style="margin:6px 0 0;color:rgba(255,255,255,0.65);font-size:13px">{date_str}</p></td>
      <td style="text-align:right;padding-left:20px;vertical-align:top">
        <div style="background:rgba(255,255,255,0.12);border-radius:12px;padding:10px 16px;text-align:center;min-width:70px">
          <div style="font-size:24px;font-weight:800;color:#fff">{pending_count}</div>
          <div style="font-size:10px;color:rgba(255,255,255,0.6);margin-top:2px">open tasks</div>
        </div>
      </td>
    </tr></table>
  </td></tr>

  <!-- KPI row -->
  <tr><td style="padding:0;background:#0f172a">
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td width="25%" style="padding:16px 8px 16px 16px;text-align:center;border-right:1px solid rgba(255,255,255,0.05)">
          <div style="font-size:24px;font-weight:800;color:#22d3ee">{pending_count}</div>
          <div style="font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:0.6px;margin-top:2px">📌 Open</div>
        </td>
        <td width="25%" style="padding:16px 8px;text-align:center;border-right:1px solid rgba(255,255,255,0.05)">
          <div style="font-size:24px;font-weight:800;color:{'#f87171' if overdue_count else '#34d399'}">{overdue_count}</div>
          <div style="font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:0.6px;margin-top:2px">{'⚠️ Overdue' if overdue_count else '✓ On Track'}</div>
        </td>
        <td width="25%" style="padding:16px 8px;text-align:center;border-right:1px solid rgba(255,255,255,0.05)">
          <div style="font-size:24px;font-weight:800;color:#22d3ee">{len(events)}</div>
          <div style="font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:0.6px;margin-top:2px">📅 Events</div>
        </td>
        <td width="25%" style="padding:16px 16px 16px 8px;text-align:center">
          <div style="font-size:24px;font-weight:800;color:#a78bfa">{urgent_count}</div>
          <div style="font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:0.6px;margin-top:2px">🔥 Urgent</div>
        </td>
      </tr>
    </table>
  </td></tr>

  {"<!-- Overdue alert --><tr><td style='padding:10px 24px 0'><div style='padding:10px 16px;background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.25);border-radius:10px;color:#f87171;font-size:12px;font-weight:600'>⚠️ " + str(overdue_count) + " task" + ("s" if overdue_count!=1 else "") + " overdue — action needed today!</div></td></tr>" if overdue_count else ""}

  <!-- Tasks -->
  <tr><td style="padding:20px 24px 8px">
    <p style="margin:0 0 10px;color:#94a3b8;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px">📋 Top Tasks ({len(tasks)} total)</p>
    {"<table width='100%' cellpadding='0' cellspacing='0'>" + task_rows + "</table>" if task_rows else "<div style='padding:12px 16px;background:#0f172a;border-radius:10px;color:#64748b;font-size:13px'>✨ No tasks due today — great work!</div>"}
  </td></tr>

  <!-- Events -->
  <tr><td style="padding:8px 24px 8px">
    <p style="margin:0 0 10px;color:#94a3b8;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px">📅 Next Events ({len(events)} total)</p>
    {"<table width='100%' cellpadding='0' cellspacing='0' style='background:#0f172a;border-radius:10px;overflow:hidden'>" + event_rows + "</table>" if event_rows else "<div style='padding:12px 16px;background:#0f172a;border-radius:10px;color:#64748b;font-size:13px'>📭 No events today — ideal for deep work!</div>"}
  </td></tr>

  {"<!-- Notes --><tr><td style='padding:8px 24px 8px'><p style='margin:0 0 10px;color:#94a3b8;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px'>📝 Notes Snapshot</p>" + note_items + "</td></tr>" if note_items and not tasks and not events else ""}

  <!-- Tip -->
  <tr><td style="padding:8px 24px 20px">
    <div style="padding:14px 18px;background:linear-gradient(135deg,rgba(99,102,241,0.12),rgba(6,182,212,0.08));border-radius:12px;border:1px solid rgba(99,102,241,0.2)">
      <p style="margin:0 0 4px;color:#818cf8;font-size:11px;text-transform:uppercase;font-weight:700;letter-spacing:0.6px">💡 Today's Tip</p>
      <p style="margin:0;color:#cbd5e1;font-size:13px;line-height:1.6">{today_tip}</p>
    </div>
  </td></tr>

  <!-- Footer -->
  <tr><td style="padding:14px 24px;border-top:1px solid rgba(255,255,255,0.05);text-align:center">
    <p style="margin:0;color:#475569;font-size:11px">Have a focused, productive day! · <strong style="color:#6366f1">Smart Daily Planner</strong> · GenAI Academy Hackathon Cohort 1</p>
  </td></tr>

</table>
</td></tr>
</table>
</body></html>"""


def _compose_custom_html(subject: str, body: str) -> str:
    """Render a custom plain-text message (MoM, meeting notes, retro share) as a
    well-formatted HTML email.  Detects section headers (ALL CAPS lines or lines
    ending with ':') and renders them as coloured section dividers."""
    import html as html_mod
    lines = body.split("\n")
    sections: list[str] = []
    i = 0
    accent = "#6366f1"
    section_colors = {
        "MINUTES OF MEETING": "#6366f1",
        "SUMMARY": "#6366f1",
        "ACTION ITEMS": "#10b981",
        "KEY DECISIONS": "#06b6d4",
        "PARTICIPANTS": "#818cf8",
        "MEETING NOTES": "#6366f1",
        "SCORE": "#f59e0b",
        "SENTIMENT": "#a78bfa",
    }
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        # Detect separator lines
        if set(stripped) <= set("=─-") and len(stripped) >= 4:
            i += 1
            continue
        # Detect section header (ALL CAPS + colon, or known header text)
        is_header = (
            stripped.isupper() and len(stripped) > 2
        ) or any(stripped.startswith(k) for k in section_colors)
        if is_header:
            color = next((v for k, v in section_colors.items() if stripped.startswith(k)), accent)
            sections.append(
                f'<div style="margin:20px 0 8px;padding:6px 12px;background:{color}18;'
                f'border-left:3px solid {color};border-radius:4px">'
                f'<span style="color:{color};font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px">'
                f'{html_mod.escape(stripped)}</span></div>'
            )
            i += 1
            continue
        # Numbered item (1. / 2.)
        if stripped and stripped[0].isdigit() and len(stripped) > 2 and stripped[1] in ".):":
            num = stripped[0]
            text = html_mod.escape(stripped[2:].strip())
            sections.append(
                f'<div style="display:flex;gap:10px;margin:6px 0;padding:8px 12px;'
                f'background:rgba(255,255,255,0.03);border-radius:8px">'
                f'<span style="color:{accent};font-weight:700;font-size:12px;min-width:18px">{num}.</span>'
                f'<span style="color:#e2e8f0;font-size:13px;line-height:1.5">{text}</span></div>'
            )
            i += 1
            continue
        # Label: value lines (e.g. "Title : something")
        if ":" in stripped and stripped.index(":") < 20:
            parts = stripped.split(":", 1)
            label = html_mod.escape(parts[0].strip())
            value = html_mod.escape(parts[1].strip())
            sections.append(
                f'<div style="display:flex;gap:8px;margin:4px 0;font-size:13px">'
                f'<span style="color:#64748b;min-width:110px;flex-shrink:0">{label}</span>'
                f'<span style="color:#e2e8f0">{value}</span></div>'
            )
            i += 1
            continue
        # Regular text
        sections.append(f'<p style="margin:4px 0;color:#cbd5e1;font-size:13px;line-height:1.6">{html_mod.escape(stripped)}</p>')
        i += 1

    content_html = "\n".join(sections)
    # Derive a short subtitle from the subject
    subtitle = html_mod.escape(subject)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#0f172a;font-family:'Segoe UI',Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#0f172a;padding:24px 0">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#1e293b;border-radius:16px;overflow:hidden;border:1px solid rgba(99,102,241,0.25)">
  <!-- Header -->
  <tr><td style="background:linear-gradient(135deg,#312e81 0%,#1e3a5f 100%);padding:28px 32px">
    <p style="margin:0 0 4px;color:rgba(255,255,255,0.5);font-size:11px;text-transform:uppercase;letter-spacing:1px">📅 Smart Daily Planner</p>
    <h1 style="margin:0;color:#fff;font-size:20px;font-weight:700;line-height:1.3">{subtitle}</h1>
  </td></tr>
  <!-- Body -->
  <tr><td style="padding:24px 32px 16px">
    {content_html}
  </td></tr>
  <!-- Footer -->
  <tr><td style="padding:16px 32px;border-top:1px solid rgba(255,255,255,0.06);text-align:center">
    <p style="margin:0;color:#475569;font-size:11px">Sent from <strong style="color:{accent}">Smart Daily Planner</strong> · Built for GenAI Academy Hackathon Cohort 1</p>
  </td></tr>
</table>
</td></tr>
</table>
</body></html>"""


# ── Tool functions ─────────────────────────────────────────────────────────────


async def compose_briefing(
    user_id: str = "default_user",
    include_overdue: bool = True,
) -> Dict[str, Any]:
    """Compose a morning briefing digest from today's tasks, events, and notes.

    Fetches data from Firestore and Google Calendar, then builds a structured
    text summary. Does NOT send the email — use send_briefing_email for that.

    Args:
        user_id: Owner whose data to include in the briefing.
        include_overdue: When True, overdue tasks are included alongside
            today's pending tasks. Defaults to True.

    Returns:
        Dict with:
          - 'subject' (str): Suggested email subject line.
          - 'body' (str): Full formatted briefing text.
          - 'task_count' (int): Number of tasks included.
          - 'event_count' (int): Number of events included.
          - 'note_count' (int): Number of notes included.
    """
    now = datetime.now(LOCAL_TZ)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)

    # Fetch today's tasks
    statuses = ["pending", "overdue"] if include_overdue else ["pending"]
    all_tasks = []
    for status in statuses:
        t = await list_tasks(user_id=user_id, status=status, limit=30)
        # Filter to only today's tasks
        for task in t:
            due = task.get("due_date", "")
            if due:
                try:
                    from dateutil import parser as dp
                    due_dt = dp.parse(due)
                    if due_dt.tzinfo is None:
                        due_dt = LOCAL_TZ.localize(due_dt)
                    if today_start <= due_dt < today_end or (include_overdue and status == "overdue"):
                        all_tasks.append(task)
                except Exception:
                    all_tasks.append(task)

    # Fetch today's calendar events
    events = await list_calendar_events(
        time_min=today_start.isoformat(),
        time_max=today_end.isoformat(),
        max_results=15,
    )

    # Fetch recent notes
    notes = await list_notes(user_id=user_id, limit=5)

    date_str = now.strftime("%A, %d %B %Y")
    body = _compose_briefing(all_tasks, events, notes, date_str)
    html_body = _compose_briefing_html(all_tasks, events, notes, date_str)

    return {
        "subject": f"🌅 Your Daily Briefing — {date_str}",
        "body": body,
        "html_body": html_body,
        "task_count": len(all_tasks),
        "event_count": len(events),
        "note_count": len(notes),
    }


async def send_briefing_email(
    recipient_email: Optional[str] = None,
    user_id: str = "default_user",
    sender_email: str = "",
) -> Dict[str, Any]:
    """Compose and send the morning briefing as an email (Gmail or Yahoo).

    If ENABLE_GMAIL_SEND is False in settings, the email body is returned
    without being sent (dry-run mode).

    Args:
        recipient_email: Email address to send the briefing to. Defaults to
            BRIEFING_RECIPIENT_EMAIL from settings (which falls back to
            GMAIL_USER_EMAIL).
        user_id: Owner whose data to include in the briefing.
        sender_email: Which account to send from. Defaults to GMAIL_USER_EMAIL.

    Returns:
        Dict with:
          - 'sent' (bool): True when the email was dispatched.
          - 'sender' (str): Account used to send.
          - 'recipient' (str): Email address that received the briefing.
          - 'subject' (str): Email subject line.
          - 'message_id' (str | None): SMTP message ID if sent.
          - 'body_preview' (str): First 200 chars of the briefing body.
    """
    briefing = await compose_briefing(user_id=user_id)
    subject = briefing["subject"]
    body = briefing["body"]
    html_body = briefing.get("html_body", "")

    to_email = recipient_email or settings.BRIEFING_RECIPIENT_EMAIL or settings.GMAIL_USER_EMAIL
    from_acct = sender_email or settings.GMAIL_USER_EMAIL

    if not settings.ENABLE_GMAIL_SEND:
        return {
            "sent": False,
            "dry_run": True,
            "recipient": to_email,
            "subject": subject,
            "message_id": None,
            "body_preview": body[:200],
        }

    msg_id = await _send_email_smart(
        to_email=to_email, subject=subject, body=body, html_body=html_body,
        sender_email=from_acct, user_id=user_id,
    )
    return {
        "sent": True,
        "dry_run": False,
        "sender": from_acct,
        "recipient": to_email,
        "subject": subject,
        "message_id": msg_id,
        "body_preview": body[:200],
    }


async def get_briefing_preview(user_id: str = "default_user") -> Dict[str, Any]:
    """Generate a briefing preview without sending it.

    Args:
        user_id: Owner whose data to include.

    Returns:
        Full briefing dict from compose_briefing including subject and body.
    """
    return await compose_briefing(user_id=user_id)


# ── ADK LlmAgent ──────────────────────────────────────────────────────────────

briefing_agent = LlmAgent(
    name="briefing_agent",
    model=settings.GEMINI_MODEL,
    description=(
        "Composes and sends morning briefing emails summarising today's tasks, "
        "calendar events, and recent notes. Triggered by Cloud Scheduler daily."
    ),
    instruction="""You are the Briefing Composer for the Smart Daily Planner.

Your responsibilities:
1. Compose a structured morning digest of tasks, calendar events, and notes.
2. Send the digest via Gmail when triggered by the user or Cloud Scheduler.
3. Provide a preview of the briefing without sending when asked.

Guidelines:
- Always call compose_briefing before send_briefing_email to show the user
  what will be sent.
- If ENABLE_GMAIL_SEND is False, inform the user it is a dry-run.
- Keep the briefing positive and action-oriented.
- Highlight overdue or urgent tasks prominently.
- Format times in the user's local timezone (Asia/Kolkata IST by default).
""",
    tools=[
        FunctionTool(compose_briefing),
        FunctionTool(send_briefing_email),
        FunctionTool(get_briefing_preview),
    ],
)
