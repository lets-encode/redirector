# Let's Encode! — slug registry & redirector

Maps user-chosen campaign names under `https://letsenco.de/` to
system-generated campaign pages in the (separate) Let's Encode! campaign
application.

**This is not a URL shortener.** Destinations are never user-supplied: a
registered name always resolves to a campaign page under `${CAMPAIGN_APP_BASE}`,
and the stored value is the campaign's **forge + repo id** (supplied by the
campaign app when it registers the name, after it has created the repo — the
`forge` qualifies the id so ids from different forges never collide). The single
exception is admin-reserved names, where a staff member explicitly chooses the
target URL. There are no user accounts here, no campaign content, no contributor
state — all of that lives in the campaign app. This service owns exactly one
thing: the `name → (forge, repo_id)` mapping and the redirect logic around it.
The repo id is stable across repo renames and transfers; the app resolves it to
the repo's current owner/name in the background.

## Architecture rationale

* **Python + FastAPI, server-rendered Jinja templates.** Every page renders
  server-side. The one action that needs JavaScript is the landing page's
  *Create campaign* button, which probes `GET /<name>` and forwards to the
  campaign app (see [How the flows work](#how-the-flows-work)); `POST /register`
  remains as a scriptless registration API. The other script is a small
  progressive enhancement — the light/dark theme toggle — carried over from the
  Let's Encode! site so these pages match its look (shared `styles.css`, logo,
  and favicons are served from `app/static/` under `/assets/`). Four direct
  dependencies (`fastapi`, `uvicorn`, `jinja2`, `python-multipart`); the data
  layer is stdlib `sqlite3`.
* **SQLite, file-backed, WAL mode.** One table, a handful of writes per day,
  reads that are a single primary-key lookup. A database server would add an
  entire second service to operate for no benefit. Backup = copy one file.
* **Single process.** `uvicorn app.asgi:app` and that's the whole deployment.

## How the flows work

| Request | Situation | Result |
|---|---|---|
| `GET /` | — | `200` landing page; *Create campaign* probes `GET /<name>` client-side (see below) |
| `GET /<name>` | free | `404` + *“no campaign called this yet — start one?”* claim page linking to `${CAMPAIGN_APP_BASE}/?campaign=<name>` |
| `GET /<name>` | active | `302` to `${CAMPAIGN_APP_BASE}/campaign/<name>` (or the admin-set URL for reserved names) |
| `GET /<name>` | reserved | `403` |
| `GET /<name>` | malformed / percent-encoded | `400` |
| `GET /<name>` | tombstoned | `410` blocked page |
| `GET /api/slug/<name>` (from the campaign app) | any valid name | `200` JSON `{ name, status, forge, repo_id }` (`forge`/`repo_id` only when active); malformed → `400` |
| `POST /register` (from the campaign app) | name free | store `name → (forge, repo_id)`, `201` JSON `{ name, status: "active", forge, repo_id }` |
| `POST /register` | same name, same (forge, repo_id) | `200` (idempotent) |
| `POST /register` | name occupied by a different repo | `409` |
| `POST /register` | invalid / reserved name | `422` |

The `GET /<name>` status codes are deliberately distinct so the landing page's
probe can tell the states apart without reading a cross-origin body — only a
`404` means "free, go ahead".

**Landing page (`GET /`)** checks availability **live as the user types**
(debounced `GET /<name>` probes): a free name enables *Create campaign*, an
active one shows a "Go to this campaign →" link, and reserved/blocked/malformed
names show an inline reason. **Direct visits** to `letsenco.de/<name>` need no
UI: a free name's `404` page auto-forwards to the campaign app's setup page (via
a small script the fetch-probe never runs, so the status codes still work), an
active name `302`-redirects to the campaign, and reserved/malformed/blocked
render a friendly `403`/`400`/`410` page.

### Create flow (website ↔ campaign app)

The name is registered only *after* the campaign app has created the GitHub repo,
so the stored `repo_id` always exists. *Create campaign* on the landing page (and
*Start* on the direct-visit claim page) both:

1. **probe `GET /<name>`** — only `404` (free) proceeds; `302`/`403`/`400`/`410`
   show an inline message and stop;
2. **forward the browser** to `${CAMPAIGN_APP_BASE}/?campaign=<name>`.

**Campaign-app contract.** The "start a new campaign" page lives in the campaign
app, not here. It must:

* read the proposed name from the **`campaign` query parameter** and prefill it,
  keeping it editable — its handle validation must match the slug rules below so
  the created repo name is a valid slug;
* after it creates the GitHub repo, call **`POST https://letsenco.de/register`**
  with JSON `{ name, repo_id, forge }` and handle the responses: `201`/`200`
  (registered), `409` (name taken by a different repo since the probe — offer
  another name / the existing campaign), `422` (invalid or reserved name);
* resolve a name for its own routing via **`GET /api/slug/<name>`** →
  `(forge, repo_id)`, then reach the repo by id on that forge.

* **Campaign-app routes** are `${CAMPAIGN_APP_BASE}/campaign/<name>` (the console)
  and `${CAMPAIGN_APP_BASE}/?campaign=<name>` (start a campaign, name
  prefilled). They are constants at the top of `app/config.py` — adjust there if
  the campaign app's contract differs.
* **Stored value** is the campaign's **forge + numeric repo id**, supplied by the
  campaign app. The `forge` qualifies the id so ids from different forges (GitHub,
  GitLab, …) never collide. It is stable across renames/transfers; the app
  resolves it to the current owner/name on that forge.
* **Deleting is always tombstoning.** There is no hard delete; the row stays,
  keeping the name occupied, with `notes` recording why.

## Slug rules

Allowlist, not blocklist: `^[a-z0-9]([a-z0-9-]{1,38}[a-z0-9])?$`, length 3–40,
no leading/trailing/double hyphens, no percent-encoding, plus:

* **Reserved names** (`api`, `admin`, `assets`, `static`, `.well-known`,
  `robots.txt`, `favicon.ico`, and this service's own routes) — constant
  `RESERVED_NAMES` in `app/config.py`; keep it in sync with routes.

## Admin

Two operations, JSON over `/admin/*`:

| Request | Situation | Result |
|---|---|---|
| `GET /admin/slugs` | authorised | `200` JSON array of all rows |
| `POST /admin/slugs` | name free, valid URL | `201` reserved with the admin-set destination |
| `POST /admin/slugs` | bad name or non-http(s) URL | `422` |
| `POST /admin/slugs` | name already occupied | `409` |
| `DELETE /admin/slugs/<name>` | name exists | `200` tombstoned (row kept, name stays occupied) |
| `DELETE /admin/slugs/<name>` | name unknown | `404` |
| any `/admin/*` | missing/invalid token | `401` |
| any `/admin/*` | `ADMIN_TOKEN` unset | `503` (fail closed) |

```bash
# Reserve a name for an arbitrary URL (the one admin-supplied-destination case)
curl -X POST https://letsenco.de/admin/slugs \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "workshop", "destination_url": "https://www.mdw.ac.at/...", "notes": "2026 workshop"}'

# Tombstone a slug (name stays occupied, cannot be re-registered)
curl -X DELETE https://letsenco.de/admin/slugs/hostile-name \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"notes": "abusive name, reported 2026-07-06"}'

# List everything
curl -H "Authorization: Bearer $ADMIN_TOKEN" https://letsenco.de/admin/slugs
```

### Auth assumption — read this before deploying

This service deliberately has **no user accounts**. In production it must sit
behind the institution's reverse proxy, and the proxy must enforce
institutional auth (Shibboleth/OIDC/whatever mdw provides) **for everything
under `/admin/`** before requests reach this service. Example nginx sketch:

```nginx
location /admin/ {
    # institutional auth module goes here (e.g. auth_request / shib)
    proxy_pass http://127.0.0.1:8000;
}
location / {
    proxy_pass http://127.0.0.1:8000;
}
```

The `ADMIN_TOKEN` bearer check built into the service is a **dev/local
fallback and defence in depth**, not a production auth system: no rotation, no
audit trail, one shared secret. If `ADMIN_TOKEN` is unset, admin routes return
503 (fail closed). If the proxy forwards an `X-Remote-User` header, it is
recorded as `created_by` on admin actions.

## Running it

### Bare (dev or systemd)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # edit values
set -a; source .env; set +a
.venv/bin/uvicorn app.asgi:app --host 127.0.0.1 --port 8000
```

Tests: `.venv/bin/pip install pytest httpx && .venv/bin/python -m pytest`

### Docker

```bash
docker build -t lets-encode-redirector .
docker run -d --name letsencode \
  -p 127.0.0.1:8000:8000 \
  -v letsencode-data:/data \
  -e CAMPAIGN_APP_BASE=https://campaigns.example.org \
  -e ADMIN_TOKEN=change-me \
  lets-encode-redirector
```

Backups: the entire state is the SQLite file (`/data/slugs.db` in Docker) —
copy it while the service is idle, or use `sqlite3 slugs.db ".backup ..."`.

## Data model

One table, `slugs`:

| column | notes |
|---|---|
| `name` | PK — the slug |
| `campaign_id` | minted UUIDv4; NULL for admin-reserved rows |
| `destination_url` | NULL except admin-reserved custom targets |
| `status` | `active` / `reserved` / `tombstoned` |
| `created_at` | UTC ISO-8601 |
| `created_by` | NULL for public registrations; proxy user or `admin` for admin actions |
| `notes` | free text, e.g. tombstone reason |

Registration relies on the primary key for race safety: concurrent claims of
the same name resolve to exactly one winner, and the loser gets the normal
collision branch for their flow.

## Explicitly out of scope

User accounts/OAuth, contributor management, campaign content, analytics,
multi-domain support, link expiry, email. **Rate limiting** is also not
implemented here — if registration abuse becomes a problem, apply
`limit_req` (or equivalent) at the reverse proxy rather than adding state to
this service.
