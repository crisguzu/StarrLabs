"""Shared, bounded PostgreSQL access for every BrawlStarsAnalytics process."""

from __future__ import annotations

import atexit
import os
import threading
from dataclasses import dataclass
from typing import Optional

import psycopg2
from psycopg2 import extensions
from psycopg2.pool import ThreadedConnectionPool


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    name: str
    user: str
    password: str
    sslmode: str
    connect_timeout: int
    statement_timeout_ms: int
    pool_min: int
    pool_max: int
    target: str
    expected_name: str

    @classmethod
    def from_env(cls) -> "DatabaseConfig":
        required = ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_TARGET")
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            raise RuntimeError(f"Missing required database settings: {', '.join(missing)}")

        host = os.environ["DB_HOST"].strip().lower()
        reject_supabase = os.getenv("REJECT_SUPABASE_HOSTS", "1") == "1"
        if reject_supabase and "supabase" in host:
            raise RuntimeError("Supabase database hosts are rejected by policy")

        allowed = {
            item.strip().lower()
            for item in os.getenv("DB_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")
            if item.strip()
        }
        if host not in allowed:
            raise RuntimeError(f"Database host {host!r} is outside DB_ALLOWED_HOSTS")

        target = os.environ["DB_TARGET"].strip()
        if target != "self-hosted-postgres":
            raise RuntimeError(f"Unexpected DB_TARGET {target!r}")

        name = os.environ["DB_NAME"].strip()
        expected_name = os.getenv("DB_EXPECTED_NAME", name).strip()
        if name != expected_name:
            raise RuntimeError(f"DB_NAME {name!r} does not match DB_EXPECTED_NAME")

        pool_min = int(os.getenv("DB_POOL_MIN", "1"))
        pool_max = int(os.getenv("DB_POOL_MAX", "8"))
        if pool_min < 1 or pool_max < pool_min or pool_max > 32:
            raise RuntimeError("Invalid DB_POOL_MIN/DB_POOL_MAX bounds")

        return cls(
            host=host,
            port=int(os.getenv("DB_PORT", "55432")),
            name=name,
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"],
            sslmode=os.getenv("DB_SSLMODE", "disable"),
            connect_timeout=int(os.getenv("DB_CONNECT_TIMEOUT_SECONDS", "5")),
            statement_timeout_ms=int(os.getenv("DB_STATEMENT_TIMEOUT_MS", "300000")),
            pool_min=pool_min,
            pool_max=pool_max,
            target=target,
            expected_name=expected_name,
        )


class DatabasePoolBusyError(RuntimeError):
    """The configured connection budget was not available within its wait bound."""


class _PooledConnection:
    def __init__(self, owner: "DatabasePool", raw):
        self._owner = owner
        self._raw = raw
        self._closed = False

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            if exc_type is None:
                self._raw.commit()
            else:
                self._raw.rollback()
        finally:
            self.close()
        return False

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._owner.release(self._raw)


class DatabasePool:
    def __init__(self, config: DatabaseConfig):
        self.config = config
        self._slots = threading.BoundedSemaphore(config.pool_max)
        self._pool = ThreadedConnectionPool(
            config.pool_min,
            config.pool_max,
            host=config.host,
            port=config.port,
            user=config.user,
            password=config.password,
            dbname=config.name,
            sslmode=config.sslmode,
            connect_timeout=config.connect_timeout,
            application_name=os.getenv("DB_APPLICATION_NAME", "brawlstarsanalytics"),
            options=f"-c statement_timeout={config.statement_timeout_ms}",
        )
        raw = self._pool.getconn()
        try:
            self._assert_identity(raw)
        finally:
            self._pool.putconn(raw)

    def _assert_identity(self, conn):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT current_database(), current_setting('server_version_num')::int, "
                "inet_server_addr()::text, current_user"
            )
            database, version_num, server_address, user = cur.fetchone()
        conn.rollback()
        if database != self.config.expected_name:
            raise RuntimeError(f"Connected to unexpected database {database!r}")
        if version_num // 10000 != 17:
            raise RuntimeError(f"PostgreSQL 17 required; server reported {version_num}")
        self.identity = {
            "target": self.config.target,
            "database": database,
            "postgres_major": version_num // 10000,
            "server_address": server_address,
            "user": user,
        }

    def connect(self) -> _PooledConnection:
        if not self._slots.acquire(timeout=self.config.connect_timeout):
            raise DatabasePoolBusyError("Database connection budget is temporarily busy")
        try:
            raw = self._pool.getconn()
            if raw.closed:
                self._pool.putconn(raw, close=True)
                raw = self._pool.getconn()
            return _PooledConnection(self, raw)
        except BaseException:
            self._slots.release()
            raise

    def release(self, raw):
        close = bool(raw.closed)
        try:
            if not close and raw.get_transaction_status() != extensions.TRANSACTION_STATUS_IDLE:
                raw.rollback()
        except Exception:
            close = True
        finally:
            try:
                self._pool.putconn(raw, close=close)
            finally:
                self._slots.release()

    def closeall(self):
        self._pool.closeall()


_lock = threading.Lock()
_pool: Optional[DatabasePool] = None


def get_pool() -> DatabasePool:
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                _pool = DatabasePool(DatabaseConfig.from_env())
                atexit.register(_pool.closeall)
    return _pool


def connect() -> _PooledConnection:
    return get_pool().connect()


def identity() -> dict:
    return dict(get_pool().identity)


if __name__ == "__main__":
    info = identity()
    print(
        "DB_IDENTITY|"
        f"target={info['target']}|database={info['database']}|"
        f"postgres_major={info['postgres_major']}|server_address={info['server_address']}|"
        f"user={info['user']}"
    )
