"""People and business units: BU admins manage their own BU, members are chat users, disabled people lose access."""

from tests.harness import build

ADMIN = {"X-Dev-User": "admin@example.com", "X-Dev-Roles": "*:platform_admin"}
CHAT = {"X-Dev-User": "chat", "X-Dev-Kind": "service"}


def hdr(user):
    return {"X-Dev-User": user}


def ok(r, status=200):
    assert r.status_code == status, r.text
    return r.json()


def test_people_and_business_units():
    p = build()
    g = p.gov
    ok(g("POST", "/api/v1/business-units", ADMIN, json={"id": "retail", "name": "Retail Banking",
                                                        "settings": {"chat_welcome": "Hi from Retail"}}), 201)
    ok(g("POST", "/api/v1/business-units", ADMIN, json={"id": "wealth", "name": "Wealth"}), 201)
    assert g("POST", "/api/v1/business-units", ADMIN, json={"id": "retail"}).status_code == 409
    assert g("POST", "/api/v1/business-units", ADMIN, json={"id": "Bad Name"}).status_code == 422

    # platform admin makes rita BU admin of retail
    ok(g("PUT", "/api/v1/users/rita@example.com", ADMIN, json={"display_name": "Rita", "default_bu": "retail"}))
    ok(g("PUT", "/api/v1/users/rita@example.com/memberships/retail", ADMIN, json={"roles": ["bu_admin"]}))
    rita = hdr("rita@example.com")
    # rita manages retail people only
    ok(g("PUT", "/api/v1/users/ann@example.com/memberships/retail", rita, json={"roles": ["member"]}))
    ok(g("PUT", "/api/v1/users/fred@example.com/memberships/retail", rita, json={"roles": ["functional_author"]}))
    assert g("PUT", "/api/v1/users/ann@example.com/memberships/wealth", rita, json={"roles": ["member"]}).status_code == 403
    assert g("PUT", "/api/v1/users/ann@example.com/memberships/retail", rita, json={"roles": ["platform_admin"]}).status_code == 422
    ok(g("PUT", "/api/v1/users/ann@example.com/memberships/wealth", ADMIN, json={"roles": ["member"]}))
    people = ok(g("GET", "/api/v1/users", rita))["users"]
    assert {u["email"]: u["memberships"] for u in people} == {
        "ann@example.com": {"retail": ["member"]},  # wealth membership hidden from the retail admin
        "fred@example.com": {"retail": ["functional_author"]},
        "rita@example.com": {"retail": ["bu_admin"]}}
    assert g("GET", "/api/v1/users", hdr("ann@example.com")).status_code == 403
    # only platform admins disable people
    assert g("PUT", "/api/v1/users/ann@example.com", rita, json={"status": "disabled"}).status_code == 403

    bus = ok(g("GET", "/api/v1/business-units", rita))
    assert [b["id"] for b in bus["business_units"]] == ["retail"] and bus["business_units"][0]["members"] == 3
    assert bus["business_units"][0]["can_manage"] and not bus["can_create"]

    # member-only people see no agents in the console, but are members for the chat app
    ok(g("POST", "/api/v1/agents", rita, json={"name": "loans-helper", "owner_bu": "retail"}), 201)
    assert ok(g("GET", "/api/v1/agents", hdr("ann@example.com")))["agents"] == []
    assert [a["name"] for a in ok(g("GET", "/api/v1/agents", hdr("fred@example.com")))["agents"]] == ["loans-helper"]
    prof = ok(g("GET", "/internal/v1/users/ann@example.com/profile", CHAT))
    assert [(b["id"], b["name"]) for b in prof["bus"]] == [("retail", "Retail Banking"), ("wealth", "Wealth")]
    assert prof["bus"][0]["settings"]["chat_welcome"] == "Hi from Retail"
    assert g("GET", "/internal/v1/users/ann@example.com/profile", hdr("ann@example.com")).status_code == 403

    # disabling removes all access, also for the chat app
    ok(g("PUT", "/api/v1/users/fred@example.com", ADMIN, json={"status": "disabled"}))
    assert ok(g("GET", "/api/v1/agents", hdr("fred@example.com")))["agents"] == []
    assert ok(g("GET", "/internal/v1/users/fred@example.com/profile", CHAT))["bus"] == []
    # removing all roles removes the membership
    ok(g("PUT", "/api/v1/users/ann@example.com/memberships/wealth", ADMIN, json={"roles": []}))
    assert [b["id"] for b in ok(g("GET", "/internal/v1/users/ann@example.com/profile", CHAT))["bus"]] == ["retail"]
    audit = ok(g("GET", "/api/v1/audit", ADMIN))["events"]
    assert {"bu.create", "membership.set", "user.update"} <= {e["action"] for e in audit}
