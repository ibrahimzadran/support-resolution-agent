"""Anthropic tool schemas + the dispatcher that runs them.

Tool descriptions are written to be prescriptive about WHEN to call each tool,
not just what it does — trigger conditions in the description are what actually
drive correct tool selection. They deliberately avoid pressure language
("CRITICAL", "you MUST"); current models follow the system prompt closely and
shouted tool descriptions cause over-triggering.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import db_tools  # noqa: E402

TOOLS = [
    {
        "name": "search_knowledge_base",
        "description": (
            "Search the company's published support policy documents (returns and refunds, "
            "shipping and delivery, warranty terms, agent authority and escalation) and return "
            "a cited answer. Call this whenever a customer asks what the policy is, or whenever "
            "you need to confirm a rule before acting — return windows, what can and cannot be "
            "returned, how long delivery takes, what the warranty covers, or when something must "
            "go to a human. Do not answer policy questions from general knowledge about how "
            "retailers usually work; this company's policy is the only authority."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "A specific, self-contained policy question. Prefer 'how long "
                                   "is the return window for delivered items' over 'returns'.",
                }
            },
            "required": ["question"],
        },
    },
    {
        "name": "look_up_order",
        "description": (
            "Look up an order belonging to the customer who wrote in. Given an order reference, "
            "returns its status, dates, total, tracking, line items, and any refunds already "
            "issued. Omit order_id to list the customer's recent orders instead — do that when "
            "the customer refers to 'my order' without quoting a reference, so you can see what "
            "they might mean before asking. If the reference belongs to a different account, "
            "this returns an error and no order details; the order is then not yours to discuss."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "requester_email": {
                    "type": "string",
                    "description": "Email address the ticket was sent from.",
                },
                "order_id": {
                    "type": "string",
                    "description": "Order reference such as ORD-10001. Omit to list recent orders.",
                },
            },
            "required": ["requester_email"],
        },
    },
    {
        "name": "check_refund_eligibility",
        "description": (
            "Assess whether a refund is owed on an order and whether you may issue it yourself. "
            "Returns the outstanding refundable balance, whether policy permits a refund at all "
            "(with reasons if not), and whether the case requires a human reviewer. Call this "
            "before issuing any refund and before telling a customer a refund is coming. It "
            "checks order status, the return window, non-returnable items, prior refunds, the "
            "account's refund history, and your authority limit. It does NOT read the customer's "
            "message, so it cannot know about fraud claims or ambiguity — those remain your "
            "judgment."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "requester_email": {"type": "string", "description": "Ticket sender's email."},
                "order_id": {"type": "string", "description": "Order reference to assess."},
                "requested_amount": {
                    "type": "number",
                    "description": "Dollar amount the customer asked for, if they named one. "
                                   "Omit to assess the full outstanding balance.",
                },
            },
            "required": ["requester_email", "order_id"],
        },
    },
    {
        "name": "issue_refund",
        "description": (
            "Issue a refund against an order and record it. Only call this once "
            "check_refund_eligibility has confirmed the order is refundable, the amount is within "
            "the outstanding balance, and the case does not require human review. The refund is "
            "recorded immediately and money moves; there is no undo. Requests outside your "
            "authority are refused and the attempt is logged."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "requester_email": {"type": "string", "description": "Ticket sender's email."},
                "order_id": {"type": "string", "description": "Order to refund."},
                "amount": {
                    "type": "number",
                    "description": "Dollar amount to refund, e.g. 34.50. Must not exceed the "
                                   "outstanding balance or your authority limit.",
                },
                "reason": {
                    "type": "string",
                    "description": "Short reason for the refund, recorded on the transaction.",
                },
            },
            "required": ["requester_email", "order_id", "amount", "reason"],
        },
    },
    {
        "name": "escalate_to_human",
        "description": (
            "Hand the ticket to a human reviewer. This ends your handling of the ticket. Use it "
            "when the matter falls outside your authority — an amount above your limit, any "
            "suggestion that a payment was not authorised by the account holder, a defect or "
            "warranty claim, a consignment past the presumed-lost threshold, an account flagged "
            "for frequent returns, a suspended or closed account, a request for a policy "
            "exception, or a customer asking to speak to a person."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "What the human reviewer needs to know: what the customer "
                                   "wants, what you found, and why this needs a person. "
                                   "Internal — the customer does not see this.",
                },
                "customer_message": {
                    "type": "string",
                    "description": "The reply the customer receives now. Write it to them "
                                   "directly: what you found, that a colleague is taking it on, "
                                   "and what happens next. Escalating does not excuse leaving "
                                   "them without an answer.",
                },
                "category": {
                    "type": "string",
                    "enum": ["fraud", "over_authority", "warranty", "lost_package",
                             "refund_history", "account_status", "policy_exception",
                             "identity", "billing", "other"],
                    "description": "Primary reason for escalation.",
                },
                "order_id": {"type": "string", "description": "Related order, if there is one."},
            },
            "required": ["reason", "category", "customer_message"],
        },
    },
    {
        "name": "close_ticket",
        "description": (
            "Close the ticket. This ends your handling of the ticket. Use outcome 'resolved' when "
            "nothing further is owed to the customer — you answered their question, gave them "
            "their order status, issued their refund, or explained why the policy does not allow "
            "what they asked for. Use outcome 'awaiting_customer' when you have asked them a "
            "question and need their reply before anything can happen."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "resolution": {
                    "type": "string",
                    "description": "The reply the customer receives. Write it to them directly.",
                },
                "outcome": {
                    "type": "string",
                    "enum": ["resolved", "awaiting_customer"],
                    "description": "'resolved' if nothing further is owed; 'awaiting_customer' if "
                                   "you asked a clarifying question.",
                },
            },
            "required": ["resolution", "outcome"],
        },
    },
]


def dispatch(ctx, name: str, args: dict) -> dict:
    """Execute one tool call. Unknown tools and bad arguments return errors, never raise.

    Every outcome is written to the action log, including failures. An earlier
    version returned the error to the model without logging it, which meant a
    tool that raised was invisible to the grader: a ticket that called
    search_knowledge_base four times and got rate-limited scored as never
    having called it at all, and -- far worse -- a FORBIDDEN tool call that
    raised would have vanished from the log entirely, making a violating agent
    look compliant. The log must record what was attempted, not only what
    succeeded.
    """
    try:
        if name == "search_knowledge_base":
            from tools import kb
            result = kb.answer(args["question"])
            ctx.log("search_knowledge_base", args, result)
            return result
        if name == "look_up_order":
            return db_tools.look_up_order(ctx, args["requester_email"], args.get("order_id"))
        if name == "check_refund_eligibility":
            return db_tools.check_refund_eligibility(
                ctx, args["requester_email"], args["order_id"], args.get("requested_amount"))
        if name == "issue_refund":
            return db_tools.issue_refund(
                ctx, args["requester_email"], args["order_id"], args["amount"], args["reason"])
        if name == "escalate_to_human":
            return db_tools.escalate_to_human(
                ctx, args["reason"], args["category"], args["customer_message"],
                args.get("order_id"))
        if name == "close_ticket":
            return db_tools.close_ticket(ctx, args["resolution"], args.get("outcome", "resolved"))
        result = {"error": "unknown_tool", "message": f"No tool named {name}."}
    except KeyError as e:
        result = {"error": "missing_argument",
                  "message": f"Required argument {e} was not provided."}
    except Exception as e:  # surfaced to the model as a tool error so it can recover
        result = {"error": "tool_failed", "message": f"{type(e).__name__}: {e}"}

    # Reached only on an error path; the success paths logged and returned above.
    ctx.log(name, args, result)
    return result
