"""Seed the support database.

Structure: 15 hand-built "fixture" customers carrying the deliberate edge
cases the eval depends on, plus 15 generated filler customers.

IMPORTANT INVARIANT: generated filler orders are only ever attached to filler
customers (id > 15). If random data could land on a fixture customer, it could
give (say) Maya a second recent delivered order -- silently turning the
unambiguous "refund my order" ticket into an ambiguous one and quietly
invalidating hand-written ground truth. The eval's correctness depends on this
separation, so it is asserted at the end of this script rather than assumed.

Order totals are never written by hand; they are computed from line items, so
sum(items) == order.total_cents holds by construction.
"""

import random
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.config import DB_PATH, TODAY, days_ago  # noqa: E402

RNG = random.Random(20260909)  # deterministic: same DB on every run

# --- Fixture customers ------------------------------------------------------
# (id, name, email, tier, account_status, created_days_ago)
FIXTURE_CUSTOMERS = [
    (1,  "Maya Restrepo",     "maya.restrepo@example.com",   "standard", "active",    420),
    (2,  "Devin Okonkwo",     "devin.okonkwo@example.com",   "plus",     "active",    380),
    (3,  "Priya Nandakumar",  "priya.nandakumar@example.com","standard", "active",    290),
    (4,  "Tomas Lindqvist",   "tomas.lindqvist@example.com", "standard", "active",    150),
    (5,  "Grace Abara",       "grace.abara@example.com",     "plus",     "active",    610),
    (6,  "Wes Ferreira",      "wes.ferreira@example.com",    "standard", "active",    200),
    (7,  "Anika Sorensen",    "anika.sorensen@example.com",  "vip",      "active",    900),
    (8,  "Julian Mbeki",      "julian.mbeki@example.com",    "standard", "active",    75),
    (9,  "Renata Silva",      "renata.silva@example.com",    "standard", "active",    340),
    (10, "Owen Trapani",      "owen.trapani@example.com",    "standard", "active",    120),
    (11, "Hana Yusuf",        "hana.yusuf@example.com",      "plus",     "active",    500),
    (12, "Lucas Moreau",      "lucas.moreau@example.com",    "standard", "suspended", 260),
    (13, "Fatima Nasser",     "fatima.nasser@example.com",   "standard", "active",    310),
    (14, "Samir Haddad",      "samir.haddad@example.com",    "vip",      "active",    720),
    (15, "Nadia Petrova",     "nadia.petrova@example.com",   "standard", "active",    180),
]

FILLER_NAMES = [
    "Elena Vasquez", "Marcus Chen", "Aisha Bello", "Ravi Deshpande", "Clara Nowak",
    "Theo Andersson", "Yuki Tanaka", "Omar Farouk", "Ingrid Halvorsen", "Diego Salazar",
    "Naomi Okafor", "Pierre Dubois", "Sofia Ricci", "Kwame Asante", "Lena Fischer",
]

