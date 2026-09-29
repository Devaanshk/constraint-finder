"""Write the diagnosis.

With an Anthropic API key, Claude turns the computed numbers into a short
Theory of Constraints report. Without one, a rule-based writer produces the same
structure, so the tool always works. In both paths every number comes from
metrics.py; the language model only explains, it never calculates.
"""

from __future__ import annotations

import json
import os
from typing import Any

from .metrics import Analysis, facts_for_llm

DEFAULT_MODEL = os.getenv("CF_MODEL", "claude-sonnet-5-5")

SYSTEM_PROMPT = """You are a principal architect who applies the Theory of Constraints \
(Goldratt) to software delivery. You write for an engineering leader who is short on time.

You receive flow metrics already computed from a repository's merged pull requests. \
Rules:
- Use ONLY numbers present in the input. Never invent, estimate, or recompute figures.
- Convert hours to days when above 48 hours, rounded sensibly.
- If a signal is null or weak, do not lean on it.
- If review_mode says there is no review gate, do NOT diagnose review stages. Say review \
is not how work flows here, then look at who carries the work (top_author_share_pct). \
A single person authoring most changes is a key-person constraint.
- Durations are calendar hours; mention this only if it changes the conclusion.
- If the constraint stage's p85 is 5x or more its median, most work flows fine and a \
minority gets stuck. Say so, and aim the actions at the stuck items, not the whole team.
- Plain, direct sentences. No hype, no em dashes.

Write GitHub-flavored Markdown with exactly these sections:

### The constraint
Two or three sentences naming the stage that limits throughput and why the data says so.

### Evidence
Three to five bullets, each tied to a specific number from the input.

### What to do (the five focusing steps)
- **Exploit:** get more from the constraint without new resources.
- **Subordinate:** change other steps to serve the constraint.
- **Elevate:** add capacity only if the first two are not enough.
One concrete, specific action each.

### Where AI coding agents fit
Two or three sentences: would faster code generation help this team, or would it \
just pile more work in front of the constraint? Be honest either way.
"""


def _fmt_h(h: float | None) -> str:
    if h is None:
        return "n/a"
    if h >= 48:
        return f"{h / 24:.1f} days"
    if h >= 1:
        n = round(h)
        return f"{n} hour" if n == 1 else f"{n} hours"
    n = round(h * 60)
    return f"{n} minute" if n == 1 else f"{n} minutes"


def _direct_report(a: Analysis) -> str:
    """Repos where merges are not gated on review (solo maintainers, trunk-based teams)."""
    s = a.signals
    st = a.stages.set_index("stage")
    solo = s["top_author_share"] >= 0.5
    lines = [
        "### The constraint",
        (
            f"**One person's capacity.** Pull request review is not how work flows in this repo: "
            f"{s['prs_analyzed'] - s['prs_reviewed']} of {s['prs_analyzed']} PRs merged without a human review, and "
            f"{s['top_author_share'] * 100:.0f}% of all PRs come from a single author. "
            "Throughput is bounded by that person's time, not by a queue."
            if solo else
            f"**No review gate to measure.** {s['prs_analyzed'] - s['prs_reviewed']} of {s['prs_analyzed']} PRs merged without a human review, "
            "so waiting-for-review can't be the constraint here. Work moves from open to merged directly."
        ),
        "",
        "### Evidence",
        f"- Only **{s['prs_reviewed']}** of {s['prs_analyzed']} merged PRs had a human review.",
        f"- Median lead time from first commit to merge is **{_fmt_h(s['median_lead_time_h'])}**, "
        f"and a typical PR is open for **{_fmt_h(st.loc['open_to_merge', 'median_h'])}** before merging.",
        f"- **{s['authors']}** people authored PRs; the most active wrote **{s['top_author_share'] * 100:.0f}%** of them.",
        "",
        "### What to do (the five focusing steps)",
    ]
    if solo:
        lines += [
            "- **Exploit:** Protect the key person's time for the work only they can do; route triage, docs and support elsewhere.",
            "- **Subordinate:** Make contributions easy to accept: templates, tests and CI checks so outside PRs arrive merge-ready.",
            "- **Elevate:** Grow a second trusted maintainer with merge rights, starting with one well-bounded area of the code.",
            "",
            "### Where AI coding agents fit",
            "This is the one setup where AI coding help can raise throughput directly, because the constraint is one "
            "builder's hours. The risk is that nobody else reviews what gets produced, so pair AI output with strong automated tests.",
        ]
    else:
        lines += [
            "- **Exploit:** Keep changes small and merge them fast; that is already this team's strength.",
            "- **Subordinate:** Put automated checks where review would sit, so quality doesn't depend on who merges.",
            "- **Elevate:** If quality issues appear, add lightweight review for risky areas only, not a gate on everything.",
            "",
            "### Where AI coding agents fit",
            "With no review queue, faster coding flows straight to main. That raises throughput but also risk; "
            "invest in tests and AI-assisted review before scaling up AI-written code.",
        ]
    return "\n".join(lines)


