#!/usr/bin/env python3
"""Collect public MAUI PR evidence; requires an existing gh CLI login."""

import argparse
import calendar
import datetime as dt
import json
import os
import re
import statistics
import subprocess
from pathlib import Path
from urllib.parse import quote


REPO = "dotnet/maui"
AI_LOGINS = {"copilot", "copilot-swe-agent"}
AUTOMATED_LOGINS = AI_LOGINS | {"mauibot", "dotnet-maestro", "dotnet-policy-service"}
AI_CREDIT = re.compile(
    r"^Co-authored-by:\s*(?:Copilot(?: App| CLI)?|GitHub Copilot|"
    r"copilot-swe-agent(?:\[bot\])?)\s*<[^>]+>\s*$",
    re.IGNORECASE | re.MULTILINE,
)
PR_FIELDS = """
number title url createdAt mergedAt
author { login __typename }
mergedBy { login __typename }
labels(first:100) { nodes { name } pageInfo { hasNextPage } }
mergeCommit { message }
commits(first:100) {
  totalCount
  nodes { commit { message } }
  pageInfo { hasNextPage }
}
"""
MONTH_QUERY = """
query($mergedQuery:String!, $openedQuery:String!, $cursor:String) {
  merged:search(query:$mergedQuery, type:ISSUE, first:50, after:$cursor) {
    issueCount pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest { %s } }
  }
  opened:search(query:$openedQuery, type:ISSUE, first:1) { issueCount }
}
""" % PR_FIELDS
INTERACTION_QUERY = """
query($searchQuery:String!, $cursor:String) {
  search(query:$searchQuery, type:ISSUE, first:50, after:$cursor) {
    issueCount pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest {
      number mergedAt
      comments(first:100) {
        totalCount pageInfo { hasNextPage }
        nodes { author { login __typename } createdAt }
      }
      reviews(first:100) {
        totalCount pageInfo { hasNextPage }
        nodes { author { login __typename } submittedAt state }
      }
    } }
  }
}
"""


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def api(document, **variables):
    env = {k: v for k, v in os.environ.items() if k not in {"GH_TOKEN", "GITHUB_TOKEN"}}
    args = ["gh", "api", "graphql", "-f", f"query={document}"]
    for name, value in variables.items():
        if value is not None:
            args += ["-f", f"{name}={value}"]
    result = subprocess.run(args, env=env, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"GitHub request failed: {result.stderr.strip()}")
    payload = json.loads(result.stdout)
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")
    return payload["data"]


def human_account(actor):
    return bool(
        actor
        and actor["__typename"] == "User"
        and actor["login"].lower() not in AUTOMATED_LOGINS
        and not actor["login"].lower().endswith("[bot]")
    )


def credited(message):
    return bool(AI_CREDIT.search(message.rstrip().split("\n\n")[-1]))


def record(node):
    author = node["author"] or {}
    merged_by = node["mergedBy"] or {}
    if node["labels"]["pageInfo"]["hasNextPage"]:
        raise RuntimeError(f"PR #{node['number']} has more than 100 labels")
    commits = node["commits"]
    messages = [item["commit"]["message"] for item in commits["nodes"]]
    if node["mergeCommit"]:
        messages.append(node["mergeCommit"]["message"])
    created = dt.datetime.fromisoformat(node["createdAt"].replace("Z", "+00:00"))
    merged = dt.datetime.fromisoformat(node["mergedAt"].replace("Z", "+00:00"))
    labels = sorted(item["name"] for item in node["labels"]["nodes"])
    return {
        "number": node["number"],
        "title": node["title"],
        "url": node["url"],
        "created_at": node["createdAt"],
        "merged_at": node["mergedAt"],
        "author": author.get("login"),
        "author_type": author.get("__typename"),
        "merged_by": merged_by.get("login"),
        "merged_by_type": merged_by.get("__typename"),
        "merged_by_human_account": human_account(node["mergedBy"]),
        "days_to_merge": round((merged - created).total_seconds() / 86400, 6),
        "labels": labels,
        "signals": {
            "copilot_authored": author.get("login", "").lower() in AI_LOGINS,
            "copilot_credited": any(credited(message) for message in messages),
            "agent_reviewed": "s/agent-reviewed" in labels,
        },
        "commit_count": commits["totalCount"],
        "scanned_commit_count": len(commits["nodes"]),
        "commit_scan_complete": not commits["pageInfo"]["hasNextPage"],
    }


