"""Simulated team for the demo mode.

Clearly labeled as simulated in the UI. It models a common pattern: eight
engineers, one senior who does most first reviews, and large PRs that sit in
the queue. Real repositories are analyzed with the live mode or a snapshot.
"""

from __future__ import annotations

import datetime as dt
import random

AUTHORS = ["ana", "ben", "chen", "dev", "eli", "fatima", "gus", "hana"]
REVIEWERS = ["morgan", "ana", "dev", "fatima"]
REVIEWER_WEIGHTS = [0.58, 0.16, 0.14, 0.12]  # morgan is the bottleneck reviewer


def _skip_weekend(t: dt.datetime) -> dt.datetime:
    while t.weekday() >= 5:
        t += dt.timedelta(days=1)
    return t


def simulated_records(days: int = 90, seed: int = 7) -> list[dict]:
    rng = random.Random(seed)
    now = dt.datetime(2026, 9, 25, 17, 0, tzinfo=dt.timezone.utc)
    start = now - dt.timedelta(days=days)
    records = []
    t = start
    number = 1400
    while True:
        t += dt.timedelta(hours=rng.expovariate(1 / 7.5))
        if t >= now - dt.timedelta(days=6):
            break
        number += 1
        author = rng.choice(AUTHORS)
        size = int(rng.lognormvariate(4.9, 1.0))  # median ~135 lines, long tail
        large = size >= 500

        first_commit = _skip_weekend(t)
        ready = first_commit + dt.timedelta(hours=rng.lognormvariate(2.6, 0.7) * (1.6 if large else 1.0))

        pool = [(r, w) for r, w in zip(REVIEWERS, REVIEWER_WEIGHTS) if r != author]
        reviewer = rng.choices([r for r, _ in pool], weights=[w for _, w in pool])[0]
        base_wait = 34 if reviewer == "morgan" else 11
        wait = rng.lognormvariate(0, 0.8) * base_wait * (2.2 if large else 1.0)
        first_review = _skip_weekend(ready + dt.timedelta(hours=wait))

        reviews = []
        rework = rng.random() < (0.45 if large else 0.2)
        if rework:
            reviews.append({"author": reviewer, "is_bot": False, "state": "CHANGES_REQUESTED", "submitted_at": first_review})
            approve_at = first_review + dt.timedelta(hours=rng.lognormvariate(2.4, 0.6))
            reviews.append({"author": reviewer, "is_bot": False, "state": "APPROVED", "submitted_at": approve_at})
        else:
            approve_at = first_review
            reviews.append({"author": reviewer, "is_bot": False, "state": "APPROVED", "submitted_at": approve_at})

        merged = approve_at + dt.timedelta(hours=rng.lognormvariate(0.3, 1.0))
        if merged > now:
            continue
        records.append(
            {
                "number": number,
                "title": f"Simulated change #{number}",
                "url": "",
                "author": author,
                "is_bot": False,
                "created_at": ready,
                "ready_at": ready,
                "first_commit_at": first_commit,
                "merged_at": merged,
                "additions": int(size * 0.7),
                "deletions": size - int(size * 0.7),
                "changed_files": max(1, size // 40),
                "reviews": reviews,
            }
        )

    # A few dependency-bot PRs, which the analysis excludes by default.
    for i in range(9):
        ts = start + dt.timedelta(days=9 * i + 2)
        records.append(
            {
                "number": 900 + i, "title": "Bump dependency", "url": "", "author": "dependabot[bot]",
                "is_bot": True, "created_at": ts, "ready_at": ts, "first_commit_at": ts,
                "merged_at": ts + dt.timedelta(hours=3), "additions": 4, "deletions": 4,
                "changed_files": 1, "reviews": [],
            }
        )
    return records
