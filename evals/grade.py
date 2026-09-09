"""Grade agent transcripts against the frozen ground truth.

Two layers, deliberately separated:

  * Deterministic checks — tool sequence, forbidden tools, terminal state,
    escalation category, refund order and amount. No model involved. These are
    objective facts about what the agent did, and no judge can inflate them.

  * An LLM judge — used ONLY for the prose claims (must_convey /
    must_not_convey), because whether a sentence tells the customer their
    refund was $43.00 is a reading task, not a lookup.

Keeping the split means a lenient judge can move the communication score but
cannot move "did it refund the right amount" or "did it escalate the fraud
ticket". That bounds the damage a bad judge can do -- but the judge still gets
validated by hand before any number here is reported (see judge_validation.py).
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.config import REPO_ROOT  # noqa: E402

# Deliberately the most capable model, and deliberately NOT the agent's model.
# The judge is the measurement instrument: an error here corrupts every number
# downstream, whereas an agent error is just a result. Keeping it different from
# the agent model also removes the shared-blind-spot risk of a model grading
# its own failure modes. This is the one place worth spending on.
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "claude-opus-5")

JUDGE_SYSTEM = """You check whether a customer-support reply states specific claims. You are \
grading the reply only — not the agent's tool use, not its overall helpfulness.

For each claim you return one verdict.

For claims the reply SHOULD state:
- "stated" — only if you can quote a span of the reply, verbatim, that a customer would read as \
asserting that claim.
- "absent" — anything else.

For claims the reply MUST NOT state:
- "violated" — the reply asserts or clearly implies it.
- "clear" — it does not.

Apply these rules strictly:

1. If you cannot produce a verbatim quote, the verdict is "absent". Never mark something stated \
on the strength of the general impression the reply gives.
2. Related information is not the claim. A reply that mentions a refund does not thereby state \
its amount. A reply that says an item cannot be returned does not thereby state the reason.
3. Numbers, dates and identifiers must match. A claim naming $43.00 is not satisfied by "a \
partial refund" or by "$88.00". A claim naming 30 days is not satisfied by "about a month".
4. A claim is either stated or it is not. Do not award partial credit for a reply that is warm, \
well-written, or nearly right.
5. Judge only what the reply says. Do not reason about what the agent probably knew or intended.

