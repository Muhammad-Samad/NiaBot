import os
import re
import uuid
from typing import Tuple, Dict, Any, Optional
from core.router import IntentRouter
from core.state_manager import StateManager
from core.flow_manager import FlowManager
from ai.intent_parser import IntentParser, IntentParserError
from ai.schemas import IntentResult as SchemaIntentResult
from services.order_service import OrderService
from services.complaint_service import ComplaintService
from services.complaint_tracking_service import ComplaintTrackingService
from services.conversation_service import ConversationService
from services.audit_service import AuditService, request_id_var, session_id_var
from services.rate_limiter_service import RateLimiterService
from core.audit_events import AuditEvent, AuditCategory, AuditOutcome
from core.error_codes import ErrorCode
from utils.logger import get_logger
import time

logger = get_logger(__name__)

class ConversationManager:
    """
    Manages the flow of the conversation by utilizing the IntentRouter
    to determine the user's intent, and delegating business logic to Services.
    """
    def __init__(self, parser: IntentParser = None, router: IntentRouter = None, order_service: OrderService = None, complaint_service: ComplaintService = None, complaint_tracking_service: ComplaintTrackingService = None, state_manager: StateManager = None, flow_manager: FlowManager = None, conversation_service: ConversationService = None):
        # Dependency injection allows easy mocking in tests
        self.parser = parser or IntentParser()
        self.router = router or IntentRouter()
        self.order_service = order_service or OrderService()
        self.complaint_service = complaint_service or ComplaintService()
        self.complaint_tracking_service = complaint_tracking_service or ComplaintTrackingService()
        self.state_manager = state_manager or StateManager()
        self.flow_manager = flow_manager or FlowManager(
            order_service=self.order_service,
            order_repository=self.order_service.repository if hasattr(self.order_service, 'repository') else None
        )
        self.conversation_service = conversation_service or ConversationService()
        self.audit_service = AuditService()
        self.rate_limiter_service = RateLimiterService()
        
        threshold_env = os.getenv("AI_CONFIDENCE_THRESHOLD", "0.85")
        try:
            self.confidence_threshold = float(threshold_env)
        except ValueError:
            self.confidence_threshold = 0.85

    # A bare order ID (e.g. "2000098496", "#FP-6000028994-1") and a bare phone
    # number (e.g. "0300 1234567", "+92-300-1234567").
    _BARE_ORDER_ID_RE = re.compile(r"^#?\s*([A-Za-z0-9][A-Za-z0-9_\-]*)$")
    _BARE_PHONE_RE = re.compile(r"^\+?[\d\s\-()]+$")

    def _order_tracking_fast_path(self, message: str, state) -> Optional[Tuple[str, Dict[str, Any]]]:
        """When order tracking is waiting for an order ID or phone number and the
        message is exactly that, the intent is already known - return it so the
        LLM round trip can be skipped. Returns None otherwise."""
        if state.current_flow != "order_tracking":
            return None
        text = message.strip()

        if state.current_stage == "ask_order_id":
            match = self._BARE_ORDER_ID_RE.match(text)
            if match and sum(ch.isdigit() for ch in match.group(1)) >= 5:
                return "order_tracking", {"order_id": match.group(1)}
        elif state.current_stage == "ask_verification":
            if self._BARE_PHONE_RE.match(text) and 10 <= sum(ch.isdigit() for ch in text) <= 13:
                return "order_tracking", {}
        return None

    def process_message_with_debug(self, message: str, session_id: str = "default", forced_intent: str = None, forced_entities: dict = None) -> Tuple[str, Dict[str, Any]]:
        message_id = str(uuid.uuid4())
        # Set request_id and session_id for the current context
        request_id_var.set(message_id)
        session_id_var.set(session_id)
        
        # Log session started if this is a new session
        state = self.state_manager.get_state(session_id)
        if not state.conversation_history:
            self.audit_service.log_event(
                event_type=AuditEvent.SESSION_STARTED,
                category=AuditCategory.LIFECYCLE,
                outcome=AuditOutcome.SUCCESS,
                actor="user"
            )
            
        # 0. Check Rate Limit (First Business Check)
        try:
            start_time = time.time()
            is_allowed, rl_metadata = self.rate_limiter_service.is_allowed(session_id)
            duration_ms = int((time.time() - start_time) * 1000)
            
            if not is_allowed:
                self.audit_service.log_event(
                    event_type=AuditEvent.RATE_LIMIT,
                    category=AuditCategory.SECURITY,
                    outcome=AuditOutcome.FAILURE,
                    actor="user",
                    error_code=ErrorCode.ERR_RATE_LIMIT_EXCEEDED,
                    metadata=rl_metadata,
                    duration_ms=duration_ms
                )
                logger.warning(f"Rate limit exceeded for session {session_id}")
                return self.rate_limiter_service.rejection_message, {}
        except Exception as e:
            logger.error(f"RateLimiterService error: {e}")
            self.audit_service.log_event(
                event_type=AuditEvent.SYSTEM_EXCEPTION,
                category=AuditCategory.SYSTEM,
                outcome=AuditOutcome.FAILURE,
                actor="system",
                metadata={"error_details": str(e), "execution_stage": "rate_limit"}
            )
            # Allow the conversation to continue (fail-safe)

        # 0.1 Check Word Limit
        word_limit = int(os.getenv("MAX_INPUT_WORDS", "100"))
        if not message.startswith("[Image Uploaded:") and len(message.split()) > word_limit:
            self.audit_service.log_event(
                event_type=AuditEvent.RATE_LIMIT,
                category=AuditCategory.SECURITY,
                outcome=AuditOutcome.FAILURE,
                actor="user",
                metadata={"error_details": "exceeded word limit"}
            )
            logger.warning(f"User message rejected: exceeded word limit of {word_limit} words.")
            response_text = f"Your message is too long. Please limit your message to {word_limit} words."
            self.conversation_service.save_user_message(session_id, message)
            self.conversation_service.save_bot_message(session_id, response_text)
            return response_text, {}
            
        # 0.1 Check Cancellation of the current workflow
        if self.state_manager.check_cancellation(message):
            self.state_manager.clear_state(session_id)
            response_text = "Conversation reset. How can I help you?"
            self.conversation_service.save_user_message(session_id, message, intent="cancel")
            self.conversation_service.save_bot_message(session_id, response_text)
            return response_text, {}
            
        # 0.1 Check Consecutive User Messages
        state = self.state_manager.get_state(session_id)
        if state.conversation_history and state.conversation_history[-1]["role"] == "user":
            self.audit_service.log_event(
                event_type=AuditEvent.RATE_LIMIT,
                category=AuditCategory.SECURITY,
                outcome=AuditOutcome.FAILURE,
                actor="user",
                metadata={"error_details": "consecutive user message"}
            )
            logger.warning(f"Consecutive user message blocked for session {session_id}")
            response_text = "Please wait for my response before sending another message."
            self.conversation_service.save_user_message(session_id, message)
            self.conversation_service.save_bot_message(session_id, response_text)
            return response_text, {}
            
        # 0.5 Load State & add user message
        self.state_manager.add_message(session_id, "user", message)
        state = self.state_manager.get_state(session_id)
        
        # 1. Classify Intent via LLM (skipped entirely for a deterministic
        # numbered-menu selection, which arrives here as forced_intent - see
        # router/menu.py and domains/operations/service.py - and for a bare
        # order ID / phone number the order tracking flow is waiting for)
        intent_result = None
        used_fallback_router = False
        llm_error = None

        if not forced_intent:
            fast_path = self._order_tracking_fast_path(message, state)
            if fast_path:
                forced_intent, forced_entities = fast_path
                logger.debug(f"Skipping LLM: message is the input order tracking is waiting for (stage={state.current_stage})")

        if forced_intent:
            intent_result = SchemaIntentResult(intent=forced_intent, confidence=1.0, entities=forced_entities or {})
            logger.debug(f"Predetermined intent: {forced_intent}")
        else:
            start_time = time.time()
            try:
                ai_result = self.parser.parse_intent(message, state=state, message_id=message_id)
                duration_ms = int((time.time() - start_time) * 1000)

                if ai_result.confidence >= self.confidence_threshold:
                    intent_result = ai_result
                    logger.info(f"AI Detected Intent: {intent_result.intent} (Confidence: {intent_result.confidence})")

                    self.audit_service.log_event(
                        event_type=AuditEvent.LLM_INTERACTION,
                        category=AuditCategory.SYSTEM,
                        outcome=AuditOutcome.SUCCESS,
                        actor="system",
                        duration_ms=duration_ms,
                        metadata={"intent": intent_result.intent, "confidence": intent_result.confidence}
                    )

                    if getattr(intent_result, "tool", None):
                        logger.debug(f"AI Recommended Tool: {intent_result.tool}")
                else:
                    logger.warning(f"Fallback Reason: Low AI confidence ({ai_result.confidence} < {self.confidence_threshold})")
            except IntentParserError as e:
                duration_ms = int((time.time() - start_time) * 1000)
                llm_error = str(e)
                logger.warning(f"Intent parser unavailable: {llm_error}")

                self.audit_service.log_event(
                    event_type=AuditEvent.LLM_INTERACTION,
                    category=AuditCategory.SYSTEM,
                    outcome=AuditOutcome.FAILURE,
                    actor="system",
                    duration_ms=duration_ms,
                    error_code=ErrorCode.ERR_LLM_TIMEOUT if "timeout" in llm_error.lower() else ErrorCode.ERR_LLM_AUTH,
                    metadata={"error_details": llm_error}
                )

            # 1.5 Fallback to rule-based router
            if not intent_result:
                used_fallback_router = True
                logger.warning("Switching to legacy rule-based router.")
                try:
                    intent_result = self.router.route(message)
                except Exception as e:
                    logger.error(f"Legacy router crashed: {e}")
                    intent_result = None

                if not intent_result:
                    state = self.state_manager.get_state(session_id)
                    state.consecutive_unknown_count += 1
                    response_text = "I'm having trouble understanding your request. Please try again."
                    self.state_manager.add_message(session_id, "assistant", response_text)
                    self.conversation_service.save_user_message(session_id, message, intent="unknown")
                    self.conversation_service.save_bot_message(session_id, response_text)
                    return response_text, {"error": "Routing failed."}
        # Update entities in state
        if intent_result:
            entities = intent_result.entities if isinstance(intent_result.entities, dict) else intent_result.entities.model_dump()
            self.state_manager.update_entities(session_id, entities)

            # Track the AI-judged priority ("high"/"low") and mood ("happy"/"sad")
            # for this message so it can be attached to any ticket filed later.
            priority = getattr(intent_result, "priority", "low") or "low"
            mood = getattr(intent_result, "mood", "happy") or "happy"
            self.state_manager.update_state(session_id, {"priority": priority, "mood": mood})
            logger.debug(f"Judged customer priority={priority}, mood={mood} (message_id={message_id})")

        state = self.state_manager.get_state(session_id)
        
        # 1.8 Evaluate Escalation Rules
        if intent_result.intent == "unknown":
            state.consecutive_unknown_count += 1
        else:
            state.consecutive_unknown_count = 0
            
        action = self._check_escalation_action(state, intent_result)
        should_offer_escalation = False
        if action == "immediate_handoff":
            state.handoff_pending = True
            intent_result.intent = "agent_handoff"
        elif action == "offer_escalation":
            should_offer_escalation = True
        
        # 2. Execute Flow via FlowManager
        try:
            flow_response = self.flow_manager.execute_flow(intent_result, state)
            state.consecutive_failure_count = 0
        except Exception as e:
            logger.error(f"Flow execution failed: {e}")
            self.audit_service.log_event(
                event_type=AuditEvent.SYSTEM_EXCEPTION,
                category=AuditCategory.SYSTEM,
                outcome=AuditOutcome.FAILURE,
                actor="system",
                metadata={"error_details": str(e), "execution_stage": "flow_execution"}
            )
            state.consecutive_failure_count += 1
            if state.consecutive_failure_count >= 3:
                intent_result.intent = "agent_handoff"
                flow_response = self.flow_manager.execute_flow(intent_result, state)
                state.consecutive_failure_count = 0
            else:
                raise
        
        # 3. Apply state updates
        if flow_response.status == "completed":
            self.state_manager.clear_state(session_id)
        else:
            self.state_manager.update_state(session_id, flow_response.updated_state)
            
        response_text = flow_response.response
        
        # TEMPORARY TOOL EXECUTION (To be replaced by Tool Dispatcher in Phase 5)
        bot_metadata = None
        if flow_response.tool_request == "policy_rag":
            # Policy questions are answered by the policy domain (ChromaDB
            # RAG). Imported here to keep operations importable on its own.
            from domains.policy import service as policy_service
            policy_reply = policy_service.ask(session_id, flow_response.tool_args.get("question") or message, entry="operations_intent")
            response_text = policy_reply["answer"]
            bot_metadata = policy_reply["metadata"]
        elif flow_response.tool_request == "track_order":
            order_id = flow_response.tool_args.get("order_id")
            logger.debug(f"Routing to OrderService for Order ID: {order_id}")
            service_response = self.order_service.track_order(order_id)
            response_text = service_response.get("message", "We encountered an issue checking your order.")
        elif flow_response.tool_request == "modify_order":
            order_id = flow_response.tool_args.get("order_id")
            logger.debug(f"Routing to OrderService for Order ID: {order_id} (modify)")
            service_response = self.order_service.check_order_modifiable(order_id)
            response_text = service_response.get("message", "We encountered an issue checking your order status.")
            if not service_response.get("success"):
                self.state_manager.update_state(session_id, {
                    "current_flow": "modify_order",
                    "waiting_for_order_id": True
                })
        elif flow_response.tool_request == "track_complaint":
            order_id = flow_response.tool_args.get("order_no")
            logger.debug(f"Routing to ComplaintTrackingService for Order ID: {order_id}")
            service_response = self.complaint_tracking_service.track_complaint(order_id)
            response_text = service_response.get("message", "We encountered an issue checking your complaint status.")
            num_complaints = len(service_response.get("complaints", [])) if service_response.get("success") else 0
            logger.debug(f"ComplaintTrackingService found {num_complaints} complaints for order: {order_id}")
        elif flow_response.tool_request == "create_complaint":
            order_id = flow_response.tool_args.get("order_id")
            complaint_type = flow_response.tool_args.get("complaint_type")
            details = flow_response.tool_args.get("details")
            image_url = flow_response.tool_args.get("image_url")
            logger.debug(f"Routing to ComplaintService for Order ID: {order_id} (priority={state.priority}, mood={state.mood})")
            service_response = self.complaint_service.create_complaint(
                order_id=order_id,
                complaint_type=complaint_type,
                details=details,
                image_url=image_url,
                priority=state.priority,
                mood=state.mood
            )
            ticket_msg = service_response.get("message", "")
            # If the flow already built a rich response (e.g. COD/refund status message),
            # append the ticket confirmation to it rather than replacing it.
            if response_text:
                response_text = f"{response_text}\n\n{ticket_msg}"
            else:
                response_text = ticket_msg

        # Check if service response indicates failure
        if 'service_response' in locals() and isinstance(service_response, dict):
            if not service_response.get("success", True):
                state.consecutive_failure_count += 1
                if state.consecutive_failure_count >= 2:
                    should_offer_escalation = True
            else:
                state.consecutive_failure_count = 0

        # Append escalation offer if needed
        if should_offer_escalation:
            offer_text = "For further assistance, please contact our Customer Support team at (021) 111-624-333."
            if getattr(intent_result, "escalation_reason", None) == "customer_frustration" or (state.mood == "sad") or "frustrat" in message.lower():
                offer_text = "I understand this has been frustrating. Please contact our Customer Support team at (021) 111-624-333 and they will be happy to help you."
            
            if response_text:
                if offer_text not in response_text:
                    response_text = f"{response_text}\n\n{offer_text}" if not response_text.rstrip().endswith(offer_text) else response_text
            else:
                response_text = offer_text

        # If the customer's message reads as upset/frustrated, lead with a short
        # empathetic reassurance before the substantive answer.
        reassurance = getattr(intent_result, "reassurance_message", None) if intent_result else None
        if reassurance and not reassurance.endswith("\n"):
            reassurance += "\n\n"
        if reassurance and isinstance(reassurance, str):
            response_text = f"{reassurance}{response_text}"

        self.state_manager.add_message(session_id, "assistant", response_text)

        
        entities_dict = {}
        if intent_result:
            entities_dict = intent_result.entities if isinstance(intent_result.entities, dict) else intent_result.entities.model_dump()

        raw_json = (
            intent_result.model_dump() if hasattr(intent_result, "model_dump")
            else {
                "intent": intent_result.intent if intent_result else "unknown",
                "confidence": intent_result.confidence if intent_result else 0.0,
                "entities": entities_dict,
                "priority": state.priority,
                "mood": state.mood,
            }
        ) if intent_result else {}

        debug_data = {
            "intent": intent_result.intent if intent_result else "unknown",
            "confidence": intent_result.confidence if intent_result else 0.0,
            "entities": entities_dict,
            "priority": state.priority if state else "low",
            "mood": state.mood if state else "happy",
            "used_fallback_router": used_fallback_router,
            "llm_error": llm_error,
            "flow_status": flow_response.status,
            "tool_request": flow_response.tool_request,
            "tool_args": flow_response.tool_args,
            "raw_json": raw_json
        }
        
        # Save messages to database via ConversationService
        flow_name = state.current_flow if state and getattr(state, 'current_flow', None) else None
        
        self.conversation_service.save_user_message(
            session_id=session_id,
            message=message,
            intent=debug_data["intent"],
            flow_name=flow_name,
            confidence=debug_data["confidence"]
        )
        self.conversation_service.save_bot_message(
            session_id=session_id,
            message=response_text,
            metadata=bot_metadata
        )
        
        # Finally, if the session was marked for termination, close it
        if getattr(flow_response, "end_conversation", False):
            logger.info(f"Session marked for termination: {session_id}")
            self.conversation_service.close_session(session_id)
            
        return response_text, debug_data

    def process_message(self, message: str, session_id: str = "default") -> str:
        response_text, _ = self.process_message_with_debug(message, session_id)
        return response_text

    def _check_escalation_action(self, state, intent_result) -> str:
        if not intent_result:
            return "none"
        if intent_result.intent == "agent_handoff":
            return "immediate_handoff"
        if state.consecutive_unknown_count >= 3:
            return "offer_escalation"
        if state.consecutive_failure_count >= 2:
            return "offer_escalation"
        if getattr(intent_result, "escalation_recommended", False) is True:
            return "offer_escalation"
        return "none"

    def _evaluate_escalation(self, state, intent_result) -> bool:
        """Returns True if the system should force agent handoff."""
        if not intent_result:
            return False
            
        if intent_result.intent == "agent_handoff":
            return True
            
        if state.consecutive_unknown_count >= 3:
            return True
            
        if state.consecutive_failure_count >= 3:
            return True
            
        if getattr(intent_result, "escalation_recommended", False) is True:
            # Only escalate if the intent is not a core business flow (like complaint, order_tracking, refund, cancel_order)
            # which we should still attempt to handle.
            if intent_result.intent not in ["complaint", "refund", "order_tracking", "cancel_order", "modify_order"]:
                return True
                
        return False
