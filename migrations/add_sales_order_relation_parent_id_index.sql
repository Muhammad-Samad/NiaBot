-- Index for order tracking: child-order lookups by parent.
--
-- OrderRepository.get_order_by_increment_id finds child orders with
-- `WHERE relation_parent_id = ?`. sales_order has no index on that column, so
-- every tracking request does a full scan (~808k rows; measured 2.5s warm,
-- 14s cold). All other columns the tracking queries filter/join on are
-- already indexed.
--
-- ALGORITHM=INPLACE, LOCK=NONE builds the index online - reads and writes to
-- sales_order continue while it builds. If sales_order is managed by a Magento
-- module's db_schema.xml, also declare the index there so it is tracked.

ALTER TABLE sales_order
    ADD INDEX IDX_SALES_ORDER_RELATION_PARENT_ID (relation_parent_id),
    ALGORITHM=INPLACE, LOCK=NONE;

-- Rollback:
-- ALTER TABLE sales_order DROP INDEX IDX_SALES_ORDER_RELATION_PARENT_ID;
