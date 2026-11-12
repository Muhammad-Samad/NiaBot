import logging
import json
from typing import List, Dict, Any, Optional
from database.connection import DatabaseManager

logger = logging.getLogger(__name__)

class ConversationRepository:
    """Operations/policy turn log: ncb_conversation_sessions (one row per sid)
    and ncb_intent_logs (intent, flow, confidence and metadata per turn)."""

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves the most recent session for a given session_id.
        """
        try:
            with DatabaseManager() as conn:
                cursor = conn.cursor(dictionary=True)
                cursor.execute(
                    "SELECT * FROM ncb_conversation_sessions WHERE session_id = %s ORDER BY id DESC LIMIT 1",
                    (session_id,)
                )
                result = cursor.fetchone()
                cursor.close()
                return result
        except Exception as e:
            logger.exception(f"[ConversationRepository] Failed to get session {session_id}: {e}")
            return None

    def create_session(self, session_id: str, customer_identifier: Optional[str] = None) -> Optional[int]:
        """
        Creates a new session and returns its internal ID.
        """
        try:
            insert_query = """
            INSERT IGNORE INTO ncb_conversation_sessions (session_id, customer_identifier)
            VALUES (%s, %s)
            """
            with DatabaseManager() as conn:
                cursor = conn.cursor()
                cursor.execute(insert_query, (session_id, customer_identifier))
                conn.commit()

                cursor.execute("SELECT id FROM ncb_conversation_sessions WHERE session_id = %s LIMIT 1", (session_id,))
                result = cursor.fetchone()
                internal_id = result[0] if result else None

                cursor.close()
                return internal_id
        except Exception as e:
            logger.exception(f"[ConversationRepository] Failed to create session {session_id}: {e}")
            return None

    def close_session(self, session_id: str):
        """
        Marks the active session as closed.
        """
        try:
            query = """
            UPDATE ncb_conversation_sessions
            SET status = 'closed', last_activity = CURRENT_TIMESTAMP, ended_at = CURRENT_TIMESTAMP
            WHERE session_id = %s AND (status != 'closed' OR status IS NULL)
            """
            with DatabaseManager() as conn:
                cursor = conn.cursor()
                cursor.execute(query, (session_id,))
                conn.commit()
                cursor.close()
                logger.info(f"Successfully closed session {session_id}")
        except Exception as e:
            logger.exception(f"[ConversationRepository] Failed to close session {session_id}: {e}")

    def update_last_activity(self, session_id: str):
        try:
            with DatabaseManager() as conn:
                cursor = conn.cursor()
                # MySQL updates `last_activity` automatically on UPDATE if the row changes,
                # but let's explicitly update it just in case, or just do a dummy update.
                # Usually setting it to CURRENT_TIMESTAMP works.
                cursor.execute("""
                UPDATE ncb_conversation_sessions
                SET last_activity = CURRENT_TIMESTAMP
                WHERE session_id = %s
                """, (session_id,))
                conn.commit()
                cursor.close()
        except Exception as e:
            logger.error(f"[ConversationRepository] Failed to update last activity for session {session_id}: {e}")

    def save_message(self, conversation_id: int, session_id: str, sender: str, message: str,
                     message_type: Optional[str] = None,
                     intent: Optional[str] = None,
                     flow_name: Optional[str] = None,
                     confidence: Optional[float] = None,
                     metadata: Optional[dict] = None):
        """
        Saves one turn to the ncb_intent_logs table. `conversation_id` is
        ncb_conversation_sessions.id; customer_id is copied from the
        ncb_bot_sessions row of this sid (NULL when no verified customer).
        Logs and swallows exceptions.
        """
        try:
            metadata_json = json.dumps(metadata) if metadata else None

            insert_query = """
            INSERT INTO ncb_intent_logs
            (conversation_id, customer_id, sender, message, message_type, intent, flow_name, confidence, metadata)
            VALUES (%s, (SELECT bs.customer_id FROM ncb_bot_sessions bs WHERE bs.session_id = %s),
                    %s, %s, %s, %s, %s, %s, %s)
            """
            with DatabaseManager() as conn:
                cursor = conn.cursor()
                cursor.execute(insert_query, (
                    conversation_id, session_id, sender, message, message_type,
                    intent, flow_name, confidence, metadata_json
                ))
                conn.commit()
                cursor.close()
        except Exception as e:
            logger.exception(f"[ConversationRepository] Failed to save message for conversation {conversation_id}: {e}")

    def get_messages(self, session_id: str) -> List[Dict[str, Any]]:
        try:
            query = """
            SELECT m.*
            FROM ncb_intent_logs m
            JOIN ncb_conversation_sessions s ON m.conversation_id = s.id
            WHERE s.session_id = %s
            ORDER BY m.timestamp ASC
            """
            with DatabaseManager() as conn:
                cursor = conn.cursor(dictionary=True)
                cursor.execute(query, (session_id,))
                results = cursor.fetchall()
                cursor.close()

                # Parse metadata JSON
                for row in results:
                    if row.get('metadata'):
                        try:
                            row['metadata'] = json.loads(row['metadata'])
                        except:
                            pass

                return results
        except Exception as e:
            logger.error(f"[ConversationRepository] Failed to get messages for session {session_id}: {e}")
            return []
