"""Orchestratorul: guardrails pe intrare -> creier (LLM sau offline) -> guardrails pe ieșire -> stare + audit.

Mașina de stări a unei sesiuni:
    active ──request_refund──▶ awaiting_approval ──decizie operator──▶ active
       │                                 │
       └──────escalate_to_human──────────┴──────────▶ handoff ──operator rezolvă──▶ active
    (orice) ──client închide──▶ closed
"""
import json
import sqlite3
import threading
import uuid

from . import audit, config, guardrails
from .db import now_iso, row_to_dict
from .rag import KnowledgeBase
from .tools import ToolContext, run_tool

_LOCKS: dict[str, threading.Lock] = {}


class SessionError(Exception):
    pass


def create_session(conn: sqlite3.Connection, user: dict) -> dict:
    if user["role"] != "customer":
        raise SessionError("Doar clienții pot deschide conversații.")
    sid = uuid.uuid4().hex[:12]
    ts = now_iso()
    with conn:
        conn.execute("INSERT INTO sessions (id, customer_id, created_at, updated_at) VALUES (?,?,?,?)",
                     (sid, user["id"], ts, ts))
    add_transcript(conn, sid, "assistant",
                   f"Bună, {user['name'].split()[0]}! Sunt agentul virtual TechNova. Vă pot ajuta cu informații "
                   "despre politici, statusul comenzilor, tichete și cereri de rambursare.")
    audit.log(conn, f"user:{user['id']}", "session.created", {}, sid)
    return get_session(conn, sid, user)


def get_session(conn: sqlite3.Connection, sid: str, user: dict) -> dict:
    s = row_to_dict(conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone())
    if not s or (user["role"] == "customer" and s["customer_id"] != user["id"]):
        raise SessionError("Sesiune inexistentă.")
    rows = conn.execute("SELECT role, content, citations, created_at FROM transcript WHERE session_id=? ORDER BY id",
                        (sid,)).fetchall()
    return {"id": s["id"], "customer_id": s["customer_id"], "state": s["state"],
            "messages": [{**dict(r), "citations": json.loads(r["citations"])} for r in rows]}


def add_transcript(conn, sid: str, role: str, content: str, citations: list | None = None) -> None:
    with conn:
        conn.execute("INSERT INTO transcript (session_id, role, content, citations, created_at) VALUES (?,?,?,?,?)",
                     (sid, role, content, json.dumps(citations or [], ensure_ascii=False), now_iso()))
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now_iso(), sid))


def _set(conn, sid: str, **fields) -> None:
    cols = ", ".join(f"{k}=?" for k in fields)
    with conn:
        conn.execute(f"UPDATE sessions SET {cols}, updated_at=? WHERE id=?", (*fields.values(), now_iso(), sid))


def _reply(conn, sid, text, citations=None, actions=None, warnings=None, guardrail=None) -> dict:
    add_transcript(conn, sid, "assistant", text, citations)
    audit.log(conn, "agent", "message.replied",
              {"text": text[:500], "citations": [c["source_id"] for c in citations or []],
               "warnings": warnings or [], "guardrail": guardrail}, sid)
    state = conn.execute("SELECT state FROM sessions WHERE id=?", (sid,)).fetchone()["state"]
    return {"reply": text, "citations": citations or [], "actions": actions or [], "state": state,
            "warnings": warnings or [], "guardrail": guardrail}


def handle_message(conn: sqlite3.Connection, kb: KnowledgeBase, brain, user: dict, sid: str, text: str) -> dict:
    lock = _LOCKS.setdefault(sid, threading.Lock())
    with lock:
        return _handle(conn, kb, brain, user, sid, text)


