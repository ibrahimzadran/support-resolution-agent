-- Support Resolution Agent — core schema
--
-- Design notes:
--   * All money is INTEGER cents. The agent's authority limit is a comparison
--     against $50; float dollars can land on 50.000000000001 and flip the
--     branch nondeterministically. Cents makes `amount_cents <= 5000` exact.
--   * The "refunds cannot exceed the order total" rule is a TRIGGER, not a
--     CHECK: it is a cross-row aggregate (SUM(refunds) + new <= total) and
--     SQLite CHECK constraints cannot see other rows.
--   * Dates are ISO-8601 date strings seeded relative to a pinned reference
--     date (see backend/config.py). Nothing in this database or the tools
--     calls date('now'), so eligibility windows do not drift over time.

PRAGMA foreign_keys = ON;

DROP TRIGGER IF EXISTS refunds_no_overrefund_insert;
DROP TRIGGER IF EXISTS refunds_no_overrefund_update;
DROP TRIGGER IF EXISTS refunds_no_refund_on_cancelled;
DROP TABLE IF EXISTS refunds;
DROP TABLE IF EXISTS order_items;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS customers;

CREATE TABLE customers (
    customer_id     INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    email           TEXT NOT NULL UNIQUE,
    created_at      TEXT NOT NULL,
    tier            TEXT NOT NULL CHECK (tier IN ('standard', 'plus', 'vip')),
    account_status  TEXT NOT NULL CHECK (account_status IN ('active', 'suspended', 'closed'))
);

CREATE TABLE orders (
    order_id        TEXT PRIMARY KEY,
    customer_id     INTEGER NOT NULL REFERENCES customers(customer_id),
    placed_at       TEXT NOT NULL,
    estimated_delivery TEXT NOT NULL,
    shipped_at      TEXT,
    delivered_at    TEXT,
    status          TEXT NOT NULL CHECK (status IN (
                        'pending', 'shipped', 'in_transit',
                        'delivered', 'returned', 'cancelled')),
    total_cents     INTEGER NOT NULL CHECK (total_cents > 0),
    currency        TEXT NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
    carrier         TEXT,
    tracking_number TEXT,

    -- A delivery timestamp exists if and only if the goods reached the customer.
    CHECK (
        (status IN ('delivered', 'returned') AND delivered_at IS NOT NULL)
        OR
        (status NOT IN ('delivered', 'returned') AND delivered_at IS NULL)
    ),
    -- Anything that has left the warehouse has a ship date and a tracking number.
    CHECK (
        (status IN ('shipped', 'in_transit', 'delivered', 'returned')
             AND shipped_at IS NOT NULL AND tracking_number IS NOT NULL AND carrier IS NOT NULL)
        OR
        (status IN ('pending', 'cancelled')
             AND shipped_at IS NULL AND tracking_number IS NULL AND carrier IS NULL)
    ),
    -- Ordering of lifecycle timestamps.
    CHECK (estimated_delivery >= placed_at),
    CHECK (shipped_at IS NULL OR shipped_at >= placed_at),
    CHECK (delivered_at IS NULL OR delivered_at >= shipped_at)
);

CREATE INDEX idx_orders_customer ON orders(customer_id);

CREATE TABLE order_items (
    item_id          INTEGER PRIMARY KEY,
    order_id         TEXT NOT NULL REFERENCES orders(order_id),
    sku              TEXT NOT NULL,
    name             TEXT NOT NULL,
    category         TEXT NOT NULL CHECK (category IN (
                         'electronics', 'apparel', 'home', 'accessories',
                         'gift_card', 'final_sale')),
    quantity         INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0)
);

CREATE INDEX idx_items_order ON order_items(order_id);

CREATE TABLE refunds (
    refund_id    TEXT PRIMARY KEY,
    order_id     TEXT NOT NULL REFERENCES orders(order_id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    reason       TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('completed', 'pending', 'failed')),
    issued_by    TEXT NOT NULL CHECK (issued_by IN ('agent', 'human', 'system')),
    created_at   TEXT NOT NULL
);

CREATE INDEX idx_refunds_order ON refunds(order_id);

-- ---------------------------------------------------------------------------
-- Cross-row invariants that CHECK cannot express.
--
-- 'failed' refunds are excluded from the committed total (the money never
-- moved). 'pending' refunds ARE counted -- money is earmarked, and letting a
-- second refund through while the first is in flight is exactly the
-- double-refund bug this trigger exists to stop.
-- ---------------------------------------------------------------------------

CREATE TRIGGER refunds_no_overrefund_insert
BEFORE INSERT ON refunds
FOR EACH ROW
WHEN NEW.status IN ('completed', 'pending')
     AND (
        NEW.amount_cents + (
            SELECT COALESCE(SUM(amount_cents), 0) FROM refunds
            WHERE order_id = NEW.order_id AND status IN ('completed', 'pending')
        )
     ) > (SELECT total_cents FROM orders WHERE order_id = NEW.order_id)
BEGIN
    SELECT RAISE(ABORT, 'refund would exceed order total');
END;

CREATE TRIGGER refunds_no_overrefund_update
BEFORE UPDATE ON refunds
FOR EACH ROW
WHEN NEW.status IN ('completed', 'pending')
     AND (
        NEW.amount_cents + (
            SELECT COALESCE(SUM(amount_cents), 0) FROM refunds
            WHERE order_id = NEW.order_id
              AND status IN ('completed', 'pending')
              AND refund_id != NEW.refund_id
        )
     ) > (SELECT total_cents FROM orders WHERE order_id = NEW.order_id)
BEGIN
    SELECT RAISE(ABORT, 'refund would exceed order total');
END;

CREATE TRIGGER refunds_no_refund_on_cancelled
BEFORE INSERT ON refunds
FOR EACH ROW
WHEN (SELECT status FROM orders WHERE order_id = NEW.order_id) = 'cancelled'
BEGIN
    SELECT RAISE(ABORT, 'cannot refund a cancelled order');
END;
