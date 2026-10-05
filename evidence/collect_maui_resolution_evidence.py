#!/usr/bin/env python3
"""Collect dated public PR resolution counts without treating nonmerges as backlog."""

import argparse
import calendar
import datetime as dt
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import quote


YEARS = (2023, 2024, 2025, 2026)
AGENT_REVIEWED = "s/agent-reviewed"


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def api(searches, sample_aliases=()):
    fields = []
    for alias, query in searches.items():
        nodes = ""
        if alias in sample_aliases:
            nodes = """
            nodes { __typename ... on PullRequest {
              number title url createdAt closedAt mergedAt state merged
              labels(first:100) { nodes { name } pageInfo { hasNextPage } }
            } }
            """
        fields.append(
            f"{alias}:search(query:{json.dumps(query)},type:ISSUE,"
            f"first:{5 if nodes else 1}) {{ issueCount {nodes} }}"
        )
    command = ["gh", "api", "graphql", "-f", "query=query{" + "\n".join(fields) + "}"]
    env = {k: v for k, v in os.environ.items() if k not in {"GH_TOKEN", "GITHUB_TOKEN"}}
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"GitHub request failed: {result.stderr.strip()}")
    payload = json.loads(result.stdout)
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")
    data = payload["data"]
    for alias in searches:
        if not isinstance(data[alias]["issueCount"], int):
            raise RuntimeError(f"{alias} has no integer count")
    return data


def query_source(query):
    return {
        "query": query,
        "url": "https://github.com/dotnet/maui/pulls?q=" + quote(query, safe=""),
    }


