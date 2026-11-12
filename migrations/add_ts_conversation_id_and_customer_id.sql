-- ============================================================
-- Typesense conversation id column + integer customer_id columns
--
-- EXISTING databases only - a fresh install from SQL_SETUP.sql already has
-- these columns. Run AFTER the tables have been renamed to their ncb_* names.
-- Needs MySQL 8.0+ (RENAME COLUMN). Run BEFORE deploying the matching code:
-- until it runs, every insert into these tables fails.
--
-- 1. ts_conversation_id - `conversation_id` in ncb_bot_sessions and
--    ncb_conversation_messages held Typesense's conversation id, while
--    ncb_intent_logs.conversation_id / ncb_audit_logs.conversation_id point
--    to ncb_conversation_sessions.id. The Typesense one is renamed so
--    `conversation_id` now always means ncb_conversation_sessions.id.
--    RENAME COLUMN keeps the data and updates idx_session_conv automatically.
--
-- 2. customer_id INT UNSIGNED - the numeric Naheed customer id of a verified
--    logged-in customer ("cust:<id>" customer_key), NULL for guests and
--    device-only ("dev:") visitors. Written by:
--      ncb_bot_sessions          services/memory_service.py link_session()
--      ncb_conversation_messages app.py -> save_conversation_message()
--      ncb_intent_logs           copied from ncb_bot_sessions on insert
--                                (data/repositories/conversation_repository.py)
-- ============================================================

-- 1 + 2: schema
ALTER TABLE ncb_bot_sessions
    RENAME COLUMN conversation_id TO ts_conversation_id,
    ADD COLUMN customer_id INT UNSIGNED NULL AFTER customer_key,
    ADD INDEX idx_customer_id (customer_id);

ALTER TABLE ncb_conversation_messages
    RENAME COLUMN conversation_id TO ts_conversation_id,
    ADD COLUMN customer_id INT UNSIGNED NULL AFTER ts_conversation_id,
    ADD INDEX idx_customer_id (customer_id);

ALTER TABLE ncb_intent_logs
    ADD COLUMN customer_id INT UNSIGNED NULL DEFAULT NULL AFTER conversation_id,
    ADD INDEX customer_id (customer_id);

-- 3. Backfill customer_id from the existing customer_key ("cust:12345").
--    Every message in a session is attributed to the session's owner.
UPDATE ncb_bot_sessions
SET customer_id = CAST(SUBSTRING(customer_key, 6) AS UNSIGNED)
WHERE customer_key REGEXP '^cust:[0-9]{1,10}$'
  AND CAST(SUBSTRING(customer_key, 6) AS UNSIGNED) BETWEEN 1 AND 4294967295;

UPDATE ncb_conversation_messages cm
JOIN ncb_bot_sessions bs ON bs.session_id = cm.session_id
SET cm.customer_id = bs.customer_id
WHERE bs.customer_id IS NOT NULL;

-- ncb_conversation_sessions.session_id is declared utf8mb4_unicode_ci while
-- ncb_bot_sessions.session_id uses the server default (utf8mb4_general_ci on
-- staging), so the join needs an explicit COLLATE (error 1267 otherwise).
UPDATE ncb_intent_logs il
JOIN ncb_conversation_sessions s ON s.id = il.conversation_id
JOIN ncb_bot_sessions bs ON bs.session_id = s.session_id COLLATE utf8mb4_unicode_ci
SET il.customer_id = bs.customer_id
WHERE bs.customer_id IS NOT NULL;

-- 4. LEGACY view: renaming its base tables leaves the old v_cart_summary
--    pointing at tables that no longer exist. Recreate it under the new name
--    (skip if you don't have the legacy cart tables).
DROP VIEW IF EXISTS v_cart_summary;
CREATE OR REPLACE VIEW ncb_v_cart_summary AS
SELECT ci.session_id, COUNT(ci.id) AS item_count, SUM(ci.price * ci.quantity) AS cart_total, cs.ts_conversation_id
FROM ncb_cart_items ci
JOIN ncb_bot_sessions cs ON cs.session_id = ci.session_id
GROUP BY ci.session_id, cs.ts_conversation_id;

-- Rollback:
-- ALTER TABLE ncb_intent_logs DROP INDEX customer_id, DROP COLUMN customer_id;
-- ALTER TABLE ncb_conversation_messages DROP INDEX idx_customer_id, DROP COLUMN customer_id,
--     RENAME COLUMN ts_conversation_id TO conversation_id;
-- ALTER TABLE ncb_bot_sessions DROP INDEX idx_customer_id, DROP COLUMN customer_id,
--     RENAME COLUMN ts_conversation_id TO conversation_id;
