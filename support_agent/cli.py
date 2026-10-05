"""Linie de comandă:

    python -m support_agent.cli serve              # pornește UI + API pe http://127.0.0.1:8000
    python -m support_agent.cli chat --user tok_ana  # chat în terminal
    python -m support_agent.cli reset              # șterge baza de date demo
"""
import argparse
import os

from . import agent, config, db
from .brains import make_brain
from .rag import KnowledgeBase


def chat(token: str, brain_kind: str | None) -> None:
    conn = db.connect()
    db.seed(conn)
    row = conn.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone()
    if not row or row["role"] != "customer":
        raise SystemExit("Token de client invalid (încearcă tok_ana sau tok_ion).")
    user, kb, brain = dict(row), KnowledgeBase(), make_brain(brain_kind)
    s = agent.create_session(conn, user)
    print(f"[creier: {brain.name}] sesiune {s['id']} — scrie „exit” ca să ieși\n")
    print("Agent:", s["messages"][0]["content"])
    while True:
        try:
            text = input("\nTu: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in {"exit", "quit"}:
            break
        if not text:
            continue
        r = agent.handle_message(conn, kb, brain, user, s["id"], text)
        print("\nAgent:", r["reply"])
        for c in r["citations"]:
            print(f"   📄 {c['source_id']} — {c['title']}")
        for a in r["actions"]:
            print(f"   ⚙ {a['tool']}({a['args']}) {'ok' if a['ok'] else 'EROARE'}")
        if r["guardrail"] or r["warnings"]:
            print(f"   🛡 {r['guardrail'] or ''} {r['warnings'] or ''}")
        print(f"   [stare: {r['state']}]")


def main() -> None:
    ap = argparse.ArgumentParser(prog="support_agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)
    sp.add_argument("--brain", choices=["auto", "offline", "claude"])
    cp = sub.add_parser("chat")
    cp.add_argument("--user", default="tok_ana")
    cp.add_argument("--brain", choices=["auto", "offline", "claude"])
    sub.add_parser("reset")
    args = ap.parse_args()

    if args.cmd == "serve":
        import uvicorn
        if args.brain:
            os.environ["SUPPORT_BRAIN"] = args.brain
            config.BRAIN = args.brain
        uvicorn.run("support_agent.api:get_app", factory=True, host=args.host, port=args.port)
    elif args.cmd == "chat":
        chat(args.user, args.brain)
    elif args.cmd == "reset":
        for suffix in ("", "-wal", "-shm"):
            p = config.DB_PATH.with_name(config.DB_PATH.name + suffix)
            p.unlink(missing_ok=True)
        print("Baza de date demo a fost ștearsă; se recreează la următoarea pornire.")


if __name__ == "__main__":
    main()
