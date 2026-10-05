"""Evaluare automată a agentului.

    python -m eval.run_eval                  # creier offline (gratuit, determinist)
    python -m eval.run_eval --brain claude   # agent real pe Claude (consumă tokeni)

Două niveluri:
  1. retrieval  — hit@1, hit@3, MRR și respingerea întrebărilor din afara documentației;
  2. end-to-end — conversații scriptate verificate pe acțiuni, citări, stare, guardrails și DB.
Fiecare caz rulează pe o bază de date nouă, ca rezultatele să nu se influențeze între ele.
"""
import argparse
import json
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

from support_agent import agent, db
from support_agent.api import business_metrics
from support_agent.brains import make_brain
from support_agent.rag import KnowledgeBase, normalize

HERE = Path(__file__).parent


def eval_retrieval(kb: KnowledgeBase) -> dict:
    cases = json.loads((HERE / "retrieval.json").read_text(encoding="utf-8"))
    hit1 = hit3 = rr = answerable = rejected = unanswerable = 0
    failures = []
    for c in cases:
        ids = [h.chunk.source_id for h in kb.search(c["q"])]
        if c["expect"] is None:
            unanswerable += 1
            rejected += not ids
            if ids:
                failures.append({"q": c["q"], "expected": None, "got": ids})
            continue
        answerable += 1
        if ids[:1] == [c["expect"]]:
            hit1 += 1
        if c["expect"] in ids:
            hit3 += 1
            rr += 1 / (ids.index(c["expect"]) + 1)
        else:
            failures.append({"q": c["q"], "expected": c["expect"], "got": ids})
    return {"hit@1": round(hit1 / answerable, 3), "hit@3": round(hit3 / answerable, 3),
            "mrr": round(rr / answerable, 3), "out_of_scope_rejected": round(rejected / max(unanswerable, 1), 3),
            "failures": failures}


def check_case(case: dict, conn, sid: str, results: list) -> list[str]:
    exp, last = case["expect"], results[-1]
    actions = [a for r in results for a in r["actions"]]
    reply = normalize(last["reply"])
    errors = []
    for t in exp.get("tools_called", []):
        if not any(a["tool"] == t and a["ok"] for a in actions):
            errors.append(f"unealta {t} nu a fost apelată cu succes")
    for t in exp.get("tools_not_called_ok", []):
        if any(a["tool"] == t and a["ok"] for a in actions):
            errors.append(f"unealta {t} NU trebuia să reușească")
    cited = [c["source_id"] for c in last["citations"]]
    for c in exp.get("citations_include", []):
        if c not in cited:
            errors.append(f"lipsește citarea {c} (are {cited})")
    if "citations_count" in exp and len(cited) != exp["citations_count"]:
        errors.append(f"citări așteptate {exp['citations_count']}, primite {len(cited)}")
    if "reply_contains_any" in exp and not any(normalize(s) in reply for s in exp["reply_contains_any"]):
        errors.append(f"răspunsul nu conține niciunul din {exp['reply_contains_any']}")
    for s in exp.get("reply_not_contains", []):
        if normalize(s) in reply:
            errors.append(f"răspunsul conține interzis: {s!r}")
    state = conn.execute("SELECT state FROM sessions WHERE id=?", (sid,)).fetchone()["state"]
    if "state" in exp and state != exp["state"]:
        errors.append(f"stare {state!r}, așteptată {exp['state']!r}")
    if "guardrail" in exp and last["guardrail"] != exp["guardrail"]:
        errors.append(f"guardrail {last['guardrail']!r}, așteptat {exp['guardrail']!r}")
    one = lambda q: conn.execute(q).fetchone()[0]
    for key, val in exp.get("db", {}).items():
        got = {"refunds": lambda: one("SELECT COUNT(*) FROM refunds"),
               "refunds_pending": lambda: one("SELECT COUNT(*) FROM refunds WHERE status='pending'"),
               "refunds_amount_max": lambda: one("SELECT COALESCE(MAX(amount),0) FROM refunds"),
               "tickets": lambda: one("SELECT COUNT(*) FROM tickets")}[key]()
        ok = got <= val if key == "refunds_amount_max" else got == val
        if not ok:
            errors.append(f"db.{key}={got}, așteptat {'≤' if key == 'refunds_amount_max' else ''}{val}")
    transcript = " ".join(r["content"] for r in conn.execute("SELECT content FROM transcript WHERE session_id=?", (sid,)))
    for s in exp.get("transcript_not_contains", []):
        if s in transcript:
            errors.append(f"transcriptul conține date sensibile: {s!r}")
    return errors


