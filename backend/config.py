"""Shared configuration for the support agent backend.

TODAY is pinned deliberately. Refund eligibility depends on windows like
"delivered within the last 30 days". If seed data used real wall-clock dates,
a ticket that is inside the return window today would fall outside it in two
months, and the hand-written ground truth in evals/ would silently become
wrong without a single line of code changing. Everything -- seed data and
tool logic alike -- derives from this one constant.
"""

from datetime import date, timedelta
from pathlib import Path

# Pinned "current date" for the entire system. Do not replace with date.today().
TODAY = date(2026, 9, 9)

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "backend" / "support.db"
CHROMA_PATH = REPO_ROOT / "evals" / ".chroma"
DOCS_PATH = REPO_ROOT / "docs"

# --- Policy constants -------------------------------------------------------
# These are the machine-readable form of the prose in docs/. They are
# duplicated by necessity: the agent reads the prose, the tools enforce the
# numbers. Any change to one MUST be mirrored in the other -- see
# evals/test_policy_sync.py, which fails if the numbers drift apart.

RETURN_WINDOW_DAYS = 30
AGENT_REFUND_AUTHORITY_CENTS = 5000          # $50.00
REFUND_HISTORY_REVIEW_THRESHOLD = 3          # >3 refunds in 12 months -> human review
REFUND_HISTORY_WINDOW_DAYS = 365
LOST_PACKAGE_DAYS_PAST_ESTIMATE = 10
NON_RETURNABLE_CATEGORIES = ("gift_card", "final_sale")


def days_ago(n: int) -> str:
    """ISO date string for n days before the pinned TODAY."""
    return (TODAY - timedelta(days=n)).isoformat()


def days_between(earlier_iso: str, later_iso: str) -> int:
    return (date.fromisoformat(later_iso) - date.fromisoformat(earlier_iso)).days
