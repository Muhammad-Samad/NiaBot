-- Policy domain integration: record which domain handled each chat turn, and
-- add the conversation_sessions.ended_at column the app already writes.
--
-- 1. conversation_messages.domain - app.py now writes the handling domain
--    (shopping / operations / policy / menu) for every turn. Run this BEFORE
--    deploying the new code: until the column exists, every history insert
--    fails (the error is logged and the chat reply is unaffected, but those
--    turns are missing from /api/history).
--    Policy turns' RAG sources/metadata go in the existing
--    chatbot_messages.metadata JSON column - no change needed there.
--
-- 2. conversation_sessions.ended_at - ConversationRepository.close_session()
--    sets it when a conversation ends (goodbye flow). Without the column that
--    UPDATE fails, so sessions were never marked closed.

ALTER TABLE conversation_messages
    ADD COLUMN domain VARCHAR(32) NULL AFTER cart_action_json,
    ADD INDEX idx_domain (domain);

ALTER TABLE conversation_sessions
    ADD COLUMN ended_at DATETIME NULL DEFAULT NULL AFTER last_activity;

-- Rollback:
-- ALTER TABLE conversation_messages DROP INDEX idx_domain, DROP COLUMN domain;
-- ALTER TABLE conversation_sessions DROP COLUMN ended_at;
