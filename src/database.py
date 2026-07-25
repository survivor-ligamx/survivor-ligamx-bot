#!/usr/bin/env python3
"""
database.py — Capa de persistencia unificada (Postgres en prod, SQLite en local).
"""
import json
import logging
import os
import re
import unicodedata
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, Generator, List, Optional, cast
if TYPE_CHECKING:
    from psycopg2.pool import ThreadedConnectionPool
logger = logging.getLogger(__name__)
DATABASE_URL = os.getenv("DATABASE_URL", "") or ""
_pool: Optional["ThreadedConnectionPool"] = None
def _get_pool() -> Optional["ThreadedConnectionPool"]:
    global _pool
    if _pool is None:
        try:
            from psycopg2.pool import ThreadedConnectionPool
        except ImportError:
            return None
        dsn = DATABASE_URL
        if "sslmode=" not in dsn:
            dsn = dsn + ("?" if "?" not in dsn else "&") + "sslmode=require"
        _pool = ThreadedConnectionPool(minconn=1, maxconn=5, dsn=dsn)
    return _pool
def _es_postgres(url: str) -> bool:
    return url.startswith("postgres://") or url.startswith("postgresql://")
USE_POSTGRES = _es_postgres(DATABASE_URL)
PH = "%s" if USE_POSTGRES else "?"
SQLITE_PATH = DATABASE_URL if (DATABASE_URL and not USE_POSTGRES) else os.path.join("data", "premium_history.db")
SURVIVOR_ESTADOS = {"recomendado", "confirmado", "bloqueado", "resuelto", "cancelado"}
SURVIVOR_RESULTADOS = {"gano", "empate", "perdio"}
SURVIVOR_LEGACY_MIGRATION = "2026-07-survivor-usados-por-temporada-v1"
SURVIVOR_SEED_MIGRATION = "2026-07-survivor-apertura-picks-v1"