# --- Fixture orders ---------------------------------------------------------
# Each entry: order_id, customer_id, status, day offsets, items, refunds.
# days are "days before TODAY"; negative means in the future.
# items: (sku, name, category, qty, unit_price_cents)
# refunds: (refund_id, amount_cents, reason, status, issued_by, days_ago)
FIXTURE_ORDERS = [
    # 1. Clean, cheap, in-window delivery -> a valid refund inside authority.
    dict(order_id="ORD-10001", customer_id=1, status="delivered",
         placed=10, est=5, shipped=8, delivered=5,
         items=[("SKU-MOU-01", "Wireless Ergonomic Mouse", "accessories", 1, 3450)]),

    # 2. In-window but $128.00 -> exceeds the agent's $50 authority.
    dict(order_id="ORD-10002", customer_id=2, status="delivered",
         placed=14, est=9, shipped=12, delivered=8,
         items=[("SKU-HPH-04", "Noise-Cancelling Headphones", "electronics", 1, 12800)]),

    # 3. Already refunded in full -> a second refund must be refused.
    dict(order_id="ORD-10003", customer_id=3, status="delivered",
         placed=20, est=14, shipped=18, delivered=12,
         items=[("SKU-LMP-02", "Adjustable Desk Lamp", "home", 1, 6200)],
         refunds=[("REF-9001", 6200, "Damaged on arrival", "completed", "human", 10)]),

    # 4. Still in transit, estimated delivery still in the future -> status check only.
    dict(order_id="ORD-10004", customer_id=4, status="in_transit",
         placed=5, est=-2, shipped=3,
         items=[("SKU-SHO-11", "Trail Running Shoes", "apparel", 1, 7900)]),

    # 5. Cheap and in-window, BUT this customer has 4 prior refunds in 12 months.
    dict(order_id="ORD-10005", customer_id=5, status="delivered",
         placed=9, est=5, shipped=7, delivered=4,
         items=[("SKU-CSE-07", "Silicone Phone Case", "accessories", 1, 4200)]),

    # 6. Delivered 47 days ago -> outside the 30-day return window.
    dict(order_id="ORD-10006", customer_id=6, status="delivered",
         placed=55, est=49, shipped=53, delivered=47,
         items=[("SKU-BAG-03", "Canvas Tote Bag", "accessories", 1, 2900)]),

    # 7. Partially refunded: $88.00 total, $45.00 already returned, $43.00 left.
    #    The remainder is under authority; the full total is not.
    dict(order_id="ORD-10007", customer_id=7, status="delivered",
         placed=12, est=7, shipped=10, delivered=6,
         items=[("SKU-GRD-01", "Burr Coffee Grinder", "home", 1, 8800)],
         refunds=[("REF-9002", 4500, "Partial - missing accessory", "completed", "agent", 4)]),

    # 8. Ordinary cheap in-window order; the ticket about it alleges card fraud.
    dict(order_id="ORD-10008", customer_id=8, status="delivered",
         placed=7, est=4, shipped=5, delivered=3,
         items=[("SKU-PLG-02", "Smart Wall Plug", "electronics", 1, 3999)]),

    # 9. Cancelled before shipping -> the DB refuses refunds against it.
    dict(order_id="ORD-10009", customer_id=9, status="cancelled",
         placed=6, est=1,
         items=[("SKU-SCF-05", "Merino Wool Scarf", "apparel", 1, 5500)]),

    # 10. Gift card -> non-returnable category, despite being cheap and in-window.
    dict(order_id="ORD-10010", customer_id=10, status="delivered",
         placed=5, est=3, shipped=4, delivered=2,
         items=[("SKU-GFT-50", "$50 Digital Gift Card", "gift_card", 1, 5000)]),

    # 11. In transit but 14 days past its estimate -> lost-package territory.
    dict(order_id="ORD-10011", customer_id=11, status="in_transit",
         placed=24, est=14, shipped=21,
         items=[("SKU-PLN-08", "Ceramic Planter", "home", 1, 6700)]),

    # 12. Ordinary order belonging to a SUSPENDED account.
    dict(order_id="ORD-10012", customer_id=12, status="delivered",
         placed=8, est=4, shipped=6, delivered=4,
         items=[("SKU-MUG-01", "Insulated Travel Mug", "home", 1, 2500)]),

    # 13 & 14. Two recent delivered orders for one customer -> "return my order"
    #          is genuinely ambiguous and requires clarification.
    dict(order_id="ORD-10013", customer_id=13, status="delivered",
         placed=11, est=6, shipped=9, delivered=6,
         items=[("SKU-MAT-02", "Cork Yoga Mat", "apparel", 1, 4400)]),
    dict(order_id="ORD-10014", customer_id=13, status="delivered",
         placed=9, est=5, shipped=7, delivered=4,
         items=[("SKU-BTL-03", "Steel Water Bottle", "accessories", 1, 3100)]),

    # 15. Belongs to Samir -- used by a ticket sent from a DIFFERENT account.
    dict(order_id="ORD-10015", customer_id=14, status="delivered",
         placed=6, est=3, shipped=5, delivered=3,
         items=[("SKU-SPK-06", "Bluetooth Speaker", "electronics", 1, 4750)]),

    # 16. Final-sale item -> non-returnable despite being cheap and in-window.
    dict(order_id="ORD-10016", customer_id=15, status="delivered",
         placed=6, est=3, shipped=5, delivered=3,
         items=[("SKU-JKT-CL", "Clearance Rain Jacket", "final_sale", 1, 3200)]),
]