def stats(records):
    times = [r["days_to_merge"] for r in records]
    authored = [r for r in records if r["signals"]["copilot_authored"]]
    code_credit = [
        r for r in records
        if r["signals"]["copilot_authored"] or r["signals"]["copilot_credited"]
    ]
    reviewed = [r for r in records if r["signals"]["agent_reviewed"]]
    community = [r for r in records if "community ✨" in r["labels"]]
    community_times = [r["days_to_merge"] for r in community]
    observed = [
        r for r in records
        if any(r["signals"].values())
    ]
    return {
        "merged": len(records),
        "copilot_authored": len(authored),
        "copilot_code_credit": len(code_credit),
        "copilot_credited_human_authored": sum(
            r["signals"]["copilot_credited"]
            and not r["signals"]["copilot_authored"]
            and r["author_type"] == "User"
            for r in records
        ),
        "agent_reviewed": len(reviewed),
        "community_merged": len(community),
        "community_adoption_labeled": sum(
            bool({"s/agent-suggestions-implemented", "s/agent-fix-implemented"}
                 & set(r["labels"]))
            for r in community
        ),
        "community_median_days_to_merge": (
            round(statistics.median(community_times), 3) if community_times else None
        ),
        "any_observed_ai_signal": len(observed),
        "agent_reviewed_community": sum("community ✨" in r["labels"] for r in reviewed),
        "agent_suggestions_implemented": sum(
            bool({"s/agent-suggestions-implemented", "s/agent-fix-implemented"}
                 & set(r["labels"]))
            for r in reviewed
        ),
        "agent_fix_win": sum("s/agent-fix-win" in r["labels"] for r in reviewed),
        "agent_fix_pr_picked": sum("s/agent-fix-pr-picked" in r["labels"] for r in reviewed),
        "agent_fix_lose": sum("s/agent-fix-lose" in r["labels"] for r in reviewed),
        "agent_gate_passed": sum("s/agent-gate-passed" in r["labels"] for r in reviewed),
        "agent_gate_failed": sum("s/agent-gate-failed" in r["labels"] for r in reviewed),
        "median_days_to_merge": round(statistics.median(times), 3) if times else None,
        "same_day_merges": sum(t < 1 for t in times),
        "under_three_day_merges": sum(t < 3 for t in times),
        "copilot_authored_human_account_merges": sum(
            r["merged_by_human_account"] for r in authored
        ),
        "code_credit_human_account_merges": sum(
            r["merged_by_human_account"] for r in code_credit
        ),
        "truncated_commit_scans": sum(not r["commit_scan_complete"] for r in records),
    }