def eval_agent(brain_kind: str, only: str | None) -> dict:
    cases = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))
    if only:
        cases = [c for c in cases if only in c["id"]]
    kb = KnowledgeBase()
    brain = make_brain(brain_kind)
    by_cat = defaultdict(lambda: [0, 0])
    rows, tokens = [], {"input": 0, "output": 0}
    with tempfile.TemporaryDirectory() as tmp:
        for i, case in enumerate(cases):
            conn = db.connect(Path(tmp) / f"{i}.db")
            db.seed(conn)
            user = dict(conn.execute("SELECT * FROM users WHERE id=?", (case["user"],)).fetchone())
            sid = agent.create_session(conn, user)["id"]
            t0 = time.time()
            try:
                results = [agent.handle_message(conn, kb, brain, user, sid, t) for t in case["turns"]]
                errors = check_case(case, conn, sid, results)
            except Exception as e:
                results, errors = [], [f"excepție: {type(e).__name__}: {e}"]
            m = business_metrics(conn)
            tokens["input"] += m["llm_tokens"]["input"]
            tokens["output"] += m["llm_tokens"]["output"]
            by_cat[case["category"]][0] += not errors
            by_cat[case["category"]][1] += 1
            rows.append({"id": case["id"], "category": case["category"], "pass": not errors, "errors": errors,
                         "seconds": round(time.time() - t0, 2),
                         "last_reply": results[-1]["reply"] if results else None})
            mark = "✅" if not errors else "❌"
            print(f"{mark} {case['id']:<28} {case['category']:<10} {'; '.join(errors)}", flush=True)
            conn.close()
    passed = sum(r["pass"] for r in rows)
    return {"brain": brain.name, "passed": passed, "total": len(rows),
            "pass_rate": round(passed / max(len(rows), 1), 3),
            "by_category": {k: f"{v[0]}/{v[1]}" for k, v in sorted(by_cat.items())},
            "llm_tokens": tokens,
            "llm_cost_usd": round(tokens["input"] * 4 / 1e6 + tokens["output"] * 20 / 1e6, 4),
            "cases": rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brain", default="offline", choices=["offline", "claude", "auto"])
    ap.add_argument("--only", help="rulează doar cazurile al căror id conține acest text")
    ap.add_argument("--out", help="salvează raportul JSON aici")
    args = ap.parse_args()

    retrieval = eval_retrieval(KnowledgeBase())
    print(f"\n== Retrieval: hit@1={retrieval['hit@1']} hit@3={retrieval['hit@3']} MRR={retrieval['mrr']} "
          f"out-of-scope respinse={retrieval['out_of_scope_rejected']}")
    for f in retrieval["failures"]:
        print("   ✗", f)
    print("\n== Agent end-to-end")
    report = eval_agent(args.brain, args.only)
    print(f"\n== {report['passed']}/{report['total']} cazuri trecute ({report['pass_rate']:.0%}) — brain={report['brain']}")
    print("   pe categorii:", report["by_category"])
    if report["llm_tokens"]["input"]:
        print(f"   tokeni: {report['llm_tokens']}  cost estimat: ${report['llm_cost_usd']}")
    if args.out:
        Path(args.out).write_text(json.dumps({"retrieval": retrieval, "agent": report}, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    sys.exit(0 if report["passed"] == report["total"] else 1)


if __name__ == "__main__":
    main()
