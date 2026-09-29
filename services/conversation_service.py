import logging
from typing import Optional, Dict, List, Any
from data.repositories.conversation_repository import ConversationRepository
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

# Global thread pool for fire-and-forget DB persistence
_executor = ThreadPoolExecutor(max_workers=5)

class ConversationService:
    def __init__(self, repository: Optional[ConversationRepository] = None):
        self.repository = repository or ConversationRepository()

    def get_or_create_session(self, session_id: str, customer_identifier: Optional[str] = None) -> Optional[int]:
        session = self.repository.get_session(session_id)
        if session:
            return session['id']
        return self.repository.create_session(session_id, customer_identifier)

    def close_session(self, session_id: str):
        def _task():
            try:
                self.repository.close_session(session_id)
            except Exception as e:
                logger.exception(f"[ConversationService] Exception closing session {session_id}: {e}")
        _executor.submit(_task)

    def save_user_message(self, session_id: str, message: str, 
                          customer_identifier: Optional[str] = None, 
                          message_type: str = 'text', 
                          intent: Optional[str] = None, 
                          flow_name: Optional[str] = None, 
                          confidence: Optional[float] = None, 
                          metadata: Optional[Dict[str, Any]] = None):
        """
        Saves a user message. Non-critical feature; dispatched to background thread.
        """
        def _task():
            logger.info(f"ENTER save_user_message\nsession_id={session_id}")
            try:
                internal_id = self.get_or_create_session(session_id, customer_identifier)
                if internal_id is not None:
                    self.repository.save_message(
                        conversation_id=internal_id,
                        sender='user',
                        message=message,
                        message_type=message_type,
                        intent=intent,
                        flow_name=flow_name,
                        confidence=confidence,
                        metadata=metadata
                    )
                    self.repository.update_last_activity(session_id)
            except Exception as e:
                logger.exception(f"[ConversationService] Exception saving user message for session {session_id}: {e}")
            logger.info("EXIT save_user_message")

        _executor.submit(_task)

    def save_bot_message(self, session_id: str, message: str, 
                         metadata: Optional[Dict[str, Any]] = None):
        """
        Saves a bot message. Non-critical feature; dispatched to background thread.
        """
        def _task():
            try:
                internal_id = self.get_or_create_session(session_id)
                if internal_id is not None:
                    self.repository.save_message(
                        conversation_id=internal_id,
                        sender='assistant',
                        message=message,
                        metadata=metadata
                    )
                    self.repository.update_last_activity(session_id)
            except Exception as e:
                logger.exception(f"[ConversationService] Exception saving bot message for session {session_id}: {e}")
                
        _executor.submit(_task)

    def get_conversation_history(self, session_id: str) -> List[Dict[str, Any]]:
        try:
            return self.repository.get_messages(session_id)
        except Exception as e:
            logger.error(f"[ConversationService] Exception getting history for session {session_id}: {e}")
            return []