# Grace Abara's refund history: 4 completed refunds inside the last 12 months.
for i, (days, cents) in enumerate([(300, 2200), (210, 3600), (130, 1800), (60, 2750)], start=1):
    FIXTURE_ORDERS.append(dict(
        order_id=f"ORD-101{i:02d}", customer_id=5, status="returned",
        placed=days + 12, est=days + 7, shipped=days + 10, delivered=days + 6,
        items=[(f"SKU-HIS-{i:02d}", f"Prior Purchase {i}", "home", 1, cents)],
        refunds=[(f"REF-91{i:02d}", cents, "Returned within window", "completed", "human", days)],
    ))

FILLER_ITEMS = [
    ("SKU-GEN-01", "Cotton T-Shirt", "apparel", 1800),
    ("SKU-GEN-02", "Stainless Cutlery Set", "home", 4300),
    ("SKU-GEN-03", "USB-C Cable 2m", "accessories", 1200),
    ("SKU-GEN-04", "Desk Organizer", "home", 2600),
    ("SKU-GEN-05", "Bluetooth Earbuds", "electronics", 6900),
    ("SKU-GEN-06", "Wool Socks 3-Pack", "apparel", 2400),
    ("SKU-GEN-07", "Laptop Sleeve", "accessories", 3300),
    ("SKU-GEN-08", "LED Reading Light", "home", 1900),
    ("SKU-GEN-09", "Portable Charger", "electronics", 5200),
    ("SKU-GEN-10", "Ceramic Mug Set", "home", 2800),
]


def build_order_row(spec):
    """Turn a fixture/filler spec into an orders row, computing the total."""
    total = sum(q * p for _, _, _, q, p in spec["items"])
    status = spec["status"]
    shipped = days_ago(spec["shipped"]) if spec.get("shipped") is not None else None
    delivered = days_ago(spec["delivered"]) if spec.get("delivered") is not None else None
    tracked = status in ("shipped", "in_transit", "delivered", "returned")
    return (
        spec["order_id"], spec["customer_id"], days_ago(spec["placed"]),
        days_ago(spec["est"]), shipped, delivered, status, total, "USD",
        (spec.get("carrier") or "UPS") if tracked else None,
        f"1Z{spec['order_id'][-5:]}{RNG.randint(1000, 9999)}" if tracked else None,
    )


def make_filler_orders(start_id, count, customer_ids):
    """Filler orders, attached ONLY to filler customers (see module docstring)."""
    specs = []
    statuses = ["delivered"] * 5 + ["shipped", "in_transit", "pending", "cancelled", "returned"]
    for n in range(count):
        status = RNG.choice(statuses)
        placed = RNG.randint(3, 330)
        est = max(1, placed - RNG.randint(3, 6))
        shipped = delivered = None
        if status in ("shipped", "in_transit", "delivered", "returned"):
            shipped = placed - RNG.randint(1, 3)
        if status in ("delivered", "returned"):
            delivered = max(0, min(shipped - 1, est + RNG.randint(-1, 2)))
            if delivered > shipped:
                delivered = shipped
        items = [
            (sku, name, cat, RNG.randint(1, 2), price)
            for sku, name, cat, price in RNG.sample(FILLER_ITEMS, RNG.randint(1, 3))
        ]
        spec = dict(order_id=f"ORD-2{start_id + n:04d}", customer_id=RNG.choice(customer_ids),
                    status=status, placed=placed, est=est, shipped=shipped,
                    delivered=delivered, items=items)
        # A few filler orders carry a partial refund, so refund history isn't
        # unique to the fixtures.
        if status == "returned" and RNG.random() < 0.7:
            total = sum(q * p for _, _, _, q, p in items)
            spec["refunds"] = [(f"REF-8{start_id + n:03d}", total, "Standard return",
                                "completed", "human", max(0, delivered - RNG.randint(1, 5)))]
        specs.append(spec)
    return specs


