"""Command line: analyze a repo, print the report, optionally save a snapshot.

    export GITHUB_TOKEN=...            # read-only token
    export ANTHROPIC_API_KEY=...       # optional
    python cli.py JasperFx/wolverine --days 90 --save
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from cf import diagnosis, github_client, snapshot
from cf.metrics import analyze


def main() -> int:
    p = argparse.ArgumentParser(description="Find the constraint in a repo's PR flow.")
    p.add_argument("repo", help="owner/name")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--max-prs", type=int, default=300)
    p.add_argument("--save", action="store_true", help="save a snapshot to samples/ for the dashboard")
    p.add_argument("--include-bots", action="store_true")
    args = p.parse_args()

    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    try:
        records = github_client.fetch_merged_prs(
            args.repo, token, days=args.days, max_prs=args.max_prs,
            on_progress=lambda n, t: print(f"\r  fetched {n}/{t} PRs", end="", file=sys.stderr),
        )
    except github_client.GitHubError as e:
        print(f"\nError: {e}", file=sys.stderr)
        return 1
    print(file=sys.stderr)

    if args.save:
        out = Path(__file__).parent / "samples" / f"{args.repo.replace('/', '__')}.json"
        out.parent.mkdir(exist_ok=True)
        snapshot.save(out, args.repo, args.days, records)
        print(f"  saved snapshot to {out}", file=sys.stderr)

    a = analyze(records, args.repo, args.days, include_bots=args.include_bots)
    report, src = diagnosis.diagnose(a)
    window = f"last {args.days} days"
    if len(records) >= args.max_prs:
        oldest = min(r["merged_at"] for r in records)
        window = f"most recent {len(records)} merged PRs, since {oldest:%b %d} (raise --max-prs for more)"
    print(f"# Constraint Finder: {args.repo} ({window}, {a.signals['prs_analyzed']} human PRs)\n")
    print(report)
    print(f"\n_source: {src}_")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
