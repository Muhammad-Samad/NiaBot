import logging
from typing import Dict, Any

from database.repository import OrderRepository, OrderNotFoundError
from services.audit_service import AuditService
from core.audit_events import AuditEvent, AuditCategory, AuditOutcome
from core.error_codes import ErrorCode
from utils.logger import get_logger

logger = get_logger(__name__)

from datetime import datetime

class OrderService:
    def __init__(self, repository: OrderRepository = None, audit_service: AuditService = None):
        # Allow injecting a mock repository for testing
        self.repository = repository or OrderRepository()
        self.audit_service = audit_service or AuditService()

    def _get_courier_url(self, carrier_code: str, tracking_number: str) -> str:
        if not carrier_code or not tracking_number:
            return None
        mapping = {
            "mnpshipping": f"https://www.mulphilog.com/tracking?consignment_number={tracking_number}",
            "lcsshipping": "https://pk.leopardscourier.com/"
        }
        return mapping.get(carrier_code.lower())

    def track_order(self, increment_id: str) -> Dict[str, Any]:
        logger.debug(f"OrderService received tracking request for ID: {increment_id}")
        
        # 1. Validate Order ID
        if not increment_id or not isinstance(increment_id, str) or not increment_id.strip():
            logger.warning("Invalid Order ID provided (empty or whitespace).")
            return {
                "success": False,
                "message": "Please enter a valid Order ID.",
                "status": None,
                "order": None
            }
            
        increment_id = increment_id.strip()
        import re
        if not re.match(r"^[a-zA-Z0-9_\-]+$", increment_id):
            logger.warning(f"Invalid Order ID format: {increment_id}")
            return {
                "success": False,
                "message": "Order ID contains invalid characters. Please check and try again.",
                "status": None,
                "order": None
            }

        # 2 & 3. Call Repository and Handle Responses
        import time
        start_time = time.time()
        try:
            parent_order = self.repository.get_order_by_increment_id(increment_id)
            duration_ms = int((time.time() - start_time) * 1000)
            logger.debug(f"Order fetch for {increment_id} took {duration_ms}ms")

            self.audit_service.log_event(
                event_type=AuditEvent.ORDER_TRACKING,
                category=AuditCategory.BUSINESS,
                outcome=AuditOutcome.SUCCESS,
                actor="user",
                duration_ms=duration_ms,
                order_id=increment_id
            )
            
            # Select active order (child if exists, else parent)
            if parent_order.child_orders:
                active_order = next((c for c in parent_order.child_orders if c.increment_id == increment_id), parent_order.child_orders[0])
                logger.debug(f"Using active child order {active_order.increment_id} for tracking.")
            else:
                active_order = parent_order
            
            # 4. Handle exact DB status and ETA
            eta_str = None
            if active_order.estimated_delivery_datetime:
                logger.debug(f"ETA found: {active_order.estimated_delivery_datetime}")
                if active_order.estimated_delivery_datetime > datetime.now():
                    logger.debug("ETA valid.")
                    eta_str = active_order.estimated_delivery_datetime.strftime("%d %B %Y, %I:%M %p")
                else:
                    logger.debug("ETA expired.")
            else:
                logger.debug("No ETA found.")
            
            logger.debug(f"Exact DB status returned: {active_order.status}")
            
            # External Order Detection
            is_external = False
            if active_order.shipping_city and active_order.shipping_city.strip().lower() != "karachi":
                is_external = True
                
            logger.debug(f"Order type: {'External' if is_external else 'Karachi'}")

            status_label = self.repository.get_status_label(active_order.status)
            
            # 5. Format response message
            lines = [
                "Order ID:",
                f"{active_order.increment_id}",
                "\nShipment Status:",
                f"{status_label}"
            ]
            
            if eta_str:
                lines.append("\nEstimated Delivery:")
                lines.append(eta_str)
                
            has_unavailable = bool(parent_order.unavailable_items)
            if has_unavailable:
                logger.debug(f"Formatting {len(parent_order.unavailable_items)} unavailable items.")
                lines.append("\nUnavailable Items:")
                for item in parent_order.unavailable_items:
                    qty = item['qty']
                    lines.append(f"• {item['name']} ({qty:g})")
                    
            is_cod = False
            if parent_order.payment_method:
                is_cod = "cashondelivery" in parent_order.payment_method.lower() or "cod" in parent_order.payment_method.lower()
                
            if has_unavailable:
                if is_cod:
                    lines.append("\nPayment:")
                    lines.append("This order was placed using Cash on Delivery. You will only pay for the items that were delivered. No refund is required for unavailable items.")
                else:
                    lines.append("\nRefund Status:")
                    if parent_order.refund_state == 2:
                        lines.append("• Completed")
                    elif parent_order.refund_state == 1:
                        lines.append("• Processing")
                    elif parent_order.refund_state:
                        lines.append("• Pending")
                    else:
                        lines.append("• No refund has been initiated yet.")
                        
            if is_external:
                lines.append("\nTracking Information:")
                if active_order.tracking_number:
                    lines.append(f"Courier: {active_order.carrier_code or 'Unknown'}")
                    lines.append(f"Tracking Number: {active_order.tracking_number}")
                    url = self._get_courier_url(active_order.carrier_code, active_order.tracking_number)
                    if url:
                        lines.append(f"Tracking Link: {url}")
                else:
                    lines.append("Your order has been handed over for external delivery. A tracking number will be shared once it becomes available.")
            
            # Karachi orders are delivered by our own riders; share the rider's
            # details once the order has been shipped.
            rider = None
            if not is_external and (active_order.status or "").lower() == "shipped":
                rider = self.repository.get_rider_info(active_order.increment_id)
                if rider:
                    lines.append("\nRider Information:")
                    if rider["name"]:
                        lines.append(f"Name: {rider['name']}")
                    if rider["contact_number"]:
                        lines.append(f"Contact No: {rider['contact_number']}")

            notes = []
            if rider:
                notes.append("Your order has been shipped! You can contact our rider for further tracking, or simply wait - you will receive it today. Happy shopping!")
            if not has_unavailable:
                if eta_str and not rider:
                    notes.append("Your order is on its way. You can expect it to arrive before the estimated delivery time.")
                # else:
                #     notes.append("We'll continue processing your order and update its status as it progresses.")
            else:
                if not is_cod:
                    notes.append("Unavailable items are being refunded.")
            
            if notes:
                lines.append("\nNotes:")
                lines.extend(notes)
            
            friendly_message = "\n".join(lines)
            
            # 6. Return structured response
            return {
                "success": True,
                "message": friendly_message,
                "status": active_order.status,
                "order": {
                    "entity_id": active_order.entity_id,
                    "increment_id": active_order.increment_id,
                    "status": active_order.status,
                    "is_external": is_external
                }
            }
            
        except OrderNotFoundError as e:
            duration_ms = int((time.time() - start_time) * 1000)
            self.audit_service.log_event(
                event_type=AuditEvent.ORDER_TRACKING,
                category=AuditCategory.BUSINESS,
                outcome=AuditOutcome.FAILURE,
                actor="user",
                duration_ms=duration_ms,
                order_id=increment_id,
                error_code=ErrorCode.ERR_RECORD_NOT_FOUND,
                metadata={"error_details": str(e)}
            )
            return {
                "success": False,
                "message": f"Order {increment_id} not found or you don't have access to it.",
                "status": None,
                "order": None
            }
            
        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            logger.error(f"Error fetching order: {e}")
            self.audit_service.log_event(
                event_type=AuditEvent.ORDER_TRACKING,
                category=AuditCategory.BUSINESS,
                outcome=AuditOutcome.FAILURE,
                actor="user",
                duration_ms=duration_ms,
                order_id=increment_id,
                error_code=ErrorCode.ERR_UNKNOWN,
                metadata={"error_details": str(e)}
            )
            return {
                "success": False,
                "message": "Technical difficulties.",
                "status": None,
                "order": None
            }

    def check_order_modifiable(self, increment_id: str) -> Dict[str, Any]:
        logger.debug(f"OrderService received modifiability check for ID: {increment_id}")
        
        # 1. Validate Order ID
        if not increment_id or not isinstance(increment_id, str) or not increment_id.strip():
            logger.warning("Invalid Order ID provided for modifiability check.")
            return {
                "success": False,
                "modifiable": False,
                "message": "Please enter a valid Order ID."
            }
            
        increment_id = increment_id.strip()
        import re
        if not re.match(r"^[a-zA-Z0-9_\-]+$", increment_id):
            logger.warning(f"Invalid Order ID format: {increment_id}")
            return {
                "success": False,
                "modifiable": False,
                "message": "Order ID contains invalid characters. Please check and try again."
            }
            
        try:
            parent_order = self.repository.get_order_by_increment_id(increment_id)
            
            # Select active order (child if exists, else parent)
            if parent_order.child_orders:
                active_order = next((c for c in parent_order.child_orders if c.increment_id == increment_id), parent_order.child_orders[0])
            else:
                active_order = parent_order
                
            status = active_order.status.strip().lower() if active_order.status else ""
            status_label = self.repository.get_status_label(active_order.status)
            if status in ["packed", "shipped", "complete"]:
                return {
                    "success": True,
                    "modifiable": False,
                    "message": f"We are sorry, your order has already been packed or shipped (current status: '{status_label}') and therefore cannot be modified."
                }
            else:
                return {
                    "success": True,
                    "modifiable": True,
                    "message": f"Your order #{increment_id} is currently in '{status_label}' status and can be modified. To add or remove items, please contact our Customer Support team at (021) 111-624-333."
                }
        except OrderNotFoundError:
            logger.debug(f"OrderService: Order {increment_id} not found for modifiability check.")
            return {
                "success": False,
                "modifiable": False,
                "message": "We couldn't find an order with that ID. Please check and try again."
            }
        except Exception as e:
            logger.error(f"OrderService encountered an error checking modifiability for {increment_id}: {e}")
            return {
                "success": False,
                "modifiable": False,
                "message": "We are currently experiencing technical difficulties. Please try again later."
            }

    def get_order_status(self, increment_id: str) -> Dict[str, Any]:
        """Retrieves only the status of the active order."""
        if not increment_id or not str(increment_id).strip():
            return {"success": False, "status": None, "message": "Invalid Order ID."}
            
        try:
            parent_order = self.repository.get_order_by_increment_id(increment_id.strip())
            
            if parent_order.child_orders:
                active_order = next((c for c in parent_order.child_orders if c.increment_id == increment_id), parent_order.child_orders[0])
            else:
                active_order = parent_order
                
            return {
                "success": True,
                "status": active_order.status,
                "state": active_order.state,
                "message": ""
            }
        except OrderNotFoundError:
            return {"success": False, "status": None, "message": "Order not found."}
        except Exception as e:
            logger.error(f"OrderService error in get_order_status for {increment_id}: {e}")
            return {"success": False, "status": None, "message": "Technical difficulties."}

    def verify_customer(self, increment_id: str, provided_phone: str) -> bool:
        """
        Normalizes and matches the provided phone number against the billing address phone.
        """
        db_phone = self.repository.get_order_phone(increment_id)
        if not db_phone:
            return False
            
        import re
        def normalize_phone(phone: str) -> str:
            if not phone: return ""
            digits = re.sub(r'\D', '', str(phone))
            if digits.startswith('92') and len(digits) == 12:
                return '0' + digits[2:]
            elif len(digits) == 10 and not digits.startswith('0'):
                return '0' + digits
            return digits
            
        norm_db = normalize_phone(db_phone)
        norm_provided = normalize_phone(provided_phone)
        
        is_match = (norm_db == norm_provided and bool(norm_db))
        self.audit_service.log_event(
            event_type=AuditEvent.PHONE_VERIFICATION,
            category=AuditCategory.SECURITY,
            outcome=AuditOutcome.SUCCESS if is_match else AuditOutcome.FAILURE,
            actor="user",
            order_id=increment_id,
            metadata={"verification_result": is_match}
        )
        
        return is_match

    def execute_cancellation(self, increment_id: str, cancel_reason: str = "Unknown reason") -> Dict[str, Any]:
        """Executes the cancellation of the active order."""
        if not increment_id or not str(increment_id).strip():
            return {"success": False, "message": "Invalid Order ID."}
            
        import time
        start_time = time.time()
        try:
            logger.info(f"OrderService: Executing cancellation for {increment_id} with reason: {cancel_reason}")
            success = self.repository.cancel_order(increment_id.strip(), cancel_reason)
            duration_ms = int((time.time() - start_time) * 1000)
            
            if success:
                self.audit_service.log_event(
                    event_type=AuditEvent.ORDER_CANCELLATION,
                    category=AuditCategory.BUSINESS,
                    outcome=AuditOutcome.SUCCESS,
                    actor="user",
                    duration_ms=duration_ms,
                    order_id=increment_id
                )
                return {
                    "success": True,
                    "message": f"Your order #{increment_id} has been successfully cancelled."
                }
            else:
                return {
                    "success": False,
                    "message": "We couldn't cancel your order at this time. Please try again later."
                }
        except Exception as e:
            logger.error(f"OrderService error in execute_cancellation for {increment_id}: {e}")
            return {"success": False, "message": "We encountered an error while canceling your order. Please try again later."}
