"""Fetch merged pull requests from GitHub and normalize them into plain records.

Uses the GraphQL search API so one request returns PRs together with their
reviews and ready-for-review events. A token is required (GitHub's GraphQL API
does not allow anonymous calls); a classic or fine-grained token with public
read access is enough.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Callable

import requests

GRAPHQL_URL = "https://api.github.com/graphql"

QUERY = """
query($q: String!, $cursor: String) {
  rateLimit { remaining resetAt }
  search(query: $q, type: ISSUE, first: 50, after: $cursor) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number
        title
        url
        createdAt
        mergedAt
        isDraft
        additions
        deletions
        changedFiles
        author { login __typename }
        commits(first: 1) { nodes { commit { authoredDate } } }
        reviews(first: 60) {
          nodes { state submittedAt author { login __typename } }
        }
        timelineItems(first: 5, itemTypes: [READY_FOR_REVIEW_EVENT]) {
          nodes { ... on ReadyForReviewEvent { createdAt } }
        }
      }
    }
  }
}
"""


class GitHubError(RuntimeError):
    pass


def _parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _is_bot(author: dict | None) -> bool:
    if not author:
        return True  # deleted ("ghost") accounts: treat as non-human
    login = author.get("login") or ""
    return author.get("__typename") == "Bot" or login.endswith("[bot]") or login.endswith("-bot")


def normalize(node: dict) -> dict[str, Any]:
    """Turn one GraphQL PullRequest node into a flat record used by metrics."""
    author = node.get("author") or {}
    commits = (node.get("commits") or {}).get("nodes") or []
    first_commit = commits[0]["commit"]["authoredDate"] if commits else None
    ready_events = (node.get("timelineItems") or {}).get("nodes") or []
    ready_ts = [e.get("createdAt") for e in ready_events if e and e.get("createdAt")]

    reviews = []
    for r in (node.get("reviews") or {}).get("nodes") or []:
        if not r or not r.get("submittedAt"):
            continue
        ra = r.get("author") or {}
        reviews.append(
            {
                "author": ra.get("login") or "ghost",
                "is_bot": _is_bot(ra),
                "state": r.get("state"),
                "submitted_at": _parse_ts(r["submittedAt"]),
            }
        )

    return {
        "number": node["number"],
        "title": node.get("title", ""),
        "url": node.get("url", ""),
        "author": author.get("login") or "ghost",
        "is_bot": _is_bot(author),
        "created_at": _parse_ts(node["createdAt"]),
        # A PR opened as draft only enters the review queue when marked ready.
        "ready_at": _parse_ts(max(ready_ts)) if ready_ts else _parse_ts(node["createdAt"]),
        "first_commit_at": _parse_ts(first_commit),
        "merged_at": _parse_ts(node.get("mergedAt")),
        "additions": node.get("additions") or 0,
        "deletions": node.get("deletions") or 0,
        "changed_files": node.get("changedFiles") or 0,
        "reviews": sorted(reviews, key=lambda r: r["submitted_at"]),
    }


def fetch_merged_prs(
    repo: str,
    token: str,
    days: int = 90,
    max_prs: int = 300,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[dict[str, Any]]:
    """Return normalized records for PRs merged in the last `days` days."""
    if "/" not in repo:
        raise GitHubError("Repository must look like owner/name, e.g. JasperFx/wolverine")
    if not token:
        raise GitHubError("A GitHub token is required. Set GITHUB_TOKEN.")

    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).date().isoformat()
    search = f"repo:{repo.strip()} is:pr is:merged merged:>={since} sort:updated-desc"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

    records: list[dict[str, Any]] = []
    cursor = None
    total = None
    while True:
        resp = requests.post(
            GRAPHQL_URL,
            json={"query": QUERY, "variables": {"q": search, "cursor": cursor}},
            headers=headers,
            timeout=60,
        )
        if resp.status_code == 401:
            raise GitHubError("GitHub rejected the token (401). Check GITHUB_TOKEN.")
        if resp.status_code >= 400:
            raise GitHubError(f"GitHub API error {resp.status_code}: {resp.text[:300]}")
        payload = resp.json()
        if payload.get("errors"):
            raise GitHubError("; ".join(e.get("message", "") for e in payload["errors"]))

        result = payload["data"]["search"]
        total = min(result["issueCount"], max_prs)
        for node in result["nodes"]:
            if node and node.get("mergedAt"):
                records.append(normalize(node))
        if on_progress:
            on_progress(len(records), total)
        if not result["pageInfo"]["hasNextPage"] or len(records) >= max_prs:
            break
        cursor = result["pageInfo"]["endCursor"]

    if not records:
        raise GitHubError(f"No merged PRs found in {repo} over the last {days} days.")
    return records[:max_prs]
