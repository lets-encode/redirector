import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

BASE = "https://campaigns.example.org"
TOKEN = "test-admin-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        campaign_app_base=BASE,
        db_path=str(tmp_path / "slugs.db"),
        admin_token=TOKEN,
    )
    return TestClient(create_app(settings), follow_redirects=False)


def register(client, name, repo_id=12345, forge="github", claim_token=None):
    """The campaign app registers a name against a created repo's (forge, id)."""
    return client.post(
        "/register",
        json={"name": name, "repo_id": repo_id, "forge": forge, "claim_token": claim_token},
    )


def claim(client, name):
    """The campaign app holds a name before the repo it will belong to exists."""
    return client.post("/claim", json={"name": name})


def expire_claim(client, name):
    """Backdate a claim, standing in for CLAIM_TTL_MINUTES passing."""
    with sqlite3.connect(client.app.state.store.db_path) as conn:
        conn.execute(
            "UPDATE slugs SET expires_at = '2020-01-01T00:00:00+00:00' WHERE name = ?",
            (name,),
        )


# ------------------------------------------------------------- happy path

def test_register_then_resolve(client):
    r = register(client, "lute-tablature", repo_id=987)
    assert r.status_code == 201
    assert r.json() == {"name": "lute-tablature", "status": "active", "forge": "github", "repo_id": 987}

    # The live name resolves to the campaign page — the NAME, not the id, is in
    # the app URL; the app resolves the id in the background.
    r = client.get("/lute-tablature")
    assert r.status_code == 302
    assert r.headers["location"] == f"{BASE}/campaign/lute-tablature"


def test_register_is_idempotent_for_same_repo(client):
    assert register(client, "same-name", repo_id=42).status_code == 201
    # A retry with the same repo id succeeds (200), not a collision.
    r = register(client, "same-name", repo_id=42)
    assert r.status_code == 200
    assert r.json()["repo_id"] == 42


def test_register_collision_different_repo_is_409(client):
    assert register(client, "hot-name", repo_id=1).status_code == 201
    r = register(client, "hot-name", repo_id=2)
    assert r.status_code == 409
    assert "already taken" in r.json()["detail"]


def test_free_name_page_forwards_to_create_form(client):
    # A direct visit to a free name auto-forwards to the campaign app's create
    # form (name still editable there) — the target is in data-forward, which the
    # page's script follows. The 404 status still lets the landing probe see "free".
    r = client.get("/fresh-name")
    assert r.status_code == 404
    assert "no campaign called" in r.text
    assert f'data-forward="{BASE}/c?slug=fresh-name"' in r.text
    assert "/fresh-name/claim" not in r.text  # no browser-side claim endpoint


def test_malformed_name_page_is_friendly_400(client):
    r = client.get("/Bad--Name")
    assert r.status_code == 400
    assert "3–40 characters" in r.text  # the friendly rule, not a JSON blob
    assert "data-forward" not in r.text


def test_landing_page_probes_and_forwards(client):
    r = client.get("/")
    assert r.status_code == 200
    # The website drives the flow client-side; it never posts to /register.
    assert 'action="/register"' not in r.text
    assert 'id="create-form"' in r.text
    assert f'data-campaign-base="{BASE}"' in r.text


# ------------------------------------------------------------------- claims

def test_claim_then_register_with_token(client):
    r = claim(client, "held-name")
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "pending"
    assert body["claim_token"]

    r = register(client, "held-name", repo_id=77, claim_token=body["claim_token"])
    assert r.status_code == 201
    assert client.get("/held-name").status_code == 302


def test_claimed_name_is_occupied_but_not_a_campaign(client):
    claim(client, "mid-setup")
    # No campaign to send anyone to yet, and not free either.
    r = client.get("/mid-setup")
    assert r.status_code == 409
    assert "being set up" in r.text
    assert "data-forward" not in r.text  # never offered for the taking
    assert client.get("/api/slug/mid-setup").json() == {
        "name": "mid-setup", "status": "pending", "forge": None, "repo_id": None
    }
    # Nobody else can claim or register it while the claim stands.
    assert claim(client, "mid-setup").status_code == 409
    assert register(client, "mid-setup", repo_id=2).status_code == 409


