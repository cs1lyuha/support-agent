import pytest
from fastapi.testclient import TestClient

from support_agent import db
from support_agent.api import create_app
from support_agent.rag import KnowledgeBase


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.seed(c)
    yield c
    c.close()


@pytest.fixture
def kb():
    return KnowledgeBase()


@pytest.fixture
def user(conn):
    return lambda uid: dict(conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "api.db"), brain_kind="offline")
    return TestClient(app)


def auth(token):
    return {"Authorization": f"Bearer {token}"}
