"""Guard against prose/code drift.

backend/config.py holds the numbers; docs/ holds the prose the agent reads via
the knowledge base. If someone edits the return window in one place and not the
other, the agent will confidently cite a policy the tools do not enforce -- and
every eval result becomes untrustworthy without any test failing. This test
fails loudly on that divergence.

The docs deliberately spell numbers as words ("thirty days", "more than three")
so retrieval is not trivially keyword-matched, so each rule is checked against
both spellings.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.config import (  # noqa: E402
    AGENT_REFUND_AUTHORITY_CENTS, DOCS_PATH, LOST_PACKAGE_DAYS_PAST_ESTIMATE,
    REFUND_HISTORY_REVIEW_THRESHOLD, RETURN_WINDOW_DAYS,
)

CORPUS = "\n".join(p.read_text() for p in sorted(DOCS_PATH.glob("*.md")))

CHECKS = [
    ("return window", RETURN_WINDOW_DAYS, [r"\b30 days\b", r"\bthirty-day\b", r"\bthirty days\b"]),
    ("agent authority", AGENT_REFUND_AUTHORITY_CENTS // 100,
     [r"\$50\.00 or less", r"\bfifty dollars\b"]),
    ("refund history threshold", REFUND_HISTORY_REVIEW_THRESHOLD,
     [r"more than\s+\*?\*?three", r"more than three"]),
    ("lost package threshold", LOST_PACKAGE_DAYS_PAST_ESTIMATE,
     [r"\bten days have elapsed\b", r"\btenth day\b"]),
]

failures = []
for name, value, patterns in CHECKS:
    hits = [p for p in patterns if re.search(p, CORPUS, re.IGNORECASE)]
    if not hits:
        failures.append(f"{name}: config says {value} but no matching prose found in docs/")
    else:
        print(f"  ok   {name}: config={value}, docs state it ({len(hits)} phrasing(s) matched)")

# The literal digits must NOT be the only way to find these rules, or the KB
# questions become keyword lookups rather than a real retrieval test.
if re.search(r"\b30-day return\b", CORPUS) and not re.search(r"thirty", CORPUS, re.I):
    failures.append("return window appears only as digits; docs should also spell it out")

for f in failures:
    print(f"  FAIL {f}")
print(f"\n{len(CHECKS) - len(failures)} passed, {len(failures)} failed")
sys.exit(1 if failures else 0)
