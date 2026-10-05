from conftest import auth


def test_requires_token(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers=auth("gresit")).status_code == 401


def test_customer_cannot_use_operator_endpoints(client):
    assert client.get("/api/operator/refunds", headers=auth("tok_ana")).status_code == 403
    assert client.post("/api/operator/refunds/1/decision", json={"approve": True},
                       headers=auth("tok_ana")).status_code == 403


def test_customer_cannot_read_other_session(client):
    sid = client.post("/api/sessions", headers=auth("tok_ana")).json()["id"]
    assert client.get(f"/api/sessions/{sid}", headers=auth("tok_ion")).status_code == 404
    assert client.post(f"/api/sessions/{sid}/messages", json={"text": "salut"},
                       headers=auth("tok_ion")).status_code == 404


def test_refund_human_approval_flow(client):
    sid = client.post("/api/sessions", headers=auth("tok_ana")).json()["id"]
    r = client.post(f"/api/sessions/{sid}/messages", json={"text": "Vreau rambursare pentru A1001"},
                    headers=auth("tok_ana")).json()
    assert r["state"] == "awaiting_approval"
    pending = client.get("/api/operator/refunds", headers=auth("tok_operator")).json()
    assert len(pending) == 1 and pending[0]["status"] == "pending"
    rid = pending[0]["id"]

    d = client.post(f"/api/operator/refunds/{rid}/decision", json={"approve": True, "note": "ok"},
                    headers=auth("tok_operator"))
    assert d.status_code == 200 and d.json()["status"] == "approved"
    # nu se poate decide de două ori
    assert client.post(f"/api/operator/refunds/{rid}/decision", json={"approve": False},
                       headers=auth("tok_operator")).status_code == 400

    s = client.get(f"/api/sessions/{sid}", headers=auth("tok_ana")).json()
    assert s["state"] == "active"
    assert "aprobată" in s["messages"][-1]["content"]
    orders = {o["id"]: o for o in client.get("/api/orders", headers=auth("tok_ana")).json()}
    assert orders["A1001"]["refunded"] == 1299.0

    actions = [e["action"] for e in client.get("/api/operator/audit", headers=auth("tok_operator")).json()]
    assert "refund.approved" in actions and "tool.request_refund" in actions
    assert client.get("/api/operator/audit/verify", headers=auth("tok_operator")).json()["ok"]


def test_handoff_and_operator_reply(client):
    sid = client.post("/api/sessions", headers=auth("tok_ion")).json()["id"]
    r = client.post(f"/api/sessions/{sid}/messages", json={"text": "Vreau un operator"}, headers=auth("tok_ion")).json()
    assert r["state"] == "handoff"
    assert len(client.get("/api/operator/handoffs", headers=auth("tok_operator")).json()) == 1
    client.post(f"/api/operator/sessions/{sid}/reply", json={"text": "Bună, sunt Maria.", "resolve": True},
                headers=auth("tok_operator"))
    s = client.get(f"/api/sessions/{sid}", headers=auth("tok_ion")).json()
    assert s["state"] == "active"
    assert any(m["role"] == "operator" for m in s["messages"])


def test_metrics(client):
    m = client.get("/api/operator/metrics", headers=auth("tok_operator")).json()
    assert {"containment_rate", "refunds", "llm_cost_usd"} <= m.keys()
