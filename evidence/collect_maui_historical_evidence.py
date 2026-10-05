#!/usr/bin/env python3
"""Extend the dated MAUI PR snapshot with matched 2023/2024 public cohorts."""

import argparse
import calendar
import datetime as dt
import hashlib
import json
import os
import statistics
import subprocess
from pathlib import Path
from urllib.parse import quote


COMMUNITY_LABEL = "community \u2728"
QUERY = """
query($mergedQuery:String!, $openedQuery:String!, $cursor:String) {
  merged:search(query:$mergedQuery, type:ISSUE, first:100, after:$cursor) {
    issueCount pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest {
      number title url createdAt mergedAt
      labels(first:100) {
        nodes { name }
        pageInfo { hasNextPage }
      }
    } }
  }
  opened:search(query:$openedQuery, type:ISSUE, first:1) { issueCount }
}
"""


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def api(**variables):
    env = {k: v for k, v in os.environ.items() if k not in {"GH_TOKEN", "GITHUB_TOKEN"}}
    command = ["gh", "api", "graphql", "-f", f"query={QUERY}"]
    for name, value in variables.items():
        if value is not None:
            command += ["-f", f"{name}={value}"]
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"GitHub request failed: {result.stderr.strip()}")
    payload = json.loads(result.stdout)
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")
    return payload["data"]


def record(node):
    if node["labels"]["pageInfo"]["hasNextPage"]:
        raise RuntimeError(f"PR #{node['number']} has an incomplete label connection")
    created = dt.datetime.fromisoformat(node["createdAt"].replace("Z", "+00:00"))
    merged = dt.datetime.fromisoformat(node["mergedAt"].replace("Z", "+00:00"))
    days = (merged - created).total_seconds() / 86400
    if days < 0:
        raise RuntimeError(f"PR #{node['number']} has negative merge latency")
    return {
        "number": node["number"],
        "title": node["title"],
        "url": node["url"],
        "created_at": node["createdAt"],
        "merged_at": node["mergedAt"],
        "days_to_merge": round(days, 6),
        "labels": sorted(label["name"] for label in node["labels"]["nodes"]),
    }


def summarize(records, opened):
    community = [r for r in records if COMMUNITY_LABEL in r["labels"]]
    times = [r["days_to_merge"] for r in records]
    community_times = [r["days_to_merge"] for r in community]
    return {
        "opened": opened,
        "merged": len(records),
        "community_merged": len(community),
        "community_share_percent": (
            round(len(community) / len(records) * 100, 1) if records else None
        ),
        "all_pr_median_days_to_merge": (
            round(statistics.median(times), 3) if times else None
        ),
        "community_median_days_to_merge": (
            round(statistics.median(community_times), 3) if community_times else None
        ),
        "community_merged_within_24h": sum(t < 1 for t in community_times),
        "community_merged_within_72h": sum(t < 3 for t in community_times),
    }


