import json

with open('watchdog/config/caspro_sandman_L1.json') as f:
    cfg = json.load(f)

cfg.pop('foundry_configs', None)

j = json.dumps(cfg, ensure_ascii=False).replace("'", "''")

sql = (
    "INSERT INTO `watchdog_si_config` (`foundry_label`, `config_json`, `updated_at`) "
    f"VALUES ('caspro_sandman_L1', '{j}', NOW()) "
    "ON DUPLICATE KEY UPDATE `config_json` = VALUES(`config_json`), `updated_at` = NOW();"
)

with open('caspro_insert.sql', 'w', encoding='utf-8') as out:
    out.write(sql)

print("Written to caspro_insert.sql")
