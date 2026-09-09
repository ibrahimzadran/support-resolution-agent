"""Exercise the policy engine against every fixture order and print the verdict.

This is the by-hand read of the underlying cases. The point is not that the
code runs -- it is that I can look at twenty rows and confirm each verdict is
the one I intended when I designed the fixture, BEFORE any of it is used to
grade an agent.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.db_tools import ToolContext, check_refund_eligibility, connect  # noqa: E402

con = connect(read_only=True)
ctx = ToolContext(ticket_id="policy-probe", con=con)
rows = con.execute(
    "SELECT o.order_id, c.email FROM orders o JOIN customers c USING (customer_id)"
    " WHERE o.order_id LIKE 'ORD-100%' OR o.order_id LIKE 'ORD-101%' ORDER BY o.order_id"
).fetchall()

hdr = f"{'order':<11}{'refundable':<12}{'max':>9}{'authority':<11}{'review':<8} why"
print(hdr); print("-" * 110)
for r in rows:
    a = check_refund_eligibility(ctx, r["email"], r["order_id"])
    why = (a["blockers"] + a["review_reasons"])
    first = why[0] if why else "clean: agent may issue"
    print(f"{r['order_id']:<11}{str(a['refundable']):<12}{a['max_refundable_amount']:>9}"
          f"{'  yes' if a['within_agent_authority'] else '  NO':<11}"
          f"{'  yes' if a['requires_human_review'] else '  no':<8} {first[:70]}")
    for extra in why[1:]:
        print(f"{'':<41}{'':<8} + {extra[:70]}")
