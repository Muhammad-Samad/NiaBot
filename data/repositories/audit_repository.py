import json
from typing import Dict, Any, Optional
from database.connection import DatabaseManager
from utils.logger import get_logger

logger = get_logger(__name__)

class AuditRepository:
    def insert_log(self, request_id: str, session_id: str, conversation_id: Optional[int],
                   source: str, actor: str, actor_id: Optional[str],
                   event_type: str, category: str, outcome: str,
                   duration_ms: Optional[int], error_code: Optional[str],
                   order_id: Optional[str], complaint_id: Optional[str],
                   metadata: Optional[Dict[str, Any]]) -> None:
        """
        Inserts an audit log into the database independently of any active transaction.
        """
        query = """
        INSERT INTO audit_logs (
            request_id, session_id, conversation_id, source, actor, actor_id,
            event_type, category, outcome, duration_ms, error_code,
            order_id, complaint_id, metadata
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        
        metadata_json = json.dumps(metadata) if metadata else None
        
        try:
            db = DatabaseManager()
            with db as conn:
                if not conn:
                    logger.error("Failed to get database connection for audit logging.")
                    return
                    
                cursor = conn.cursor()
                cursor.execute(query, (
                    request_id, session_id, conversation_id, source, actor, actor_id,
                    event_type, category, outcome, duration_ms, error_code,
                    order_id, complaint_id, metadata_json
                ))
                conn.commit()
        except Exception as e:
            # We swallow exceptions here so that audit logging never crashes the bot
            logger.error(f"Failed to insert audit log: {e}")