def collect():
    started = utc_now()
    monthly = []
    records = []
    for year in (2025, 2026):
        for month in range(1, 10):
            start = f"{year}-{month:02d}-01"
            end = f"{year}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"
            merged_query = f"repo:{REPO} is:pr is:merged merged:{start}..{end}"
            opened_query = f"repo:{REPO} is:pr created:{start}..{end}"
            cursor = None
            bucket = []
            while True:
                data = api(
                    MONTH_QUERY, mergedQuery=merged_query,
                    openedQuery=opened_query, cursor=cursor,
                )
                page = data["merged"]
                bucket.extend(record(node) for node in page["nodes"])
                if not page["pageInfo"]["hasNextPage"]:
                    break
                cursor = page["pageInfo"]["endCursor"]
            if len(bucket) != page["issueCount"] or len(bucket) > 1000:
                raise RuntimeError(f"Incomplete merge-date cohort: {start}")
            if any(not r["merged_at"].startswith(f"{year}-{month:02d}") for r in bucket):
                raise RuntimeError(f"Unexpected merge date in {start}")
            monthly.append({
                "month": f"{year}-{month:02d}",
                "opened": data["opened"]["issueCount"],
                **stats(bucket),
                "merged_query": merged_query,
                "opened_query": opened_query,
                "source_url": "https://github.com/dotnet/maui/pulls?q=" + quote(merged_query),
            })
            records.extend(bucket)
            print(f"{year}-{month:02d}: {len(bucket)} merged PRs", flush=True)
    if len({r["number"] for r in records}) != len(records):
        raise RuntimeError("Duplicate PRs across merge-date cohorts")

    interactions = []
    cursor = None
    query = (
        f"repo:{REPO} is:pr is:merged merged:2026-01-01..2026-09-30 "
        "author:app/copilot-swe-agent"
    )
    while True:
        page = api(INTERACTION_QUERY, searchQuery=query, cursor=cursor)["search"]
        for node in page["nodes"]:
            comments = [
                c for c in node["comments"]["nodes"]
                if c["createdAt"] <= node["mergedAt"] and human_account(c["author"])
            ]
            reviews = [
                r for r in node["reviews"]["nodes"]
                if r["submittedAt"] and r["submittedAt"] <= node["mergedAt"]
                and human_account(r["author"])
            ]
            interactions.append({
                "number": node["number"],
                "pre_merge_human_account_comments": len(comments),
                "pre_merge_human_account_reviews": len(reviews),
                "pre_merge_human_account_approvals": sum(
                    r["state"] == "APPROVED" for r in reviews
                ),
                "comment_scan_complete": not node["comments"]["pageInfo"]["hasNextPage"],
                "review_scan_complete": not node["reviews"]["pageInfo"]["hasNextPage"],
            })
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    authored_numbers = {
        r["number"] for r in records
        if r["merged_at"].startswith("2026") and r["signals"]["copilot_authored"]
    }
    if {r["number"] for r in interactions} != authored_numbers:
        raise RuntimeError("Copilot search and per-PR author attribution disagree")
    years = {
        str(year): stats([r for r in records if r["merged_at"].startswith(str(year))])
        for year in (2025, 2026)
    }
    return {
        "repository": REPO,
        "collection_started_at": started,
        "collected_at": utc_now(),
        "periods": {
            "baseline": {"from": "2025-01-01", "through": "2025-09-30"},
            "current": {"from": "2026-01-01", "through": "2026-09-30"},
        },
        "methodology": {
            "date_buckets": "Calendar month of mergedAt in UTC; opened is an independent created-date count.",
            "cohort": "All merged repository PRs, including backports, dependency updates and automation; not unique product fixes.",
            "copilot_authored": "PR author GraphQL login is copilot-swe-agent or Copilot (case-insensitive); app search is author:app/copilot-swe-agent.",
            "copilot_code_credit": "Copilot-authored OR a Copilot/GitHub Copilot/Copilot App/Copilot CLI/copilot-swe-agent Co-authored-by line in the final paragraph of an original or merge commit message.",
            "agent_reviewed": "PR currently carries s/agent-reviewed; not historical event-time review coverage.",
            "community_cohort": "Merged PRs currently carrying community ✨; the same label criterion is applied to both matched periods.",
            "human_account": "GraphQL actor type User, excluding Copilot, copilot-swe-agent, MauiBot, dotnet-maestro, dotnet-policy-service and [bot]-suffix accounts.",
            "merge_timing": "Elapsed calendar days from PR creation to merge; not issue-to-fix time or reviewer effort.",
            "visible_interaction": "Comments and formal reviews by human-classified accounts before merge, for the 2026 Copilot-authored cohort only.",
            "limits": [
                "AI credits and workflow labels are lower-bound public signals; absence does not mean no AI use.",
                "AI coding and AI review overlap; do not add their counts.",
                "Authorship and human-account activity do not establish zero human interaction, autonomy or manual effort.",
                "Commit/comment/review connections scan the first 100 records; truncation is explicitly reported.",
                "Observational year-over-year comparisons cannot establish AI caused throughput or merge-time differences.",
                "No AI-attributable regression rate is measured; regression/fix labels do not identify introducing changes.",
                "No representative community satisfaction score or issue-to-fix metric is measured.",
                "Current labels can change after publication; this is a dated snapshot, not a live dashboard.",
            ],
        },
        "monthly": monthly,
        "summary": {
            **years,
            "merged_growth_percent": round(
                (years["2026"]["merged"] / years["2025"]["merged"] - 1) * 100, 1
            ),
            "copilot_authored_interactions": {
                "cohort": len(interactions),
                "with_pre_merge_human_account_comment_or_review": sum(
                    r["pre_merge_human_account_comments"] > 0
                    or r["pre_merge_human_account_reviews"] > 0
                    for r in interactions
                ),
                "with_pre_merge_human_account_approval": sum(
                    r["pre_merge_human_account_approvals"] > 0 for r in interactions
                ),
                "truncated_comment_scans": sum(not r["comment_scan_complete"] for r in interactions),
                "truncated_review_scans": sum(not r["review_scan_complete"] for r in interactions),
            },
        },
        "copilot_authored_interactions": sorted(interactions, key=lambda r: r["number"]),
        "pull_requests": sorted(records, key=lambda r: r["number"]),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    snapshot = collect()
    args.output.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(snapshot["summary"], indent=2))


if __name__ == "__main__":
    main()
