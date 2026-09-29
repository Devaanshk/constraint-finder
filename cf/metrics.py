"""Turn pull request records into flow metrics and name the constraint.

The idea comes from the Theory of Constraints: a delivery system moves only as
fast as its slowest step, and the slowest step shows up as the place where work
waits. Each merged PR is split into four stages:

    coding          first commit            -> PR ready for review
    wait_review     ready for review        -> first human review
    review_loop     first review            -> final approval (rework lives here)
    wait_merge      final approval          -> merged

Coding is the only stage where the author is actively building. The other
three are mostly queue time, so they are where hidden constraints live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

STAGES = ["coding", "wait_review", "review_loop", "wait_merge"]
STAGE_LABELS = {
    "coding": "Coding",
    "wait_review": "Waiting for review",
    "review_loop": "Review and rework",
    "wait_merge": "Waiting to merge",
    "open_to_merge": "Open until merge",
}
WAIT_STAGES = {"wait_review", "review_loop", "wait_merge", "open_to_merge"}
# Used when a repo does not gate merges on review (e.g. a solo maintainer).
DIRECT_STAGES = ["coding", "open_to_merge"]
MIN_REVIEWED = 10          # need at least this many reviewed PRs...
MIN_REVIEWED_SHARE = 0.25  # ...and this share of all PRs to measure review flow
MAX_CODING_HOURS = 24 * 30  # rebased branches can carry very old commit dates


def _hours(a, b) -> float | None:
    if a is None or b is None:
        return None
    return max((b - a).total_seconds() / 3600.0, 0.0)


def pr_frame(records: list[dict[str, Any]], include_bots: bool = False) -> pd.DataFrame:
    """One row per PR with stage durations in hours."""
    rows = []
    for pr in records:
        if pr["is_bot"] and not include_bots:
            continue
        human_reviews = [
            r for r in pr["reviews"]
            if not r["is_bot"] and r["author"] != pr["author"] and r["submitted_at"] <= pr["merged_at"]
        ]
        # Reviews before the PR was ready (drafts) still count as the first look.
        first = human_reviews[0] if human_reviews else None
        approvals = [r for r in human_reviews if r["state"] == "APPROVED"]
        final = approvals[-1] if approvals else (human_reviews[-1] if human_reviews else None)
        changes_requested = any(r["state"] == "CHANGES_REQUESTED" for r in human_reviews)
        pre_approval = [
            r for r in human_reviews
            if final is not None and r["submitted_at"] < final["submitted_at"] and r["state"] != "APPROVED"
        ]

        start = pr["first_commit_at"] or pr["created_at"]
        coding = _hours(start, pr["ready_at"])
        if coding is not None:
            coding = min(coding, MAX_CODING_HOURS)

        size = pr["additions"] + pr["deletions"]
        rows.append(
            {
                "number": pr["number"],
                "title": pr["title"],
                "url": pr["url"],
                "author": pr["author"],
                "merged_at": pr["merged_at"],
                "size": size,
                "changed_files": pr["changed_files"],
                "reviewed": first is not None,
                "first_reviewer": first["author"] if first else None,
                "reviewers": sorted({r["author"] for r in human_reviews}),
                "rework": changes_requested or len(pre_approval) >= 2,
                "coding": coding,
                "wait_review": _hours(pr["ready_at"], first["submitted_at"]) if first else None,
                "review_loop": _hours(first["submitted_at"], final["submitted_at"]) if first else None,
                "wait_merge": _hours(final["submitted_at"], pr["merged_at"]) if final else None,
                "open_to_merge": _hours(pr["ready_at"], pr["merged_at"]),
                "lead_time": _hours(start, pr["merged_at"]),
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["merged_at"] = pd.to_datetime(df["merged_at"], utc=True)
    df["size_bucket"] = pd.cut(
        df["size"], bins=[-1, 99, 499, np.inf], labels=["Small (<100 lines)", "Medium", "Large (500+ lines)"]
    )
    return df


@dataclass
class Analysis:
    repo: str
    days: int
    prs: pd.DataFrame
    stages: pd.DataFrame  # one row per stage
    constraint: str
    signals: dict[str, Any] = field(default_factory=dict)
    weekly: pd.DataFrame = field(default_factory=pd.DataFrame)
    reviewers: pd.DataFrame = field(default_factory=pd.DataFrame)
    excluded_bots: int = 0
    mode: str = "review"  # "review" or "direct" (no review gate)

    @property
    def queue_col(self) -> str:
        """The queue stage used for size/trend charts."""
        return "wait_review" if self.mode == "review" else "open_to_merge"

    @property
    def constraint_label(self) -> str:
        return STAGE_LABELS[self.constraint]


def _stage_table(frame: pd.DataFrame, stages: list[str]) -> pd.DataFrame:
    rows = []
    for s in stages:
        col = frame[s].dropna()
        # Trim the top 5% so one abandoned-then-revived PR can't pick the constraint.
        cap = col.quantile(0.95) if len(col) else 0
        rows.append(
            {
                "stage": s,
                "label": STAGE_LABELS[s],
                "is_wait": s in WAIT_STAGES,
                "median_h": float(col.median()) if len(col) else 0.0,
                "p85_h": float(col.quantile(0.85)) if len(col) else 0.0,
                "total_h": float(col.clip(upper=cap).sum()) if len(col) else 0.0,
                "n": int(len(col)),
            }
        )
    table = pd.DataFrame(rows)
    total = table["total_h"].sum() or 1.0
    table["share"] = table["total_h"] / total
    return table


def analyze(records: list[dict[str, Any]], repo: str, days: int, include_bots: bool = False) -> Analysis:
    all_count = len(records)
    df = pr_frame(records, include_bots=include_bots)
    if df.empty:
        raise ValueError("No human-authored merged PRs to analyze.")
    excluded = all_count - len(df)

    reviewed = df[df["reviewed"]]
    # Measuring review flow on a handful of reviewed PRs gives confident nonsense.
    # If review is not how work gets merged here, analyze the flow that does exist.
    mode = "review" if len(reviewed) >= max(MIN_REVIEWED, MIN_REVIEWED_SHARE * len(df)) else "direct"
    base = reviewed if mode == "review" else df

    stages = _stage_table(base, STAGES if mode == "review" else DIRECT_STAGES)
    constraint = stages.loc[stages["share"].idxmax(), "stage"]

    # Reviewer load: who gives first reviews, the gate every PR passes through.
    first_counts = reviewed["first_reviewer"].value_counts()
    reviewer_df = first_counts.rename_axis("reviewer").reset_index(name="first_reviews")
    reviewer_df["share"] = reviewer_df["first_reviews"] / reviewer_df["first_reviews"].sum()
    top_share = float(reviewer_df["share"].iloc[0]) if len(reviewer_df) else 0.0
    top_reviewer = reviewer_df["reviewer"].iloc[0] if len(reviewer_df) else None
    wait_by_reviewer = reviewed.groupby("first_reviewer")["wait_review"].median()

    author_counts = df["author"].value_counts()
    top_author = author_counts.index[0]
    top_author_share = float(author_counts.iloc[0] / len(df))

    # Does size drive waiting? Compare the typical wait for small vs large PRs.
    qcol = "wait_review" if mode == "review" else "open_to_merge"
    by_size = base.groupby("size_bucket", observed=False)[qcol].median()
    small = by_size.get("Small (<100 lines)")
    large = by_size.get("Large (500+ lines)")

    lead = df["lead_time"].dropna()

    signals = {
        "prs_analyzed": int(len(df)),
        "prs_reviewed": int(len(reviewed)),
        "unreviewed_rate": float(1 - len(reviewed) / len(df)),
        "rework_rate": float(reviewed["rework"].mean()) if len(reviewed) else 0.0,
        "median_lead_time_h": float(lead.median()) if len(lead) else 0.0,
        "p85_lead_time_h": float(lead.quantile(0.85)) if len(lead) else 0.0,
        # Flow efficiency: share of elapsed time spent actively building. Taken from
        # the same stage table as the cards so the two numbers always agree.
        "flow_efficiency": float(stages.set_index("stage").loc["coding", "share"]),
        "authors": int(df["author"].nunique()),
        "top_author": top_author,
        "top_author_share": top_author_share,
        "first_reviewers": int(reviewed["first_reviewer"].nunique()),
        "top_reviewer": top_reviewer,
        "top_reviewer_share": top_share,
        "top_reviewer_median_wait_h": float(wait_by_reviewer.get(top_reviewer, np.nan)) if top_reviewer else None,
        "others_median_wait_h": float(
            reviewed.loc[reviewed["first_reviewer"] != top_reviewer, "wait_review"].median()
        ) if top_reviewer and (reviewed["first_reviewer"] != top_reviewer).any() else None,
        "small_pr_wait_h": None if small is None or pd.isna(small) else float(small),
        "large_pr_wait_h": None if large is None or pd.isna(large) else float(large),
        "large_pr_share": float((base["size"] >= 500).mean()),
        "median_pr_size": float(df["size"].median()),
        "base_prs": int(len(base)),
        "oldest_merge": df["merged_at"].min(),
        "newest_merge": df["merged_at"].max(),
    }

    weekly = (
        df.set_index("merged_at")
        .resample("W-MON", label="left", closed="left")
        .agg(merged=("number", "count"), median_wait=(qcol, "median"))
        .reset_index()
    )

    return Analysis(
        repo=repo,
        days=days,
        prs=df,
        stages=stages,
        constraint=constraint,
        signals=signals,
        weekly=weekly,
        reviewers=reviewer_df,
        excluded_bots=excluded,
        mode=mode,
    )


def facts_for_llm(a: Analysis) -> dict[str, Any]:
    """Compact, number-only summary. The LLM explains these; it never computes them."""
    s = a.signals

    def r(x, n=1):
        return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), n)

    return {
        "repo": a.repo,
        "window_days": a.days,
        "prs_analyzed": s["prs_analyzed"],
        "bot_prs_excluded": a.excluded_bots,
        "review_mode": "review gate" if a.mode == "review" else "no review gate (most PRs merged without human review)",
        "constraint_stage": a.constraint_label,
        "stages": [
            {
                "stage": row.label,
                "kind": "queue" if row.is_wait else "active work",
                "median_hours": r(row.median_h),
                "p85_hours": r(row.p85_h),
                "share_of_total_flow_time_pct": r(row.share * 100, 0),
            }
            for row in a.stages.itertuples()
        ],
        "median_lead_time_hours": r(s["median_lead_time_h"]),
        "p85_lead_time_hours": r(s["p85_lead_time_h"]),
        "flow_efficiency_pct": r(s["flow_efficiency"] * 100, 0),
        "rework_rate_pct": r(s["rework_rate"] * 100, 0),
        "merged_without_human_review_pct": r(s["unreviewed_rate"] * 100, 0),
        "distinct_authors": s["authors"],
        "top_author_share_pct": r(s["top_author_share"] * 100, 0),
        "distinct_first_reviewers": s["first_reviewers"],
        "top_first_reviewer_share_pct": r(s["top_reviewer_share"] * 100, 0),
        "top_reviewer_median_wait_hours": r(s["top_reviewer_median_wait_h"]),
        "other_reviewers_median_wait_hours": r(s["others_median_wait_h"]),
        "small_pr_median_queue_hours": r(s["small_pr_wait_h"]),
        "large_pr_median_queue_hours": r(s["large_pr_wait_h"]),
        "large_pr_share_pct": r(s["large_pr_share"] * 100, 0),
        "median_pr_size_lines": r(s["median_pr_size"], 0),
        "note": "Durations are calendar hours, so nights and weekends count as waiting.",
    }
