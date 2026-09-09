"""Prove the schema's constraints actually fire, before any data is seeded.

A constraint that looks enforced but isn't is worse than no constraint: it
licenses every downstream layer to skip the check. Each test here asserts that
an *invalid* write is rejected -- and, just as importantly, that a valid write
of the same shape succeeds, so a test cannot pass because of an unrelated error.
"""

import sqlite3
import sys
from pathlib import Path

SCHEMA = (Path(__file__).parent / "schema.sql").read_text()

PASS, FAIL = [], []


def fresh():
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA)
    con.execute("PRAGMA foreign_keys = ON")
    con.execute(
        "INSERT INTO customers VALUES (1,'Test','t@example.com','2026-01-01','standard','active')"
    )
    return con


def order(con, oid="ORD-1", status="delivered", total=10000):
    ship = "2026-08-01" if status in ("shipped", "in_transit", "delivered", "returned") else None
    deliv = "2026-08-05" if status in ("delivered", "returned") else None
    track = "TRK1" if ship else None
    carrier = "UPS" if ship else None
    con.execute(
        "INSERT INTO orders (order_id, customer_id, placed_at, estimated_delivery, shipped_at,"
        " delivered_at, status, total_cents, carrier, tracking_number)"
        " VALUES (?,1,'2026-07-30','2026-08-04',?,?,?,?,?,?)",
        (oid, ship, deliv, status, total, carrier, track),
    )


def expect_reject(name, fn):
    con = fresh()
    try:
        fn(con)
        FAIL.append(f"{name}: write was ACCEPTED but should have been rejected")
    except sqlite3.IntegrityError as e:
        PASS.append(f"{name}: rejected -> {e}")
    except Exception as e:  # wrong error type means the test isn't testing what it claims
        FAIL.append(f"{name}: raised {type(e).__name__} ({e}), expected IntegrityError")
    finally:
        con.close()


def expect_accept(name, fn):
    con = fresh()
    try:
        fn(con)
        PASS.append(f"{name}: accepted (control)")
    except Exception as e:
        FAIL.append(f"{name}: control write REJECTED ({type(e).__name__}: {e})")
    finally:
        con.close()


# --- refund total invariant -------------------------------------------------
def over_refund(con):
    order(con, total=10000)
    con.execute("INSERT INTO refunds VALUES ('R1','ORD-1',6000,'x','completed','agent','2026-08-10')")
    con.execute("INSERT INTO refunds VALUES ('R2','ORD-1',5000,'x','completed','agent','2026-08-11')")


def exact_refund(con):
    order(con, total=10000)
    con.execute("INSERT INTO refunds VALUES ('R1','ORD-1',6000,'x','completed','agent','2026-08-10')")
    con.execute("INSERT INTO refunds VALUES ('R2','ORD-1',4000,'x','completed','agent','2026-08-11')")


def pending_counts(con):
    """A pending refund must block a second refund -- money is already earmarked."""
    order(con, total=10000)
    con.execute("INSERT INTO refunds VALUES ('R1','ORD-1',6000,'x','pending','agent','2026-08-10')")
    con.execute("INSERT INTO refunds VALUES ('R2','ORD-1',5000,'x','completed','agent','2026-08-11')")


def failed_does_not_count(con):
    """A failed refund must NOT block a retry -- that money never moved."""
    order(con, total=10000)
    con.execute("INSERT INTO refunds VALUES ('R1','ORD-1',9000,'x','failed','agent','2026-08-10')")
    con.execute("INSERT INTO refunds VALUES ('R2','ORD-1',9000,'x','completed','agent','2026-08-11')")


def update_over_refund(con):
    order(con, total=10000)
    con.execute("INSERT INTO refunds VALUES ('R1','ORD-1',4000,'x','completed','agent','2026-08-10')")
    con.execute("INSERT INTO refunds VALUES ('R2','ORD-1',4000,'x','completed','agent','2026-08-11')")
    con.execute("UPDATE refunds SET amount_cents = 7000 WHERE refund_id = 'R2'")


def update_self_allowed(con):
    """Raising a refund within the remaining headroom must still work."""
    order(con, total=10000)
    con.execute("INSERT INTO refunds VALUES ('R1','ORD-1',4000,'x','completed','agent','2026-08-10')")
    con.execute("UPDATE refunds SET amount_cents = 9000 WHERE refund_id = 'R1'")


def refund_cancelled(con):
    order(con, oid="ORD-C", status="cancelled")
    con.execute("INSERT INTO refunds VALUES ('R1','ORD-C',100,'x','completed','agent','2026-08-10')")


