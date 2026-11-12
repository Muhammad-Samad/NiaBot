-- ============================================================
-- Naheed Shopping Assistant - MySQL Setup
-- Run this in MySQL Workbench before starting the app
--
-- All NiaBot-owned tables are prefixed `ncb_`. Magento tables (sales_order*,
-- nhd_*, nop_*, ...) are NOT created here - they belong to the store.
-- ============================================================

CREATE DATABASE IF NOT EXISTS naheed_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE naheed_db;

-- ─── Bot Sessions ─────────────────────────────────────────────────────────────
-- One row per chat thread (client-supplied sid). Holds per-session state:
-- the Typesense conversation thread, last topic, and (via
-- migrations/add_conversation_memory.sql) conversation_state + customer_key.
CREATE TABLE IF NOT EXISTS ncb_bot_sessions (
    id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    session_id VARCHAR(64) NOT NULL UNIQUE, -- client-supplied id (browser localStorage), not Flask cookie session
    ts_conversation_id VARCHAR(128) NULL, -- Typesense conversation_id (or app.py's fallback id)
    customer_id INT UNSIGNED NULL, -- verified Naheed customer id (cust:<id>), NULL for guests/devices
    last_topic VARCHAR(512) NULL, -- last resolved product search topic, used to contextualize follow-up messages
    created_at DATETIME NOT NULL DEFAULT NOW(),
    updated_at DATETIME NOT NULL DEFAULT NOW() ON UPDATE NOW(),
    INDEX idx_customer_id (customer_id)
) ENGINE = InnoDB;

-- If upgrading an existing install that already has ncb_bot_sessions without
-- the last_topic column, run this once (ignore the error if it's already there):
-- ALTER TABLE ncb_bot_sessions ADD COLUMN last_topic VARCHAR(512) NULL;

-- Conversational context + long-term memory: run
-- migrations/add_conversation_memory.sql (adds ncb_bot_sessions.conversation_state
-- and customer_key, plus ncb_chat_customer_memory and ncb_chat_conversation_summaries).

-- ─── LEGACY: Cart Items / Orders / Order Items ────────────────────────────────
-- From the old standalone shopping bot. No longer written ("+ Add" goes to the
-- Magento cart); services/memory_service.py still reads them if present.
CREATE TABLE IF NOT EXISTS ncb_cart_items (
    id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    session_id VARCHAR(64) NOT NULL,
    product_id VARCHAR(64) NOT NULL, -- Typesense document id
    sku VARCHAR(128) NULL,
    product_name VARCHAR(512) NOT NULL,
    price DECIMAL(10, 2) NOT NULL DEFAULT 0.00,
    quantity INT UNSIGNED NOT NULL DEFAULT 1,
    image_url TEXT NULL,
    category VARCHAR(255) NULL,
    added_at DATETIME NOT NULL DEFAULT NOW(),
    FOREIGN KEY (session_id) REFERENCES ncb_bot_sessions (session_id) ON DELETE CASCADE,
    UNIQUE KEY uq_session_product (session_id, product_id)
) ENGINE = InnoDB;

CREATE TABLE IF NOT EXISTS ncb_orders (
    id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    session_id VARCHAR(64) NOT NULL,
    order_ref VARCHAR(32) NOT NULL UNIQUE, -- e.g. ORD-20240601-0001
    total_amount DECIMAL(10, 2) NOT NULL DEFAULT 0.00,
    status ENUM(
        'pending',
        'confirmed',
        'cancelled'
    ) NOT NULL DEFAULT 'pending',
    customer_name VARCHAR(255) NULL,
    customer_phone VARCHAR(32) NULL,
    customer_address TEXT NULL,
    placed_at DATETIME NOT NULL DEFAULT NOW()
) ENGINE = InnoDB;

CREATE TABLE IF NOT EXISTS ncb_order_items (
    id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    order_id INT UNSIGNED NOT NULL,
    product_id VARCHAR(64) NOT NULL,
    sku VARCHAR(128) NULL,
    product_name VARCHAR(512) NOT NULL,
    price DECIMAL(10, 2) NOT NULL,
    quantity INT UNSIGNED NOT NULL DEFAULT 1,
    FOREIGN KEY (order_id) REFERENCES ncb_orders (id) ON DELETE CASCADE
) ENGINE = InnoDB;

CREATE OR REPLACE VIEW ncb_v_cart_summary AS
SELECT ci.session_id, COUNT(ci.id) AS item_count, SUM(ci.price * ci.quantity) AS cart_total, cs.ts_conversation_id
FROM
    ncb_cart_items ci
    JOIN ncb_bot_sessions cs ON cs.session_id = ci.session_id
GROUP BY
    ci.session_id,
    cs.ts_conversation_id;

-- ─── Conversation Messages (unified chat transcript, every domain) ────────────
CREATE TABLE IF NOT EXISTS ncb_conversation_messages (
    id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    session_id VARCHAR(64) NOT NULL,
    ts_conversation_id VARCHAR(128) NOT NULL, -- Typesense conversation_id (or app.py's fallback id)
    customer_id INT UNSIGNED NULL, -- verified Naheed customer id, NULL for guests/devices
    role ENUM('user', 'assistant') NOT NULL,
    message TEXT NOT NULL,
    products_json LONGTEXT NULL, -- JSON array of product cards (assistant only)
    cart_action_json TEXT NULL, -- JSON cart action if any
    domain VARCHAR(32) NULL, -- domain that handled the turn: shopping / operations / policy / menu
    created_at DATETIME NOT NULL DEFAULT NOW(),
    INDEX idx_session_conv (session_id, ts_conversation_id),
    INDEX idx_customer_id (customer_id),
    INDEX idx_domain (domain)
) ENGINE = InnoDB;

-- If upgrading an existing install, run migrations/add_policy_domain_columns.sql
-- (adds ncb_conversation_messages.domain and ncb_conversation_sessions.ended_at)
-- and migrations/add_ts_conversation_id_and_customer_id.sql.

-- ─── Conversation Sessions (operations/policy log, one row per sid) ───────────
CREATE TABLE `ncb_conversation_sessions` (
    `id` INT(10) NOT NULL AUTO_INCREMENT,
    `session_id` VARCHAR(255) NOT NULL COLLATE 'utf8mb4_unicode_ci',
    `customer_identifier` VARCHAR(255) NULL DEFAULT NULL COLLATE 'utf8mb4_unicode_ci',
    `summary` TEXT NULL DEFAULT NULL COLLATE 'utf8mb4_unicode_ci',
    `started_at` DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
    `last_activity` DATETIME NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    `ended_at` DATETIME NULL DEFAULT NULL,
    `status` VARCHAR(50) NULL DEFAULT 'active' COLLATE 'utf8mb4_unicode_ci',
    PRIMARY KEY (`id`),
    UNIQUE INDEX `session_id` (`session_id`),
    INDEX `last_activity` (`last_activity`),
    CONSTRAINT `ncb_conversation_sessions_chk_1` CHECK (
        `status` IN (
            'active',
            'closed',
            'escalated'
        )
    )
) ENGINE = InnoDB;

-- ─── Intent Logs (operations + policy turns) ──────────────────────────────────
-- One row per operations/policy turn with what the bot understood: intent,
-- flow, confidence, metadata. For policy turns, flow_name = 'policy',
-- intent = 'policy_query' and `metadata` holds the RAG details:
-- {domain, entry, model, grounded, standalone_question, sources[]}.
-- conversation_id = ncb_conversation_sessions.id (NOT the Typesense id).
CREATE TABLE `ncb_intent_logs` (
    `id` INT NOT NULL AUTO_INCREMENT,
    `conversation_id` INT NOT NULL,
    `customer_id` INT UNSIGNED NULL DEFAULT NULL,
    `sender` VARCHAR(50) NOT NULL COLLATE 'utf8mb4_unicode_ci',
    `message` TEXT NOT NULL COLLATE 'utf8mb4_unicode_ci',
    `message_type` VARCHAR(50) NULL DEFAULT NULL COLLATE 'utf8mb4_unicode_ci',
    `intent` VARCHAR(255) NULL DEFAULT NULL COLLATE 'utf8mb4_unicode_ci',
    `flow_name` VARCHAR(255) NULL DEFAULT NULL COLLATE 'utf8mb4_unicode_ci',
    `confidence` FLOAT NULL DEFAULT NULL,
    `metadata` JSON NULL DEFAULT NULL,
    `timestamp` DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    INDEX `conversation_id` (`conversation_id`),
    INDEX `customer_id` (`customer_id`),
    INDEX `timestamp` (`timestamp`),
    INDEX `sender` (`sender`),
    CONSTRAINT `ncb_intent_logs_ibfk_1` FOREIGN KEY (`conversation_id`) REFERENCES `ncb_conversation_sessions` (`id`) ON UPDATE NO ACTION ON DELETE CASCADE
) ENGINE = InnoDB;

-- ─── Audit Logs ───────────────────────────────────────────────────────────────
CREATE TABLE `ncb_audit_logs` (
    `id` INT(10) NOT NULL AUTO_INCREMENT,
    `timestamp` DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
    `request_id` VARCHAR(50) NOT NULL COLLATE 'utf8mb4_unicode_ci',
    `session_id` VARCHAR(255) NOT NULL COLLATE 'utf8mb4_unicode_ci',
    `conversation_id` INT(10) NULL DEFAULT NULL,
    `source` VARCHAR(50) NULL DEFAULT 'chatbot' COLLATE 'utf8mb4_unicode_ci',
    `actor` VARCHAR(50) NULL DEFAULT 'system' COLLATE 'utf8mb4_unicode_ci',
    `actor_id` VARCHAR(100) NULL DEFAULT NULL COLLATE 'utf8mb4_unicode_ci',
    `event_type` VARCHAR(100) NOT NULL COLLATE 'utf8mb4_unicode_ci',
    `category` VARCHAR(50) NOT NULL COLLATE 'utf8mb4_unicode_ci',
    `outcome` VARCHAR(20) NOT NULL COLLATE 'utf8mb4_unicode_ci',
    `duration_ms` INT(10) NULL DEFAULT NULL,
    `error_code` VARCHAR(100) NULL DEFAULT NULL COLLATE 'utf8mb4_unicode_ci',
    `order_id` VARCHAR(100) NULL DEFAULT NULL COLLATE 'utf8mb4_unicode_ci',
    `complaint_id` VARCHAR(100) NULL DEFAULT NULL COLLATE 'utf8mb4_unicode_ci',
    `metadata` JSON NULL DEFAULT NULL,
    PRIMARY KEY (`id`) USING BTREE,
    INDEX `request_id` (`request_id`) USING BTREE,
    INDEX `session_id` (`session_id`) USING BTREE,
    INDEX `timestamp` (`timestamp`) USING BTREE,
    INDEX `event_type` (`event_type`) USING BTREE,
    INDEX `source` (`source`) USING BTREE
) COLLATE = 'utf8mb4_general_ci' ENGINE = InnoDB;
