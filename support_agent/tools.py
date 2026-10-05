"""Uneltele agentului. Fiecare unealtă:
  1. verifică autorizarea (clientul vede/acționează doar pe comenzile lui);
  2. aplică guardrails de business;
  3. scrie în audit log;
  4. returnează un rezultat JSON pe care modelul îl poate citi.
"""
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from . import audit, guardrails
from .db import now, now_iso, row_to_dict
from .rag import KnowledgeBase


@dataclass
class ToolContext:
    conn: sqlite3.Connection
    kb: KnowledgeBase
    user: dict
    session_id: str
    today: datetime = field(default_factory=now)
    # completate pe parcursul unei ture — folosite de guardrails pe ieșire
    retrieved: dict = field(default_factory=dict)
    actions: list = field(default_factory=list)


TOOL_SCHEMAS = [
    {
        "name": "search_knowledge_base",
        "description": "Caută în documentația oficială (politici retur, livrare, garanție, plăți). "
                       "Folosește-o înainte de a răspunde la ORICE întrebare despre politici sau proceduri. "
                       "Rezultatele au un source_id pe care trebuie să-l citezi ca [source_id].",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Întrebarea, reformulată ca interogare de căutare."}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_my_orders",
        "description": "Listează comenzile clientului autentificat (id, status, total, dată).",
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    {
        "name": "get_order",
        "description": "Detaliile unei comenzi a clientului autentificat: status, produse, AWB, date, "
                       "dacă e eligibilă pentru rambursare și suma maximă rambursabilă.",
        "input_schema": {
            "type": "object",
            "properties": {"order_id": {"type": "string", "description": "ID-ul comenzii, ex. A1001"}},
            "required": ["order_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "create_ticket",
        "description": "Deschide un tichet de suport pentru probleme ce necesită investigație "
                       "(colet întârziat/deteriorat, defect, garanție, altele).",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "ID-ul comenzii sau șir gol dacă nu e cazul."},
                "category": {"type": "string", "enum": ["livrare", "defect", "garantie", "plata", "cont", "altele"]},
                "priority": {"type": "string", "enum": ["low", "normal", "high"]},
                "subject": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["order_id", "category", "priority", "subject", "description"],
            "additionalProperties": False,
        },
    },
    {
        "name": "request_refund",
        "description": "Creează o CERERE de rambursare. Nu rambursează nimic: cererea intră în coada unui "
                       "operator uman care o aprobă sau o respinge. Verifică întâi comanda cu get_order.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "amount": {"type": "number", "description": "Suma în MDL, cel mult suma rambursabilă rămasă."},
                "reason": {"type": "string", "description": "Motivul declarat de client."},
            },
            "required": ["order_id", "amount", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "escalate_to_human",
        "description": "Transferă conversația unui operator uman. Folosește când clientul cere un om, "
                       "când nu găsești răspunsul în documentație, la reclamații/situații sensibile "
                       "sau când o cerere nu poate fi rezolvată prin uneltele disponibile.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
            "additionalProperties": False,
        },
    },
]


def _own_order(ctx: ToolContext, order_id: str) -> dict | None:
    row = ctx.conn.execute("SELECT * FROM orders WHERE id=? AND customer_id=?",
                           (order_id.strip().upper(), ctx.user["id"])).fetchone()
    return row_to_dict(row)


def _open_refund_sum(ctx: ToolContext, order_id: str) -> float:
    return ctx.conn.execute("SELECT COALESCE(SUM(amount),0) FROM refunds WHERE order_id=? AND status='pending'",
                            (order_id,)).fetchone()[0]


def search_knowledge_base(ctx: ToolContext, query: str) -> dict:
    hits = ctx.kb.search(query)
    for h in hits:
        ctx.retrieved[h.chunk.source_id] = h.chunk
    return {"results": [h.as_dict() for h in hits],
            "note": None if hits else "Nimic relevant în documentație. Nu inventa; propune escaladarea la un om."}


def list_my_orders(ctx: ToolContext) -> dict:
    rows = ctx.conn.execute("SELECT id, status, total, created_at FROM orders WHERE customer_id=? ORDER BY created_at DESC",
                            (ctx.user["id"],)).fetchall()
    return {"orders": [dict(r) for r in rows]}


