"""Audit log append-only cu lanț de hash-uri (tamper-evident).

Fiecare intrare conține hash-ul celei precedente; dacă cineva modifică sau șterge
un rând din mijloc, `verify()` detectează ruptura lanțului.
"""
import hashlib
import json
import sqlite3
import threading

from .db import now_iso
from .guardrails import redact_pii

GENESIS = "0" * 64
_LOCK = threading.Lock()


def _digest(prev_hash: str, ts: str, actor: str, session_id: str | None, action: str, details: str) -> str:
    payload = json.dumps([prev_hash, ts, actor, session_id, action, details], ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def log(conn: sqlite3.Connection, actor: str, action: str, details: dict | None = None,
        session_id: str | None = None) -> None:
    # Datele personale sunt mascate înainte să ajungă în log.
    body = redact_pii(json.dumps(details or {}, ensure_ascii=False, default=str))
    ts = now_iso()
    with _LOCK:
        last = conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        prev = last["hash"] if last else GENESIS
        h = _digest(prev, ts, actor, session_id, action, body)
        conn.execute(
            "INSERT INTO audit_log (ts, actor, session_id, action, details, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
            (ts, actor, session_id, action, body, prev, h),
        )


def verify(conn: sqlite3.Connection) -> dict:
    prev = GENESIS
    count = 0
    for row in conn.execute("SELECT * FROM audit_log ORDER BY id"):
        expected = _digest(prev, row["ts"], row["actor"], row["session_id"], row["action"], row["details"])
        if row["prev_hash"] != prev or row["hash"] != expected:
            return {"ok": False, "broken_at_id": row["id"], "checked": count}
        prev = row["hash"]
        count += 1
    return {"ok": True, "checked": count}


def entries(conn: sqlite3.Connection, limit: int = 200, session_id: str | None = None) -> list[dict]:
    if session_id:
        rows = conn.execute("SELECT * FROM audit_log WHERE session_id=? ORDER BY id DESC LIMIT ?", (session_id, limit))
    else:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
    return [{**dict(r), "details": json.loads(r["details"])} for r in rows]