def rule_based_report(a: Analysis) -> str:
    if a.mode == "direct":
        return _direct_report(a)
    s = a.signals
    st = a.stages.set_index("stage")
    c = a.constraint
    share = st.loc[c, "share"] * 100
    med = st.loc[c, "median_h"]
    p85 = st.loc[c, "p85_h"]

    # A long tail means most PRs flow fine and a minority get stuck; that is a
    # different problem (things falling through the cracks) than a slow queue.
    tail = p85 / max(med, 0.5) >= 5 and c != "coding"
    headline = {
        "coding": "Building the change is where most of the time goes. The queues after it are comparatively short.",
        "wait_review": "Finished work sits idle waiting for someone to pick up the review. The team produces code faster than it starts reviewing it.",
        "review_loop": "Once review starts, PRs bounce back and forth before approval. Rework inside review is what holds work in the system.",
        "wait_merge": "Approved work is not shipping. PRs wait after approval before anyone merges them.",
    }[c]
    if tail:
        headline = (
            f"Most PRs move through this stage quickly (typical: {_fmt_h(med)}), but the slowest 15% "
            f"sit for over {_fmt_h(p85)}. Those stuck PRs account for most of the waiting, so the problem "
            "is work falling through the cracks, not a uniformly slow team."
        )

    evidence = [
        f"**{st.loc[c, 'label']}** takes **{share:.0f}%** of all flow time across {s['prs_reviewed']} reviewed PRs "
        f"(typical PR: {_fmt_h(med)}, slowest 15%: over {_fmt_h(p85)}).",
        f"Flow efficiency is **{s['flow_efficiency'] * 100:.0f}%**: only that share of the time from first commit "
        f"to merge is active building. The rest is waiting.",
        f"Median lead time from first commit to merge is **{_fmt_h(s['median_lead_time_h'])}**.",
    ]
    if s["top_reviewer_share"] >= 0.35 and s["first_reviewers"] > 1:
        evidence.append(
            f"One reviewer gives **{s['top_reviewer_share'] * 100:.0f}%** of first reviews "
            f"({s['first_reviewers']} people review for {s['authors']} authors)."
        )
    if (s["small_pr_wait_h"] is not None and s["large_pr_wait_h"] is not None
            and s["large_pr_wait_h"] > 1.5 * max(s["small_pr_wait_h"], 0.1)
            and s["large_pr_wait_h"] - s["small_pr_wait_h"] >= 4):
        evidence.append(
            f"Large PRs wait **{_fmt_h(s['large_pr_wait_h'])}** for a first review vs "
            f"**{_fmt_h(s['small_pr_wait_h'])}** for small ones."
        )
    if s["rework_rate"] >= 0.25:
        evidence.append(f"**{s['rework_rate'] * 100:.0f}%** of reviewed PRs needed rework before approval.")

    tail_actions = {
        "wait_review": (
            "Add an aging alert: any PR waiting more than one business day for a first review gets flagged in the team channel.",
            "Assign a rotating reviewer-of-the-day who owns the oldest items in the queue, so no PR depends on someone noticing it.",
            "Look at what the stuck PRs have in common (area of code, author, size) and give that area a named owner.",
        ),
        "review_loop": (
            "Flag PRs whose review has gone quiet for a day and nudge the reviewer or author.",
            "Timebox review discussions: if a PR is on its third round, switch to a short call instead of more comments.",
            "Look at what the stuck PRs share and add upfront design review for that kind of change.",
        ),
        "wait_merge": (
            "Flag approved PRs that haven't merged within a day.",
            "Turn on auto-merge when checks pass, so approval is the last human step.",
            "Find why the stuck ones stall (flaky CI, release freezes) and fix that path.",
        ),
    }
    actions = {
        "coding": (
            "Protect maker time on the stories already in progress; stop starting new work before finishing current work.",
            "Have review and planning cadence follow the coding stage: small, well-scoped tickets so builders are never blocked on unclear requirements.",
            "This is the one stage where AI coding agents add capacity directly. Pilot them on the PR types with the longest coding time.",
        ),
        "wait_review": (
            "Make review the first thing people do each day: a visible review queue and a same-day first-response target.",
            "Cap work in progress: no one opens a new PR while one of theirs or a teammate's is waiting for a first review. Keep PRs small so reviews are quick to start.",
            "Only then add reviewers: pair a second person with the top reviewer and write down what they check so others can share the load.",
        ),
        "review_loop": (
            "Agree on what 'done' means before coding starts (a short checklist) so review is not where requirements get discovered.",
            "Have authors self-review and run automated checks (lint, tests, style) before requesting review, so humans only review design and logic.",
            "Add pairing or short design reviews up front for large changes, turning late rework into early conversation.",
        ),
        "wait_merge": (
            "Merge as soon as approval lands: whoever approves merges, or auto-merge on green checks.",
            "Make CI and release windows serve the flow, not the calendar: faster pipelines, no batching approved work for a release train.",
            "Invest in deployment automation so merging is cheap and safe at any hour.",
        ),
    }[c]
    if tail:
        actions = tail_actions[c]

    ai_note = (
        "AI coding agents speed up the one stage that is already the constraint, so they are worth piloting here. "
        "Watch the review queue as you do: once coding gets faster, the constraint will move."
        if c == "coding"
        else f"This team's limit is **{st.loc[c, 'label'].lower()}**, not writing code. Faster code generation would add "
        "more PRs to the queue in front of the constraint and raise lead time, not lower it. Fix the flow first, "
        "then use AI where it shortens the constraint itself (for example, AI pre-review to cut rework)."
    )

    return "\n".join(
        [
            "### The constraint",
            f"**{st.loc[c, 'label']}.** {headline}",
            "",
            "### Evidence",
            *[f"- {e}" for e in evidence],
            "",
            "### What to do (the five focusing steps)",
            f"- **Exploit:** {actions[0]}",
            f"- **Subordinate:** {actions[1]}",
            f"- **Elevate:** {actions[2]}",
            "",
            "### Where AI coding agents fit",
            ai_note,
        ]
    )


def llm_report(a: Analysis, api_key: str | None = None, model: str | None = None) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    facts = facts_for_llm(a)
    msg = client.messages.create(
        model=model or DEFAULT_MODEL,
        max_tokens=1200,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": "Flow metrics:\n```json\n" + json.dumps(facts, indent=2) + "\n```"}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()


def diagnose(a: Analysis, api_key: str | None = None, model: str | None = None) -> tuple[str, str]:
    """Return (markdown_report, source) where source is 'claude' or 'rules'."""
    key = api_key or os.getenv("ANTHROPIC_API_KEY")
    if key:
        try:
            return llm_report(a, api_key=key, model=model), "claude"
        except Exception as exc:  # network, auth, model name: never block the report
            return rule_based_report(a) + f"\n\n_AI diagnosis unavailable ({type(exc).__name__}); showing rule-based report._", "rules"
    return rule_based_report(a), "rules"
