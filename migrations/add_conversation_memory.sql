-- ============================================================
-- Conversational context + long-term customer memory
-- (domains/shopping/conversation.py, services/memory_service.py)
--
-- All additive: nothing is dropped or altered in place, so code that
-- predates this migration keeps working against the upgraded schema.
-- ============================================================

-- 1. Per-conversation structured shopping state (who/what/budget/last list)
--    and the customer key the session belongs to (device id or verified
--    customer id), which links sessions across "New Chat" and visits.
ALTER TABLE cart_sessions
    ADD COLUMN conversation_state JSON NULL,
    ADD COLUMN customer_key VARCHAR(64) NULL,
    ADD INDEX idx_customer_key (customer_key);

-- 2. Long-term profile facts per customer ("has a son, about 25",
--    "prefers Nike", "shoe size 42").
CREATE TABLE IF NOT EXISTS chat_customer_memory (
    customer_key  VARCHAR(64) NOT NULL PRIMARY KEY,
    profile_json  JSON NULL,
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB;

-- 3. One rolling summary per past chat session (sid). last_message_id is the
--    newest conversation_messages.id the summary covers, so a session that
--    continues later is re-summarized instead of going stale.
CREATE TABLE IF NOT EXISTS chat_conversation_summaries (
    id               INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    customer_key     VARCHAR(64) NOT NULL,
    session_id       VARCHAR(64) NOT NULL UNIQUE,
    summary          TEXT NOT NULL,
    topics_json      JSON NULL,
    last_message_id  INT UNSIGNED NOT NULL,
    created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_customer_updated (customer_key, updated_at)
) ENGINE=InnoDB;