def collect(current_path):
    started = now()
    current_bytes = current_path.read_bytes()
    current = json.loads(current_bytes)
    if current["periods"] != {
        "baseline": {"from": "2025-01-01", "through": "2025-09-30"},
        "current": {"from": "2026-01-01", "through": "2026-09-30"},
    }:
        raise RuntimeError("The input snapshot must use matched Jan-Sep 2025/2026")
    monthly = []
    historical = []
    for year in (2023, 2024):
        for month in range(1, 10):
            key = f"{year}-{month:02d}"
            start = key + "-01"
            end = f"{key}-{calendar.monthrange(year, month)[1]:02d}"
            merged_query = (
                f"repo:dotnet/maui is:pr is:merged merged:{start}..{end}"
            )
            opened_query = f"repo:dotnet/maui is:pr created:{start}..{end}"
            cursor = None
            bucket = []
            while True:
                data = api(
                    mergedQuery=merged_query, openedQuery=opened_query, cursor=cursor
                )
                page = data["merged"]
                if page["issueCount"] > 1000:
                    raise RuntimeError(f"{key} exceeds the GitHub search result cap")
                bucket.extend(record(node) for node in page["nodes"])
                if not page["pageInfo"]["hasNextPage"]:
                    break
                cursor = page["pageInfo"]["endCursor"]
            if len(bucket) != page["issueCount"]:
                raise RuntimeError(f"{key} has an incomplete merge-date cohort")
            if any(not r["merged_at"].startswith(key) for r in bucket):
                raise RuntimeError(f"{key} contains an unexpected merge date")
            monthly.append({
                "month": key,
                **summarize(bucket, data["opened"]["issueCount"]),
                "merged_query": merged_query,
                "opened_query": opened_query,
                "source_url": (
                    "https://github.com/dotnet/maui/pulls?q=" + quote(merged_query)
                ),
                "collection": "historical_extension",
            })
            historical.extend(bucket)
            print(f"{key}: {len(bucket)} merged PRs", flush=True)
    if len({r["number"] for r in historical}) != len(historical):
        raise RuntimeError("Duplicate PRs across historical merge-date buckets")
    all_records = historical + current["pull_requests"]
    if len({r["number"] for r in all_records}) != len(all_records):
        raise RuntimeError("Historical and existing snapshot cohorts overlap")
    for row in current["monthly"]:
        bucket = [
            r for r in current["pull_requests"]
            if r["merged_at"].startswith(row["month"])
        ]
        monthly.append({
            "month": row["month"],
            **summarize(bucket, row["opened"]),
            "merged_query": row["merged_query"],
            "opened_query": row["opened_query"],
            "source_url": row["source_url"],
            "collection": "existing_snapshot",
        })
    years = {}
    for year in (2023, 2024, 2025, 2026):
        key = str(year)
        rows = [m for m in monthly if m["month"].startswith(key)]
        records = [r for r in all_records if r["merged_at"].startswith(key)]
        if len(rows) != 9 or sum(m["merged"] for m in rows) != len(records):
            raise RuntimeError(f"{year} is not a complete Jan-Sep cohort")
        years[key] = {
            "period": {"from": f"{year}-01-01", "through": f"{year}-09-30"},
            **summarize(records, sum(m["opened"] for m in rows)),
            "copilot_code_credit": (
                current["summary"][key]["copilot_code_credit"]
                if key in current["summary"] else None
            ),
            "agent_reviewed": (
                current["summary"][key]["agent_reviewed"]
                if key in current["summary"] else None
            ),
            "any_observed_ai_signal": (
                current["summary"][key]["any_observed_ai_signal"]
                if key in current["summary"] else None
            ),
        }
    for year in ("2025", "2026"):
        for metric in ("merged", "community_merged", "community_median_days_to_merge"):
            if years[year][metric] != current["summary"][year][metric]:
                raise RuntimeError(f"Existing {year} {metric} was not preserved")
    comparisons = {}
    for baseline in ("2023", "2024", "2025"):
        old = years[baseline]
        latest = years["2026"]
        comparisons[f"{baseline}_to_2026"] = {
            "merged_growth_percent": round(
                (latest["merged"] / old["merged"] - 1) * 100, 1
            ),
            "community_merged_multiplier": round(
                latest["community_merged"] / old["community_merged"], 3
            ),
            "community_median_change_percent": round(
                (latest["community_median_days_to_merge"]
                 / old["community_median_days_to_merge"] - 1) * 100, 1
            ),
        }
    return {
        "repository": "dotnet/maui",
        "collection_started_at": started,
        "collected_at": now(),
        "existing_snapshot": {
            "filename": "maui-ai-evidence-2026-10-04.json",
            "collected_at": current["collected_at"],
            "sha256": hashlib.sha256(current_bytes).hexdigest(),
            "merged_pr_records": len(current["pull_requests"]),
        },
        "methodology": {
            "period": "Jan 1 through Sep 30 in every year; merge-date calendar buckets in UTC.",
            "cohort": "All merged repository PRs, including ports/dependency automation, not unique fixes.",
            "community_cohort": "Merged PRs currently carrying community \u2728, using the same label criterion for all years.",
            "merge_timing": "Calendar days from PR creation to merge; not issue-to-fix or shipping time.",
            "older_ai_signals": "Original-commit AI credits were not scanned for 2023/2024; null means unmeasured, not zero.",
            "collection_dates": "2023/2024 records are newly collected; 2025/2026 retain the unchanged previous snapshot.",
            "limits": [
                "Current labels can be incomplete or differ in historical application.",
                "Different release cadence, PR mix, backlog clearance and contributor mix confound comparisons.",
                "Observed growth or merge-time changes do not establish AI causation.",
                "Public AI author/credit/review signals under-detect assistance and credits can propagate through ports.",
                "No representative satisfaction, AI-only, AI-attributable regression or issue-to-shipped-fix rate is measured.",
            ],
        },
        "years": years,
        "comparisons": comparisons,
        "monthly": sorted(monthly, key=lambda m: m["month"]),
        "historical_pull_requests": sorted(historical, key=lambda r: r["number"]),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current-snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = collect(args.current_snapshot)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"years": result["years"], "comparisons": result["comparisons"]}, indent=2))


if __name__ == "__main__":
    main()
