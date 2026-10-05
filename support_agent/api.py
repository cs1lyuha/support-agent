"""API HTTP + UI. Autentificare prin token Bearer; roluri: customer / operator."""
import json
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import agent, audit, db
from .brains import make_brain
from .rag import KnowledgeBase

STATIC = Path(__file__).parent / "static"


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class DecisionIn(BaseModel):
    approve: bool
    note: str = Field(default="", max_length=500)


class OperatorReplyIn(BaseModel):
    text: str = Field(default="", max_length=2000)
    resolve: bool = False


def create_app(db_path: str | None = None, brain_kind: str | None = None) -> FastAPI:
    conn = db.connect(db_path)
    db.seed(conn)
    kb = KnowledgeBase()
    brain = make_brain(brain_kind)
    app = FastAPI(title="Support Agent", version="1.0")
    app.state.conn, app.state.kb, app.state.brain = conn, kb, brain

    def current_user(authorization: str = Header(default="")) -> dict:
        token = authorization.removeprefix("Bearer ").strip()
        row = conn.execute("SELECT id, name, email, role FROM users WHERE token=?", (token,)).fetchone()
        if not row:
            raise HTTPException(401, "Token invalid sau lipsă.")
        return dict(row)

    def operator(user: dict = Depends(current_user)) -> dict:
        if user["role"] != "operator":
            audit.log(conn, f"user:{user['id']}", "auth.denied", {"reason": "operator endpoint"})
            raise HTTPException(403, "Necesită rol de operator.")
        return user

    def guard(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except agent.SessionError as e:
            raise HTTPException(404 if "inexistent" in str(e) else 400, str(e))
        except PermissionError as e:
            raise HTTPException(403, str(e))

    # --- pagini ---
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/operator", include_in_schema=False)
    def operator_page():
        return FileResponse(STATIC / "operator.html")

    # --- client ---
    @app.get("/api/health")
    def health():
        return {"ok": True, "brain": brain.name, "kb_chunks": len(kb.chunks)}

    @app.get("/api/me")
    def me(user: dict = Depends(current_user)):
        return {**user, "brain": brain.name}

    @app.get("/api/orders")
    def my_orders(user: dict = Depends(current_user)):
        rows = conn.execute("SELECT id, status, total, refunded, items, tracking, created_at, delivered_at"
                            " FROM orders WHERE customer_id=? ORDER BY created_at DESC", (user["id"],))
        return [{**dict(r), "items": json.loads(r["items"])} for r in rows]

    @app.post("/api/sessions")
    def new_session(user: dict = Depends(current_user)):
        return guard(agent.create_session, conn, user)

    @app.get("/api/sessions/{sid}")
    def get_session(sid: str, user: dict = Depends(current_user)):
        return guard(agent.get_session, conn, sid, user)

    @app.post("/api/sessions/{sid}/messages")
    def post_message(sid: str, body: MessageIn, user: dict = Depends(current_user)):
        return guard(agent.handle_message, conn, kb, brain, user, sid, body.text)

    # --- operator ---
    @app.get("/api/operator/refunds")
    def refunds(status: str = "pending", _: dict = Depends(operator)):
        q = "SELECT r.*, u.name AS customer_name FROM refunds r JOIN users u ON u.id=r.customer_id"
        rows = conn.execute(q + (" WHERE r.status=?" if status != "all" else "") + " ORDER BY r.id DESC",
                            (status,) if status != "all" else ())
        return [dict(r) for r in rows]

    @app.post("/api/operator/refunds/{rid}/decision")
    def decide(rid: int, body: DecisionIn, op: dict = Depends(operator)):
        return guard(agent.decide_refund, conn, op, rid, body.approve, body.note)

    @app.get("/api/operator/handoffs")
    def handoffs(status: str = "open", _: dict = Depends(operator)):
        rows = conn.execute("SELECT h.*, u.name AS customer_name FROM handoffs h JOIN users u ON u.id=h.customer_id"
                            " WHERE h.status=? ORDER BY h.id DESC", (status,))
        return [dict(r) for r in rows]

    @app.get("/api/operator/sessions/{sid}")
    def op_session(sid: str, op: dict = Depends(operator)):
        return guard(agent.get_session, conn, sid, op)

    @app.post("/api/operator/sessions/{sid}/reply")
    def op_reply(sid: str, body: OperatorReplyIn, op: dict = Depends(operator)):
        guard(agent.operator_reply, conn, op, sid, body.text, body.resolve)
        return {"ok": True}

    @app.get("/api/operator/tickets")
    def tickets(_: dict = Depends(operator)):
        return [dict(r) for r in conn.execute("SELECT * FROM tickets ORDER BY id DESC")]

    @app.get("/api/operator/audit")
    def audit_log(limit: int = 200, session_id: str | None = None, _: dict = Depends(operator)):
        return audit.entries(conn, min(limit, 1000), session_id)

    @app.get("/api/operator/audit/verify")
    def audit_verify(_: dict = Depends(operator)):
        return audit.verify(conn)

    @app.get("/api/operator/metrics")
    def metrics(_: dict = Depends(operator)):
        return business_metrics(conn)

    return app


def business_metrics(conn) -> dict:
    one = lambda q, *a: conn.execute(q, a).fetchone()[0]
    sessions = one("SELECT COUNT(*) FROM sessions")
    handed = one("SELECT COUNT(DISTINCT session_id) FROM handoffs")
    tokens_in = one("SELECT COALESCE(SUM(input_tokens),0) FROM usage")
    tokens_out = one("SELECT COALESCE(SUM(output_tokens),0) FROM usage")
    return {
        "sessions": sessions,
        "handoffs": handed,
        # % conversații rezolvate fără om — principalul KPI al unui agent de suport
        "containment_rate": round(1 - handed / sessions, 3) if sessions else None,
        "tickets_open": one("SELECT COUNT(*) FROM tickets WHERE status='open'"),
        "refunds": {s: {"count": one("SELECT COUNT(*) FROM refunds WHERE status=?", s),
                        "amount": one("SELECT COALESCE(SUM(amount),0) FROM refunds WHERE status=?", s)}
                    for s in ("pending", "approved", "rejected")},
        "refunds_blocked_by_policy": one("SELECT COUNT(*) FROM audit_log WHERE action='refund.blocked'"),
        # răspunsuri în care a intervenit un guardrail (injecție, PII, fallback la om etc.)
        "guardrail_events": one("SELECT COUNT(*) FROM audit_log WHERE action='message.replied'"
                                " AND details NOT LIKE '%\"guardrail\": null%'"),
        "llm_tokens": {"input": tokens_in, "output": tokens_out},
        # prețuri claude-opus-5-5: $4 / $20 per milion de tokeni
        "llm_cost_usd": round(tokens_in * 4 / 1e6 + tokens_out * 20 / 1e6, 4),
    }



def get_app() -> FastAPI:
    """Factory pentru `uvicorn support_agent.api:get_app --factory`."""
    return create_app()
