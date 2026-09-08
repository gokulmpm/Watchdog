"""
pipeline/db_connector.py
-------------------------
SQLAlchemy MySQL connection manager for the AI Watchdog pipeline.
Credentials are read from watchdog_config.json.
Optional SSL: set ssl_ca / ssl_cert / ssl_key in the database section.
"""

import json
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

try:
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import Engine
    from sqlalchemy.pool import QueuePool
except ImportError:
    raise ImportError("Install SQLAlchemy:  pip install sqlalchemy pymysql")

import pandas as pd

logger = logging.getLogger(__name__)

_HERE   = Path(__file__).resolve().parent
_CONFIG = _HERE.parent / "config" / "watchdog_config.json"

_engines: dict[str, Engine] = {}


def load_config(path: Path = _CONFIG) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _engine_key(db_cfg: dict) -> str:
    return f"{db_cfg['host']}:{db_cfg.get('port',3306)}/{db_cfg['name']}#{db_cfg['user']}"


def get_engine(config: Optional[dict] = None) -> Engine:
    """Return a cached SQLAlchemy Engine for the configured database."""
    cfg    = config or load_config()
    db_cfg = cfg["database"]
    key    = _engine_key(db_cfg)

    if key in _engines:
        return _engines[key]

    host     = db_cfg.get("host", "localhost")
    port     = int(db_cfg.get("port", 3306))
    user     = db_cfg.get("user", "root")
    password = db_cfg.get("password", "")
    dbname   = db_cfg.get("name", "caspro_sandman")
    pool_sz  = int(db_cfg.get("pool_size", 3))
    timeout  = int(db_cfg.get("connect_timeout", 10))

    connect_args: dict = {"connect_timeout": timeout}
    ssl_ca   = db_cfg.get("ssl_ca")
    ssl_cert = db_cfg.get("ssl_cert")
    ssl_key  = db_cfg.get("ssl_key")
    if ssl_ca or ssl_cert:
        ssl_args: dict = {}
        if ssl_ca:   ssl_args["ssl_ca"]   = ssl_ca
        if ssl_cert: ssl_args["ssl_cert"] = ssl_cert
        if ssl_key:  ssl_args["ssl_key"]  = ssl_key
        connect_args["ssl"] = ssl_args
        logger.info("DB SSL/TLS enabled (ca=%s)", ssl_ca or "not set")

    url = (
        f"mysql+pymysql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{dbname}"
        "?charset=utf8mb4"
    )

    engine = create_engine(
        url,
        poolclass     = QueuePool,
        pool_size     = pool_sz,
        max_overflow  = pool_sz,
        pool_pre_ping = True,
        pool_recycle  = 3600,
        connect_args  = connect_args,
        echo          = False,
    )

    _engines[key] = engine
    logger.info("SQLAlchemy engine created (%s@%s:%s/%s  pool=%d)",
                user, host, port, dbname, pool_sz)
    return engine


@contextmanager
def get_connection(config: Optional[dict] = None):
    """Context manager yielding an open SQLAlchemy connection."""
    engine = get_engine(config)
    with engine.connect() as conn:
        logger.debug("DB connection acquired from pool")
        yield conn
    logger.debug("DB connection returned to pool")


def query_df(sql: str, params=None, config: Optional[dict] = None) -> pd.DataFrame:
    """Execute a SELECT and return a pandas DataFrame."""
    engine = get_engine(config)
    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn, params=params)


def test_connection(config: Optional[dict] = None) -> bool:
    """Return True if the database is reachable, False otherwise."""
    try:
        engine = get_engine(config)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("DB connection test: OK")
        return True
    except Exception as exc:
        logger.error("DB connection test FAILED: %s", exc)
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG,
                        format="%(levelname)s  %(name)s  %(message)s")
    ok = test_connection()
    print("Connection:", "OK" if ok else "FAILED")
