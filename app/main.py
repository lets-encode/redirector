"""Let's Encode! slug registry & redirector.

Owns exactly one thing: the mapping from user-chosen campaign names under
letsenco.de/ to the GitHub repo id of the campaign in the (separate) campaign
app. The repo id is the stable, rename/transfer-proof reference — the campaign
name stays in the app URL, and the app resolves the id to the repo's current
owner/name for everything in the background. No accounts, no campaign content,
no analytics — see README.md.

The landing page does not register on submit. "Create campaign" probes
GET /{name} over AJAX and, when the name is free, forwards the browser to the
campaign app's "start a new campaign" page (${CAMPAIGN_APP_BASE}/c?slug=<name>),
where the name stays editable. GET /{name} uses distinct status codes the probe
can tell apart (free vs claimed vs reserved vs malformed).

A name is taken in two steps, because the repo id it is stored against does not
exist until the campaign app has created the repo — and asking for the name only
then would leave the whole setup exposed to losing it at the last moment:

    POST /claim     holds the name the moment the organiser picks it, against a
                    claim token, for CLAIM_TTL_MINUTES. No repo id needed.
    POST /register  presents that token once the repo exists and turns the claim
                    into the live campaign.

So a claim is a lease on a name. The lease running out does not revoke the
token — it only lets someone else take the name — so a long setup loses the name
only if somebody actually wanted it. A claim nobody promotes occupies nothing
once it has run out; it is dropped on the next write and read as free before
that, so no sweeper is needed.

Route map (public):
    GET  /                → landing page; JS probes GET /{name}, forwards if free
    POST /claim           → { name } → holds it: 201 { claim_token, expires_at },
                            409 if occupied, 422 on an invalid name.
    DELETE /claim/{name}  → { claim_token } → gives the name back (the organiser
                            renamed the campaign before its repo existed).
    POST /register        → JSON API the campaign app calls after creating the
                            repo: { name, repo_id, claim_token } → stores the
                            mapping. 201 on success, 409 on collision, 422 on an
                            invalid name.
    GET  /api/slug/{name} → JSON resolver for the campaign app:
                            { name, status, repo_id } (repo_id only when active).
    GET  /{name}          → live slug: 302 to the campaign page (or admin-set URL)
                            free name: 404 + "claim this name?" page
                            claimed name: 409 · reserved name: 403
                            malformed name: 400 · tombstoned (blocked): 410

Route map (admin — see README for the auth assumption):
    GET    /admin/slugs         → list everything (JSON)
    POST   /admin/slugs         → reserve a name for an arbitrary URL
    DELETE /admin/slugs/{name}  → tombstone a name (stays occupied)
"""

from __future__ import annotations

import secrets
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import validation
from .config import CLAIM_TTL_MINUTES, Settings
from .db import SlugExists, Store, in_minutes_iso, now_iso

_HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))


def _path_is_percent_encoded(request: Request) -> bool:
    """The spec rejects percent-encoded slugs outright; Starlette hands us the
    decoded path, so peek at the raw bytes."""
    return b"%" in request.scope.get("raw_path", b"")


def _occupying(row) -> bool:
    """Whether a row still occupies its name. A claim that has run out does not:
    reads report the name as free, and the next write drops the row."""
    return row is not None and not (
        row["status"] == "pending" and row["expires_at"] < now_iso()
    )


class ClaimBody(BaseModel):
    name: str


class ReleaseBody(BaseModel):
    claim_token: str


