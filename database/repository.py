import logging
from typing import Optional
from database.connection import DatabaseManager
from database.schema import Order
from utils.logger import get_logger
import datetime
from typing import Optional

logger = get_logger(__name__)

class OrderNotFoundError(Exception):
    pass

class OrderRepository:
    def resolve_to_latest_order_id(self, increment_id: str) -> str:
        if not increment_id:
            return increment_id
        if "-" not in increment_id:
            child_query = """
            SELECT c.increment_id 
            FROM sales_order c
            JOIN sales_order p ON c.relation_parent_id = p.entity_id
            WHERE p.increment_id = %s
            ORDER BY c.entity_id DESC
            LIMIT 1
            """
            try:
                with DatabaseManager() as conn:
                    cursor = conn.cursor(dictionary=True)
                    cursor.execute(child_query, (increment_id,))
                    res = cursor.fetchone()
                    cursor.close()
                    if res:
                        logger.debug(f"Resolved parent order {increment_id} to child order {res['increment_id']}")
                        return res["increment_id"]
            except Exception as e:
                logger.error(f"Error resolving parent order: {e}")
        return increment_id

    def get_entity_id_by_increment_id(self, increment_id: str) -> Optional[int]:
        query = "SELECT entity_id FROM sales_order WHERE increment_id = %s"
        try:
            with DatabaseManager() as conn:
                cursor = conn.cursor(dictionary=True)
                cursor.execute(query, (increment_id,))
                res = cursor.fetchone()
                cursor.close()
                return res["entity_id"] if res else None
        except Exception:
            return None

    def get_order_phone(self, increment_id: str) -> Optional[str]:
        """
        Retrieves the shipping phone number for a given order increment_id.
        """
        query = """
        SELECT a.telephone 
        FROM sales_order_address a
        JOIN sales_order o ON a.parent_id = o.entity_id
        WHERE o.increment_id = %s AND a.address_type = 'shipping'
        LIMIT 1
        """
        try:
            with DatabaseManager() as conn:
                cursor = conn.cursor(dictionary=True)
                cursor.execute(query, (increment_id,))
                res = cursor.fetchone()
                cursor.close()
                return res["telephone"] if res else None
        except Exception as e:
            logger.error(f"Error fetching order phone for {increment_id}: {e}")
            return None

    def get_rider_info(self, increment_id: str) -> Optional[dict]:
        """
        Returns the rider assigned to a shipped order as {"name", "contact_number"},
        or None if no rider has been assigned.
        """
        query = """
        SELECT os.ridername, r.contact_number
        FROM nop_order_shipment AS os
        LEFT JOIN nop_riders AS r ON os.rider_id = r.id
        WHERE os.ordernumber = %s
        LIMIT 1
        """
        try:
            with DatabaseManager() as conn:
                cursor = conn.cursor(dictionary=True)
                cursor.execute(query, (increment_id,))
                res = cursor.fetchone()
                cursor.close()
                if not res or not (res["ridername"] or res["contact_number"]):
                    return None
                return {"name": res["ridername"], "contact_number": res["contact_number"]}
        except Exception as e:
            logger.error(f"Error fetching rider info for {increment_id}: {e}")
            return None

    def get_items_by_entity_id(self, entity_id: int) -> list:
        query = "SELECT sku, name, qty_ordered FROM sales_order_item WHERE order_id = %s"
        try:
            with DatabaseManager() as conn:
                cursor = conn.cursor(dictionary=True)
                cursor.execute(query, (entity_id,))
                res = cursor.fetchall()
                cursor.close()
                return res
        except Exception:
            return []

    def get_unavailable_items(self, parent_increment_id: str, child_increment_id: str) -> list:
        parent_entity = self.get_entity_id_by_increment_id(parent_increment_id)
        child_entity = self.get_entity_id_by_increment_id(child_increment_id)
        if not parent_entity or not child_entity:
            return []
            
        parent_items = self.get_items_by_entity_id(parent_entity)
        child_items = self.get_items_by_entity_id(child_entity)
        
        child_map = {item['sku']: item['qty_ordered'] for item in child_items}
        
        unavailable = []
        for p_item in parent_items:
            sku = p_item['sku']
            name = p_item['name']
            p_qty = p_item['qty_ordered']
            
            c_qty = child_map.get(sku, 0)
            
            if p_qty > c_qty:
                diff_qty = int(p_qty - c_qty)
                unavailable.append(f"{name} (Qty: {diff_qty})")
                
        return unavailable

    def get_payment_method(self, increment_id: str) -> str:
        """
        Returns the payment method code for an order.
        e.g. 'cashondelivery', 'jazzcash', 'bankalfalah', etc.
        Returns empty string if not found.
        """
        query = """
        SELECT p.method 
        FROM sales_order o 
        JOIN sales_order_payment p ON o.entity_id = p.parent_id 
        WHERE o.increment_id = %s 
        LIMIT 1
        """
        try:
            with DatabaseManager() as conn:
                cursor = conn.cursor(dictionary=True)
                cursor.execute(query, (increment_id,))
                res = cursor.fetchone()
                cursor.close()
                return res["method"] if res else ""
        except Exception as e:
            logger.error(f"Error fetching payment method for {increment_id}: {e}")
            return ""

    def get_refund_status(self, child_increment_id: str) -> Optional[str]:
        """
        Checks if a refund has already been initiated for the given child order.
        Looks in nop_refund_products_info and sales_creditmemo.
        Returns a human-readable status string, or None if no refund found.
        """
        try:
            with DatabaseManager() as conn:
                cursor = conn.cursor(dictionary=True)
                
                # Check nop_refund_products_info
                cursor.execute(
                    "SELECT COUNT(*) as cnt FROM nop_refund_products_info WHERE ordernumber = %s",
                    (child_increment_id,)
                )
                row = cursor.fetchone()
                if row and row["cnt"] > 0:
                    cursor.close()
                    return "in progress"
                
                # Check sales_creditmemo (state: 1=open, 2=refunded, 3=cancelled)
                cursor.execute("""
                    SELECT cm.state 
                    FROM sales_creditmemo cm
                    JOIN sales_order o ON cm.order_id = o.entity_id
                    WHERE o.increment_id = %s
                    ORDER BY cm.created_at DESC
                    LIMIT 1
                """, (child_increment_id,))
                row = cursor.fetchone()
                cursor.close()
                if row:
                    state_map = {1: "in progress", 2: "completed", 3: "cancelled"}
                    return state_map.get(row["state"], "in progress")
                
                return None
        except Exception as e:
            logger.error(f"Error checking refund status for {child_increment_id}: {e}")
            return None

    # Columns + joins shared by every order lookup. Payment method and the latest
    # credit memo are folded in so the tracking flow needs no extra round trips
    # (each round trip to the DB costs ~200ms).
    _ORDER_COLUMNS = """
            o.entity_id,
            o.increment_id,
            o.status,
            o.state,
            o.relation_parent_id,
            o.relation_parent_real_id,
            a.delivery_due_date,
            a.courier,
            a.cn_number,
            addr.city,
            addr.firstname,
            addr.lastname,
            addr.telephone,
            addr.street,
            pay.method AS payment_method,
            (SELECT cm.state FROM sales_creditmemo cm WHERE cm.order_id = o.entity_id ORDER BY cm.entity_id DESC LIMIT 1) AS refund_state,
            (SELECT cm.grand_total FROM sales_creditmemo cm WHERE cm.order_id = o.entity_id ORDER BY cm.entity_id DESC LIMIT 1) AS refund_amount,
            (SELECT cm.created_at FROM sales_creditmemo cm WHERE cm.order_id = o.entity_id ORDER BY cm.entity_id DESC LIMIT 1) AS refund_date
    """
    _ORDER_FROM = """
        FROM sales_order o
        LEFT JOIN nhd_sales_order_additionals a ON o.entity_id = a.order_id
        LEFT JOIN sales_order_address addr ON o.entity_id = addr.parent_id AND addr.address_type = 'shipping'
        LEFT JOIN sales_order_payment pay ON o.entity_id = pay.parent_id
    """

    # Resolves the family root (the parent's entity_id) of the entered order,
    # whether the customer typed the parent or a child increment_id.
    _ROOT_SUBQUERY = "(SELECT COALESCE(NULLIF(r.relation_parent_id, 0), r.entity_id) FROM sales_order r WHERE r.increment_id = %s LIMIT 1)"

    # Status labels rarely change, so they are loaded once and reused.
    _STATUS_LABEL_TTL_SECONDS = 3600
    _status_labels: dict = {}
    _status_labels_loaded_at: float = 0.0

    def order_exists(self, increment_id: str) -> bool:
        """Cheap existence check used before phone verification."""
        import os
        try:
            with DatabaseManager() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT 1 FROM sales_order WHERE increment_id = %s LIMIT 1", (increment_id,))
                found = cursor.fetchall()
                cursor.close()
                return bool(found)
        except Exception as e:
            if os.getenv("DB_MOCK_FALLBACK", "false").lower() == "true":
                logger.warning(f"Database error ({e}), but DB_MOCK_FALLBACK is enabled. Treating order {increment_id} as existing.")
                return True
            logger.error(f"Database error while checking order {increment_id} exists: {e}")
            raise RuntimeError(f"Database error: {e}") from e

    def _fetch_order_data(self, cursor, identifier, is_increment=True):
        where_clause = "o.increment_id" if is_increment else "o.entity_id"
        cursor.execute(f"SELECT {self._ORDER_COLUMNS} {self._ORDER_FROM} WHERE {where_clause} = %s", (identifier,))
        rows = cursor.fetchall()
        return self._row_to_order(rows[0]) if rows else None

    def _fetch_order_family(self, cursor, increment_id):
        """Fetches the parent order and all of its child orders in one round trip.
        Returns (parent_row_or_None, [child_rows])."""
        query = f"""
            SELECT 'parent' AS family_role, {self._ORDER_COLUMNS} {self._ORDER_FROM}
            WHERE o.entity_id = {self._ROOT_SUBQUERY}
            UNION ALL
            SELECT 'child' AS family_role, {self._ORDER_COLUMNS} {self._ORDER_FROM}
            WHERE o.relation_parent_id = {self._ROOT_SUBQUERY}
        """
        cursor.execute(query, (increment_id, increment_id))
        rows = cursor.fetchall()

        # The address/payment joins can repeat an order; keep the first row per entity.
        parent_row, children = None, {}
        for row in rows:
            if row["family_role"] == "parent":
                parent_row = parent_row or row
            else:
                children.setdefault(row["entity_id"], row)
        child_rows = list(children.values())
        child_rows.sort(key=lambda r: r["entity_id"])
        return parent_row, child_rows

    def _row_to_order(self, result):
        order = Order(
            entity_id=result["entity_id"],
            increment_id=result["increment_id"],
            status=result["status"],
            state=result["state"],
            estimated_delivery_datetime=result["delivery_due_date"],
            shipping_city=result["city"],
            recipient_name=f"{result['firstname'] or ''} {result['lastname'] or ''}".strip() if result['firstname'] or result['lastname'] else None,
            recipient_phone=result["telephone"],
            shipping_address=result["street"],
            carrier_code=result["courier"],
            tracking_number=result["cn_number"]
        )
        
        # Attach raw relation fields for internal repository use
        order._relation_parent_id = result.get("relation_parent_id")
        order._relation_parent_real_id = result.get("relation_parent_real_id")

        order.payment_method = result.get("payment_method")
        # Refund status only matters for prepaid orders
        if order.payment_method and "cashondelivery" not in order.payment_method.lower() and "cod" not in order.payment_method.lower():
            order.refund_state = result.get("refund_state")
            order.refund_amount = float(result["refund_amount"]) if result.get("refund_amount") else (0.0 if order.refund_state is not None else None)
            order.refund_date = result.get("refund_date")
        return order

    def get_order_by_increment_id(self, increment_id: str) -> Order:
        import os
        mock_fallback = os.getenv("DB_MOCK_FALLBACK", "false").lower() == "true"
        try:
            with DatabaseManager() as conn:
                logger.debug(f"Fetching order with increment_id: {increment_id}")
                cursor = conn.cursor(dictionary=True)

                # Parent + all children in a single round trip, whether the
                # customer entered the parent or a child increment_id.
                parent_row, child_rows = self._fetch_order_family(cursor, increment_id)

                if parent_row:
                    parent_order = self._row_to_order(parent_row)
                    parent_order.child_orders = [self._row_to_order(r) for r in child_rows]
                else:
                    # Either the order doesn't exist, or it's a child whose parent
                    # row is missing - in that case treat the entered order as the parent.
                    parent_order = self._fetch_order_data(cursor, increment_id, is_increment=True)
                    if not parent_order:
                        logger.warning(f"Order not found for increment_id: {increment_id}")
                        raise OrderNotFoundError(f"Order with increment_id {increment_id} not found.")
                    logger.debug(f"Parent of order {increment_id} not found. Using entered order as parent.")
                    cursor.execute(f"SELECT {self._ORDER_COLUMNS} {self._ORDER_FROM} WHERE o.relation_parent_id = %s ORDER BY o.entity_id", (parent_order.entity_id,))
                    seen = set()
                    for row in cursor.fetchall():
                        if row["entity_id"] not in seen:
                            seen.add(row["entity_id"])
                            parent_order.child_orders.append(self._row_to_order(row))

                logger.debug(f"Successfully retrieved parent order: {parent_order.increment_id}")
                logger.debug(f"Parent order detection: found {len(parent_order.child_orders)} child orders for {parent_order.increment_id}")
                logger.debug(f"Payment method for {increment_id}: {parent_order.payment_method}")
                if parent_order.refund_state is not None:
                    logger.debug(f"Refund status for {increment_id}: State={parent_order.refund_state}")

                if parent_order.child_orders:
                    # Fetch unavailable items (comparing parent to ALL child orders)
                    child_entity_ids = [str(c.entity_id) for c in parent_order.child_orders]
                    child_ids_str = ",".join(child_entity_ids)
                    
                    logger.debug(f"Parent Order ID: {parent_order.increment_id}")
                    logger.debug(f"Child Order IDs: {[c.increment_id for c in parent_order.child_orders]}")
                    
                    query = f"""
                        SELECT p.name, (p.qty_ordered - COALESCE(c.child_qty, 0)) as unavailable_qty, p.sku, p.qty_ordered as parent_qty, c.child_qty
                        FROM sales_order_item p
                        LEFT JOIN (
                            SELECT sku, SUM(qty_ordered) as child_qty
                            FROM sales_order_item
                            WHERE order_id IN ({child_ids_str}) AND parent_item_id IS NULL
                            GROUP BY sku
                        ) c ON p.sku = c.sku
                        WHERE p.order_id = %s AND p.parent_item_id IS NULL
                        HAVING unavailable_qty > 0
                    """
                    cursor.execute(query, (parent_order.entity_id,))
                    for item in cursor.fetchall():
                        parent_order.unavailable_items.append({"name": item["name"], "qty": float(item["unavailable_qty"])})
                        logger.debug(f"Unavailable calculation for SKU {item['sku']}: Parent Qty={item['parent_qty']}, Child Qty={item['child_qty']}, Unavailable={item['unavailable_qty']}")
                        
                    logger.debug(f"Final unavailable_items list: {parent_order.unavailable_items}")

                cursor.close()
                return parent_order
        except OrderNotFoundError:
            raise
        except Exception as e:
            if mock_fallback:
                logger.warning(f"Database error ({e}), but DB_MOCK_FALLBACK is enabled. Returning mock order.")
                last_char = increment_id[-1] if increment_id else "0"
                if last_char in ["0", "2", "4", "6", "8"]:
                    status = "shipped"
                elif last_char in ["1", "3", "5", "7"]:
                    status = "processing"
                else:
                    status = "packed"
                
                from database.schema import Order
                from datetime import datetime, timedelta
                return Order(
                    entity_id=99999,
                    increment_id=increment_id,
                    status=status,
                    estimated_delivery_datetime=datetime.now() + timedelta(days=2),
                    shipping_city="Karachi",
                    recipient_name="Mock Customer",
                    recipient_phone="03001234567",
                    shipping_address="123 Mock Street",
                    carrier_code="lcsshipping",
                    tracking_number="LCS12345678"
                )
            else:
                logger.error(f"Database error while fetching order {increment_id}: {e}")
                raise RuntimeError(f"Database error: {e}") from e

    def get_order_status(self, increment_id: str) -> str:
        query = "SELECT status FROM sales_order WHERE increment_id = %s"
        try:
            with DatabaseManager() as conn:
                logger.debug(f"Fetching order status for increment_id: {increment_id}")
                cursor = conn.cursor(dictionary=True)
                cursor.execute(query, (increment_id,))
                result = cursor.fetchone()
                cursor.close()
                
                if not result:
                    logger.warning(f"Order not found for increment_id: {increment_id}")
                    raise OrderNotFoundError(f"Order with increment_id {increment_id} not found.")
                
                status = result["status"]
                logger.debug(f"Successfully retrieved status '{status}' for order: {increment_id}")
                return status
        except OrderNotFoundError:
            raise
        except Exception as e:
            logger.error(f"Database error while fetching order status {increment_id}: {e}")
            raise RuntimeError(f"Database error: {e}") from e

    def get_status_label(self, status: str) -> str:
        """Fetch user-friendly label for the given status from sales_order_status table.
        Returns the original status string capitalized if not found or on database error."""
        if not status:
            return ""
        import time
        cls = OrderRepository
        try:
            if not cls._status_labels or time.time() - cls._status_labels_loaded_at > cls._STATUS_LABEL_TTL_SECONDS:
                with DatabaseManager() as conn:
                    logger.debug("Loading status labels from sales_order_status")
                    cursor = conn.cursor(dictionary=True)
                    cursor.execute("SELECT status, label FROM sales_order_status")
                    rows = cursor.fetchall()
                    cursor.close()
                cls._status_labels = {r["status"].lower(): r["label"] for r in rows if r.get("status") and r.get("label")}
                cls._status_labels_loaded_at = time.time()

            label = cls._status_labels.get(status.lower())
            if label:
                logger.debug(f"Retrieved label '{label}' for status: {status}")
                return label
            return status.capitalize()
        except Exception as e:
            logger.error(f"Error fetching status label for status {status}: {e}")
            import os
            mock_fallback = os.getenv("DB_MOCK_FALLBACK", "false").lower() == "true"
            if mock_fallback:
                mapping = {
                    "pending": "Pending",
                    "processing": "Processing",
                    "shipped": "Shipped / Dispatched",
                    "packed": "Packed",
                    "complete": "Complete",
                    "canceled": "Canceled"
                }
                return mapping.get(status.lower(), status.capitalize())
            return status.capitalize()

    def get_delivery_date(self, increment_id: str) -> Optional[datetime.datetime]:
        """Fetch the actual completion/delivery datetime (completed_at) for the given order increment ID.
        Returns None if not found or if the field is unavailable."""
        query = """
        SELECT a.completed_at 
        FROM sales_order o
        LEFT JOIN nhd_sales_order_additionals a ON o.entity_id = a.order_id
        WHERE o.increment_id = %s
        """
        try:
            with DatabaseManager() as conn:
                cursor = conn.cursor(dictionary=True)
                cursor.execute(query, (increment_id,))
                result = cursor.fetchone()
                cursor.close()
                if result and result["completed_at"]:
                    return result["completed_at"]
                return None
        except Exception as e:
            logger.error(f"Error fetching delivery date for {increment_id}: {e}")
            return None

    def cancel_order(self, increment_id: str, cancel_reason: str = "Unknown reason") -> bool:
        """
        Cancels an order by updating its state and status to 'canceled' in sales_order and sales_order_grid,
        and inserts a history comment indicating cancellation.
        """
        try:
            with DatabaseManager() as conn:
                cursor = conn.cursor(dictionary=True)
                
                # 1. Retrieve entity_id, status, and state with FOR UPDATE to prevent race conditions
                select_query = "SELECT entity_id, status, state FROM sales_order WHERE increment_id = %s FOR UPDATE"
                cursor.execute(select_query, (increment_id,))
                order_row = cursor.fetchone()
                
                if not order_row:
                    logger.warning(f"Failed to cancel order {increment_id}: order not found.")
                    conn.rollback()
                    cursor.close()
                    return False
                
                entity_id = order_row["entity_id"]
                current_status = order_row["status"]
                current_state = order_row["state"]
                
                # Defensive validation: Do not update if already canceled
                if current_state == 'canceled' or current_status == 'canceled':
                    logger.info(f"Order {increment_id} is already canceled in the database.")
                    conn.rollback()
                    cursor.close()
                    return True
                
                cursor.close()
                cursor = conn.cursor()
                
                # 2. Update sales_order
                update_so_query = "UPDATE sales_order SET state = 'canceled', status = 'canceled', updated_at = NOW() WHERE entity_id = %s"
                cursor.execute(update_so_query, (entity_id,))
                
                # 3. Update sales_order_grid
                update_sog_query = "UPDATE sales_order_grid SET status = 'canceled', updated_at = NOW() WHERE entity_id = %s"
                cursor.execute(update_sog_query, (entity_id,))
                
                # 4. Insert into sales_order_status_history
                clean_reason = cancel_reason.replace('_', ' ').capitalize()
                formatted_comment = f"Order cancelled by Naheed AI Chatbot.\n\nCustomer reason: {clean_reason}.\n\nCancelled automatically via AI Customer Support."
                insert_history_query = """
                INSERT INTO sales_order_status_history 
                (parent_id, is_customer_notified, is_visible_on_front, comment, status, entity_name) 
                VALUES (%s, 0, 0, %s, 'canceled', 'order')
                """
                cursor.execute(insert_history_query, (entity_id, formatted_comment))
                
                conn.commit()
                logger.info(f"Successfully cancelled order {increment_id} (entity_id={entity_id}) in database via transaction.")
                cursor.close()
                return True
                
        except Exception as e:
            logger.exception(f"Database transaction error while cancelling order {increment_id}: {e}")
            return False