def test_register_needs_the_claims_own_token(client):
    token = claim(client, "someones-name").json()["claim_token"]
    assert register(client, "someones-name", repo_id=5, claim_token="not-it").status_code == 409
    assert register(client, "someones-name", repo_id=5, claim_token=token).status_code == 201


def test_release_frees_a_claimed_name(client):
    token = claim(client, "second-thoughts").json()["claim_token"]
    # The wrong token gives nothing away.
    r = client.request("DELETE", "/claim/second-thoughts", json={"claim_token": "nope"})
    assert r.status_code == 404
    assert client.get("/second-thoughts").status_code == 409

    r = client.request("DELETE", "/claim/second-thoughts", json={"claim_token": token})
    assert r.status_code == 200
    assert client.get("/second-thoughts").status_code == 404
    assert claim(client, "second-thoughts").status_code == 201


def test_expired_claim_occupies_nothing(client):
    claim(client, "abandoned")
    expire_claim(client, "abandoned")
    # Reported free, and free to take.
    assert client.get("/abandoned").status_code == 404
    assert client.get("/api/slug/abandoned").json()["status"] == "free"
    assert claim(client, "abandoned").status_code == 201


def test_running_out_does_not_revoke_the_claims_own_token(client):
    # A slow setup keeps the right to its name: the claim running out only lets
    # someone else take it, so an unclaimed name still activates afterwards.
    token = claim(client, "slow-setup").json()["claim_token"]
    expire_claim(client, "slow-setup")
    r = register(client, "slow-setup", repo_id=31, claim_token=token)
    assert r.status_code == 201
    assert client.get("/slow-setup").status_code == 302


def test_taken_over_claim_can_no_longer_activate(client):
    token = claim(client, "contested").json()["claim_token"]
    expire_claim(client, "contested")
    assert claim(client, "contested").status_code == 201  # someone else takes it
    r = register(client, "contested", repo_id=9, claim_token=token)
    assert r.status_code == 409


def test_claim_refuses_occupied_and_invalid_names(client):
    register(client, "live-already", repo_id=3)
    assert claim(client, "live-already").status_code == 409
    assert claim(client, "Bad--Name").status_code == 422
    assert claim(client, "admin").status_code == 422


def test_register_without_a_claim_still_works_on_a_free_name(client):
    # The registry does not require a name to have been claimed first.
    assert register(client, "unclaimed-name", repo_id=64).status_code == 201


# ------------------------------------------------- api resolver for the app

def test_api_slug_reports_states(client):
    assert client.get("/api/slug/nope-yet").json() == {
        "name": "nope-yet", "status": "free", "forge": None, "repo_id": None
    }
    register(client, "live-one", repo_id=555)
    assert client.get("/api/slug/live-one").json() == {
        "name": "live-one", "status": "active", "forge": "github", "repo_id": 555
    }
    assert client.get("/api/slug/admin").json()["status"] == "reserved"
    assert client.get("/api/slug/Bad--Name").status_code == 400


def test_register_same_id_different_forge_is_collision(client):
    # The name is the identity; a second campaign can't take it even if only the
    # forge differs. The forge qualifies repo_id so ids across forges never mix.
    assert register(client, "shared-id", repo_id=7, forge="github").status_code == 201
    r = register(client, "shared-id", repo_id=7, forge="gitlab")
    assert r.status_code == 409


# ------------------------------------------------------- validation at HTTP

@pytest.mark.parametrize("name", ["ab", "Nope", "-abc", "abc-", "ab--cd", "my_name"])
def test_register_rejects_invalid_names(client, name):
    r = register(client, name)
    assert r.status_code == 422
    assert "3-40 characters" in r.json()["detail"]


@pytest.mark.parametrize("name", ["api", "admin", "static", "assets", "register"])
def test_register_refuses_reserved_paths(client, name):
    r = register(client, name)
    assert r.status_code == 422
    assert "reserved" in r.json()["detail"]


def test_percent_encoded_slug_rejected(client):
    register(client, "my-campaign")
    # %2D decodes to '-', so the decoded path would exist; reject anyway.
    r = client.get("/my%2Dcampaign")
    assert r.status_code == 400


# ------------------------------------------------- GET /{name} probe states

