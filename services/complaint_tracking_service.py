import logging
from typing import Dict, Any
from data.repositories.complaint_tracking_repository import ComplaintTrackingRepository

logger = logging.getLogger(__name__)

class ComplaintTrackingService:
    def __init__(self, tracking_repository=None):
        self.repository = tracking_repository or ComplaintTrackingRepository()

    def track_complaint(self, order_number: str) -> Dict[str, Any]:
        logger.info(f"ComplaintTrackingService.track_complaint() executed for order_number={order_number}")
        try:
            results = self.repository.find_by_order(order_number=order_number)
            logger.info(f"ComplaintTrackingService: repository returned {len(results)} rows for order {order_number}")
            
            if not results:
                if self.repository.check_order_exists(order_number):
                    return {
                        "success": True,
                        "message": f"We couldn't find any complaints for Order {order_number}."
                    }
                else:
                    return {
                        "success": False,
                        "message": f"Order #{order_number} does not exist in our system."
                    }
            
            message_lines = [f"Order Number: {order_number}\n"]
            
            num_complaints = len(results)
            message_lines.append(f"Found {num_complaints} complaint{'s' if num_complaints != 1 else ''}\n")
            
            for idx, row in enumerate(results, start=1):
                ticket_no = row.get("ticket_no", "Unknown")
                status = row.get("status", "Unknown")
                c_type = row.get("type", "Unknown")
                subject = row.get("subject", "Complaint")
                created_at = row.get("created_at")
                action_taken = row.get("action_taken")

                category = f"{subject} ({c_type})" if c_type else subject
                date_str = created_at.strftime("%d %b %Y") if hasattr(created_at, "strftime") else "Unknown Date"
                
                msg = f"Complaint {idx}\n"
                msg += f"Ticket No: {ticket_no}\n"
                msg += f"Status: {status}\n"
                msg += f"Category:\n{category}\n"
                msg += f"Created:\n{date_str}\n"
                
                if action_taken:
                    msg += f"Latest Update:\n{action_taken}"
                else:
                    msg += "Latest Update:\nNo update yet."
                
                message_lines.append(msg)
                
            return {
                "success": True,
                "message": "\n".join(message_lines),
                "complaints": results
            }
            
        except Exception as e:
            logger.error(f"Error tracking complaint for order {order_number}: {e}")
            return {
                "success": False,
                "message": "We encountered an issue retrieving your complaint information."
            }
