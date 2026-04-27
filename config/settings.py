# === config/settings.py ===
"""
Application settings loaded from environment variables.
All secrets come from environment — no hardcoded credentials.
Local: set via .env or export.
Cloud Run: injected via --set-env-vars / Secret Manager.
"""

import pytz
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration for Smart Daily Planner.

    All fields are read from environment variables. Validation runs at
    import time so misconfiguration is caught before any request arrives.
    """

    # ── Google AI ────────────────────────────────────────────────────────────
    GOOGLE_API_KEY: str = Field(default="", description="Google AI Studio API key (optional when using Vertex AI).")
    GEMINI_MODEL: str = Field(
        default="gemini-2.5-flash",
        description="Gemini model ID used by all ADK agents.",
    )

    # ── GCP Project ──────────────────────────────────────────────────────────
    GCP_PROJECT_ID: str = Field(..., description="GCP project ID for Firestore / Calendar.")
    GOOGLE_CLOUD_LOCATION: str = Field(default="us-central1", description="GCP region for Vertex AI.")

    # ── Calendar / Gmail ─────────────────────────────────────────────────────
    GOOGLE_CALENDAR_ID: str = Field(
        default="primary",
        description="Google Calendar ID to manage (default: primary).",
    )
    ADDITIONAL_CALENDAR_IDS: str = Field(
        default="",
        description="Comma-separated extra calendar IDs to aggregate (e.g. work@group.calendar.google.com,personal@gmail.com).",
    )
    GMAIL_USER_EMAIL: str = Field(
        ..., description="Gmail address used to send briefing emails."
    )
    BRIEFING_RECIPIENT_EMAIL: str = Field(
        default="",
        description="Briefing email recipient. Defaults to GMAIL_USER_EMAIL if empty.",
    )

    # ── Multi-Email Accounts ──────────────────────────────────────────────────
    GMAIL_APP_PASSWORD: str = Field(
        default="",
        description="Gmail app password (myaccount.google.com/apppasswords).",
    )
    GMAIL2_EMAIL: str = Field(
        default="",
        description="Second Gmail address for sending emails (optional).",
    )
    GMAIL2_APP_PASSWORD: str = Field(
        default="",
        description="App password for the second Gmail account (myaccount.google.com/apppasswords).",
    )
    YAHOO_EMAIL: str = Field(
        default="",
        description="Yahoo Mail address for a second send-from account (optional).",
    )
    YAHOO_APP_PASSWORD: str = Field(
        default="",
        description="Yahoo Mail app password (account.yahoo.com/security → App passwords).",
    )

    # ── Timezone ─────────────────────────────────────────────────────────────
    DEFAULT_TIMEZONE: str = Field(
        default="Asia/Kolkata",
        description="IANA timezone for all datetime operations.",
    )

    # ── Server ───────────────────────────────────────────────────────────────
    PORT: int = Field(default=8080, description="FastAPI server port.")
    MCP_PORT: int = Field(default=8081, description="FastMCP SSE server port.")

    # ── Firestore ─────────────────────────────────────────────────────────────
    FIRESTORE_DATABASE: str = Field(
        default="(default)", description="Firestore database name."
    )

    # ── Feature flags ─────────────────────────────────────────────────────────
    ENABLE_GMAIL_SEND: bool = Field(
        default=True,
        description="Set False to skip actual Gmail send (dry-run mode).",
    )

    # ── Authentication ────────────────────────────────────────────────────────
    AUTH_ENABLED: bool = Field(
        default=False,
        description="Enable Google OAuth2 login. Set True in production.",
    )
    JWT_SECRET_KEY: str = Field(
        default="change-me-in-production-use-openssl-rand-hex-32",
        description="Secret key for signing JWT session tokens. Must be changed in production.",
    )
    JWT_ALGORITHM: str = Field(default="HS256")
    JWT_EXPIRE_HOURS: int = Field(default=24, description="JWT session lifetime in hours.")

    # Google OAuth2 Web Application credentials (from GCP Console → APIs & Services → Credentials)
    OAUTH_CLIENT_ID: str = Field(default="", description="Google OAuth2 client ID (web application type).")
    OAUTH_CLIENT_SECRET: str = Field(default="", description="Google OAuth2 client secret.")

    # ── CORS & App URL ────────────────────────────────────────────────────────
    APP_URL: str = Field(
        default="http://localhost:8080",
        description="Public base URL of the app (used for OAuth redirect URI and CORS).",
    )
    ALLOWED_ORIGINS: str = Field(
        default="http://localhost:8080,http://localhost:3000",
        description="Comma-separated list of allowed CORS origins.",
    )

    # ── Observability ─────────────────────────────────────────────────────────
    LOG_LEVEL: str = Field(default="INFO", description="Logging level: DEBUG, INFO, WARNING, ERROR.")
    SENTRY_DSN: str = Field(default="", description="Sentry DSN for error tracking (optional).")

    # ── Cloud Scheduler (optional runtime management) ────────────────────────
    ENABLE_CLOUD_SCHEDULER_MANAGEMENT: bool = Field(
        default=False,
        description="Allow API endpoints to create/update Cloud Scheduler jobs.",
    )
    CLOUD_SCHEDULER_REGION: str = Field(
        default="us-central1",
        description="Region for Cloud Scheduler jobs.",
    )
    SCHEDULER_SHARED_SECRET: str = Field(
        default="",
        description="Optional shared secret for /briefing/scheduled endpoint protection.",
    )
    MCP_SERVER_URL: str = Field(
        default="",
        description="Optional deployed MCP SSE endpoint URL for connectivity checks.",
    )

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("DEFAULT_TIMEZONE")
    @classmethod
    def validate_timezone(cls, v: str) -> str:
        """Ensure the timezone string is a valid IANA zone."""
        if v not in pytz.all_timezones_set:
            raise ValueError(
                f"Invalid timezone '{v}'. Use an IANA zone, e.g. 'Asia/Kolkata'."
            )
        return v

    @field_validator("BRIEFING_RECIPIENT_EMAIL", mode="before")
    @classmethod
    def default_recipient(cls, v: str, info) -> str:
        """Fall back to GMAIL_USER_EMAIL when recipient is not set."""
        if not v:
            # Access sibling field via model_fields_set / values
            return info.data.get("GMAIL_USER_EMAIL", "")
        return v


# Singleton — imported everywhere
settings = Settings()

# Expose timezone object for convenience
LOCAL_TZ = pytz.timezone(settings.DEFAULT_TIMEZONE)
