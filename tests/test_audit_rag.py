from support_agent import audit


def test_audit_chain_detects_tampering(conn):
    for i in range(3):
        audit.log(conn, "test", "x", {"i": i})
    assert audit.verify(conn) == {"ok": True, "checked": 3}
    conn.execute("UPDATE audit_log SET details='{\"i\": 99}' WHERE id=2")
    result = audit.verify(conn)
    assert result["ok"] is False and result["broken_at_id"] == 2


def test_audit_redacts_pii(conn):
    audit.log(conn, "test", "x", {"text": "cardul meu 4111 1111 1111 1111"})
    assert "4111" not in conn.execute("SELECT details FROM audit_log").fetchone()[0]


def test_rag_finds_and_rejects(kb):
    assert kb.search("Care este termenul de retur?")[0].chunk.source_id == "politica-retur#termen-de-retur"
    assert kb.search("Câte planete are Jupiter?") == []
    assert all("#" in c.source_id for c in kb.chunks)