def get_order(ctx: ToolContext, order_id: str) -> dict:
    order = _own_order(ctx, order_id)
    if not order:
        # Același mesaj pentru „nu există” și „nu e a ta”: nu divulgăm existența comenzilor altor clienți.
        return {"error": "Comanda nu a fost găsită în contul dumneavoastră."}
    decision = guardrails.check_refund(order, 0.01, _open_refund_sum(ctx, order["id"]), 0, ctx.today)
    items = json.loads(order["items"])
    refundable = sum(i["price"] * i["qty"] for i in items if i.get("returnable", True)) \
        - order["refunded"] - _open_refund_sum(ctx, order["id"])
    return {
        "order_id": order["id"], "status": order["status"], "total": order["total"],
        "already_refunded": order["refunded"], "items": items, "tracking": order["tracking"],
        "created_at": order["created_at"], "delivered_at": order["delivered_at"],
        "refund_eligible": decision.allowed,
        "refund_eligibility_reason": decision.reason,
        "max_refundable": round(max(refundable, 0), 2) if decision.allowed else 0,
    }


def create_ticket(ctx: ToolContext, order_id: str, category: str, priority: str, subject: str, description: str) -> dict:
    if order_id and not _own_order(ctx, order_id):
        return {"error": "Comanda nu a fost găsită în contul dumneavoastră."}
    with ctx.conn:
        cur = ctx.conn.execute(
            "INSERT INTO tickets (session_id, customer_id, order_id, category, priority, subject, description, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (ctx.session_id, ctx.user["id"], order_id.upper() or None, category, priority, subject,
             guardrails.redact_pii(description), now_iso()),
        )
    return {"ticket_id": cur.lastrowid, "status": "open"}


def request_refund(ctx: ToolContext, order_id: str, amount: float, reason: str) -> dict:
    order = _own_order(ctx, order_id)
    requests = ctx.conn.execute("SELECT COUNT(*) FROM refunds WHERE session_id=?", (ctx.session_id,)).fetchone()[0]
    oid = order["id"] if order else order_id
    decision = guardrails.check_refund(order, float(amount), _open_refund_sum(ctx, oid), requests, ctx.today)
    if not decision.allowed:
        audit.log(ctx.conn, "guardrail", "refund.blocked",
                  {"order_id": order_id, "amount": amount, "code": decision.code, "reason": decision.reason},
                  ctx.session_id)
        return {"error": decision.reason, "code": decision.code}
    with ctx.conn:
        cur = ctx.conn.execute(
            "INSERT INTO refunds (session_id, order_id, customer_id, amount, reason, created_at) VALUES (?,?,?,?,?,?)",
            (ctx.session_id, order["id"], ctx.user["id"], float(amount), guardrails.redact_pii(reason), now_iso()),
        )
        ctx.conn.execute("UPDATE sessions SET state='awaiting_approval', updated_at=? WHERE id=?",
                         (now_iso(), ctx.session_id))
    return {"refund_id": cur.lastrowid, "status": "pending_human_approval", "amount": float(amount),
            "note": "Cererea NU este aprobată. Un operator uman o va analiza."}


def escalate_to_human(ctx: ToolContext, reason: str) -> dict:
    existing = ctx.conn.execute("SELECT id FROM handoffs WHERE session_id=? AND status='open'",
                                (ctx.session_id,)).fetchone()
    if existing:
        return {"handoff_id": existing["id"], "status": "already_open"}
    with ctx.conn:
        cur = ctx.conn.execute("INSERT INTO handoffs (session_id, customer_id, reason, created_at) VALUES (?,?,?,?)",
                               (ctx.session_id, ctx.user["id"], reason, now_iso()))
        ctx.conn.execute("UPDATE sessions SET state='handoff', updated_at=? WHERE id=?", (now_iso(), ctx.session_id))
    return {"handoff_id": cur.lastrowid, "status": "open",
            "note": "Un operator preia conversația în programul de lucru (L–V 09:00–18:00)."}


REGISTRY = {
    "search_knowledge_base": search_knowledge_base,
    "list_my_orders": list_my_orders,
    "get_order": get_order,
    "create_ticket": create_ticket,
    "request_refund": request_refund,
    "escalate_to_human": escalate_to_human,
}


def run_tool(ctx: ToolContext, name: str, args: dict) -> dict:
    """Punctul unic prin care trece orice acțiune — aici se face validarea și auditul."""
    fn = REGISTRY.get(name)
    if fn is None:
        result = {"error": f"Unealtă necunoscută: {name}"}
    else:
        try:
            result = fn(ctx, **args)
        except (TypeError, ValueError, KeyError) as e:
            result = {"error": f"Argumente invalide: {e}"}
    ctx.actions.append({"tool": name, "args": args, "ok": "error" not in result})
    audit.log(ctx.conn, f"agent:{ctx.user['id']}", f"tool.{name}",
              {"args": args, "ok": "error" not in result,
               "result": {k: v for k, v in result.items() if k != "results"}}, ctx.session_id)
    return result
