"""Support resolution agent: a multi-step tool-use loop over Claude.

This is a hand-written loop rather than the SDK's tool runner. The runner would
be the right production choice, but the whole point of this project is to see
the request -> tool_use -> execute -> tool_result -> request cycle, and hiding
it behind a helper defeats that.

Three details that matter and are easy to get wrong:

  1. The FULL `response.content` is appended to history, not just the text.
     Dropping the tool_use blocks breaks the tool_use_id pairing on the next
     request and the API rejects it.
  2. ALL tool results for one assistant turn go back in a SINGLE user message.
     Splitting them across messages trains the model out of parallel tool calls.
  3. The loop stops on a terminal tool (close_ticket / escalate_to_human), not
     only on end_turn. A ticket ends in exactly one state; without this, a
     model that closes a ticket and then keeps talking could close it twice.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.config import AGENT_REFUND_AUTHORITY_CENTS, REPO_ROOT, TODAY  # noqa: E402
from tools.db_tools import ToolContext, connect  # noqa: E402
from tools.definitions import TOOLS, dispatch  # noqa: E402

MODEL = os.environ.get("AGENT_MODEL", "claude-sonnet-5")
MAX_ITERATIONS = 12

SYSTEM_PROMPT = f"""You are a front-line support agent for an online retailer. You read one \
customer ticket at a time, use your tools to find out what is actually true, and then either \
settle the matter yourself or hand it to a human colleague.

Today's date is {TODAY.isoformat()}. Use it for any question about how long ago something \
happened; do not guess dates.

# Your authority

You may authorise a refund of ${AGENT_REFUND_AUTHORITY_CENTS / 100:.2f} or less on a single \
order. The test applies to the amount you are actually crediting, not what the order originally \
cost: an expensive order that has already been partly refunded may leave a balance inside your \
limit. Never split one claim into smaller refunds to stay under the limit.

Regardless of the amount, these go to a human colleague:

- Any suggestion that a payment was not authorised by the account holder — a charge they do not \
recognise, a card used without their knowledge, an account they think has been taken over. \
Escalate immediately. Do not first check whether the claim looks plausible, do not issue any \
credit, and do not close the ticket.
- Any claim that a product is faulty or has broken, which is a warranty matter.
- Any parcel that has passed the point where it is treated as missing.
- Accounts flagged for frequent refunds.
- Accounts that are suspended or closed.
- Requests to make an exception to a published policy.
- Anyone who asks to speak to a person.

You may discuss an order only with the account that placed it. If an order is not associated \
with the account that wrote in, say so and share nothing about it — not its status, not its \
contents, not whether it exists.

# How to work

Find out before you act. The eligibility check is what tells you the outstanding balance, \
whether the return window has passed, whether the items can be returned at all, and whether \
something about the account requires review. An order can look perfectly refundable and still \
be blocked by something you can only see by checking.

When a request could mean more than one order, ask which one. Do not guess and act on the guess.

For questions about what the policy is, search the knowledge base rather than answering from \
what retailers usually do. Our policy is the only authority, and a confident wrong answer about \
a return window is worse than looking it up.

Every ticket ends in exactly one state: you settle it with close_ticket, or you hand it over \
with escalate_to_human. Never both.

# Writing to the customer

The resolution text you pass to close_ticket is what the customer reads, so write it to them \
directly, warmly and plainly. Lead with the outcome — what has happened or what you found — then \
any detail they need. Give concrete figures and dates rather than vague reassurance. Keep it to \
a short paragraph or two; do not pad it with apologies, restatements of their own message, or \
caveats about things that did not happen.

