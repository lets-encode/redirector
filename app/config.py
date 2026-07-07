"""Configuration for the Let's Encode! slug registry & redirector.

Everything comes from environment variables (see .env.example), except the
reserved-name list, which is a code constant because it must stay in lockstep
with the routes this service itself owns.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Names that can never be registered as campaign slugs because they are (or
# may become) routes of this service itself, or well-known root paths.
# Keep in sync with the routes in app/main.py.
RESERVED_NAMES: frozenset[str] = frozenset(
    {
        "api",
        "admin",
        "assets",
        "static",
        ".well-known",
        "robots.txt",
        "favicon.ico",
        # own routes / likely future own routes
        "register",
        "claim",
        "healthz",
        "health",
        "c",
        "new",
        "join",
        "www",
        "index",
    }
)

# Routes in the campaign app (relative to CAMPAIGN_APP_BASE). These are an
# agreed contract with the campaign app, not user-configurable surface.
CAMPAIGN_PAGE_PATH = "/c/{campaign_id}"
CAMPAIGN_CREATE_PATH = "/c/{campaign_id}/new"
CAMPAIGN_JOIN_PATH = "/c/{campaign_id}/join"


@dataclass(frozen=True)
class Settings:
    campaign_app_base: str
    db_path: str
    admin_token: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        base = os.environ.get("CAMPAIGN_APP_BASE")
        if not base:
            raise RuntimeError("CAMPAIGN_APP_BASE environment variable is required")
        return cls(
            campaign_app_base=base.rstrip("/"),
            db_path=os.environ.get("DB_PATH", "./data/slugs.db"),
            admin_token=os.environ.get("ADMIN_TOKEN") or None,
        )

    def campaign_page_url(self, campaign_id: str) -> str:
        return self.campaign_app_base + CAMPAIGN_PAGE_PATH.format(campaign_id=campaign_id)

    def campaign_create_url(self, campaign_id: str) -> str:
        return self.campaign_app_base + CAMPAIGN_CREATE_PATH.format(campaign_id=campaign_id)

    def campaign_join_url(self, campaign_id: str) -> str:
        return self.campaign_app_base + CAMPAIGN_JOIN_PATH.format(campaign_id=campaign_id)
