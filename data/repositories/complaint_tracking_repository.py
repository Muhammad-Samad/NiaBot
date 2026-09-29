import logging
from typing import List, Dict, Any
from database.connection import DatabaseManager

logger = logging.getLogger(__name__)

class ComplaintTrackingRepository:
    def get_order_family(self, order_number: str) -> List[str]:
        """
        Resolves the Magento order family for a given order number.
        Returns a list of increment IDs including the parent and all children.
        """
        query_root = "SELECT relation_parent_real_id FROM sales_order WHERE increment_id = %s"
        try:
            with DatabaseManager() as conn:
                cursor = conn.cursor(dictionary=True)
                cursor.execute(query_root, (order_number,))
                result = cursor.fetchone()
                
                if not result:
                    cursor.close()
                    return [order_number]
                
                # If there's a parent, the parent is the root. Otherwise, this order is the root.
                root_id = result.get('relation_parent_real_id')
                if not root_id:
                    root_id = order_number

                # Now get the root itself and all its children
                query_family = "SELECT increment_id FROM sales_order WHERE increment_id = %s OR relation_parent_real_id = %s"
                cursor.execute(query_family, (root_id, root_id))
                family_results = cursor.fetchall()
                cursor.close()
                
                family_ids = [row['increment_id'] for row in family_results]
                return family_ids if family_ids else [order_number]
        except Exception as e:
            logger.error(f"Database error in get_order_family for {order_number}: {e}")
            return [order_number]

    def find_by_order(self, order_number: str, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        logger.info(f"ComplaintTrackingRepository.find_by_order() called for order_number={order_number}")
        
        family_ids = self.get_order_family(order_number)
        
        placeholders = ', '.join(['%s'] * len(family_ids))
        query = f"""
        SELECT 
            ticket_no, 
            order_number, 
            status, 
            subject, 
            type, 
            created_at, 
            complain, 
            action_taken 
        FROM nhd_complain_tickets 
        WHERE order_number IN ({placeholders})
        ORDER BY created_at DESC, order_number ASC
        LIMIT %s OFFSET %s;
        """
        try:
            with DatabaseManager() as conn:
                cursor = conn.cursor(dictionary=True)
                params = tuple(family_ids) + (limit, offset)
                cursor.execute(query, params)
                results = cursor.fetchall()
                logger.info(f"ComplaintTrackingRepository.find_by_order() SQL returned {len(results)} rows for family {family_ids}.")
                cursor.close()
                return results
        except Exception as e:
            logger.error(f"Database error in find_by_order for order {order_number}: {e}")
            raise RuntimeError(f"Database error: {e}") from e

    def check_order_exists(self, order_number: str) -> bool:
        query = "SELECT COUNT(*) as count FROM sales_order WHERE increment_id = %s"
        try:
            with DatabaseManager() as conn:
                cursor = conn.cursor(dictionary=True)
                cursor.execute(query, (order_number,))
                result = cursor.fetchone()
                cursor.close()
                return (result["count"] > 0) if result else False
        except Exception as e:
            logger.error(f"Database error in check_order_exists for order {order_number}: {e}")
            raise RuntimeError(f"Database error: {e}") from e