Deliver what was asked at the scope intended. If you cannot do what the customer wants, say so \
plainly and explain why, rather than implying something is coming that is not."""


def run_ticket(ticket: dict, verbose: bool = True) -> dict:
    """Run the agent loop for one ticket. Returns a transcript record."""
    import anthropic

    client = anthropic.Anthropic()
    ctx = ToolContext(ticket_id=ticket["ticket_id"], con=connect())

    user_text = (
        f"New support ticket {ticket['ticket_id']}.\n"
        f"From: {ticket['from_email']}\n"
        f"Subject: {ticket['subject']}\n\n"
        f"{ticket['body']}"
    )
    messages = [{"role": "user", "content": user_text}]

    started = time.time()
    stop_reason = None
    usage = {"input_tokens": 0, "output_tokens": 0}
    iterations = 0
    error = None

    try:
        while iterations < MAX_ITERATIONS:
            iterations += 1
            response = client.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                output_config={"effort": "high"},
                messages=messages,
            )
            usage["input_tokens"] += response.usage.input_tokens
            usage["output_tokens"] += response.usage.output_tokens
            stop_reason = response.stop_reason

            # Claude Opus 5 can decline a request outright; content is then empty
            # or partial, so this must be checked before reading content.
            if stop_reason == "refusal":
                error = "model_refusal"
                break

            messages.append({"role": "assistant", "content": response.content})

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if verbose:
                for block in response.content:
                    if block.type == "text" and block.text.strip():
                        print(f"    · {block.text.strip()[:100]}", flush=True)
                for tu in tool_uses:
                    print(f"    → {tu.name}({json.dumps(tu.input)[:90]})", flush=True)

            if not tool_uses:
                break  # end_turn with no tool call: the model is done talking

            # All results for one assistant turn go back in ONE user message.
            results = []
            for tu in tool_uses:
                result = dispatch(ctx, tu.name, tu.input)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": json.dumps(result, default=str),
                    "is_error": bool(result.get("error")),
                })
            messages.append({"role": "user", "content": results})

            if ctx.terminal_state:
                break
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        if verbose:
            print(f"    !! {error}", flush=True)

    ctx.con.close()
    return {
        "ticket_id": ticket["ticket_id"],
        "terminal_state": ctx.terminal_state,
        "actions": ctx.actions,
        "iterations": iterations,
        "stop_reason": stop_reason,
        "usage": usage,
        "elapsed_s": round(time.time() - started, 1),
        "error": error,
        "final_message": _final_text(messages),
    }


def _final_text(messages):
    """The last thing the model said in prose (may be empty if it ended on a tool call)."""
    for msg in reversed(messages):
        if msg["role"] == "assistant" and not isinstance(msg["content"], str):
            text = "".join(b.text for b in msg["content"] if getattr(b, "type", None) == "text")
            if text.strip():
                return text.strip()
    return ""


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ticket", help="Run a single ticket by id, e.g. T02")
    ap.add_argument("--out", default=str(REPO_ROOT / "evals" / "runs" / "latest.json"))
    ap.add_argument("--no-reseed", action="store_true",
                    help="Skip the database reset (only safe for a single read-only ticket)")
    args = ap.parse_args()

    tickets = json.loads((REPO_ROOT / "tickets" / "tickets.json").read_text())
    if args.ticket:
        tickets = [t for t in tickets if t["ticket_id"] == args.ticket]
        if not tickets:
            raise SystemExit(f"No ticket {args.ticket}")

    # Refunds mutate the database, so a second run would find ORD-10001 already
    # refunded and score differently for reasons that have nothing to do with
    # the agent. Reset to the known fixture state before every run.
    if not args.no_reseed:
        from backend import seed
        seed.main()
        print()

    records = []
    for ticket in tickets:
        print(f"  {ticket['ticket_id']}  {ticket['subject']}", flush=True)
        rec = run_ticket(ticket)
        state = rec["terminal_state"] or "NO TERMINAL STATE"
        print(f"    ⇒ {state}  ({rec['iterations']} turns, {rec['elapsed_s']}s)\n", flush=True)
        records.append(rec)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, indent=2, default=str))
    print(f"Wrote {len(records)} transcripts to {out}")


if __name__ == "__main__":
    main()