def collect():
    started = now()
    queries = {}
    for year in YEARS:
        bucket = f"{year}-01-01..{year}-09-30"
        prefix = "repo:dotnet/maui is:pr"
        queries[f"merged_{year}"] = f"{prefix} is:merged merged:{bucket}"
        queries[f"closed_{year}"] = f"{prefix} is:closed -is:merged closed:{bucket}"
        created = f"{prefix} created:{bucket}"
        queries[f"created_{year}"] = created
        queries[f"created_merged_{year}"] = created + " is:merged"
        queries[f"created_closed_{year}"] = created + " is:closed -is:merged"
        queries[f"created_open_{year}"] = created + " is:open"
    queries["reviewed_closed_2026"] = queries["closed_2026"] + f' label:"{AGENT_REVIEWED}"'
    data = api(queries, ("closed_2026", "reviewed_closed_2026"))

    month_queries = {}
    for year in YEARS:
        for month in range(1, 10):
            start = f"{year}-{month:02d}-01"
            end = f"{year}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"
            month_queries[f"closed_{year}_{month:02d}"] = (
                f"repo:dotnet/maui is:pr is:closed -is:merged closed:{start}..{end}"
            )
    monthly_data = api(month_queries)
    monthly = [
        {
            "month": alias.removeprefix("closed_").replace("_", "-"),
            "closed_without_merge": monthly_data[alias]["issueCount"],
            **query_source(query),
        }
        for alias, query in month_queries.items()
    ]

    summaries = {}
    cohorts = {}
    for year in YEARS:
        merged = data[f"merged_{year}"]["issueCount"]
        closed = data[f"closed_{year}"]["issueCount"]
        if sum(row["closed_without_merge"] for row in monthly if row["month"].startswith(str(year))) != closed:
            raise RuntimeError(f"{year}: monthly closure counts do not reconcile")
        summaries[str(year)] = {
            "merged": merged,
            "closed_without_merge": closed,
            "terminal_decisions": merged + closed,
        }
        cohorts[str(year)] = {
            "created": data[f"created_{year}"]["issueCount"],
            "merged": data[f"created_merged_{year}"]["issueCount"],
            "closed_without_merge": data[f"created_closed_{year}"]["issueCount"],
            "still_open": data[f"created_open_{year}"]["issueCount"],
        }
        if cohorts[str(year)]["created"] != sum(cohorts[str(year)][key] for key in ("merged", "closed_without_merge", "still_open")):
            raise RuntimeError(f"{year}: current creation-cohort states do not reconcile")

    samples = {}
    for alias in ("closed_2026", "reviewed_closed_2026"):
        records = []
        for node in data[alias]["nodes"]:
            if node["__typename"] != "PullRequest" or node["state"] != "CLOSED" or node["merged"] or node["mergedAt"] is not None:
                raise RuntimeError(f"{alias}: sample is not a closed, unmerged PR")
            if not node["closedAt"].startswith("2026-") or not 1 <= int(node["closedAt"][5:7]) <= 9:
                raise RuntimeError(f"{alias}: sample is outside Jan-Sep 2026")
            if node["labels"]["pageInfo"]["hasNextPage"]:
                raise RuntimeError(f"PR #{node['number']} has an incomplete label list")
            labels = sorted(label["name"] for label in node["labels"]["nodes"])
            if alias.startswith("reviewed_") and AGENT_REVIEWED not in labels:
                raise RuntimeError(f"PR #{node['number']} lacks the queried review label")
            records.append({**node, "labels": labels})
        samples[alias] = records
    reviewed = data["reviewed_closed_2026"]["issueCount"]
    total_closed = summaries["2026"]["closed_without_merge"]
    if not 0 <= reviewed <= total_closed:
        raise RuntimeError("The AI-review closure subset exceeds the closure cohort")

    return {
        "repository": "dotnet/maui",
        "collection_started_at": started,
        "collection_finished_at": now(),
        "calendar_summary": summaries,
        "created_cohort_current_state": cohorts,
        "monthly_closed_without_merge": monthly,
        "ai_reviewed_closures_2026": {
            "closed_without_merge": total_closed,
            "agent_reviewed_current_label": reviewed,
            "share_percent": round(reviewed / total_closed * 100, 1) if total_closed else None,
            "detection_rule": 'Currently closed and unmerged; Jan-Sep 2026 closedAt; current exact label "s/agent-reviewed".',
        },
        "sample_closed_pull_requests": samples,
        "annual_query_sources": {alias: query_source(query) for alias, query in queries.items()},
        "ai_contribution_observation": {
            "reported_at": "2026-10-05",
            "attribution": "Maintainer-reported observation supplied for the presentation.",
            "statement": "AI helped us close many PRs without merging, as well as supporting merged changes.",
            "measurement_status": "Not a measured count or causal attribution of AI-generated closures.",
        },
        "methodology": [
            "Calendar buckets are UTC January 1 inclusive through September 30 inclusive (before October 1).",
            "Merge activity uses mergedAt; nonmerge closures use current CLOSED/unmerged state and latest closedAt.",
            "Terminal decisions add these disjoint calendar merge/closure cohorts; they are not shipped-fix or quality counts.",
            "Created cohorts use January-September createdAt, with current merged/closed-unmerged/open state at collection time; October dispositions may be included.",
            "Calendar opened and terminal-decision totals are different cohorts. Opened minus merged is not backlog or nonmerge-closure count.",
            "GitHub Search aggregate counts are collected, not exhaustive historical closure-event timelines; reopened/reclosed PR metadata can change.",
            "Current labels are mutable and may be backfilled. Label presence does not prove review-before-close or that AI caused a closure.",
            "Nonmerges may include duplicates, obsolete work, rejected changes and automation. Reasons and decision quality are not measured.",
            "Examples are the first five search results, not a representative outcome/satisfaction sample.",
            "Coding-credit grids use merged PRs only: pale cells are already-merged PRs without detected coding credit, not open PRs or AI-free work.",
            "Existing raw merged-PR snapshots remain unchanged; this is a separate dated resolution extension.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    snapshot = collect()
    args.output.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({
        "calendar_summary": snapshot["calendar_summary"],
        "created_cohort_current_state": snapshot["created_cohort_current_state"],
        "ai_reviewed_closures_2026": snapshot["ai_reviewed_closures_2026"],
        "finished_at": snapshot["collection_finished_at"],
    }, indent=2))


if __name__ == "__main__":
    main()
