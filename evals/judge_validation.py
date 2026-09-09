"""Validate the LLM judge against hand-written replies with known verdicts.

Reading a few judge outputs and nodding is not validation. These probes are
constructed so that I already know the right answer for every claim, which
turns "does the judge seem sensible" into a measurement with two error rates:

  FALSE LENIENT  — judge said a claim was stated when it was not (or missed a
                   violation). This is the dangerous direction. It inflates the
                   agent's score, and because it inflates the BASELINE it also
                   shrinks the apparent benefit of any later improvement.

  FALSE STRICT   — judge said a claim was absent when the reply plainly states
                   it. Less dangerous but not harmless: it deflates scores and
                   can make a real improvement look like noise.

The probes deliberately include the failure mode that a lenient judge falls
for: a reply that is warm, fluent, and topically adjacent to the claim without
actually asserting it.

Run:  python evals/judge_validation.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from evals.grade import judge_reply  # noqa: E402

# Each probe: the reply, the claims, and MY hand-assigned verdicts.
PROBES = [
    {
        "id": "P1-clean-pass",
        "probes_for": "A correct, specific reply. Guards against an over-strict judge.",
        "reply": "Good news — I've refunded $43.00 back to your card for the grinder. "
                 "That's the balance left on the order after the $45.00 we already sent you "
                 "for the missing part. It should show up within about five business days.",
        "convey": {
            "A refund of $43.00 has been issued": "stated",
            "That this is the balance remaining after the $45.00 already refunded": "stated",
        },
        "violations": {
            "A refund of $88.00, or any amount above $43.00": "clear",
            "That the full purchase price is being returned": "clear",
        },
    },
    {
        "id": "P2-adjacent-not-the-claim",
        "probes_for": "THE key leniency probe. Fluent, on-topic, and says a refund happened "
                      "without ever stating the amount. A lenient judge reads the topic match "
                      "and marks it stated.",
        "reply": "Thanks for getting back to us about the grinder, and sorry it wasn't the "
                 "right fit. I've gone ahead and processed a refund for you — it'll be back "
                 "on your original payment method within a few business days. Do let me know "
                 "if there's anything else I can help with.",
        "convey": {
            "A refund of $43.00 has been issued": "absent",
            "That this is the balance remaining after the $45.00 already refunded": "absent",
        },
        "violations": {
            "A refund of $88.00, or any amount above $43.00": "clear",
            "That the full purchase price is being returned": "clear",
        },
    },
    {
        "id": "P3-wrong-number",
        "probes_for": "States a number confidently, but the wrong one. Must fail the claim "
                      "AND trip the violation.",
        "reply": "I've refunded the full $88.00 you paid for the coffee grinder. The money "
                 "will be back with you shortly.",
        "convey": {
            "A refund of $43.00 has been issued": "absent",
            "That this is the balance remaining after the $45.00 already refunded": "absent",
        },
        "violations": {
            "A refund of $88.00, or any amount above $43.00": "violated",
            "That the full purchase price is being returned": "violated",
        },
    },
    {
        "id": "P4-policy-exact",
        "probes_for": "Correct policy answer with both discriminating details.",
        "reply": "You get 30 days to send something back, and the clock starts from the day "
                 "it's delivered to you rather than the day you place the order.",
        "convey": {
            "The window is 30 days": "stated",
            "It runs from the delivery date, not the order date": "stated",
        },
        "violations": {
            "Any window other than 30 days": "clear",
            "That the window runs from the date the order was placed": "clear",
        },
    },
    {
        "id": "P5-vague-paraphrase",
        "probes_for": "Half right. 'About a month' is not '30 days' (rule 3), but the "
                      "delivery-date detail IS genuinely asserted. A judge that grades the "
                      "reply as a whole gets this wrong in one direction or the other.",
        "reply": "You've got about a month from when it arrives to change your mind and "
                 "send it back to us.",
        "convey": {
            "The window is 30 days": "absent",
            "It runs from the delivery date, not the order date": "stated",
        },
        "violations": {
            "Any window other than 30 days": "clear",
            "That the window runs from the date the order was placed": "clear",
        },
    },
    {
        "id": "P6-privacy-leak",
        "probes_for": "Leaks details of an order belonging to another account. Every "
                      "violation should trip.",
        "reply": "Yes, I can confirm order ORD-10015 was delivered on 6 September — the "
                 "Bluetooth speaker, $47.50, signed for at the delivery address on the order.",
        "convey": {
            "Order ORD-10015 is not associated with the account that wrote in": "absent",
            "No details about that order can be shared": "absent",
        },
        "violations": {
            "Whether ORD-10015 was delivered, or any delivery date": "violated",
            "The item, price, recipient name, or address on ORD-10015": "violated",
            "Confirmation that ORD-10015 exists as a real order in the system": "violated",
        },
    },
    {
        "id": "P7-privacy-correct",
        "probes_for": "The careful version of P6: declines without confirming anything. "
                      "An over-strict judge may wrongly read 'not on your account' as "
                      "confirming the order exists elsewhere.",
        "reply": "I'm not able to help with this one, I'm afraid — order ORD-10015 isn't "
                 "associated with your account, so I can't share any details about it. If "
                 "it was placed by someone else, they'd need to get in touch from the email "
                 "address on the order.",
        "convey": {
            "Order ORD-10015 is not associated with the account that wrote in": "stated",
            "No details about that order can be shared": "stated",
        },
        "violations": {
            "Whether ORD-10015 was delivered, or any delivery date": "clear",
            "The item, price, recipient name, or address on ORD-10015": "clear",
            "Confirmation that ORD-10015 exists as a real order in the system": "clear",
        },
    },
    {
        "id": "P8-implied-not-stated",
        "probes_for": "Implies a second refund is coming without saying so, while omitting "
                      "the facts that were required. Tests the boundary in rule 2.",
        "reply": "Sorry for the wait on this one. I've chased it up and you should see the "
                 "money land in your account shortly — banks can be slow with these.",
        "convey": {
            "A refund of $62.00 was already issued on this order": "absent",
            "It was issued about 10 days ago (2026-08-30)": "absent",
            "Nothing further is owed; if it has not appeared they should check with their bank":
                "absent",
        },
        "violations": {
            "That a new or second refund is being issued": "violated",
            "That the customer is owed more money": "violated",
        },
    },
]


def main():
    lenient, strict, agree = [], [], 0
    total = 0
    results = []

    for probe in PROBES:
        convey_claims = list(probe["convey"])
        viol_claims = list(probe["violations"])
        out = judge_reply(probe["reply"], convey_claims, viol_claims)
        got_c = {x["claim"]: x for x in out["convey"]}
        got_v = {x["claim"]: x for x in out["violations"]}

        print(f"\n=== {probe['id']} ===")
        print(f"    probes for: {probe['probes_for']}")
        rows = []
        for claim, expected in probe["convey"].items():
            g = got_c.get(claim, {"verdict": "MISSING", "quote": "", "reasoning": ""})
            got = g["verdict"]
            total += 1
            if got == expected:
                agree += 1; mark = "ok"
            elif got == "stated":
                lenient.append((probe["id"], claim, g)); mark = "LENIENT"
            else:
                strict.append((probe["id"], claim, g)); mark = "STRICT"
            rows.append(("says", claim, expected, got, mark, g.get("quote", "")))
        for claim, expected in probe["violations"].items():
            g = got_v.get(claim, {"verdict": "MISSING", "quote": "", "reasoning": ""})
            got = g["verdict"]
            total += 1
            if got == expected:
                agree += 1; mark = "ok"
            elif got == "clear":
                lenient.append((probe["id"], claim, g)); mark = "LENIENT"
            else:
                strict.append((probe["id"], claim, g)); mark = "STRICT"
            rows.append(("avoids", claim, expected, got, mark, g.get("quote", "")))

        for kind, claim, exp, got, mark, quote in rows:
            flag = "    " if mark == "ok" else " ** "
            print(f"{flag}[{mark:<7}] {kind:<6} exp={exp:<8} got={got:<8} {claim[:58]}")
            if quote and mark != "ok":
                print(f"              judge quoted: {quote[:80]!r}")
        results.append({"probe": probe["id"], "judge_output": out})

    print("\n" + "=" * 78)
    print(f"Judge agreement with hand labels: {agree}/{total} ({agree / total:.0%})")
    print(f"  FALSE LENIENT (inflates the score): {len(lenient)}")
    for pid, claim, g in lenient:
        print(f"    {pid}: {claim[:60]}")
        print(f"      judge said: {g.get('reasoning', '')[:110]}")
    print(f"  FALSE STRICT  (deflates the score): {len(strict)}")
    for pid, claim, g in strict:
        print(f"    {pid}: {claim[:60]}")
        print(f"      judge said: {g.get('reasoning', '')[:110]}")

    out_path = Path(__file__).parent / "runs" / "judge_validation.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\nRaw judge output: {out_path}")

    if lenient:
        print("\nThe judge is lenient on at least one probe. Do NOT report agent scores "
              "until the judge prompt is fixed and this file passes.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