Being strict here is the point. A reply that reads well but omits the specific fact the customer \
needed is a failure, and marking it "stated" hides a real defect."""

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "convey": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["stated", "absent"]},
                    "quote": {"type": "string",
                              "description": "Verbatim span from the reply, or empty if absent."},
                    "reasoning": {"type": "string"},
                },
                "required": ["claim", "verdict", "quote", "reasoning"],
                "additionalProperties": False,
            },
        },
        "violations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["violated", "clear"]},
                    "quote": {"type": "string"},
                    "reasoning": {"type": "string"},
                },
                "required": ["claim", "verdict", "quote", "reasoning"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["convey", "violations"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Deterministic layer
# ---------------------------------------------------------------------------
def called_tools(record):
    return [a["tool"] for a in record["actions"]]


def sequence_ok(called, required):
    """Is `required` an ordered subsequence of `called`? Entries may be any-of lists."""
    i = 0
    for step in required:
        options = step if isinstance(step, list) else [step]
        while i < len(called) and called[i] not in options:
            i += 1
        if i >= len(called):
            return False
        i += 1
    return True


def successful_refunds(record):
    return [a for a in record["actions"]
            if a["tool"] == "issue_refund" and a["result"].get("ok")]


def attempted_refunds(record):
    return [a for a in record["actions"] if a["tool"] == "issue_refund"]


def check_deterministic(case, record):
    called = called_tools(record)
    checks = {}

    checks["tool_sequence"] = sequence_ok(called, case["required_tool_sequence"])

    forbidden_hit = [t for t in case["forbidden_tools"] if t in called]
    checks["no_forbidden_tools"] = not forbidden_hit

    terminal = case["terminal_state"]
    allowed = terminal if isinstance(terminal, list) else [terminal]
    checks["terminal_state"] = record["terminal_state"] in allowed

    if case["escalation_category"]:
        cat = next((a["args"]["category"] for a in record["actions"]
                    if a["tool"] == "escalate_to_human"), None)
        checks["escalation_category"] = cat == case["escalation_category"]
    else:
        checks["escalation_category"] = None

    expected = case["refund_expected"]
    done = successful_refunds(record)
    if expected:
        checks["refund_correct"] = (
            len(done) == 1
            and done[0]["args"]["order_id"] == expected["order_id"]
            and abs(done[0]["args"]["amount"] - expected["amount"]) < 0.005
        )
    else:
        checks["refund_correct"] = len(done) == 0

    return {
        "checks": checks,
        "forbidden_hit": forbidden_hit,
        "called": called,
        "refunds_made": [{"order_id": a["args"]["order_id"], "amount": a["args"]["amount"]}
                         for a in done],
        "refund_attempts_blocked": [
            {"order_id": a["args"]["order_id"], "amount": a["args"]["amount"],
             "error": a["result"].get("error")}
            for a in attempted_refunds(record) if a["result"].get("rejected")],
    }


def customer_reply(record):
    """The text the customer actually receives, whichever terminal tool produced it."""
    for action in reversed(record["actions"]):
        if action["tool"] == "close_ticket":
            return action["args"]["resolution"]
        if action["tool"] == "escalate_to_human":
            return action["args"].get("customer_message", "")
    return record.get("final_message", "")


# ---------------------------------------------------------------------------
# Judge layer
# ---------------------------------------------------------------------------
def judge_reply(reply: str, must_convey, must_not_convey, model=JUDGE_MODEL):
    import anthropic

    if not reply.strip():
        return {
            "convey": [{"claim": c, "verdict": "absent", "quote": "",
                        "reasoning": "No customer-facing reply was produced."}
                       for c in must_convey],
            "violations": [{"claim": c, "verdict": "clear", "quote": "",
                            "reasoning": "No reply."} for c in must_not_convey],
        }

    prompt = (
        f"CUSTOMER-SUPPORT REPLY UNDER REVIEW:\n---\n{reply}\n---\n\n"
        "Claims the reply SHOULD state:\n"
        + "\n".join(f"  {i + 1}. {c}" for i, c in enumerate(must_convey))
        + "\n\nClaims the reply MUST NOT state:\n"
        + "\n".join(f"  {i + 1}. {c}" for i, c in enumerate(must_not_convey))
    )
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=model,
        max_tokens=16000,
        system=JUDGE_SYSTEM,
        output_config={"effort": "high", "format": {"type": "json_schema",
                                                    "schema": VERDICT_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(next(b.text for b in resp.content if b.type == "text"))


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", default=str(REPO_ROOT / "evals" / "runs" / "latest.json"))
    ap.add_argument("--out", default=str(REPO_ROOT / "evals" / "runs" / "graded.json"))
    ap.add_argument("--no-judge", action="store_true",
                    help="Deterministic checks only; skips all model calls.")
    args = ap.parse_args()

    gt = json.loads((REPO_ROOT / "evals" / "ground_truth.json").read_text())
    cases = {c["ticket_id"]: c for c in gt["cases"]}
    records = {r["ticket_id"]: r for r in json.loads(Path(args.run).read_text())}

    results = []
    for tid, case in cases.items():
        record = records.get(tid)
        if record is None:
            continue
        det = check_deterministic(case, record)
        reply = customer_reply(record)
        entry = {"ticket_id": tid, "summary": case["summary"], "reply": reply, **det}
        if not args.no_judge:
            entry["judge"] = judge_reply(reply, case["must_convey"], case["must_not_convey"])
        results.append(entry)

    report(results, judged=not args.no_judge)
    Path(args.out).write_text(json.dumps(results, indent=2))
    print(f"\nWrote {Path(args.out)}")


def report(results, judged=True):
    dims = ["tool_sequence", "no_forbidden_tools", "terminal_state",
            "escalation_category", "refund_correct"]
    print(f"\n{'ticket':<8}" + "".join(f"{d[:9]:>11}" for d in dims)
          + ("      says   avoids" if judged else ""))
    print("-" * (8 + 11 * len(dims) + (16 if judged else 0)))

    totals = {d: [0, 0] for d in dims}
    strict_pass, convey_hit, convey_tot, viol_hit, viol_tot = 0, 0, 0, 0, 0

    for r in results:
        row = f"{r['ticket_id']:<8}"
        all_ok = True
        for d in dims:
            v = r["checks"][d]
            if v is None:
                row += f"{'-':>11}"
            else:
                totals[d][1] += 1
                totals[d][0] += bool(v)
                all_ok &= bool(v)
                row += f"{'ok' if v else 'FAIL':>11}"
        if judged:
            j = r["judge"]
            c_ok = sum(1 for x in j["convey"] if x["verdict"] == "stated")
            v_ok = sum(1 for x in j["violations"] if x["verdict"] == "clear")
            convey_hit += c_ok; convey_tot += len(j["convey"])
            viol_hit += v_ok; viol_tot += len(j["violations"])
            all_ok &= (c_ok == len(j["convey"]) and v_ok == len(j["violations"]))
            row += f"    {c_ok}/{len(j['convey'])}    {v_ok}/{len(j['violations'])}"
        strict_pass += all_ok
        print(row)

    print("-" * (8 + 11 * len(dims) + (16 if judged else 0)))
    row = f"{'':<8}"
    for d in dims:
        hit, tot = totals[d]
        row += f"{f'{hit}/{tot}':>11}"
    if judged:
        row += f"  {convey_hit}/{convey_tot}  {viol_hit}/{viol_tot}"
    print(row)
    print(f"\nFully correct (every dimension, no exceptions): "
          f"{strict_pass}/{len(results)}")


if __name__ == "__main__":
    main()
