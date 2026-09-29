"""Constraint Finder: Streamlit dashboard.

Run:  streamlit run app.py
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from cf import diagnosis, github_client, snapshot
from cf.demo import simulated_records
from cf.metrics import STAGE_LABELS, STAGES, analyze

ROOT = Path(__file__).parent
SAMPLES = ROOT / "samples"

# Chart roles (validated reference palette; one highlight + neutrals).
HIGHLIGHT = "#eb6834"   # the constraint
SERIES = "#2a78d6"      # single-series charts
NEUTRAL = "#c9c8c2"     # non-constraint stages
INK_2 = "#52514e"
GRID = "#ecebe7"

st.set_page_config(page_title="Constraint Finder", page_icon="🔎", layout="wide")

st.markdown(
    """
<style>
.block-container {padding-top: 2rem; max-width: 1200px;}
.cf-flow {display:flex; gap:8px; align-items:stretch; margin: 0.5rem 0 1.25rem; flex-wrap: wrap;}
.cf-stage {flex:1 1 180px; border:1px solid #e3e2dd; border-radius:10px; padding:14px 16px; background:#fcfcfb;}
.cf-stage.is-constraint {border:2px solid #eb6834; background:#fdf1eb;}
.cf-stage .k {font-size:0.78rem; color:#52514e; text-transform:uppercase; letter-spacing:.04em;}
.cf-stage .v {font-size:1.6rem; font-weight:650; color:#0b0b0b; line-height:1.2; margin-top:4px;}
.cf-stage .s {font-size:0.85rem; color:#52514e; margin-top:2px;}
.cf-stage .tag {display:inline-block; margin-top:8px; font-size:0.72rem; font-weight:600; color:#a43a10;
  background:#fbdccd; border-radius:999px; padding:2px 8px;}
.cf-arrow {align-self:center; color:#9a9994; font-size:1.2rem;}
@media (max-width: 800px){ .cf-arrow{display:none;} }
</style>
""",
    unsafe_allow_html=True,
)


def fmt_h(h: float | None) -> str:
    if h is None or pd.isna(h):
        return "n/a"
    if h >= 48:
        return f"{h / 24:.1f} d"
    if h >= 1:
        return f"{h:.0f} h"
    return f"{h * 60:.0f} min"


def secret(name: str) -> str | None:
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.getenv(name)


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_live(repo: str, days: int, _token: str) -> list[dict]:
    return github_client.fetch_merged_prs(repo, _token, days=days)


def style_fig(fig: go.Figure, height: int = 260) -> go.Figure:
    fig.update_layout(
        height=height,
        margin=dict(l=8, r=8, t=8, b=8),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=INK_2, size=13),
        showlegend=False,
        bargap=0.45,
        hoverlabel=dict(bgcolor="white", font_color="#0b0b0b"),
    )
    fig.update_xaxes(gridcolor=GRID, zeroline=False)
    fig.update_yaxes(gridcolor=GRID, zeroline=False)
    return fig


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Data source")
    snapshot_files = sorted(SAMPLES.glob("*.json")) if SAMPLES.exists() else []
    options = ["Simulated team (demo)"]
    if snapshot_files:
        options.insert(0, "Saved snapshot")
    options.append("Live GitHub repo")
    source = st.radio("Source", options, label_visibility="collapsed")

    repo, days, token, snap_path = None, 90, None, None
    if source == "Saved snapshot":
        snap_path = st.selectbox("Snapshot", snapshot_files, format_func=lambda p: p.stem.replace("__", "/"))
    elif source == "Live GitHub repo":
        repo = st.text_input("Repository", placeholder="owner/name, e.g. JasperFx/wolverine")
        days = st.slider("Look back (days)", 30, 180, 90, step=15)
        token = secret("GITHUB_TOKEN") or st.text_input(
            "GitHub token", type="password", help="Read-only token. Not stored."
        )

    st.divider()
    include_bots = st.toggle("Include bot PRs (e.g. Dependabot)", value=False)
    api_key = secret("ANTHROPIC_API_KEY")
    if not api_key:
        api_key = st.text_input(
            "Anthropic API key (optional)", type="password",
            help="Adds a Claude-written diagnosis. Without it, a rule-based report is used.",
        )
    run = st.button("Find the constraint", type="primary", use_container_width=True)

# ---------------------------------------------------------------- header
st.title("Constraint Finder")
st.markdown(
    "**Where does work wait?** Theory of Constraints applied to pull request flow. "
    "A delivery system moves only as fast as its slowest step, so this finds the step "
    "where finished work sits idle, then says what to do about it."
)

# Decide what to load
if "loaded" not in st.session_state:
    st.session_state.loaded = None

if run or st.session_state.loaded is None:
    try:
        if source == "Live GitHub repo":
            if not run:
                st.info("Enter a repository and press **Find the constraint**.")
                st.stop()
            if not repo or not token:
                st.warning("A repository and a GitHub token are both needed for live mode.")
                st.stop()
            with st.spinner(f"Reading merged PRs from {repo}..."):
                records = fetch_live(repo.strip(), days, token)
            label, is_sim, captured = repo.strip(), False, None
        elif source == "Saved snapshot":
            data = snapshot.load(snap_path)
            records, label, days, is_sim = data["records"], data["repo"], data["days"], False
            captured = data.get("captured_at", "")[:10]
        else:
            records, label, days, is_sim, captured = simulated_records(), "Simulated team", 90, True, None
        st.session_state.loaded = dict(records=records, label=label, days=days, is_sim=is_sim, captured=captured)
        st.session_state.pop("report", None)
    except github_client.GitHubError as e:
        st.error(str(e))
        st.stop()

L = st.session_state.loaded
try:
    a = analyze(L["records"], L["label"], L["days"], include_bots=include_bots)
except ValueError as e:
    st.error(str(e))
    st.stop()

report_key = (L["label"], include_bots, bool(api_key))
if st.session_state.get("report_key") != report_key:
    with st.spinner("Writing the diagnosis..."):
        st.session_state.report, st.session_state.report_src = diagnosis.diagnose(a, api_key=api_key or None)
    st.session_state.report_key = report_key

s = a.signals
sub = f"Last {a.days} days · {s['prs_analyzed']} merged PRs"
if a.excluded_bots:
    sub += f" · {a.excluded_bots} bot PRs excluded"
if L.get("captured"):
    sub += f" · captured {L['captured']}"
st.subheader(a.repo)
st.caption(sub)
if L["is_sim"]:
    st.info(
        "You are viewing a **simulated team** (8 engineers, 90 days) so the tool can be tried without a token. "
        "Switch to a snapshot or a live repo in the sidebar for real data."
    )

# ---------------------------------------------------------------- KPIs
st_row = a.stages.set_index("stage")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Median lead time", fmt_h(s["median_lead_time_h"]), help="First commit to merge, typical PR")
c2.metric("Flow efficiency", f"{s['flow_efficiency'] * 100:.0f}%", help="Share of lead time spent actively building")
weeks = max(a.days / 7, 1)
c3.metric("Throughput", f"{s['prs_analyzed'] / weeks:.0f} PRs/wk", help="Merged human-authored PRs per week")
if a.mode == "direct":
    c4.metric("Reviewed PRs", f"{(1 - s['unreviewed_rate']) * 100:.0f}%", help="Share of merged PRs with a human review")
    st.warning(
        f"**No review gate detected.** Only {s['prs_reviewed']} of {s['prs_analyzed']} PRs had a human review, "
        "too few to measure review flow honestly. Showing open-to-merge flow and who carries the work instead."
    )
else:
    c4.metric("Rework rate", f"{s['rework_rate'] * 100:.0f}%", help="Reviewed PRs that needed changes before approval")

# ---------------------------------------------------------------- flow strip
cards = []
stage_ids = list(a.stages["stage"])
highlight_stage = a.constraint if a.mode == "review" else None  # direct mode: the constraint is people, not a stage
for i, stg in enumerate(stage_ids):
    row = st_row.loc[stg]
    cls = "cf-stage is-constraint" if stg == highlight_stage else "cf-stage"
    kind = "active work" if not row["is_wait"] else "queue"
    tag = '<div class="tag">CONSTRAINT</div>' if stg == highlight_stage else ""
    typical = "most PRs pass straight through" if row["median_h"] < 0.05 else f"typical {fmt_h(row['median_h'])}"
    cards.append(
        f'<div class="{cls}"><div class="k">{STAGE_LABELS[stg]} · {kind}</div>'
        f'<div class="v">{row["share"] * 100:.0f}%</div>'
        f'<div class="s">of flow time · {typical}</div>{tag}</div>'
    )
    if i < len(stage_ids) - 1:
        cards.append('<div class="cf-arrow">→</div>')
st.markdown(f'<div class="cf-flow">{"".join(cards)}</div>', unsafe_allow_html=True)

# ---------------------------------------------------------------- body
left, right = st.columns([1.05, 1], gap="large")

with left:
    src = "Written by Claude from the numbers on this page" if st.session_state.report_src == "claude" else "Rule-based diagnosis (add an Anthropic key for a Claude-written one)"
    st.markdown("#### Diagnosis")
    st.caption(src)
    with st.container(border=True):
        # Reports use ### headings; render them at a size that fits a panel.
        st.markdown(re.sub(r"^### ", "##### ", st.session_state.report, flags=re.M))
    st.download_button(
        "Download report (Markdown)",
        data=f"# Constraint Finder: {a.repo}\n\n_{sub}_\n\n{st.session_state.report}\n",
        file_name=f"constraint-report-{a.repo.replace('/', '-')}.md",
        mime="text/markdown",
    )

with right:
    tab1, tab2, tab3, tab4 = st.tabs(
        ["Where time goes", "Reviewer load" if a.mode == "review" else "Who does the work", "PR size", "Trend"]
    )
    qcol = a.queue_col
    qlabel = "Typical wait for first review (hours)" if a.mode == "review" else "Typical time open until merge (hours)"

    with tab1:
        t = a.stages.iloc[::-1]
        fig = go.Figure(
            go.Bar(
                x=t["share"] * 100, y=t["label"], orientation="h",
                marker=dict(color=[HIGHLIGHT if x == highlight_stage else NEUTRAL for x in t["stage"]], cornerradius=4),
                text=[f"{v * 100:.0f}%" for v in t["share"]], textposition="outside", cliponaxis=False,
                customdata=list(zip(t["median_h"].map(fmt_h), t["p85_h"].map(fmt_h))),
                hovertemplate="<b>%{y}</b><br>%{x:.0f}% of flow time<br>Typical: %{customdata[0]}<br>Slowest 15%: over %{customdata[1]}<extra></extra>",
            )
        )
        fig.update_xaxes(title="Share of total flow time (%)", range=[0, max(100, t["share"].max() * 115)])
        st.plotly_chart(style_fig(fig), use_container_width=True)
        st.caption(
            "Summed across all reviewed PRs, top 5% of outliers trimmed per stage. Orange marks the constraint."
            if a.mode == "review" else "Summed across all merged PRs, top 5% of outliers trimmed per stage."
        )

    with tab2:
        if a.mode == "direct":
            ac = a.prs["author"].value_counts()
            ad = (ac / ac.sum()).head(8).iloc[::-1]
            fig = go.Figure(
                go.Bar(
                    x=ad.values * 100, y=ad.index, orientation="h",
                    marker=dict(color=[HIGHLIGHT if (i == ac.index[0] and s["top_author_share"] >= 0.5) else SERIES for i in ad.index], cornerradius=4),
                    text=[f"{v * 100:.0f}%" for v in ad.values], textposition="outside", cliponaxis=False,
                    hovertemplate="<b>%{y}</b><br>%{x:.0f}% of merged PRs<extra></extra>",
                )
            )
            fig.update_xaxes(title="Share of merged PRs authored (%)")
            st.plotly_chart(style_fig(fig, 280), use_container_width=True)
            st.caption(f"{s['authors']} people authored PRs in this window.")
        else:
            rv = a.reviewers.head(8).iloc[::-1]
            top = a.reviewers["reviewer"].iloc[0] if len(a.reviewers) else None
            concentrated = s["top_reviewer_share"] >= 0.35
            fig = go.Figure(
                go.Bar(
                    x=rv["share"] * 100, y=rv["reviewer"], orientation="h",
                    marker=dict(color=[HIGHLIGHT if (r == top and concentrated) else SERIES for r in rv["reviewer"]], cornerradius=4),
                    text=[f"{v * 100:.0f}%" for v in rv["share"]], textposition="outside", cliponaxis=False,
                    hovertemplate="<b>%{y}</b><br>%{x:.0f}% of first reviews<extra></extra>",
                )
            )
            fig.update_xaxes(title="Share of first reviews (%)")
            st.plotly_chart(style_fig(fig, 280), use_container_width=True)
            if concentrated and s["top_reviewer_median_wait_h"] is not None and s["others_median_wait_h"] is not None:
                st.caption(
                    f"PRs first reviewed by **{top}** wait {fmt_h(s['top_reviewer_median_wait_h'])} (typical) "
                    f"vs {fmt_h(s['others_median_wait_h'])} for everyone else."
                )
            else:
                st.caption("Who does the first review, the gate every PR passes through.")

    with tab3:
        base = a.prs[a.prs["reviewed"]] if a.mode == "review" else a.prs
        by = base.groupby("size_bucket", observed=False).agg(wait=(qcol, "median"), n=("number", "count")).reset_index()
        fig = go.Figure(
            go.Bar(
                x=by["size_bucket"].astype(str), y=by["wait"],
                marker=dict(color=SERIES, cornerradius=4),
                text=[fmt_h(v) for v in by["wait"]], textposition="outside", cliponaxis=False,
                customdata=by["n"], hovertemplate="<b>%{x}</b><br>Typical wait: %{text}<br>%{customdata} PRs<extra></extra>",
            )
        )
        fig.update_yaxes(title=qlabel)
        st.plotly_chart(style_fig(fig), use_container_width=True)
        st.caption(f"Median PR size is {s['median_pr_size']:.0f} changed lines; {s['large_pr_share'] * 100:.0f}% of PRs are 500+ lines.")

    with tab4:
        w = a.weekly.copy()
        # Drop the current partial week so the last point isn't misleading.
        last_merge = a.prs["merged_at"].max()
        w = w[w["merged_at"] + pd.Timedelta(days=7) <= last_merge + pd.Timedelta(days=1)]
        fig = go.Figure(
            go.Scatter(
                x=w["merged_at"], y=w["median_wait"], mode="lines+markers",
                line=dict(color=SERIES, width=2), marker=dict(size=8, color=SERIES, line=dict(color="white", width=2)),
                customdata=w["merged"],
                hovertemplate="Week of %{x|%b %d}<br>Typical: %{y:.0f} h<br>%{customdata} PRs merged<extra></extra>",
            )
        )
        fig.update_yaxes(title=qlabel, rangemode="tozero")
        fig.update_layout(hovermode="x")
        st.plotly_chart(style_fig(fig), use_container_width=True)
        st.caption(f"Weekly median, by merge week. Throughput averaged {w['merged'].mean():.0f} PRs a week.")

with st.expander("All pull requests analyzed (table view)"):
    tbl = a.prs[["number", "title", "author", "size"] + STAGES + ["open_to_merge", "lead_time", "rework", "url"]].copy()
    tbl = tbl.rename(columns={**STAGE_LABELS, "lead_time": "Lead time (h)", "size": "Lines changed"})
    st.dataframe(
        tbl.round(1), hide_index=True, use_container_width=True,
        column_config={"url": st.column_config.LinkColumn("Link", display_text="open")},
    )

with st.expander("How it works"):
    st.markdown(
        """
Every merged PR is split into four stages:

| Stage | From | To | Kind |
|---|---|---|---|
| Coding | first commit | PR ready for review | active work |
| Waiting for review | ready for review | first human review | queue |
| Review and rework | first review | final approval | queue |
| Waiting to merge | final approval | merged | queue |

The **constraint** is the stage holding the largest share of total flow time across all reviewed PRs
(top 5% of each stage trimmed so one stale PR can't decide it). **Flow efficiency** is coding time divided by lead time.

All numbers are computed in Python. The AI step only receives those numbers and explains them, so it cannot
invent statistics. Durations are calendar hours; nights and weekends count as waiting, which is how the
work item experiences them. Bot-authored PRs are excluded by default.
"""
    )
