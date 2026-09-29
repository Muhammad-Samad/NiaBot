from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from datetime import datetime

@dataclass
class Order:
    entity_id: int
    increment_id: str
    status: str
    state: Optional[str] = None
    estimated_delivery_datetime: Optional[datetime] = None
    shipping_city: Optional[str] = None
    recipient_name: Optional[str] = None
    recipient_phone: Optional[str] = None
    shipping_address: Optional[str] = None
    carrier_code: Optional[str] = None
    tracking_number: Optional[str] = None
    
    # Parent-Child Tracking Extensions
    child_orders: List['Order'] = field(default_factory=list)
    unavailable_items: List[Dict[str, Any]] = field(default_factory=list)
    payment_method: Optional[str] = None
    refund_state: Optional[int] = None
    refund_amount: Optional[float] = None
    refund_date: Optional[datetime] = None