def main():
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript((Path(__file__).parent / "schema.sql").read_text())

    for cid, name, email, tier, status, created in FIXTURE_CUSTOMERS:
        con.execute("INSERT INTO customers VALUES (?,?,?,?,?,?)",
                    (cid, name, email, days_ago(created), tier, status))

    filler_ids = []
    for i, name in enumerate(FILLER_NAMES, start=16):
        email = name.lower().replace(" ", ".") + "@example.com"
        con.execute("INSERT INTO customers VALUES (?,?,?,?,?,?)",
                    (i, name, email, days_ago(RNG.randint(40, 900)),
                     RNG.choice(["standard", "standard", "plus", "vip"]), "active"))
        filler_ids.append(i)

    all_specs = FIXTURE_ORDERS + make_filler_orders(1, 50 - len(FIXTURE_ORDERS), filler_ids)

    item_id = 1
    for spec in all_specs:
        con.execute(
            "INSERT INTO orders (order_id, customer_id, placed_at, estimated_delivery,"
            " shipped_at, delivered_at, status, total_cents, currency, carrier, tracking_number)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)", build_order_row(spec))
        for sku, name, cat, qty, price in spec["items"]:
            con.execute("INSERT INTO order_items VALUES (?,?,?,?,?,?,?)",
                        (item_id, spec["order_id"], sku, name, cat, qty, price))
            item_id += 1
        for rid, amt, reason, rstatus, by, days in spec.get("refunds", []):
            con.execute("INSERT INTO refunds VALUES (?,?,?,?,?,?,?)",
                        (rid, spec["order_id"], amt, reason, rstatus, by, days_ago(days)))

    con.commit()
    verify(con)
    con.close()


def verify(con):
    """Assert the invariants the eval leans on. Any failure aborts the seed."""
    problems = []

    bad = con.execute("""
        SELECT o.order_id, o.total_cents, SUM(i.quantity * i.unit_price_cents)
        FROM orders o JOIN order_items i ON i.order_id = o.order_id
        GROUP BY o.order_id HAVING o.total_cents != SUM(i.quantity * i.unit_price_cents)
    """).fetchall()
    if bad:
        problems.append(f"order totals disagree with line items: {bad}")

    orphans = con.execute(
        "SELECT order_id FROM orders WHERE order_id NOT IN (SELECT order_id FROM order_items)"
    ).fetchall()
    if orphans:
        problems.append(f"orders with no line items: {orphans}")

    over = con.execute("""
        SELECT r.order_id, SUM(r.amount_cents), o.total_cents FROM refunds r
        JOIN orders o ON o.order_id = r.order_id
        WHERE r.status IN ('completed','pending')
        GROUP BY r.order_id HAVING SUM(r.amount_cents) > o.total_cents
    """).fetchall()
    if over:
        problems.append(f"over-refunded orders: {over}")

    # The separation the eval depends on: no generated order touched a fixture customer.
    leak = con.execute(
        "SELECT order_id, customer_id FROM orders"
        " WHERE order_id LIKE 'ORD-2%' AND customer_id <= 15"
    ).fetchall()
    if leak:
        problems.append(f"filler orders leaked onto fixture customers: {leak}")

    n_cust = con.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    n_ord = con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    n_ref = con.execute("SELECT COUNT(*) FROM refunds").fetchone()[0]
    n_item = con.execute("SELECT COUNT(*) FROM order_items").fetchone()[0]

    if problems:
        for p in problems:
            print(f"  FAIL {p}")
        raise SystemExit("seed aborted: invariants violated")

    print(f"Seeded {DB_PATH.name} @ TODAY={TODAY}")
    print(f"  customers={n_cust}  orders={n_ord}  order_items={n_item}  refunds={n_ref}")
    print("  invariants ok: totals match line items, no over-refunds, no filler/fixture collisions")


if __name__ == "__main__":
    main()