def test_get_free_name_is_404(client):
    r = client.get("/totally-free")
    assert r.status_code == 404


@pytest.mark.parametrize("name", ["ab", "Nope", "-abc", "abc-", "ab--cd", "my_name"])
def test_get_malformed_name_is_400(client, name):
    assert client.get(f"/{name}").status_code == 400


@pytest.mark.parametrize("name", ["api", "admin", "static", "assets", "register"])
def test_get_reserved_name_is_403(client, name):
    r = client.get(f"/{name}")
    assert r.status_code == 403
    assert "reserved" in r.text.lower()
    assert "data-forward" not in r.text  # never the claim / auto-forward offer


def test_get_live_name_redirects(client):
    register(client, "live-name")
    r = client.get("/live-name")
    assert r.status_code == 302


def test_get_tombstoned_name_is_410_and_blocked(client):
    register(client, "bad-name")
    client.request("DELETE", "/admin/slugs/bad-name", headers=AUTH)
    r = client.get("/bad-name")
    assert r.status_code == 410
    assert "blocked" in r.text.lower()


# ---------------------------------------------------------------- tombstones

def test_tombstone_prevents_reregistration(client):
    register(client, "doomed-name")
    r = client.request("DELETE", "/admin/slugs/doomed-name", headers=AUTH,
                       json={"notes": "abusive"})
    assert r.status_code == 200

    r = client.get("/doomed-name")
    assert r.status_code == 410

    # A tombstoned name stays occupied: re-registration collides.
    r = register(client, "doomed-name", repo_id=999)
    assert r.status_code == 409


def test_tombstone_unknown_name_404(client):
    r = client.delete("/admin/slugs/never-existed", headers=AUTH)
    assert r.status_code == 404


# -------------------------------------------------------------------- admin

def test_admin_reserve_and_resolve(client):
    r = client.post("/admin/slugs", headers=AUTH,
                    json={"name": "workshop", "destination_url": "https://mdw.ac.at/x",
                          "notes": "2026 workshop"})
    assert r.status_code == 201

    r = client.get("/workshop")
    assert r.status_code == 302
    assert r.headers["location"] == "https://mdw.ac.at/x"

    # a reserved name stays occupied for registration
    assert register(client, "workshop").status_code == 409


def test_admin_reserve_validates_input(client):
    r = client.post("/admin/slugs", headers=AUTH,
                    json={"name": "Bad--Name", "destination_url": "https://x.org"})
    assert r.status_code == 422
    r = client.post("/admin/slugs", headers=AUTH,
                    json={"name": "fine-name", "destination_url": "javascript:alert(1)"})
    assert r.status_code == 422
    r = client.post("/admin/slugs", headers=AUTH,
                    json={"name": "admin", "destination_url": "https://x.org"})
    assert r.status_code == 422


def test_admin_reserve_conflict(client):
    register(client, "occupied")
    r = client.post("/admin/slugs", headers=AUTH,
                    json={"name": "occupied", "destination_url": "https://x.org"})
    assert r.status_code == 409


def test_admin_requires_token(client):
    assert client.get("/admin/slugs").status_code == 401
    assert client.get("/admin/slugs",
                      headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = client.get("/admin/slugs", headers=AUTH)
    assert r.status_code == 200


def test_admin_disabled_without_token(tmp_path):
    settings = Settings(campaign_app_base=BASE, db_path=str(tmp_path / "s.db"),
                        admin_token=None)
    c = TestClient(create_app(settings), follow_redirects=False)
    assert c.get("/admin/slugs", headers=AUTH).status_code == 503


def test_admin_list(client):
    register(client, "one-name", repo_id=321)
    r = client.get("/admin/slugs", headers=AUTH)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["name"] == "one-name"
    assert rows[0]["status"] == "active"
    assert rows[0]["repo_id"] == 321


# ----------------------------------------------------------------- own paths

def test_reserved_own_routes_never_claimable(client):
    assert client.get("/robots.txt").status_code == 200
    assert client.get("/healthz").status_code == 200
    # a reserved word that is not a live route is 403 (forbidden), not a claim offer
    r = client.get("/api")
    assert r.status_code == 403
    assert "data-forward" not in r.text
