from __future__ import annotations

import asyncio
import sys
import types

from fastapi import FastAPI

from voicebot import dependencies


class _FakePostgres:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.connected = False
        self.closed = False

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True


def _set_required_db_env(monkeypatch) -> None:
    monkeypatch.setenv("VOICE_BOT_DB_NAME", "voicebot")
    monkeypatch.setenv("VOICE_BOT_DB_HOST", "localhost")
    monkeypatch.setenv("VOICE_BOT_DB_PORT", "5432")
    monkeypatch.setenv("VOICE_BOT_DB_USER", "postgres")
    monkeypatch.setenv("VOICE_BOT_DB_PASSWORD", "secret")


def test_init_db_connects_with_expected_config(monkeypatch) -> None:
    _set_required_db_env(monkeypatch)
    monkeypatch.setenv("VOICE_BOT_ENABLE_READ_REPLICA", "true")
    monkeypatch.setenv("VOICE_BOT_READ_REPLICA_DB_HOST", "replica.local")
    monkeypatch.setenv("VOICE_BOT_READ_REPLICA_DB_PORT", "6432")

    module = types.ModuleType("cg_database")
    module.Postgres = _FakePostgres
    monkeypatch.setitem(sys.modules, "cg_database", module)

    db = asyncio.run(dependencies.init_db())

    assert db.connected is True
    assert db.kwargs == {
        "database": "voicebot",
        "host": "localhost",
        "port": 5432,
        "user": "postgres",
        "password": "secret",
        "enable_read_replica": True,
        "read_replica_host": "replica.local",
        "read_replica_port": 6432,
        "log_level": "INFO",
    }


def test_db_lifespan_initializes_and_closes_db(monkeypatch) -> None:
    _set_required_db_env(monkeypatch)
    app = FastAPI()
    db = _FakePostgres()

    async def _fake_init_db():
        await db.connect()
        return db

    monkeypatch.setattr(dependencies, "init_db", _fake_init_db)

    async def _run() -> None:
        async with dependencies.db_lifespan(app):
            assert app.state.db is db
            assert db.connected is True
            assert db.closed is False
        assert app.state.db is None
        assert db.closed is True

    asyncio.run(_run())


def test_db_lifespan_skips_init_when_db_env_is_missing(monkeypatch) -> None:
    for name in (
        "VOICE_BOT_DB_NAME",
        "VOICE_BOT_DB_HOST",
        "VOICE_BOT_DB_PORT",
        "VOICE_BOT_DB_USER",
        "VOICE_BOT_DB_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)

    app = FastAPI()
    called = False

    async def _fake_init_db():
        nonlocal called
        called = True
        return _FakePostgres()

    monkeypatch.setattr(dependencies, "init_db", _fake_init_db)

    async def _run() -> None:
        async with dependencies.db_lifespan(app):
            assert app.state.db is None
        assert app.state.db is None

    asyncio.run(_run())
    assert called is False