class ComplaintRepository:
    def create_complaint_ticket(
        self,
        order_number: str,
        entity_id: int,
        name: str,
        email: str,
        phone: str,
        subject: str,
        complain: str,
        complain_type: str,
        priority: str = "low",
        mood: str = "happy"
    ) -> int:
        # AI-judged urgency ("high"/"low") and customer mood ("happy"/"sad"), captured
        # from the message that triggered this ticket. Stored in `priority`/`mood`
        # columns on nhd_complain_tickets - see migrations/add_priority_mood_to_complain_tickets.sql
        
        # An upset customer always escalates the ticket to high priority, but a
        # calm mood never downgrades a priority the AI already judged as high.
        if mood.lower() in ["sad", "angry", "frustrated", "bad", "unhappy"]:
            priority = "high"

        query = """
        INSERT INTO nhd_complain_tickets (
            order_number, entity_id, customer_name, customer_email, customer_phone, 
            subject, complain, type, status, action_taken, refund_amount, priority
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'New', '', '', %s)
        """
        try:
            with DatabaseManager() as conn:
                logger.debug(f"Creating complaint ticket for order {order_number} (priority={priority}, mood={mood})")
                cursor = conn.cursor()
                cursor.execute(query, (order_number, entity_id, name, email, phone, subject, complain, complain_type, priority))
                conn.commit()
                ticket_no = cursor.lastrowid
                cursor.close()
                logger.info(f"Successfully created complaint ticket #{ticket_no}")
                return ticket_no
        except Exception as e:
            logger.error(f"Database error while creating complaint ticket: {e}")
            raise RuntimeError(f"Database error: {e}") from e

    def add_ticket_attachment(self, ticket_no: int, image_url: str):
        query = """
        INSERT INTO nhd_complain_tickets_attachments_info (
            ticket_no, image_url, status
        ) VALUES (%s, %s, 'New')
        """
        try:
            with DatabaseManager() as conn:
                logger.debug(f"Adding attachment for ticket {ticket_no}: {image_url}")
                cursor = conn.cursor()
                cursor.execute(query, (ticket_no, image_url))
                conn.commit()
                cursor.close()
                logger.info(f"Successfully added attachment to ticket #{ticket_no}")
        except Exception as e:
            logger.error(f"Database error while adding ticket attachment: {e}")
            raise RuntimeError(f"Database error: {e}") from e

    def has_existing_complaint_type(self, order_number: str, complain_type: str) -> bool:
        query = "SELECT COUNT(*) as count FROM nhd_complain_tickets WHERE order_number = %s AND type = %s"
        try:
            with DatabaseManager() as conn:
                cursor = conn.cursor(dictionary=True)
                cursor.execute(query, (order_number, complain_type))
                result = cursor.fetchone()
                cursor.close()
                return (result["count"] > 0) if result else False
        except Exception as e:
            logger.error(f"Database error while checking existing complaints: {e}")
            raise RuntimeError(f"Database error: {e}") from e
