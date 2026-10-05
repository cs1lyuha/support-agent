import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('customer', 'operator')),
    token TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES users(id),
    status TEXT NOT NULL,
    total REAL NOT NULL,
    refunded REAL NOT NULL DEFAULT 0,
    items TEXT NOT NULL,
    payment_method TEXT NOT NULL,
    tracking TEXT,
    created_at TEXT NOT NULL,
    delivered_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES users(id),
    state TEXT NOT NULL DEFAULT 'active'
        CHECK (state IN ('active', 'awaiting_approval', 'handoff', 'closed')),
    llm_messages TEXT NOT NULL DEFAULT '[]',
    kb_misses INTEGER NOT NULL DEFAULT 0,
    injection_attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transcript (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'operator', 'system')),
    content TEXT NOT NULL,
    citations TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    customer_id TEXT NOT NULL,
    order_id TEXT,
    category TEXT NOT NULL,
    priority TEXT NOT NULL,
    subject TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS refunds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    order_id TEXT NOT NULL REFERENCES orders(id),
    customer_id TEXT NOT NULL,
    amount REAL NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    decided_by TEXT,
    decision_note TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS handoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    session_id TEXT,
    action TEXT NOT NULL,
    details TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


def seed(conn: sqlite3.Connection) -> None:
    """Date demo: 2 clienți, 1 operator, comenzi în diverse stări."""
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        return
    users = [
        ("c1", "Ana Popescu", "ana@example.md", "customer", "tok_ana"),
        ("c2", "Ion Rusu", "ion@example.md", "customer", "tok_ion"),
        ("op1", "Operator Suport", "suport@example.md", "operator", "tok_operator"),
    ]
    conn.executemany("INSERT INTO users VALUES (?,?,?,?,?)", users)

    def days_ago(n: int) -> str:
        return (now() - timedelta(days=n)).isoformat(timespec="seconds")

    def item(name, price, qty=1, returnable=True, category="electronice"):
        return {"name": name, "price": price, "qty": qty, "returnable": returnable, "category": category}

    orders = [
        ("A1001", "c1", "livrată", 1299.0, [item("Căști wireless SoundMax", 1299.0)],
         "card", "AWB-55120", days_ago(8), days_ago(5)),
        ("A1002", "c1", "livrată", 2450.0, [item("Tabletă Lumo 10", 2450.0)],
         "card", "AWB-41007", days_ago(50), days_ago(45)),
        ("A1003", "c1", "procesare", 349.0, [item("Husă laptop", 349.0, category="accesorii")],
         "numerar", None, days_ago(1), None),
        ("B2001", "c2", "expediată", 899.0, [item("Mouse ergonomic", 899.0, category="accesorii")],
         "card", "AWB-77310", days_ago(3), None),
        ("B2002", "c2", "livrată", 500.0, [item("Card cadou 500 MDL", 500.0, returnable=False, category="card cadou")],
         "card", "AWB-77002", days_ago(4), days_ago(3)),
        ("B2003", "c2", "livrată", 5200.0, [item("Laptop Vertex 14", 5000.0), item("Încărcător USB-C", 200.0, category="accesorii")],
         "transfer", "AWB-77455", days_ago(12), days_ago(10)),
    ]
    conn.executemany(
        "INSERT INTO orders (id, customer_id, status, total, items, payment_method, tracking, created_at, delivered_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        [(o[0], o[1], o[2], o[3], json.dumps(o[4], ensure_ascii=False), *o[5:]) for o in orders],
    )


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None
