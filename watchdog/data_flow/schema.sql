-- Data Flow Monitoring Engine - Database Schema
-- Compatible with MySQL 5.5+

CREATE TABLE IF NOT EXISTS `watchdog_source_registry` (
    `id`                INT          NOT NULL AUTO_INCREMENT,
    `foundry_line_id`   INT          NOT NULL,
    `source_name`       VARCHAR(60)  NOT NULL,
    `behaviour_type`    ENUM(
                          'continuous',
                          'shift_completion',
                          'periodic_manual',
                          'event_driven'
                        )            NOT NULL DEFAULT 'continuous',
    `is_active`         TINYINT(1)   NOT NULL DEFAULT 1,
    `discovery_mode`    ENUM(
                          'auto',
                          'manual',
                          'disabled'
                        )            NOT NULL DEFAULT 'auto',
    `confidence_state`  ENUM(
                          'learning',
                          'calibrated',
                          'suspended'
                        )            NOT NULL DEFAULT 'learning',
    `first_seen`        DATETIME     NULL,
    `last_seen`         DATETIME     NULL,
    `created_at`        DATETIME     NULL,
    `updated_at`        DATETIME     NULL,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_line_source` (`foundry_line_id`, `source_name`),
    INDEX `idx_line` (`foundry_line_id`),
    INDEX `idx_active` (`is_active`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


CREATE TABLE IF NOT EXISTS `watchdog_source_rhythm` (
    `id`                  INT          NOT NULL AUTO_INCREMENT,
    `foundry_line_id`     INT          NOT NULL,
    `source_name`         VARCHAR(60)  NOT NULL,
    `gap_p50_seconds`     FLOAT        NULL,
    `gap_p90_seconds`     FLOAT        NULL,
    `gap_p95_seconds`     FLOAT        NULL,
    `gap_p99_seconds`     FLOAT        NULL,
    `records_per_hour`    FLOAT        NULL,
    `sample_count`        INT          NULL,
    `baseline_days`       INT          NULL,
    `baseline_start`      DATETIME     NULL,
    `baseline_end`        DATETIME     NULL,
    `learned_at`          DATETIME     NULL,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_line_source` (`foundry_line_id`, `source_name`),
    INDEX `idx_line` (`foundry_line_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


CREATE TABLE IF NOT EXISTS `watchdog_data_health` (
    `id`                  INT          NOT NULL AUTO_INCREMENT,
    `foundry_line_id`     INT          NOT NULL,
    `source_name`         VARCHAR(60)  NOT NULL,
    `status`              ENUM(
                            'healthy',
                            'delayed',
                            'stale',
                            'missing',
                            'learning',
                            'expected_silence',
                            'unknown'
                          )            NOT NULL DEFAULT 'unknown',
    `last_record_at`      DATETIME     NULL,
    `gap_seconds`         FLOAT        NULL,
    `gap_vs_p99`          FLOAT        NULL,
    `operating_context`   ENUM(
                            'running',
                            'planned_off',
                            'unplanned_stop',
                            'unknown'
                          )            NOT NULL DEFAULT 'unknown',
    `alert_fired`         TINYINT(1)   NOT NULL DEFAULT 0,
    `suppress_process`    TINYINT(1)   NOT NULL DEFAULT 0,
    `checked_at`          DATETIME     NULL,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_line_source` (`foundry_line_id`, `source_name`),
    INDEX `idx_line` (`foundry_line_id`),
    INDEX `idx_status` (`status`),
    INDEX `idx_suppress` (`suppress_process`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


CREATE TABLE IF NOT EXISTS `watchdog_data_annotations` (
    `id`               INT          NOT NULL AUTO_INCREMENT,
    `foundry_line_id`  INT          NOT NULL,
    `gap_start`        DATETIME     NOT NULL,
    `gap_end`          DATETIME     NULL,
    `annotation_type`  ENUM(
                         'planned_shutdown',
                         'unplanned_shutdown',
                         'breakdown',
                         'holiday',
                         'maintenance',
                         'plc_failure',
                         'network_failure',
                         'data_pipeline_failure',
                         'unknown_data_loss',
                         'false_alarm'
                       )            NOT NULL,
    `sources_affected` LONGTEXT     NULL,
    `confirmed_by`     VARCHAR(120) NULL,
    `notes`            TEXT         NULL,
    `confirmation_sent_at` DATETIME NULL,
    `confirmed_at`     DATETIME     NULL,
    `created_at`       DATETIME     NULL,
    `updated_at`       DATETIME     NULL,
    PRIMARY KEY (`id`),
    INDEX `idx_line` (`foundry_line_id`),
    INDEX `idx_gap_start` (`gap_start`),
    INDEX `idx_open` (`foundry_line_id`, `gap_end`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
