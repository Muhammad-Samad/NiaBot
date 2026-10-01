-- ============================================================
-- Naheed Shopping Assistant - MySQL Setup
-- Run this in MySQL Workbench before starting the app
-- ============================================================

CREATE DATABASE IF NOT EXISTS naheed_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE naheed_db;

-- ─── Cart Sessions ────────────────────────────────────────────────────────────
-- Tracks open cart sessions linked to a Typesense conversation_id
CREATE TABLE IF NOT EXISTS cart_sessions (
    id            INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    session_id    VARCHAR(64)  NOT NULL UNIQUE,          -- client-supplied id (browser localStorage), not Flask cookie session
    conversation_id VARCHAR(128) NULL,                   -- Typesense conversation_id
    last_topic    VARCHAR(512) NULL,                     -- last resolved product search topic, used to contextualize follow-up messages
    created_at    DATETIME     NOT NULL DEFAULT NOW(),
    updated_at    DATETIME     NOT NULL DEFAULT NOW() ON UPDATE NOW()
) ENGINE=InnoDB;

-- If upgrading an existing install that already has cart_sessions without
-- the last_topic column, run this once (ignore the error if it's already there):
-- ALTER TABLE cart_sessions ADD COLUMN last_topic VARCHAR(512) NULL;

-- ─── Cart Items ───────────────────────────────────────────────────────────────
-- Products the customer has confirmed for their cart
CREATE TABLE IF NOT EXISTS cart_items (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    session_id      VARCHAR(64)  NOT NULL,
    product_id      VARCHAR(64)  NOT NULL,               -- Typesense document id
    sku             VARCHAR(128) NULL,
    product_name    VARCHAR(512) NOT NULL,
    price           DECIMAL(10,2) NOT NULL DEFAULT 0.00,
    quantity        INT UNSIGNED NOT NULL DEFAULT 1,
    image_url       TEXT NULL,
    category        VARCHAR(255) NULL,
    added_at        DATETIME     NOT NULL DEFAULT NOW(),
    FOREIGN KEY (session_id) REFERENCES cart_sessions(session_id) ON DELETE CASCADE,
    UNIQUE KEY uq_session_product (session_id, product_id)
) ENGINE=InnoDB;

-- ─── Order History ────────────────────────────────────────────────────────────
-- Confirmed orders placed through the chatbot
CREATE TABLE IF NOT EXISTS orders (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    session_id      VARCHAR(64)  NOT NULL,
    order_ref       VARCHAR(32)  NOT NULL UNIQUE,        -- e.g. ORD-20240601-0001
    total_amount    DECIMAL(10,2) NOT NULL DEFAULT 0.00,
    status          ENUM('pending','confirmed','cancelled') NOT NULL DEFAULT 'pending',
    customer_name   VARCHAR(255) NULL,
    customer_phone  VARCHAR(32)  NULL,
    customer_address TEXT NULL,
    placed_at       DATETIME     NOT NULL DEFAULT NOW()
) ENGINE=InnoDB;

-- ─── Order Items ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS order_items (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    order_id        INT UNSIGNED NOT NULL,
    product_id      VARCHAR(64)  NOT NULL,
    sku             VARCHAR(128) NULL,
    product_name    VARCHAR(512) NOT NULL,
    price           DECIMAL(10,2) NOT NULL,
    quantity        INT UNSIGNED NOT NULL DEFAULT 1,
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- ─── Useful Views ─────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW v_cart_summary AS
    SELECT
        ci.session_id,
        COUNT(ci.id)          AS item_count,
        SUM(ci.price * ci.quantity) AS cart_total,
        cs.conversation_id
    FROM cart_items ci
    JOIN cart_sessions cs ON cs.session_id = ci.session_id
    GROUP BY ci.session_id, cs.conversation_id;

-- ─── Conversation Messages (for chat history with product cards) ───────────────
-- Add this table if upgrading an existing install
CREATE TABLE IF NOT EXISTS conversation_messages (
    id                  INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    session_id          VARCHAR(64)   NOT NULL,
    conversation_id     VARCHAR(128)  NOT NULL,
    role                ENUM('user','assistant') NOT NULL,
    message             TEXT          NOT NULL,
    products_json       LONGTEXT      NULL,   -- JSON array of product cards (assistant only)
    cart_action_json    TEXT          NULL,   -- JSON cart action if any
    created_at          DATETIME      NOT NULL DEFAULT NOW(),
    INDEX idx_session_conv (session_id, conversation_id)
) ENGINE=InnoDB;

CREATE TABLE `conversation_sessions` (
    `id` INT(10) NOT NULL AUTO_INCREMENT,
    `session_id` VARCHAR(255) NOT NULL COLLATE 'utf8mb4_unicode_ci',
    `customer_identifier` VARCHAR(255) NULL DEFAULT NULL COLLATE 'utf8mb4_unicode_ci',
    `summary` TEXT NULL DEFAULT NULL COLLATE 'utf8mb4_unicode_ci',
    `started_at` DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
    `last_activity` DATETIME NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    `status` VARCHAR(50) NULL DEFAULT 'active' COLLATE 'utf8mb4_unicode_ci',

    PRIMARY KEY (`id`),
    UNIQUE INDEX `session_id` (`session_id`),
    INDEX `last_activity` (`last_activity`),

    CONSTRAINT `conversation_sessions_chk_1`
        CHECK (`status` IN ('active', 'closed', 'escalated'))
)ENGINE=InnoDB;

CREATE TABLE `chatbot_messages` (
    `id` INT NOT NULL AUTO_INCREMENT,
    `conversation_id` INT NOT NULL,
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
    INDEX `timestamp` (`timestamp`),
    INDEX `sender` (`sender`),

    CONSTRAINT `chatbot_messages_ibfk_1`
        FOREIGN KEY (`conversation_id`)
        REFERENCES `conversation_sessions` (`id`)
        ON UPDATE NO ACTION
        ON DELETE CASCADE
)
ENGINE=InnoDB;