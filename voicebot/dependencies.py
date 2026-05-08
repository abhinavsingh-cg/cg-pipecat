# """
# Shared FastAPI dependencies and lifespan hooks.
# """
# from __future__ import annotations

# import logging
# import os
# from contextlib import asynccontextmanager
# from typing import Any, AsyncIterator

# from fastapi import FastAPI, Request

# import voicebot.config  # noqa: F401  Ensures .env is loaded before DB env reads.

# logger = logging.getLogger("voicebot.dependencies")

# DB_STATE_KEY = "db"
# _REQUIRED_DB_ENV_VARS = (
#     "VOICE_BOT_DB_NAME",
#     "VOICE_BOT_DB_HOST",
#     "VOICE_BOT_DB_PORT",
#     "VOICE_BOT_DB_USER",
#     "VOICE_BOT_DB_PASSWORD",
# )


# def _bool_env(name: str, default: bool = False) -> bool:
#     value = os.getenv(name)
#     if value is None:
#         return default
#     return value.lower() not in {"0", "false", "no", "off"}


# def _db_env_configured() -> bool:
#     return any(os.getenv(name) for name in _REQUIRED_DB_ENV_VARS)


# def _build_db_config() -> dict[str, Any]:
#     missing = [name for name in _REQUIRED_DB_ENV_VARS if not os.getenv(name)]
#     if missing:
#         joined = ", ".join(missing)
#         raise RuntimeError(f"Missing required database env vars: {joined}")

#     read_replica_port = os.getenv("VOICE_BOT_READ_REPLICA_DB_PORT")
#     return {
#         "database": os.environ["VOICE_BOT_DB_NAME"],
#         "host": os.environ["VOICE_BOT_DB_HOST"],
#         "port": int(os.environ["VOICE_BOT_DB_PORT"]),
#         "user": os.environ["VOICE_BOT_DB_USER"],
#         "password": os.environ["VOICE_BOT_DB_PASSWORD"],
#         "enable_read_replica": _bool_env(
#             "VOICE_BOT_ENABLE_READ_REPLICA",
#             default=False,
#         ),
#         "read_replica_host": os.getenv("VOICE_BOT_READ_REPLICA_DB_HOST"),
#         "read_replica_port": int(read_replica_port) if read_replica_port else None,
#     }


# async def init_db() -> Any:
#     try:
#         from cg_database import Postgres
#     except ModuleNotFoundError as exc:
#         raise RuntimeError(
#             "cg_database is not installed in the current runtime environment."
#         ) from exc

#     db = Postgres(**_build_db_config(), log_level="INFO")
#     await db.connect()
#     logger.info("database connection established")
#     return db


# async def close_db(db: Any | None) -> None:
#     if db is None:
#         return
#     await db.close()
#     logger.info("database connection closed")


# def get_db(request: Request) -> Any:
#     db = getattr(request.app.state, DB_STATE_KEY, None)
#     if db is None:
#         raise RuntimeError("Database client has not been initialized")
#     return db


# @asynccontextmanager
# async def db_lifespan(app: FastAPI) -> AsyncIterator[None]:
#     app.state.db = None
#     if not _db_env_configured():
#         logger.info("database env vars not set; skipping database initialization")
#         yield
#         return

#     app.state.db = await init_db()
#     try:
#         yield
#     finally:
#         try:
#             await close_db(app.state.db)
#         finally:
#             app.state.db = None
