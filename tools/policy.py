"""Pure policy logic: no I/O, no LLM, fully unit-testable.

This is the machine-readable counterpart to docs/. The agent reads the prose;
these functions enforce the numbers. Keeping the logic here (rather than inside
the tool wrappers) means the eval can test policy decisions directly, without
an API key and without the agent in the loop.

Vocabulary distinction that matters throughout:
  * `refundable`        -- policy says money can be returned for this order
  * `within_authority`  -- a front-line agent may issue it without a human
These are independent. A $128 in-window order is refundable but not within
authority; a gift card is within authority's dollar limit but not refundable.
Collapsing them into one boolean is how an agent ends up either refusing valid
claims or quietly exceeding its limit.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.config import (  # noqa: E402
    AGENT_REFUND_AUTHORITY_CENTS, LOST_PACKAGE_DAYS_PAST_ESTIMATE,
    NON_RETURNABLE_CATEGORIES, REFUND_HISTORY_REVIEW_THRESHOLD,
    RETURN_WINDOW_DAYS, TODAY, days_between,
)


def assess_refund(order, items, refunded_cents, customer, recent_refund_count,
                  requested_amount_cents=None):
    """Return a structured refund assessment for one order.

    `order`, `items`, `customer` are plain dicts from the database layer.
    Returns a dict the agent can act on. Deliberately does NOT consider the
    ticket text -- fraud allegations and ambiguity live in the customer's
    words, not in the database, and are the agent's judgment to make.
    """
    today = TODAY.isoformat()
    blockers = []        # reasons no refund is owed at all
    review_reasons = []  # reasons a human, not the agent, must decide

    total = order["total_cents"]
    outstanding = total - refunded_cents

    # --- Is any money owed at all? -----------------------------------------
    if order["status"] == "cancelled":
        blockers.append(
            "Order was cancelled before dispatch, so no payment was captured to return.")
    elif order["status"] in ("pending", "shipped", "in_transit"):
        days_late = days_between(order["estimated_delivery"], today)
        if order["status"] != "pending" and days_late > LOST_PACKAGE_DAYS_PAST_ESTIMATE:
            review_reasons.append(
                f"Consignment is {days_late} days past its estimated arrival date "
                f"(threshold {LOST_PACKAGE_DAYS_PAST_ESTIMATE}), so it is presumed lost. "
                "Presumed-loss cases are decided by a human reviewer.")
        elif days_late > 0:
            blockers.append(
                f"Order has not arrived yet and is {days_late} day(s) past its estimate, "
                f"but is not presumed lost until {LOST_PACKAGE_DAYS_PAST_ESTIMATE} days past.")
        else:
            blockers.append(
                f"Order has not been delivered yet (status '{order['status']}', "
                f"estimated arrival {order['estimated_delivery']}).")
    else:  # delivered or returned
        age = days_between(order["delivered_at"], today)
        if age > RETURN_WINDOW_DAYS:
            blockers.append(
                f"Delivered {age} days ago, outside the {RETURN_WINDOW_DAYS}-day return window.")

    if outstanding <= 0:
        blockers.append(
            f"Order has already been refunded in full (${refunded_cents / 100:.2f} "
            f"of ${total / 100:.2f}). No balance remains.")

    # --- Non-returnable merchandise ----------------------------------------
    nr_cents = sum(i["quantity"] * i["unit_price_cents"]
                   for i in items if i["category"] in NON_RETURNABLE_CATEGORIES)
    if nr_cents and nr_cents == total:
        cats = sorted({i["category"] for i in items if i["category"] in NON_RETURNABLE_CATEGORIES})
        blockers.append(
            f"Every item on this order is in a non-returnable category ({', '.join(cats)}).")
    elif nr_cents:
        review_reasons.append(
            f"Order mixes returnable and non-returnable items "
            f"(${nr_cents / 100:.2f} non-returnable); a human must apportion the credit.")

    # --- Account-level controls --------------------------------------------
    if customer["account_status"] != "active":
        review_reasons.append(
            f"Account is {customer['account_status']}; no credit may be issued and no "
            "details released without human review.")

    if recent_refund_count > REFUND_HISTORY_REVIEW_THRESHOLD:
        review_reasons.append(
            f"Account has received {recent_refund_count} refunds in the last 12 months "
            f"(review threshold is more than {REFUND_HISTORY_REVIEW_THRESHOLD}). "
            "This anti-abuse control applies regardless of the amount.")

    refundable = not blockers
    max_refundable = max(0, outstanding) if refundable else 0

    # --- Authority: applied to the amount actually being credited ----------
    amount = requested_amount_cents if requested_amount_cents is not None else max_refundable
    over_authority = refundable and amount > AGENT_REFUND_AUTHORITY_CENTS
    if over_authority:
        review_reasons.append(
            f"Amount ${amount / 100:.2f} exceeds the agent authority limit of "
            f"${AGENT_REFUND_AUTHORITY_CENTS / 100:.2f}.")

    return {
        "order_id": order["order_id"],
        "order_status": order["status"],
        "order_total": f"${total / 100:.2f}",
        "already_refunded": f"${refunded_cents / 100:.2f}",
        "outstanding_balance": f"${max(0, outstanding) / 100:.2f}",
        "refundable": refundable,
        "max_refundable_amount": f"${max_refundable / 100:.2f}",
        "max_refundable_cents": max_refundable,
        "within_agent_authority": refundable and not review_reasons,
        "requires_human_review": bool(review_reasons),
        "blockers": blockers,
        "review_reasons": review_reasons,
        "agent_authority_limit": f"${AGENT_REFUND_AUTHORITY_CENTS / 100:.2f}",
    }
