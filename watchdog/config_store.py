"""
watchdog/config_store.py
-------------------------
Per-foundry-line watchdog configuration stored in the central registry DB.

Table: watchdog_si_config  (created in registry_database, e.g. sandman_dev)
  foundry_label  VARCHAR(120)  -- e.g. "caspro_sandman_L1"
  config_json    JSON          -- full per-line config dict
  updated_at     DATETIME      -- auto-updated on every save

Benefits over JSON file storage
--------------------------------
  - All server instances share the same config automatically
  - Changes made in the UI take effect on all watchdog daemons at next refresh
  - Full history via updated_at (extend to audit table later)
  - Falls back to watchdog_config.json["foundry_configs"] when DB is unavailable
"""

import json
import logging
import pathlib
from datetime import datetime
from typing import Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)

# Local per-foundry JSON files live alongside the infrastructure config file.
_CONFIG_DIR = pathlib.Path(__file__).parent / "config"


def _foundry_file_path(label: str, config_dir: pathlib.Path = None) -> pathlib.Path:
    d = pathlib.Path(config_dir) if config_dir else _CONFIG_DIR
    return d / f"{label}.json"


def save_foundry_config_file(label: str, config: dict, config_dir=None) -> bool:
    """Write per-foundry config to a local JSON file as a local backup of the DB row."""
    path = _foundry_file_path(label, config_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("foundry config file written: %s", path.name)
        return True
    except Exception as exc:
        logger.warning("save_foundry_config_file(%s) failed: %s", label, exc)
        return False


def load_foundry_config_file(label: str, config_dir=None) -> dict:
    """Load per-foundry config from local JSON file (fallback when DB is unavailable)."""
    path = _foundry_file_path(label, config_dir)
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("load_foundry_config_file(%s) failed: %s", label, exc)
    return {}

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS `watchdog_si_config` (
    `id`            INT          NOT NULL AUTO_INCREMENT,
    `foundry_label` VARCHAR(120) NOT NULL COMMENT 'e.g. caspro_sandman_L1',
    `config_json`   LONGTEXT     NOT NULL,
    `updated_at`    TIMESTAMP    NOT NULL
                    DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE  KEY `uk_label` (`foundry_label`),
    INDEX   `idx_updated` (`updated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='Per-foundry-line SI watchdog configuration';
"""

def get_registry_engine(base_config: dict) -> Optional[Engine]:
    """
    Return a SQLAlchemy engine for the central registry DB
    (registry_database in watchdog_config.json).
    Returns None when registry_database is not configured.
    """
    reg = base_config.get("registry_database") or base_config.get("database")
    if not reg:
        return None
    host     = reg.get("host", "localhost")
    port     = int(reg.get("port", 3306))
    user     = reg.get("user", "root")
    password = reg.get("password", "")
    dbname   = reg.get("name", "")
    timeout  = int(reg.get("connect_timeout", 10))
    url = (
        f"mysql+pymysql://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{dbname}?charset=utf8mb4"
    )
    return create_engine(
        url,
        pool_size    = 2,
        max_overflow = 2,
        pool_pre_ping= True,
        pool_recycle = 3600,
        connect_args = {"connect_timeout": timeout},
        echo         = False,
    )

def ensure_config_table(engine: Engine) -> None:
    """Create watchdog_si_config if it does not exist."""
    try:
        with engine.begin() as conn:
            conn.execute(text(_CREATE_SQL))
        logger.info("watchdog_si_config table ready")
    except Exception as exc:
        logger.warning("ensure_config_table failed: %s", exc)

def load_foundry_config(engine: Engine, label: str) -> dict:
    """Return the stored config dict for one foundry label, or {} if not found.
    Falls back to the local per-foundry JSON file when the DB has no row."""
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT `config_json` FROM `watchdog_si_config` WHERE `foundry_label` = :lbl"),
                {"lbl": label},
            ).mappings().first()
        if not row:
            return load_foundry_config_file(label)
        raw = row["config_json"]
        return json.loads(raw) if isinstance(raw, str) else (dict(raw) if raw else {})
    except Exception as exc:
        logger.debug("load_foundry_config(%s) DB unavailable, trying file fallback: %s", label, exc)
        return load_foundry_config_file(label)

def load_all_configs(engine: Engine, since: datetime | None = None) -> dict:
    """
    Return {foundry_label: config_dict} for stored configs.

    Pass `since` (a datetime) to only fetch rows updated after that timestamp —
    use this on periodic reloads to avoid a full table scan every 60 s.
    Omit `since` (or pass None) on startup to load everything.
    """
    try:
        if since is not None:
            sql = text(
                "SELECT `foundry_label`, `config_json` FROM `watchdog_si_config`"
                " WHERE `updated_at` > :since"
            )
            params: dict = {"since": since}
        else:
            sql = text("SELECT `foundry_label`, `config_json` FROM `watchdog_si_config`")
            params = {}

        with engine.connect() as conn:
            rows = conn.execute(sql, params).mappings().fetchall()
        result = {}
        for row in rows:
            raw = row["config_json"]
            result[row["foundry_label"]] = (
                json.loads(raw) if isinstance(raw, str) else (dict(raw) if raw else {})
            )
        if result:
            logger.info("load_all_configs: %d foundry config(s) loaded from DB", len(result))
        return result
    except Exception as exc:
        logger.warning("load_all_configs failed: %s", exc)
        return {}

def seed_foundry_configs(engine: Engine, foundry_configs: dict) -> int:
    """
    Seed default configs from the JSON file into the DB for any label that has
    no existing row.  Uses INSERT IGNORE so it never overwrites values that were
    already saved via the Config UI.

    Returns the number of rows actually inserted.
    """
    if not foundry_configs:
        return 0
    sql = text("""
        INSERT IGNORE INTO `watchdog_si_config` (`foundry_label`, `config_json`, `updated_at`)
        VALUES (:lbl, :cfg, :now)
    """)
    inserted = 0
    now = datetime.now()
    for label, cfg in foundry_configs.items():
        try:
            with engine.begin() as conn:
                result = conn.execute(sql, {
                    "lbl": label,
                    "cfg": json.dumps(cfg, ensure_ascii=False),
                    "now": now,
                })
                if result.rowcount:
                    inserted += 1
                    logger.info("watchdog_si_config seeded default for: %s", label)
        except Exception as exc:
            logger.warning("seed_foundry_configs(%s) failed: %s", label, exc)
    return inserted

def save_foundry_config_db(engine: Engine, label: str, config: dict) -> bool:
    """
    Upsert one foundry-line config into watchdog_si_config and also write a
    local per-foundry JSON file at watchdog/config/{label}.json as a backup.
    Returns True on success.
    """
    sql = text("""
        INSERT INTO `watchdog_si_config` (`foundry_label`, `config_json`, `updated_at`)
        VALUES (:lbl, :cfg, :now)
        ON DUPLICATE KEY UPDATE
            `config_json` = VALUES(`config_json`),
            `updated_at`  = VALUES(`updated_at`)
    """)
    try:
        with engine.begin() as conn:
            conn.execute(sql, {
                "lbl": label,
                "cfg": json.dumps(config, ensure_ascii=False),
                "now": datetime.now(),
            })
        logger.info("watchdog_si_config saved: %s", label)
        save_foundry_config_file(label, config)
        return True
    except Exception as exc:
        logger.error("save_foundry_config_db(%s) failed: %s", label, exc)
        return False
