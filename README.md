# This repository is DEPRECATED and archived
The code has now migrated into <https://github.com/lets-encode/lets-encode>
 
---

# Let's Encode! — slug registry & redirector

Maps user-chosen campaign names under `https://letsenco.de/` to
system-generated campaign pages in the (separate) Let's Encode! campaign
application.

**This is not a URL shortener.** Destinations are never user-supplied: a
registered name always points at a campaign ID this service minted itself, and
redirects go only to `${CAMPAIGN_APP_BASE}`. The single exception is
admin-reserved names, where a staff member explicitly chooses the target URL.
There are no user accounts here, no campaign content, no contributor state —
all of that lives in the campaign app. This service owns exactly one thing:
the `name → campaign_id` mapping and the redirect/branch logic around it.

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
| `GET /<name>` | free | `404` + *“no campaign called this yet — start one?”* claim page linking to `${CAMPAIGN_APP_BASE}/c?slug=<name>` |
| `GET /<name>` | active | `302` to `${CAMPAIGN_APP_BASE}/c/<id>` (or the admin-set URL for reserved names) |
| `GET /<name>` | reserved | `403` |
| `GET /<name>` | malformed / percent-encoded | `400` |
| `GET /<name>` | tombstoned | `410` blocked page |
| `POST /<name>/claim` (from the campaign app) | name free | mint campaign ID, store mapping, `303` to `${CAMPAIGN_APP_BASE}/c/<id>/new` |
| `POST /<name>/claim` | name active | `409` **interstitial**: *“already a campaign — join as a contributor?”* linking to `${CAMPAIGN_APP_BASE}/c/<id>/join` |
| `POST /<name>/claim` | name tombstoned | `410` blocked page |
| `POST /<name>/claim` | reserved / malformed | `404` |
| `POST /register` | name free | mint campaign ID, store mapping, `303` to `${CAMPAIGN_APP_BASE}/c/<id>/new` |
| `POST /register` | name occupied (any status) | `409`, re-render form: *“already taken — choose another name”* |

The `GET /<name>` status codes are deliberately distinct so the landing page's
probe can tell the states apart without reading a cross-origin body — only a
`404` means "free, go ahead". `POST /register` is the original registration API;
the website no longer uses it (it now goes through the two-step create below),
but it remains a valid endpoint.

### Two-step create (website ↔ campaign app)

The website never registers a name on the first click. *Create campaign* on the
landing page (and *Start* on the direct-visit claim page) both:

1. **probe `GET /<name>`** — only `404` (free) proceeds; `302`/`403`/`400`/`410`
   show an inline message and stop;
2. **forward the browser** to `${CAMPAIGN_APP_BASE}/c?slug=<name>`.

**Campaign-app contract.** That "start a new campaign" page lives in the
campaign app, not here. It must:

* read the proposed name from the **`slug` query parameter** and prefill it,
  keeping it editable — any further validation there is the campaign app's
  concern, not this service's;
* when the user confirms, call **`POST https://letsenco.de/<name>/claim`** (with
  the possibly-edited name) — the only call that actually creates the redirect
  here — and handle its responses: `303` (created; `Location` is the
  `/c/<id>/new` route), `409` (name taken since the probe — active names carry a
  join link, reserved names do not), `410` (name blocked), `404` (name reserved
  or malformed).

* **Campaign-app routes** are assumed to be `/c/<id>` (page), `/c/<id>/new`
  (creation/landing after claiming), `/c/<id>/join` (contributor join), and
  `/c?slug=<name>` (start a new campaign, name prefilled). They are constants at
  the top of `app/config.py` — adjust there if the campaign app's contract
  differs.
* **Campaign IDs** are UUIDv4 — opaque, unguessable, no coordination needed.
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