def _handle(conn, kb, brain, user, sid, text) -> dict:
    session = row_to_dict(conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone())
    if not session or session["customer_id"] != user["id"]:
        raise SessionError("Sesiune inexistentă.")
    if session["state"] == "closed":
        raise SessionError("Conversația este închisă.")
    text = text.strip()[:2000]
    if not text:
        raise SessionError("Mesaj gol.")

    # 1. PII: datele de card nu ajung nici în model, nici în baza de date.
    has_card = guardrails.contains_card_number(text)
    clean = guardrails.redact_pii(text)
    add_transcript(conn, sid, "user", clean)
    audit.log(conn, f"user:{user['id']}", "message.received", {"text": clean[:500]}, sid)
    if has_card:
        return _reply(conn, sid, "Pentru siguranța dumneavoastră am ascuns numărul de card din mesaj. "
                                 "Nu trimiteți niciodată datele cardului prin chat — nu avem nevoie de ele.",
                      guardrail="pii_card")

    # 2. Conversație preluată de om: agentul nu mai răspunde, operatorul vede mesajul.
    if session["state"] == "handoff":
        return _reply(conn, sid, "Mesajul a fost transmis operatorului care se ocupă de conversația dumneavoastră.",
                      guardrail="handoff_active")

    # 3. Prompt injection: blocat; la a doua încercare -> om.
    if pattern := guardrails.detect_injection(clean):
        attempts = session["injection_attempts"] + 1
        _set(conn, sid, injection_attempts=attempts)
        audit.log(conn, "guardrail", "guardrail.injection", {"pattern": pattern, "attempt": attempts}, sid)
        ctx = ToolContext(conn, kb, user, sid)
        if attempts >= 2:
            run_tool(ctx, "escalate_to_human", {"reason": "Încercări repetate de a ocoli regulile agentului"})
            return _reply(conn, sid, "Nu pot da curs acestei cereri. Am transferat conversația unui operator.",
                          actions=ctx.actions, guardrail="injection_escalated")
        return _reply(conn, sid, "Nu pot schimba regulile după care funcționez și nu pot aproba acțiuni singur. "
                                 "Cu ce vă pot ajuta legat de comenzile sau politicile noastre?",
                      guardrail="injection_blocked")

    ctx = ToolContext(conn, kb, user, sid)

    # 4. Fallback la om cerut explicit — determinist, nu depinde de model.
    if guardrails.wants_human(clean):
        r = run_tool(ctx, "escalate_to_human", {"reason": f"Clientul a cerut un operator: {clean[:200]}"})
        return _reply(conn, sid, f"Desigur. Am transferat conversația unui operator uman (cazul #{r['handoff_id']}). "
                                 "Programul echipei: luni–vineri 09:00–18:00.", actions=ctx.actions,
                      guardrail="human_requested")

    # 5. Creierul (Claude sau offline) cu uneltele controlate.
    history = json.loads(session["llm_messages"])
    try:
        result, history = brain.respond(ctx, history, clean, _updates_since_last_turn(conn, sid))
    except Exception as e:  # API indisponibil, rate limit etc. -> degradare elegantă spre om
        audit.log(conn, "system", "brain.error", {"error": f"{type(e).__name__}: {e}"[:300]}, sid)
        run_tool(ctx, "escalate_to_human", {"reason": "Agentul automat este temporar indisponibil"})
        return _reply(conn, sid, "Am o problemă tehnică momentan, așa că am transferat conversația unui operator.",
                      actions=ctx.actions, guardrail="brain_error")
    _set(conn, sid, llm_messages=json.dumps(history, ensure_ascii=False))

    # 6. Guardrails pe ieșire: citări verificate, fără promisiuni false.
    text_out, cited, warnings = guardrails.check_output(result.text, set(ctx.retrieved), approved_refund_in_turn=False)
    citations = [{"source_id": c, "title": ctx.retrieved[c].title, "snippet": ctx.retrieved[c].text[:240]}
                 for c in cited]

    # 7. Fallback la om după prea multe întrebări fără acoperire în documentație.
    if result.kb_searched and not ctx.retrieved:
        misses = session["kb_misses"] + 1
        _set(conn, sid, kb_misses=misses)
        if misses >= config.MAX_KB_MISSES:
            run_tool(ctx, "escalate_to_human", {"reason": f"{misses} întrebări consecutive fără răspuns în documentație"})
            text_out += "\n\nPentru că nu găsesc răspunsurile în documentație, am transferat conversația unui operator."
    elif ctx.retrieved:
        _set(conn, sid, kb_misses=0)

    return _reply(conn, sid, text_out, citations, ctx.actions, warnings)