# --- order lifecycle invariants ---------------------------------------------
def delivered_without_date(con):
    con.execute(
        "INSERT INTO orders (order_id, customer_id, placed_at, estimated_delivery, shipped_at,"
        " delivered_at, status, total_cents, carrier, tracking_number)"
        " VALUES ('ORD-2',1,'2026-07-30','2026-08-04','2026-08-01',NULL,'delivered',100,'UPS','T')"
    )


def in_transit_with_delivery_date(con):
    con.execute(
        "INSERT INTO orders (order_id, customer_id, placed_at, estimated_delivery, shipped_at,"
        " delivered_at, status, total_cents, carrier, tracking_number)"
        " VALUES ('ORD-3',1,'2026-07-30','2026-08-04','2026-08-01','2026-08-05','in_transit',100,'UPS','T')"
    )


def shipped_without_tracking(con):
    con.execute(
        "INSERT INTO orders (order_id, customer_id, placed_at, estimated_delivery, shipped_at,"
        " delivered_at, status, total_cents, carrier, tracking_number)"
        " VALUES ('ORD-4',1,'2026-07-30','2026-08-04','2026-08-01',NULL,'shipped',100,NULL,NULL)"
    )


def pending_with_tracking(con):
    con.execute(
        "INSERT INTO orders (order_id, customer_id, placed_at, estimated_delivery, shipped_at,"
        " delivered_at, status, total_cents, carrier, tracking_number)"
        " VALUES ('ORD-5',1,'2026-07-30','2026-08-04',NULL,NULL,'pending',100,'UPS','T')"
    )


def delivered_before_shipped(con):
    con.execute(
        "INSERT INTO orders (order_id, customer_id, placed_at, estimated_delivery, shipped_at,"
        " delivered_at, status, total_cents, carrier, tracking_number)"
        " VALUES ('ORD-6',1,'2026-07-30','2026-08-04','2026-08-05','2026-08-01','delivered',100,'UPS','T')"
    )


def est_before_placed(con):
    con.execute(
        "INSERT INTO orders (order_id, customer_id, placed_at, estimated_delivery, status, total_cents)"
        " VALUES ('ORD-9',1,'2026-07-30','2026-07-01','pending',100)"
    )


def zero_total(con):
    order(con, total=0)


def bad_status(con):
    con.execute(
        "INSERT INTO orders (order_id, customer_id, placed_at, estimated_delivery, shipped_at,"
        " delivered_at, status, total_cents, carrier, tracking_number)"
        " VALUES ('ORD-7',1,'2026-07-30','2026-08-04',NULL,NULL,'lost',100,NULL,NULL)"
    )


def orphan_order(con):
    con.execute(
        "INSERT INTO orders (order_id, customer_id, placed_at, estimated_delivery, shipped_at,"
        " delivered_at, status, total_cents, carrier, tracking_number)"
        " VALUES ('ORD-8',999,'2026-07-30','2026-08-04',NULL,NULL,'pending',100,NULL,NULL)"
    )


def duplicate_email(con):
    con.execute(
        "INSERT INTO customers VALUES (2,'Other','t@example.com','2026-01-01','standard','active')"
    )


def negative_refund(con):
    order(con)
    con.execute("INSERT INTO refunds VALUES ('R1','ORD-1',-500,'x','completed','agent','2026-08-10')")


def zero_quantity(con):
    order(con)
    con.execute("INSERT INTO order_items VALUES (1,'ORD-1','S','N','home',0,100)")


expect_reject("refund sum exceeds order total", over_refund)
expect_accept("refund sum exactly equals total", exact_refund)
expect_reject("pending refund counts toward total", pending_counts)
expect_accept("failed refund does not count toward total", failed_does_not_count)
expect_reject("UPDATE pushes refund sum over total", update_over_refund)
expect_accept("UPDATE within headroom (excludes own old row)", update_self_allowed)
expect_reject("refund against cancelled order", refund_cancelled)
expect_reject("delivered order with no delivered_at", delivered_without_date)
expect_reject("in_transit order with a delivered_at", in_transit_with_delivery_date)
expect_reject("shipped order with no tracking", shipped_without_tracking)
expect_reject("pending order with tracking", pending_with_tracking)
expect_reject("delivered_at before shipped_at", delivered_before_shipped)
expect_reject("estimated_delivery before placed_at", est_before_placed)
expect_reject("order total of zero", zero_total)
expect_reject("invalid order status", bad_status)
expect_reject("order for nonexistent customer", orphan_order)
expect_reject("duplicate customer email", duplicate_email)
expect_reject("negative refund amount", negative_refund)
expect_reject("order item with zero quantity", zero_quantity)

for line in PASS:
    print(f"  ok   {line}")
for line in FAIL:
    print(f"  FAIL {line}")
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
