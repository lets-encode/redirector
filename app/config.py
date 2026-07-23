"""Configuration for the Let's Encode! slug registry & redirector.

Everything comes from environment variables (see .env.example), except the
reserved-name list, which is a code constant because it must stay in lockstep
with the routes this service itself owns.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlencode

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
# agreed contract with the campaign app, not user-configurable surface. The
# campaign name is kept in the app URL; the app resolves it to the repo's stable
# numeric id (which this service stores) for everything in the background.
CAMPAIGN_PAGE_PATH = "/campaign/{name}"
# The "start a new campaign" page: the user-chosen name is passed as a query
# param (?slug=<name>) and prefilled there, where it stays editable. The name is
# only registered later, when the campaign app calls POST /register here with
# the created repo's numeric id. The campaign app serves this at /c and opens
# its create form prefilled when the query param is present.
CAMPAIGN_START_PATH = "/c"


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

    def campaign_page_url(self, name: str) -> str:
        return self.campaign_app_base + CAMPAIGN_PAGE_PATH.format(name=name)

    def campaign_start_url(self, name: str) -> str:
        return f"{self.campaign_app_base}{CAMPAIGN_START_PATH}?{urlencode({'slug': name})}"