def _updates_since_last_turn(conn, sid: str) -> list[str]:
    """Mesajele operatorului/sistemului apărute între tura anterioară și cea curentă (ex. rambursare aprobată),
    ca modelul să știe de ele fără să rescriem istoricul."""
    users = conn.execute("SELECT id FROM transcript WHERE session_id=? AND role='user' ORDER BY id DESC LIMIT 2",
                         (sid,)).fetchall()
    since = users[1]["id"] if len(users) > 1 else 0
    rows = conn.execute("SELECT role, content FROM transcript WHERE session_id=? AND id>? AND role IN ('operator','system')",
                        (sid, since)).fetchall()
    return [f"{r['role']}: {r['content']}" for r in rows]


# --- Acțiuni operator ---------------------------------------------------------------

def decide_refund(conn: sqlite3.Connection, operator: dict, refund_id: int, approve: bool, note: str = "") -> dict:
    if operator["role"] != "operator":
        raise PermissionError("Doar operatorii pot decide rambursări.")
    r = row_to_dict(conn.execute("SELECT * FROM refunds WHERE id=?", (refund_id,)).fetchone())
    if not r:
        raise SessionError("Rambursare inexistentă.")
    if r["status"] != "pending":
        raise SessionError(f"Rambursarea are deja statusul {r['status']}.")
    status = "approved" if approve else "rejected"
    with conn:
        conn.execute("UPDATE refunds SET status=?, decided_by=?, decision_note=?, decided_at=? WHERE id=?",
                     (status, operator["id"], note, now_iso(), refund_id))
        if approve:
            conn.execute("UPDATE orders SET refunded = refunded + ? WHERE id=?", (r["amount"], r["order_id"]))
        conn.execute("UPDATE sessions SET state='active', updated_at=? WHERE id=? AND state='awaiting_approval'",
                     (now_iso(), r["session_id"]))
    audit.log(conn, f"operator:{operator['id']}", f"refund.{status}",
              {"refund_id": refund_id, "order_id": r["order_id"], "amount": r["amount"], "note": note}, r["session_id"])
    msg = (f"✅ Rambursarea #{refund_id} ({r['amount']:.2f} MDL) pentru comanda {r['order_id']} a fost aprobată "
           "de un operator. Banii ajung în 5–10 zile lucrătoare." if approve else
           f"❌ Cererea de rambursare #{refund_id} pentru comanda {r['order_id']} a fost respinsă de un operator."
           + (f" Motiv: {note}" if note else ""))
    if r["session_id"]:
        add_transcript(conn, r["session_id"], "system", msg)
    return {**r, "status": status}


def operator_reply(conn: sqlite3.Connection, operator: dict, sid: str, text: str, resolve: bool = False) -> None:
    if operator["role"] != "operator":
        raise PermissionError("Doar operatorii pot răspunde.")
    if not conn.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
        raise SessionError("Sesiune inexistentă.")
    if text.strip():
        add_transcript(conn, sid, "operator", text.strip())
    audit.log(conn, f"operator:{operator['id']}", "operator.reply", {"text": text[:500], "resolve": resolve}, sid)
    if resolve:
        with conn:
            conn.execute("UPDATE handoffs SET status='resolved', resolved_at=? WHERE session_id=? AND status='open'",
                         (now_iso(), sid))
            conn.execute("UPDATE sessions SET state='active', updated_at=? WHERE id=? AND state='handoff'",
                         (now_iso(), sid))
        add_transcript(conn, sid, "system", "Operatorul a închis cazul. Agentul virtual vă poate ajuta în continuare.")
