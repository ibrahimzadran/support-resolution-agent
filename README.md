# Support Resolution Agent

An agentic support-ticket resolution system: a multi-step tool-use loop over Claude that
reads a customer ticket, investigates with tools, and either settles the matter or hands it
to a human. Built to learn tool use and agent loops, with the evaluation treated as the
hard part rather than an afterthought.

```
tickets/tickets.json ──► agent.py ──► tools/ ──► backend/support.db  (orders, refunds)
                            │                └─► tools/kb.py ──► Chroma  (policy docs)
                            ▼
                   evals/runs/latest.json ──► evals/grade.py ──► score
                                                   ▲
                              evals/ground_truth.json (frozen, hand-written)
```

## Layout

| Path | What it is |
|---|---|
| `backend/schema.sql` | SQLite schema. Refund-total invariant enforced by trigger. |
| `backend/config.py` | Pinned `TODAY`, policy constants, `.env` loading. |
| `backend/seed.py` | 30 customers, 50 orders, deliberate edge-case fixtures. |
| `docs/*.md` | Four policy documents — the knowledge-base corpus. |
| `tools/policy.py` | Pure eligibility logic. No I/O, no LLM, unit-testable. |
| `tools/db_tools.py` | The five database-backed tools. |
| `tools/kb.py` | RAG: chunk → Voyage embed → Chroma → cited answer. |
| `tools/definitions.py` | Anthropic tool schemas + dispatcher. |
| `agent.py` | The tool-use loop. |
| `tickets/tickets.json` | 15 synthetic tickets. |
| `evals/ground_truth.json` | **Frozen.** Correct resolution for each ticket. |
| `evals/grade.py` | Deterministic checks + LLM judge. |
| `evals/judge_validation.py` | Hand-labelled probes that validate the judge. |

## Running it

```bash
python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # add ANTHROPIC_API_KEY and VOYAGE_API_KEY

.venv/bin/python tools/kb.py ingest          # build the knowledge base (once)
.venv/bin/python evals/judge_validation.py   # validate the judge FIRST
.venv/bin/python agent.py                    # run all 15 tickets
.venv/bin/python evals/grade.py              # score against ground truth
```

Offline checks (no API key needed):

```bash
.venv/bin/python backend/test_constraints.py   # 19 schema constraint tests
.venv/bin/python evals/test_policy_sync.py     # docs/ vs config.py drift guard
.venv/bin/python evals/test_policy_cases.py    # eligibility verdict for every fixture
.venv/bin/python tools/kb.py chunks            # inspect chunking without embedding
```

## Decisions that exist to protect the evaluation

**Ground truth was written before the agent existed.** `evals/ground_truth.json` was
committed in `d44b8b4`; `agent.py` first appears in `cd5e826`. The ordering is checkable in
git history, not just asserted. The file is frozen — if a case turns out to be wrong on its
own terms it gets fixed loudly in the run notes, never edited quietly to raise a score.

**Dates are pinned, not `now()`.** Refund eligibility depends on windows like "delivered
within 30 days". With wall-clock dates, a ticket inside the window today falls outside it in
two months and the hand-written ground truth silently becomes wrong with no code change.
Everything derives from `backend.config.TODAY`.

**Money is integer cents.** The authority limit is a comparison against $50. In floats an
order can land on `50.000000000001` and flip an eligibility branch nondeterministically.

**The database is re-seeded before every run.** Refunds mutate state. Without a reset, a
second run finds `ORD-10001` already refunded and scores differently for reasons that have
nothing to do with the agent.

**Filler data can never touch fixture customers.** Generated orders attach only to customers
with `id > 15`. A random second delivered order for Fatima would turn the unambiguous refund
ticket into an ambiguous one and quietly invalidate hand-written ground truth. `seed.py`
asserts this rather than assuming it.

**Tickets avoid the policy documents' vocabulary.** They are written in customer register
("if I change my mind after it turns up") rather than policy register ("return window"), so
retrieval cannot succeed through keyword overlap between question and source passage.

**Grading separates deterministic checks from the judge.** Tool sequence, terminal state,
escalation category and refund amounts are checked mechanically. The LLM judge only ever
sees the prose claims. A lenient judge can move the communication score but cannot move
"did it refund the right amount" — that bounds the damage a bad judge can do.

**The judge is validated against hand-labelled probes before any score is reported.**
`evals/judge_validation.py` holds 8 replies with 35 verdicts assigned by hand, including a
reply that is warm, fluent and topically adjacent to the claim without ever asserting it —
the failure mode a lenient judge falls for. It reports false-lenient and false-strict rates
separately and exits non-zero on any leniency, because leniency inflates the baseline and
therefore shrinks the apparent benefit of every later improvement.

**Blocked tool calls are still recorded.** `issue_refund` hard-refuses anything outside
authority, but logs the attempt with `rejected: true`. Safety and observability both hold,
and the eval can distinguish "did the right thing" from "was stopped from doing the wrong
thing".

## The 15 tickets

| | Case | Correct outcome |
|---|---|---|
| T01 | In-transit order, not yet late | Report status, resolve |
| T02 | Clean in-window refund, $34.50 | Refund, resolve |
| T03 | In-window return, $128.00 | Escalate — over authority |
| T04 | Unauthorised-charge claim | Escalate immediately — fraud |
| T05 | Already refunded in full | Explain, resolve — no second refund |
| T06 | Policy question, no order | Knowledge base, resolve |
| T07 | "Return my order", two candidates | Ask which — awaiting customer |
| T08 | Delivered 47 days ago | Explain window, resolve |
| T09 | Partly refunded: $88 total, $45 paid | Refund the $43.00 remainder |
| T10 | $42, in window, 4 prior refunds | Escalate — refund history |
| T11 | 14 days past estimated arrival | Escalate — presumed lost |
| T12 | $50 gift card | Explain non-returnable, resolve |
| T13 | Order belongs to another account | Disclose nothing, resolve |
| T14 | Order reference does not exist | Ask them to check, awaiting customer |
| T15 | Zip failed, outside return window | Escalate — warranty |

T09 and T10 are the deliberate traps. T09 punishes an agent that reads "send the whole thing
back" as "refund the order total". T10 looks approvable on every surface signal and is
blocked only by a fact visible in the eligibility tool's output.

Three seeded fixtures are not yet used by any ticket and are available for extending the
set: a cancelled order (`ORD-10009`), a suspended account (`ORD-10012`), and a final-sale
item (`ORD-10016`).
