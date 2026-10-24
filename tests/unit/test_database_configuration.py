import pytest
from neo4j.exceptions import ServiceUnavailable

from backend.database import Neo4jDatabase


def test_neo4j_driver_requires_an_explicit_password(monkeypatch):
    monkeypatch.setattr("backend.database.settings.NEO4J_PASSWORD", None)
    database = Neo4jDatabase()

    with pytest.raises(ServiceUnavailable, match="NEO4J_PASSWORD"):
        database.connect()


def test_neo4j_driver_uses_configured_uri_database_and_credentials(monkeypatch):
    from backend import database as database_module

    captured = {}
    sentinel_driver = object()

    def create_driver(uri, **kwargs):
        captured["uri"] = uri
        captured.update(kwargs)
        return sentinel_driver

    monkeypatch.setattr("backend.database.settings.NEO4J_PASSWORD", "test-only-secret")
    monkeypatch.setattr("backend.database.settings.NEO4J_URI", "bolt://test-host:7687")
    monkeypatch.setattr("backend.database.settings.NEO4J_USERNAME", "test-user")
    monkeypatch.setattr(database_module.GraphDatabase, "driver", create_driver)
    database = Neo4jDatabase()

    assert database.connect() is sentinel_driver
    assert captured["uri"] == "bolt://test-host:7687"
    assert captured["auth"] == ("test-user", "test-only-secret")
