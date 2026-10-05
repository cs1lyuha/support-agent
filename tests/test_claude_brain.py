"""Testează bucla de tool use a ClaudeBrain cu un client fals — fără rețea și fără cheie API."""
import copy
from types import SimpleNamespace

from support_agent import agent
from support_agent.brains import ClaudeBrain


def block(**kw):
    ns = SimpleNamespace(**kw)
    ns.model_dump = lambda **_: dict(kw)
    return ns


def response(content, stop):
    return SimpleNamespace(content=content, stop_reason=stop, model="claude-opus-5-5",
                           usage=SimpleNamespace(input_tokens=100, output_tokens=20))


class FakeClient:
    """Joacă rolul modelului: caută în documentație, apoi răspunde cu o citare reală și una inventată."""

    def __init__(self, script):
        self.script, self.calls = script, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.calls.append(copy.deepcopy(kw))
        return self.script.pop(0)


def test_tool_loop_citations_and_usage(conn, kb, user):
    fake = FakeClient([
        response([block(type="tool_use", id="t1", name="search_knowledge_base", input={"query": "termen retur"})],
                 "tool_use"),
        response([block(type="text", text="Aveți 30 de zile [politica-retur#termen-de-retur] [fals#x].")], "end_turn"),
    ])
    ana = user("c1")
    sid = agent.create_session(conn, ana)["id"]
    r = agent.handle_message(conn, kb, ClaudeBrain(fake), ana, sid, "Care e termenul de retur?")

    assert [c["source_id"] for c in r["citations"]] == ["politica-retur#termen-de-retur"]
    assert "citare_inventata:fals#x" in r["warnings"]
    # a doua cerere conține tool_result-ul pentru t1
    second = fake.calls[1]["messages"]
    assert second[-1]["content"][0]["type"] == "tool_result" and second[-1]["content"][0]["tool_use_id"] == "t1"
    assert all(t["strict"] for t in fake.calls[0]["tools"])
    assert fake.calls[0]["fallbacks"] == "default"
    assert conn.execute("SELECT SUM(input_tokens) FROM usage").fetchone()[0] == 200


def test_model_cannot_bypass_refund_policy(conn, kb, user):
    """Chiar dacă modelul încearcă o rambursare nepermisă, codul o blochează."""
    fake = FakeClient([
        response([block(type="tool_use", id="t1", name="request_refund",
                        input={"order_id": "A1002", "amount": 2450, "reason": "x"})], "tool_use"),
        response([block(type="text", text="Rambursarea a fost aprobată!")], "end_turn"),
    ])
    ana = user("c1")
    sid = agent.create_session(conn, ana)["id"]
    r = agent.handle_message(conn, kb, ClaudeBrain(fake), ana, sid, "vreau banii pe A1002")
    assert conn.execute("SELECT COUNT(*) FROM refunds").fetchone()[0] == 0
    assert "promisiune_falsa_rambursare" in r["warnings"]
    assert "a fost aprobată" not in r["reply"]


def test_api_error_falls_back_to_human(conn, kb, user):
    class Broken:
        beta = SimpleNamespace(messages=SimpleNamespace(create=lambda **_: (_ for _ in ()).throw(RuntimeError("down"))))

    ion = user("c2")
    sid = agent.create_session(conn, ion)["id"]
    r = agent.handle_message(conn, kb, ClaudeBrain(Broken()), ion, sid, "Cât costă livrarea?")
    assert r["state"] == "handoff" and r["guardrail"] == "brain_error"


def test_history_persists_between_turns(conn, kb, user):
    fake = FakeClient([response([block(type="text", text="Salut!")], "end_turn"),
                       response([block(type="text", text="Din nou salut!")], "end_turn")])
    ana = user("c1")
    sid = agent.create_session(conn, ana)["id"]
    brain = ClaudeBrain(fake)
    agent.handle_message(conn, kb, brain, ana, sid, "Bună")
    agent.handle_message(conn, kb, brain, ana, sid, "Mai ești?")
    roles = [m["role"] for m in fake.calls[1]["messages"]]
    assert roles == ["user", "assistant", "user"]
