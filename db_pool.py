# -*- coding: utf-8 -*-
"""Compat layer for desktop modules that expect db_pool.py.

This module exposes:
- DB_CONFIG
- get_db_pool()
- db_connection() context manager
- ConnectionPool class with .pool attribute
"""

from __future__ import annotations

from contextlib import contextmanager
import threading
from typing import Generator

import psycopg2
from psycopg2 import pool

from config import settings

DB_CONFIG = {
    "host": settings.DB_HOST,
    "port": settings.DB_PORT,
    "database": settings.DB_NAME,
    "user": settings.DB_USER,
    "password": settings.DB_PASSWORD,
    "sslmode": "require",
}


class ConnectionPool:
    """Singleton pool wrapper for desktop scripts."""

    _instance = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self.pool = psycopg2.pool.SimpleConnectionPool(
            minconn=1,
            maxconn=10,
            **DB_CONFIG,
        )

    @classmethod
    def get_instance(cls) -> "ConnectionPool":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance


def get_db_pool() -> ConnectionPool:
    """Return shared pool wrapper (legacy-compatible API)."""
    return ConnectionPool.get_instance()


@contextmanager
def db_connection() -> Generator[psycopg2.extensions.connection, None, None]:
    """Provide a transactional connection from the shared pool."""
    conn = get_db_pool().pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        get_db_pool().pool.putconn(conn)
