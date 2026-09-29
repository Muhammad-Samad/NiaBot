import time
from typing import Dict, Any, Optional
from contextvars import ContextVar
import uuid
from concurrent.futures import ThreadPoolExecutor

from core.audit_events import AuditCategory, AuditEvent, AuditOutcome
from core.error_codes import ErrorCode
from data.repositories.audit_repository import AuditRepository
from utils.logger import get_logger

logger = get_logger(__name__)

# Global ContextVars for tracing a single request
request_id_var: ContextVar[str] = ContextVar("request_id", default="unknown")
session_id_var: ContextVar[str] = ContextVar("session_id", default="unknown")

# Global thread pool for fire-and-forget DB audit persistence
_executor = ThreadPoolExecutor(max_workers=5)

class AuditService:
    def __init__(self, repository: Optional[AuditRepository] = None):
        self.repository = repository or AuditRepository()
        
    def log_event(self, event_type: AuditEvent, category: AuditCategory, outcome: AuditOutcome,
                  session_id: Optional[str] = None,
                  conversation_id: Optional[int] = None,
                  actor: str = "system", actor_id: Optional[str] = None,
                  duration_ms: Optional[int] = None, error_code: Optional[ErrorCode] = None,
                  order_id: Optional[str] = None, complaint_id: Optional[str] = None,
                  metadata: Optional[Dict[str, Any]] = None,
                  source: str = "chatbot") -> None:
        """
        Logs an audit event securely, swallowing any exceptions.
        Retrieves request_id dynamically from contextvars.
        Dispatched to a background thread to prevent blocking the main conversation thread.
        """
        request_id = request_id_var.get()
        if session_id is None:
            session_id = session_id_var.get()
        
        # Convert enums to strings for persistence
        evt = event_type.value if isinstance(event_type, AuditEvent) else str(event_type)
        cat = category.value if isinstance(category, AuditCategory) else str(category)
        out = outcome.value if isinstance(outcome, AuditOutcome) else str(outcome)
        err = error_code.value if isinstance(error_code, ErrorCode) else (str(error_code) if error_code else None)
        
        def _task():
            try:
                self.repository.insert_log(
                    request_id=request_id,
                    session_id=session_id,
                    conversation_id=conversation_id,
                    source=source,
                    actor=actor,
                    actor_id=actor_id,
                    event_type=evt,
                    category=cat,
                    outcome=out,
                    duration_ms=duration_ms,
                    error_code=err,
                    order_id=order_id,
                    complaint_id=complaint_id,
                    metadata=metadata
                )
            except Exception as e:
                logger.error(f"AuditService failed to log event {evt}: {e}")
                
        _executor.submit(_task)
