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


def register(client, name):
    return client.post("/register", data={"name": name})


# ------------------------------------------------------------- happy path

def test_register_then_resolve(client):
    r = register(client, "lute-tablature")
    assert r.status_code == 303
    assert r.headers["location"].startswith(f"{BASE}/c/")
    assert r.headers["location"].endswith("/new")
    campaign_id = r.headers["location"].removeprefix(f"{BASE}/c/").removesuffix("/new")

    r = client.get("/lute-tablature")
    assert r.status_code == 302
    assert r.headers["location"] == f"{BASE}/c/{campaign_id}"


def test_claim_free_name_via_direct_url(client):
    # GET on a free name offers a claim page that forwards to the campaign app
    # (name still editable there) rather than registering on the spot.
    r = client.get("/fresh-name")
    assert r.status_code == 404
    assert "no campaign called" in r.text
    assert f"{BASE}/c?slug=fresh-name" in r.text
    assert "/fresh-name/claim" not in r.text  # the page no longer claims directly

    # The claim endpoint itself is unchanged and still creates the redirect.
    r = client.post("/fresh-name/claim")
    assert r.status_code == 303
    assert r.headers["location"].startswith(f"{BASE}/c/")


def test_landing_page_probes_and_forwards(client):
    r = client.get("/")
    assert r.status_code == 200
    # The website drives the flow client-side; it no longer posts to /register.
    assert 'action="/register"' not in r.text
    assert 'id="create-form"' in r.text
    assert f'data-campaign-base="{BASE}"' in r.text


# ------------------------------------------------------- validation at HTTP

@pytest.mark.parametrize("name", ["ab", "Nope", "-abc", "abc-", "ab--cd", "my_name"])
def test_register_rejects_invalid_names(client, name):
    r = register(client, name)
    assert r.status_code == 409
    assert "3-40 characters" in r.text


@pytest.mark.parametrize("name", ["api", "admin", "static", "assets", "register"])
def test_register_refuses_reserved_paths(client, name):
    r = register(client, name)
    assert r.status_code == 409
    assert "reserved" in r.text


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
    assert "Start" not in r.text  # never the claim offer


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


# ------------------------------------------------------- collision branches

def test_landing_collision_says_choose_another(client):
    register(client, "taken-name")
    r = register(client, "taken-name")
    assert r.status_code == 409
    assert "choose another name" in r.text
    assert "join" not in r.text.lower()  # landing flow never offers a join


def test_direct_url_collision_offers_join(client):
    r = register(client, "taken-name")
    campaign_id = r.headers["location"].removeprefix(f"{BASE}/c/").removesuffix("/new")

    r = client.post("/taken-name/claim")
    assert r.status_code == 409
    assert f"{BASE}/c/{campaign_id}/join" in r.text
    assert "already a campaign" in r.text


# ---------------------------------------------------------------- tombstones

def test_tombstone_prevents_reregistration(client):
    register(client, "doomed-name")
    r = client.request("DELETE", "/admin/slugs/doomed-name", headers=AUTH,
                       json={"notes": "abusive"})
    assert r.status_code == 200

    r = client.get("/doomed-name")
    assert r.status_code == 410

    r = register(client, "doomed-name")           # landing flow
    assert r.status_code == 409
    r = client.post("/doomed-name/claim")         # direct-URL flow
    assert r.status_code == 410


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

    # reserved names stay occupied for both public flows
    assert register(client, "workshop").status_code == 409
    assert client.post("/workshop/claim").status_code == 409


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
    register(client, "one-name")
    r = client.get("/admin/slugs", headers=AUTH)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["name"] == "one-name"
    assert rows[0]["status"] == "active"


# ----------------------------------------------------------------- own paths

def test_reserved_own_routes_never_claimable(client):
    assert client.get("/robots.txt").status_code == 200
    assert client.get("/healthz").status_code == 200
    # a reserved word that is not a live route is 403 (forbidden), not a claim offer
    r = client.get("/api")
    assert r.status_code == 403
    assert "Start" not in r.text
