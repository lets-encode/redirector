"""Let's Encode! slug registry & redirector.

Owns exactly one thing: the mapping from user-chosen campaign names under
letsenco.de/ to system-generated campaign IDs in the (separate) campaign app.
No accounts, no campaign content, no analytics — see README.md.

Route map (public):
    GET  /                → landing page with the name form
    POST /register        → landing-page flow; collision = "choose another name"
    GET  /{name}          → active slug: 302 to campaign app (or admin-set URL)
                            tombstoned: 410 · free name: "claim this name?" page
    POST /{name}/claim    → direct-URL flow; collision = join-campaign interstitial

Route map (admin — see README for the auth assumption):
    GET    /admin/slugs         → list everything (JSON)
    POST   /admin/slugs         → reserve a name for an arbitrary URL
    DELETE /admin/slugs/{name}  → tombstone a name (stays occupied)
"""

from __future__ import annotations

import secrets
import uuid
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import validation
from .config import Settings
from .db import SlugExists, Store

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _mint_campaign_id() -> str:
    return str(uuid.uuid4())


def _path_is_percent_encoded(request: Request) -> bool:
    """The spec rejects percent-encoded slugs outright; Starlette hands us the
    decoded path, so peek at the raw bytes."""
    return b"%" in request.scope.get("raw_path", b"")


class ReserveBody(BaseModel):
    name: str
    destination_url: str
    notes: str | None = None


class DeleteBody(BaseModel):
    notes: str | None = None


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="Let's Encode! redirector", docs_url=None, redoc_url=None)
    store = Store(settings.db_path)
    app.state.settings = settings
    app.state.store = store

    # ---------------------------------------------------------------- public

    @app.get("/", response_class=HTMLResponse)
    def landing(request: Request):
        return templates.TemplateResponse(request, "landing.html")

    @app.post("/register", response_class=HTMLResponse)
    def register(request: Request, name: str = Form("")):
        """Landing-page flow: on collision, ask the user to choose another name."""
        name = name.strip()
        error = validation.registration_error(name)
        if error is None and store.get(name) is not None:
            error = f"“{name}” is already taken — please choose another name."
        if error is not None:
            return templates.TemplateResponse(
                request, "landing.html",
                {"error": error, "name": name}, status_code=409,
            )
        campaign_id = _mint_campaign_id()
        try:
            store.create_active(name, campaign_id)
        except SlugExists:  # lost a race since the SELECT above
            return templates.TemplateResponse(
                request, "landing.html",
                {"error": f"“{name}” is already taken — please choose another name.",
                 "name": name},
                status_code=409,
            )
        return RedirectResponse(settings.campaign_create_url(campaign_id), status_code=303)

    @app.post("/{name}/claim", response_class=HTMLResponse)
    def claim(request: Request, name: str):
        """Direct-URL flow: on collision, offer to join the existing campaign."""
        if _path_is_percent_encoded(request) or validation.registration_error(name):
            raise HTTPException(status_code=404)
        campaign_id = _mint_campaign_id()
        try:
            store.create_active(name, campaign_id)
        except SlugExists:
            return _existing_slug_response(request, name)
        return RedirectResponse(settings.campaign_create_url(campaign_id), status_code=303)

    def _existing_slug_response(request: Request, name: str):
        row = store.get(name)
        if row is None:  # deleted between INSERT failure and re-read; punt
            raise HTTPException(status_code=404)
        if row["status"] == "active":
            return templates.TemplateResponse(
                request, "join.html",
                {"name": name,
                 "join_url": settings.campaign_join_url(row["campaign_id"])},
                status_code=409,
            )
        if row["status"] == "tombstoned":
            return templates.TemplateResponse(
                request, "gone.html", {"name": name}, status_code=410
            )
        # reserved: occupied, but there is no campaign to join
        return templates.TemplateResponse(
            request, "landing.html",
            {"error": f"“{name}” is not available — please choose another name.",
             "name": name},
            status_code=409,
        )

    @app.get("/robots.txt", response_class=PlainTextResponse)
    def robots():
        return "User-agent: *\nDisallow: /admin/\n"

    @app.get("/favicon.ico")
    def favicon():
        raise HTTPException(status_code=404)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/{name}", response_class=HTMLResponse)
    def resolve(request: Request, name: str):
        """Resolve a slug: 302 for live ones, 410 for tombstones, and a
        'claim this name?' offer when the name is still free."""
        if _path_is_percent_encoded(request) or validation.syntax_error(name):
            raise HTTPException(status_code=404)
        row = store.get(name)
        if row is None:
            if validation.registration_error(name):
                raise HTTPException(status_code=404)  # reserved: never claimable
            return templates.TemplateResponse(
                request, "claim.html", {"name": name}, status_code=404
            )
        if row["status"] == "tombstoned":
            return templates.TemplateResponse(
                request, "gone.html", {"name": name}, status_code=410
            )
        destination = row["destination_url"] or settings.campaign_page_url(row["campaign_id"])
        return RedirectResponse(destination, status_code=302)

    # ----------------------------------------------------------------- admin

    def require_admin(request: Request) -> str:
        """Dev/fallback gate. In production the reverse proxy must already have
        authenticated anything under /admin/* (see README); this token is
        defence in depth. With no ADMIN_TOKEN configured, admin is disabled."""
        if settings.admin_token is None:
            raise HTTPException(status_code=503, detail="admin interface disabled")
        auth = request.headers.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(
            token.strip(), settings.admin_token
        ):
            raise HTTPException(status_code=401, detail="invalid admin token")
        return request.headers.get("x-remote-user", "admin")

    admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])

    @admin.get("/slugs")
    def admin_list():
        return JSONResponse([dict(row) for row in store.list_all()])

    @admin.post("/slugs", status_code=201)
    def admin_reserve(body: ReserveBody, actor: str = Depends(require_admin)):
        """Reserve a name pointing at an admin-supplied URL — the one case
        where the destination is not system-generated. Syntax and reserved-path
        rules still apply."""
        name = body.name.strip()
        if validation.syntax_error(name) or name in validation.RESERVED_NAMES:
            raise HTTPException(status_code=422, detail=validation.ERR_SYNTAX
                                if validation.syntax_error(name) else validation.ERR_RESERVED)
        parsed = urlparse(body.destination_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise HTTPException(status_code=422, detail="destination_url must be an absolute http(s) URL")
        try:
            store.create_reserved(name, body.destination_url, actor, body.notes)
        except SlugExists:
            raise HTTPException(status_code=409, detail=f"'{name}' is already occupied")
        return {"name": name, "status": "reserved", "destination_url": body.destination_url}

    @admin.delete("/slugs/{name}")
    def admin_delete(name: str, body: DeleteBody | None = None):
        """Tombstone a slug. The row is kept so the name cannot be re-registered."""
        if not store.tombstone(name, body.notes if body else None):
            raise HTTPException(status_code=404, detail=f"'{name}' does not exist")
        return {"name": name, "status": "tombstoned"}

    app.include_router(admin)
    return app
