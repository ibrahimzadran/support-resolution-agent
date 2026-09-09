"""Run the frozen ticket set N times and report per-ticket stability.

Why this exists: T05 failed two different ways across two runs (once by never
calling close_ticket, once by escalating as a warranty claim). A single run is
therefore a sample, not a measurement, and "fixing" the agent against one
sample optimises against noise.

Only the DETERMINISTIC checks run here. They involve no model call, so N seeds
cost only the agent runs -- the judge is reserved for the final comparison,
where its cost buys something. That split is deliberate: it makes measuring
variance cheap enough that there is no excuse for skipping it.

Usage:  python evals/variance.py --seeds 5
        python evals/variance.py --seeds 5 --label after-fix
"""

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.config import REPO_ROOT  # noqa: E402
from evals.grade import check_deterministic, validate_run  # noqa: E402

DIMS = ["tool_sequence", "no_forbidden_tools", "terminal_state",
        "escalation_category", "refund_correct"]


def run_once(out_path: Path) -> list:
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "agent.py"), "--out", str(out_path)],
        check=True, capture_output=True, text=True,
    )
    return json.loads(out_path.read_text())


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--label", default="baseline")
    ap.add_argument("--reuse", nargs="*", default=[],
                    help="Existing run files to count as seeds (avoids re-running).")
    args = ap.parse_args()

    runs_dir = REPO_ROOT / "evals" / "runs" / args.label
    runs_dir.mkdir(parents=True, exist_ok=True)

    gt = json.loads((REPO_ROOT / "evals" / "ground_truth.json").read_text())
    cases = {c["ticket_id"]: c for c in gt["cases"]}

    all_runs, discarded = [], []
    for path in args.reuse:
        try:
            all_runs.append(validate_run(json.loads(Path(path).read_text()), path))
            print(f"reusing {path}", flush=True)
        except RuntimeError as e:
            discarded.append(str(e)); print(f"DISCARDED {e}", flush=True)
    for i in range(len(all_runs) + len(discarded), args.seeds):
        out = runs_dir / f"seed{i + 1}.json"
        print(f"running seed {i + 1}/{args.seeds} ...", flush=True)
        try:
            all_runs.append(validate_run(run_once(out), str(out)))
        except RuntimeError as e:
            discarded.append(str(e))
            print(f"DISCARDED {e}", flush=True)
            print("Stopping: infrastructure is failing, so further seeds would "
                  "measure the outage, not the agent.", flush=True)
            break

    if discarded:
        print(f"\n!! {len(discarded)} seed(s) discarded as invalid.", flush=True)
    if not all_runs:
        raise SystemExit("No valid seeds. Nothing to report.")
    if len(all_runs) < 3:
        print(f"\n!! Only {len(all_runs)} valid seed(s). Treat the numbers below as "
              "indicative, not a variance measurement -- 3 is the practical minimum.",
              flush=True)

    # per ticket -> per dimension -> list of bools
    stats = defaultdict(lambda: defaultdict(list))
    strict = defaultdict(list)
    variants = defaultdict(set)

    for run in all_runs:
        recs = {r["ticket_id"]: r for r in run}
        for tid, case in cases.items():
            rec = recs.get(tid)
            if rec is None:
                continue
            det = check_deterministic(case, rec)
            ok = True
            for d in DIMS:
                v = det["checks"][d]
                if v is not None:
                    stats[tid][d].append(bool(v))
                    ok &= bool(v)
            strict[tid].append(ok)
            variants[tid].add((rec["terminal_state"], tuple(det["called"])))

    n = len(all_runs)
    print(f"\n{'ticket':<8}{'passed':>8}  {'behaviours':>10}  failing dimensions")
    print("-" * 78)
    flaky, solid, broken = [], [], []
    for tid in cases:
        passes = sum(strict[tid])
        bad = [d for d in DIMS if stats[tid][d] and not all(stats[tid][d])]
        rate = f"{passes}/{n}"
        print(f"{tid:<8}{rate:>8}  {len(variants[tid]):>10}  {', '.join(bad) or '-'}")
        (solid if passes == n else broken if passes == 0 else flaky).append(tid)

    print("-" * 78)
    total = sum(sum(strict[t]) for t in cases)
    print(f"strict pass rate across {n} seeds: {total}/{len(cases) * n} "
          f"({total / (len(cases) * n):.0%})")
    print(f"\n  always passed ({len(solid)}): {', '.join(solid) or 'none'}")
    print(f"  FLAKY        ({len(flaky)}): {', '.join(flaky) or 'none'}")
    print(f"  always failed({len(broken)}): {', '.join(broken) or 'none'}")
    print("\nFlaky tickets are the ones a single run would have mis-measured in "
          "either direction.\nAlways-failed tickets are systematic and worth fixing.")

    summary = {"label": args.label, "seeds": n,
               "per_ticket": {t: sum(strict[t]) for t in cases},
               "solid": solid, "flaky": flaky, "broken": broken}
    (runs_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"\nWrote {runs_dir / 'summary.json'}")


if __name__ == "__main__":
    main()
