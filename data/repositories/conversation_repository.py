import logging
import json
from typing import List, Dict, Any, Optional
from database.connection import DatabaseManager

logger = logging.getLogger(__name__)

class ConversationRepository:
    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves the most recent session for a given session_id.
        """
        logger.info("ENTER get_session")
        try:
            with DatabaseManager() as conn:
                cursor = conn.cursor(dictionary=True)
                cursor.execute(
                    "SELECT * FROM conversation_sessions WHERE session_id = %s ORDER BY id DESC LIMIT 1", 
                    (session_id,)
                )
                result = cursor.fetchone()
                cursor.close()
                logger.info("EXIT get_session")
                return result
        except Exception as e:
            logger.exception(f"[ConversationRepository] Failed to get session {session_id}: {e}")
            return None

    def create_session(self, session_id: str, customer_identifier: Optional[str] = None) -> Optional[int]:
        """
        Creates a new session and returns its internal ID.
        """
        logger.info("ENTER create_session")
        try:
            insert_query = """
            INSERT IGNORE INTO conversation_sessions (session_id, customer_identifier)
            VALUES (%s, %s)
            """
            with DatabaseManager() as conn:
                cursor = conn.cursor()
                cursor.execute(insert_query, (session_id, customer_identifier))
                conn.commit()
                
                cursor.execute("SELECT id FROM conversation_sessions WHERE session_id = %s LIMIT 1", (session_id,))
                result = cursor.fetchone()
                internal_id = result[0] if result else None
                
                cursor.close()
                logger.info(f"returned new conversation_id={internal_id}")
                logger.info("EXIT create_session")
                return internal_id
        except Exception as e:
            logger.exception(f"[ConversationRepository] Failed to create session {session_id}: {e}")
            return None

    def close_session(self, session_id: str):
        """
        Marks the active session as closed.
        """
        logger.info("ENTER close_session")
        try:
            query = """
            UPDATE conversation_sessions 
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
        logger.info("EXIT close_session")

    def update_last_activity(self, session_id: str):
        try:
            with DatabaseManager() as conn:
                cursor = conn.cursor()
                # MySQL updates `last_activity` automatically on UPDATE if the row changes,
                # but let's explicitly update it just in case, or just do a dummy update.
                # Usually setting it to CURRENT_TIMESTAMP works.
                cursor.execute("""
                UPDATE conversation_sessions 
                SET last_activity = CURRENT_TIMESTAMP 
                WHERE session_id = %s
                """, (session_id,))
                conn.commit()
                cursor.close()
        except Exception as e:
            logger.error(f"[ConversationRepository] Failed to update last activity for session {session_id}: {e}")

    def save_message(self, conversation_id: int, sender: str, message: str, 
                     message_type: Optional[str] = None, 
                     intent: Optional[str] = None, 
                     flow_name: Optional[str] = None, 
                     confidence: Optional[float] = None, 
                     metadata: Optional[dict] = None):
        """
        Saves a message to the conversation_messages table.
        Logs and swallows exceptions.
        """
        logger.info("ENTER save_message")
        try:
            metadata_json = json.dumps(metadata) if metadata else None
            
            insert_query = """
            INSERT INTO chatbot_messages 
            (conversation_id, sender, message, message_type, intent, flow_name, confidence, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """
            with DatabaseManager() as conn:
                cursor = conn.cursor()
                logger.info("executing INSERT")
                cursor.execute(insert_query, (
                    conversation_id, sender, message, message_type, 
                    intent, flow_name, confidence, metadata_json
                ))
                logger.info(f"INSERT affected {cursor.rowcount} rows")
                conn.commit()
                logger.info("commit successful")
                cursor.close()
        except Exception as e:
            logger.exception(f"[ConversationRepository] Failed to save message for conversation {conversation_id}: {e}")
        logger.info("EXIT save_message")

    def get_messages(self, session_id: str) -> List[Dict[str, Any]]:
        try:
            query = """
            SELECT m.* 
            FROM chatbot_messages m
            JOIN conversation_sessions s ON m.conversation_id = s.id
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
