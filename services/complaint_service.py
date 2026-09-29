import logging
from typing import Dict, Any
from database.repository import OrderRepository, ComplaintRepository
from services.audit_service import AuditService
from core.audit_events import AuditEvent, AuditCategory, AuditOutcome
import time

logger = logging.getLogger(__name__)

# Complaint types that may be filed more than once for the same order.
REPEATABLE_COMPLAINT_TYPES = {"Other"}

class ComplaintService:
    def __init__(self, complaint_repository=None, order_repository=None, audit_service=None):
        self.complaint_repository = complaint_repository or ComplaintRepository()
        self.order_repository = order_repository or OrderRepository()
        self.audit_service = audit_service or AuditService()

    def create_complaint(self, order_id: str, complaint_type: str, details: str, image_url: str = None, priority: str = "low", mood: str = "happy") -> Dict[str, Any]:
        start_time = time.time()
        try:
            # Check for duplicate complaint type. "Other" complaints are free text
            # typed by the customer, so each one is a distinct complaint.
            if complaint_type not in REPEATABLE_COMPLAINT_TYPES and self.complaint_repository.has_existing_complaint_type(order_id, complaint_type):
                return {
                    "success": False,
                    "message": f"You have already filed a {complaint_type} complaint for Order #{order_id}. Duplicate complaints of the same type are not allowed."
                }
            # 1. Fetch order to get entity_id and customer info
            try:
                order = self.order_repository.get_order_by_increment_id(order_id)
                entity_id = order.entity_id
                name = order.recipient_name or "Valued Customer"
                phone = order.recipient_phone or ""
                email = "" # We don't have email in Order schema, but we can set it empty or query order address
            except Exception as e:
                logger.warning(f"Could not retrieve order details for complaint #{order_id}: {e}")
                entity_id = 0
                name = "Valued Customer"
                phone = ""
                email = ""

            subject = f"Complaint for Order #{order_id} ({complaint_type})"
            ticket_no = self.complaint_repository.create_complaint_ticket(
                order_number=order_id,
                entity_id=entity_id,
                name=name,
                email=email,
                phone=phone,
                subject=subject,
                complain=details,
                complain_type=complaint_type,
                priority=priority or "low",
                mood=mood or "happy"
            )

            if image_url:
                self.complaint_repository.add_ticket_attachment(ticket_no, image_url)

            duration_ms = int((time.time() - start_time) * 1000)
            self.audit_service.log_event(
                event_type=AuditEvent.COMPLAINT_CREATION,
                category=AuditCategory.BUSINESS,
                outcome=AuditOutcome.SUCCESS,
                actor="user",
                duration_ms=duration_ms,
                order_id=order_id,
                complaint_id=str(ticket_no)
            )

            return {
                "success": True,
                "ticket_no": ticket_no,
                "message": f"Your complaint has been successfully registered. Ticket Number: #{ticket_no}. We will get back to you soon."
            }
        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            logger.error(f"Error creating complaint in service: {e}")
            self.audit_service.log_event(
                event_type=AuditEvent.COMPLAINT_CREATION,
                category=AuditCategory.BUSINESS,
                outcome=AuditOutcome.FAILURE,
                actor="user",
                duration_ms=duration_ms,
                order_id=order_id,
                metadata={"error_details": str(e)}
            )
            return {
                "success": False,
                "message": f"Failed to register complaint: {e}"
            }