class RegisterBody(BaseModel):
    name: str
    repo_id: int
    # Which forge repo_id belongs to, so ids from different forges never collide.
    # Defaults to github for existing single-forge callers.
    forge: str = "github"
    # The token POST /claim issued for this name. Absent only for a name that was
    # never claimed, which must then still be free.
    claim_token: str | None = None


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

    # Styling assets (CSS, logo, favicons) shared with the Let's Encode! site so
    # these pages look like part of it. "assets" is a reserved name (see config),
    # and "/assets/…" always carries a slash so it never matches the single-
    # segment /{name} slug route.
    app.mount("/assets", StaticFiles(directory=str(_HERE / "static")), name="assets")

    # ---------------------------------------------------------------- public

    def _render_landing(request: Request, *, status_code: int = 200, **context):
        """Render the landing page. The campaign app's base is always injected so
        the page's JS can build the forward URL (${base}/c?slug=<name>)."""
        context.setdefault("campaign_app_base", settings.campaign_app_base)
        return templates.TemplateResponse(
            request, "landing.html", context, status_code=status_code
        )

    @app.get("/", response_class=HTMLResponse)
    def landing(request: Request):
        return _render_landing(request)

    @app.post("/claim", status_code=201)
    def claim(body: ClaimBody):
        """Hold a name for the caller before it has a repo to register against,
        so the rest of a campaign's setup cannot lose it. The returned token is
        the right to activate the name later (POST /register) or to give it back
        (DELETE /claim/{name}). Occupied name → 409, invalid name → 422."""
        name = body.name.strip()
        error = validation.registration_error(name)
        if error is not None:
            raise HTTPException(status_code=422, detail=error)
        store.drop_expired_claim(name)
        claim_token = secrets.token_urlsafe(24)
        expires_at = in_minutes_iso(CLAIM_TTL_MINUTES)
        try:
            store.create_pending(name, claim_token, expires_at)
        except SlugExists:
            raise HTTPException(status_code=409, detail=f"'{name}' is already taken")
        return JSONResponse(
            {
                "name": name,
                "status": "pending",
                "claim_token": claim_token,
                "expires_at": expires_at,
            },
            status_code=201,
        )

    @app.delete("/claim/{name}")
    def release(name: str, body: ReleaseBody):
        """Give a claimed name back, so a campaign renamed before its repo exists
        does not leave its first name held. 404 if the name is not claimed under
        this token."""
        if not store.release(name, body.claim_token):
            raise HTTPException(status_code=404, detail=f"'{name}' is not claimed by you")
        return {"name": name, "status": "free"}

    @app.post("/register")
    def register(body: RegisterBody):
        """Registration API the campaign app calls AFTER creating the repo,
        passing the chosen name, the repo's numeric id, its forge (the forge
        qualifies the id so different forges' ids never collide) and the token the
        name was claimed under. Activating an own claim works even after the claim
        has run out, as long as nobody else has taken the name since. Idempotent:
        a repeat with the same (forge, repo_id) succeeds (200). A different repo on
        an occupied name is a genuine collision (409). Invalid name → 422."""
        name = body.name.strip()
        error = validation.registration_error(name)
        if error is not None:
            raise HTTPException(status_code=422, detail=error)
        active = {"name": name, "status": "active", "forge": body.forge, "repo_id": body.repo_id}
        if store.activate(name, body.forge, body.repo_id, body.claim_token):
            return JSONResponse(active, status_code=201)
        # No claim of ours to activate: the name must be free — either never
        # claimed, or claimed by someone who let it run out.
        store.drop_expired_claim(name)
        try:
            store.create_active(name, body.forge, body.repo_id)
        except SlugExists:
            row = store.get(name)
            if (
                row is not None
                and row["status"] == "active"
                and row["forge"] == body.forge
                and row["repo_id"] == body.repo_id
            ):
                return JSONResponse(active)
            raise HTTPException(status_code=409, detail=f"'{name}' is already taken")
        return JSONResponse(active, status_code=201)

    @app.get("/api/slug/{name}")
    def api_slug(request: Request, name: str):
        """JSON resolver for the campaign app: report a name's state and, when it
        is a live campaign, its forge + repo id. Malformed names are 400; every
        other state (free / pending / active / reserved / tombstoned) is 200 with
        a `status`."""
        if _path_is_percent_encoded(request) or validation.syntax_error(name):
            raise HTTPException(status_code=400, detail=validation.ERR_SYNTAX)
        row = store.get(name)
        if not _occupying(row):
            status = "reserved" if validation.registration_error(name) else "free"
            return JSONResponse({"name": name, "status": status, "forge": None, "repo_id": None})
        active = row["status"] == "active"
        return JSONResponse(
            {
                "name": name,
                "status": row["status"],
                "forge": row["forge"] if active else None,
                "repo_id": row["repo_id"] if active else None,
            }
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
        """Resolve a slug for a browser. Renders a friendly page at a distinct
        status code for each state — 302 live, 404 free (auto-forwards to the
        campaign app's setup page via claim.html's script), 409 claimed by a setup
        in progress, 403 reserved, 400 malformed, 410 tombstoned (blocked). The
        status codes are also what the landing page's fetch probe reads (it never
        runs the page scripts), so a direct visit forwards while the probe still
        tells the states apart."""
        if _path_is_percent_encoded(request) or validation.syntax_error(name):
            return templates.TemplateResponse(
                request, "notice.html",
                {"heading": "That name won't work",
                 "message": "Campaign names must be 3–40 characters: lowercase letters, "
                            "digits, and single internal hyphens (no leading, trailing, or "
                            "double hyphens)."},
                status_code=400,
            )
        row = store.get(name)
        if not _occupying(row):
            if validation.registration_error(name):  # syntax already passed → reserved
                return templates.TemplateResponse(
                    request, "notice.html",
                    {"heading": "That name is reserved",
                     "message": f"“{name}” is reserved and can't be used for a campaign. "
                                "Please choose another name."},
                    status_code=403,
                )
            return templates.TemplateResponse(
                request, "claim.html",
                {"name": name, "campaign_start_url": settings.campaign_start_url(name)},
                status_code=404,
            )
        if row["status"] == "tombstoned":
            return templates.TemplateResponse(
                request, "gone.html", {"name": name}, status_code=410
            )
        if row["status"] == "pending":
            # Held by a setup in progress: there is no campaign to send anyone to
            # yet, and the name is not free either.
            return templates.TemplateResponse(
                request, "notice.html",
                {"heading": "That name is being set up",
                 "message": f"Someone is setting up a campaign called “{name}” right now. "
                            "If it isn't finished, the name becomes free again later."},
                status_code=409,
            )
        destination = row["destination_url"] or settings.campaign_page_url(name)
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
        store.drop_expired_claim(name)
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
