#!/usr/bin/env python3
"""Reconstruct month-end open PR stock, including closed/reopened history."""

import argparse
import collections
import datetime as dt
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import quote


UTC = dt.timezone.utc
FIRST_MONTH = (2025, 1)
LAST_MONTH = (2026, 9)
PAGE_SIZE = 50
PR_FIELDS = """
id number url createdAt closedAt state isDraft reviewDecision
timelineItems(itemTypes:[REOPENED_EVENT],first:1) {
  nodes { __typename ... on ReopenedEvent { createdAt } }
}
"""
EVENT_FIELDS = """
__typename
... on ClosedEvent { createdAt }
... on ReopenedEvent { createdAt }
... on MergedEvent { createdAt }
"""


def instant(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def stamp(value):
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def has_reopening(node):
    events = node["timelineItems"]["nodes"]
    if any(event["__typename"] != "ReopenedEvent" for event in events):
        raise RuntimeError("The reopen-filtered connection contains another event type")
    return bool(events)


def api(query):
    env = {k: v for k, v in os.environ.items() if k not in {"GH_TOKEN", "GITHUB_TOKEN"}}
    for attempt in range(3):
        result = subprocess.run(
            ["gh", "api", "graphql", "-f", "query=" + query],
            env=env, capture_output=True, text=True,
        )
        transient = any(f"HTTP {status}" in result.stderr for status in (502, 503, 504))
        if result.returncode == 0 or not transient or attempt == 2:
            break
        print(f"Transient GitHub error; retry {attempt + 1}/2: {result.stderr.strip()}", flush=True)
        time.sleep(2 ** (attempt + 1))
    if result.returncode:
        raise RuntimeError(f"GitHub request failed: {result.stderr.strip()}")
    payload = json.loads(result.stdout)
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")
    return payload["data"]


def month_after(year, month):
    return (year + 1, 1) if month == 12 else (year, month + 1)


def boundaries():
    year, month = FIRST_MONTH
    result = []
    while (year, month) <= LAST_MONTH:
        next_year, next_month = month_after(year, month)
        result.append((f"{year}-{month:02d}", dt.datetime(next_year, next_month, 1, tzinfo=UTC)))
        year, month = next_year, next_month
    return result


def current_open():
    records = []
    cursor = None
    while True:
        after = ",after:" + json.dumps(cursor) if cursor else ""
        page = api(
            "query { repository(owner:\"dotnet\",name:\"maui\") { "
            f"pullRequests(states:OPEN,first:{PAGE_SIZE}{after}) {{ "
            f"totalCount pageInfo {{hasNextPage endCursor}} nodes {{{PR_FIELDS}}} }} }} }}"
        )["repository"]["pullRequests"]
        records.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    if len(records) != page["totalCount"] or len({n["number"] for n in records}) != len(records):
        raise RuntimeError("Current open-PR connection changed during collection")
    if any(n["state"] != "OPEN" for n in records):
        raise RuntimeError("Current open connection contains a nonopen PR")
    return records


def closed_records(start, end, last_boundary):
    query = (
        "repo:dotnet/maui is:pr is:closed "
        f"created:<{last_boundary.date().isoformat()} "
        f"closed:{stamp(start)}..{stamp(end - dt.timedelta(seconds=1))}"
    )
    cursor = None
    records = []
    expected = None
    while True:
        after = ",after:" + json.dumps(cursor) if cursor else ""
        page = api(
            f"query {{ search(query:{json.dumps(query)},type:ISSUE,first:{PAGE_SIZE}{after}) {{ "
            f"issueCount pageInfo {{hasNextPage endCursor}} nodes {{__typename ... on PullRequest {{{PR_FIELDS}}}}} }} }}"
        )["search"]
        if expected is None:
            expected = page["issueCount"]
            if expected > 1000:
                if end - start <= dt.timedelta(seconds=2):
                    raise RuntimeError("A closure bucket cannot be partitioned below the search cap")
                midpoint = (start + (end - start) / 2).replace(microsecond=0)
                return closed_records(start, midpoint, last_boundary) + closed_records(midpoint, end, last_boundary)
        if page["issueCount"] != expected:
            raise RuntimeError(f"Closure bucket changed while paging: {query}")
        for node in page["nodes"]:
            if node.get("__typename") != "PullRequest" or node["state"] not in {"CLOSED", "MERGED"}:
                raise RuntimeError(f"Unexpected search record in {query}")
            if node["closedAt"] is None or not start <= instant(node["closedAt"]) < end:
                raise RuntimeError(f"Closure date outside searched interval: PR {node['number']}")
            if instant(node["createdAt"]) >= last_boundary:
                raise RuntimeError(f"Creation date outside searched cohort: PR {node['number']}")
            records.append(node)
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    if len(records) != expected or len({n["number"] for n in records}) != expected:
        raise RuntimeError(f"Incomplete closure bucket: {query}")
    print(f"Scanned closures {stamp(start)} to {stamp(end)}: {expected}", flush=True)
    return records


def histories(records):
    result = []
    values = list(records)
    for offset in range(0, len(values), 40):
        ids = [node["id"] for node in values[offset:offset + 40]]
        nodes = api(
            f"query {{ nodes(ids:{json.dumps(ids)}) {{ ... on PullRequest {{ "
            "id number url createdAt closedAt state "
            "timelineItems(itemTypes:[CLOSED_EVENT,REOPENED_EVENT,MERGED_EVENT],first:100) { "
            f"pageInfo {{hasNextPage endCursor}} nodes {{{EVENT_FIELDS}}} }} }} }} }}"
        )["nodes"]
        for node in nodes:
            if node is None:
                raise RuntimeError("A reopened PR became inaccessible during collection")
            events = node["timelineItems"]["nodes"]
            page = node["timelineItems"]["pageInfo"]
            while page["hasNextPage"]:
                connection = api(
                    f"query {{ node(id:{json.dumps(node['id'])}) {{ ... on PullRequest {{ "
                    "timelineItems(itemTypes:[CLOSED_EVENT,REOPENED_EVENT,MERGED_EVENT],first:100,"
                    f"after:{json.dumps(page['endCursor'])}) {{ pageInfo {{hasNextPage endCursor}} "
                    f"nodes {{{EVENT_FIELDS}}} }} }} }} }}"
                )["node"]["timelineItems"]
                events.extend(connection["nodes"])
                page = connection["pageInfo"]
            node["events"] = events
            del node["timelineItems"]
            if not any(event["__typename"] == "ReopenedEvent" for event in events):
                raise RuntimeError(f"PR {node['number']} was indexed as reopened but has no reopen event")
            result.append(node)
    return result


def contribution(node, boundary, replay=False):
    if instant(node["createdAt"]) >= boundary:
        return 0
    if not replay:
        return int(node["state"] == "OPEN" or instant(node["closedAt"]) >= boundary)
    opened = True
    for event in sorted(node["events"], key=lambda event: instant(event["createdAt"])):
        when = instant(event["createdAt"])
        if when < instant(node["createdAt"]):
            raise RuntimeError(f"PR {node['number']} has a transition before its creation")
        if when >= boundary:
            break
        if event["__typename"] in {"ClosedEvent", "MergedEvent"}:
            opened = False
        elif event["__typename"] == "ReopenedEvent":
            opened = True
        else:
            raise RuntimeError(f"Unexpected timeline transition: {event['__typename']}")
    return int(opened)


def collect():
    started = stamp(dt.datetime.now(UTC))
    points = boundaries()
    first_boundary, last_boundary = points[0][1], points[-1][1]
    records = {}
    year, month = first_boundary.year, first_boundary.month
    today = dt.datetime.now(UTC)
    while dt.datetime(year, month, 1, tzinfo=UTC) <= today:
        next_year, next_month = month_after(year, month)
        bucket = closed_records(
            dt.datetime(year, month, 1, tzinfo=UTC),
            dt.datetime(next_year, next_month, 1, tzinfo=UTC),
            last_boundary,
        )
        records.update({node["number"]: node for node in bucket})
        year, month = next_year, next_month
    open_records = current_open()
    records.update({node["number"]: node for node in open_records if instant(node["createdAt"]) < last_boundary})
    reopened = histories(node for node in records.values() if has_reopening(node))

    queries = {}
    for index, (_, boundary) in enumerate(points):
        date = boundary.date().isoformat()
        queries[f"open_{index}"] = f"repo:dotnet/maui is:pr is:open created:<{date}"
        queries[f"terminal_{index}"] = f"repo:dotnet/maui is:pr is:closed created:<{date} closed:>={date}"
    fields = [
        f"{alias}:search(query:{json.dumps(query)},type:ISSUE,first:1) {{issueCount}}"
        for alias, query in queries.items()
    ]
    counts = api("query{" + "\n".join(fields) + "}")
    monthly = []
    for index, (month, boundary) in enumerate(points):
        open_count = counts[f"open_{index}"]["issueCount"]
        terminal_count = counts[f"terminal_{index}"]["issueCount"]
        rebuilt_baseline = sum(contribution(node, boundary) for node in records.values())
        if rebuilt_baseline != open_count + terminal_count:
            raise RuntimeError(f"Search counts and fully paginated candidate records disagree for {month}")
        adjustment = sum(contribution(node, boundary, True) - contribution(node, boundary) for node in reopened)
        stock = open_count + terminal_count + adjustment
        if stock < 0 or adjustment > 0:
            raise RuntimeError(f"Invalid reconstructed stock for {month}")
        monthly.append({
            "month": month,
            "cutoff_exclusive_utc": stamp(boundary),
            "latest_metadata_baseline": open_count + terminal_count,
            "reopening_adjustment": adjustment,
            "open_prs": stock,
            "current_open_created_before": open_count,
            "current_terminal_closed_at_or_after": terminal_count,
            "queries": {
                name: {"query": queries[f"{name}_{index}"], "url": "https://github.com/dotnet/maui/pulls?q=" + quote(queries[f"{name}_{index}"], safe="")}
                for name in ("open", "terminal")
            },
        })
    latest_open = current_open()
    covered = {node["number"] for node in reopened}
    if any(
        has_reopening(node)
        and instant(node["createdAt"]) < last_boundary
        and node["number"] not in covered
        for node in latest_open
    ):
        raise RuntimeError("An unscanned PR reopened during collection; rerun for a consistent snapshot")
    current = {
        "collected_at": stamp(dt.datetime.now(UTC)),
        "open_prs": len(latest_open),
        "draft_prs": sum(node["isDraft"] for node in latest_open),
        "review_decisions": dict(collections.Counter(node["reviewDecision"] or "UNSPECIFIED" for node in latest_open)),
        "source_url": "https://github.com/dotnet/maui/pulls?q=is%3Apr+is%3Aopen",
        "scope": "All currently open PRs across repository branches, including drafts and already-reviewed PRs. Not a strict review-required count.",
        "pull_requests": latest_open,
    }
    return {
        "repository": "dotnet/maui",
        "collection_started_at": started,
        "collection_finished_at": stamp(dt.datetime.now(UTC)),
        "current": current,
        "monthly_open_pr_stock": monthly,
        "reopening_histories": reopened,
        "historical_records_scanned": len(records),
        "historical_candidate_records": sorted(records.values(), key=lambda node: node["number"]),
        "methodology": [
            "Each historical point is open PR stock immediately before the next month's 00:00 UTC, not monthly creations, merges or current survivors.",
            "Baseline counts created-before-cutoff PRs currently open plus currently terminal PRs closed at or after cutoff.",
            "Aggregate baseline searches are cross-checked against the complete, paginated candidate records. Closure scan intervals use one inclusive date-range qualifier and validate every returned timestamp.",
            "Every accessible PR reopened after the first cutoff must be currently open or have its latest closure after that cutoff; those candidate cohorts are scanned.",
            "Closed/reopened/merged timelines are replayed for reopened candidates to correct intervals that the latest-terminal baseline incorrectly treats as continuously open.",
            "Reopened candidates use filtered ReopenedEvent nodes, not timelineItems.totalCount: GitHub's totalCount includes unrelated timeline activity despite the itemTypes filter.",
            "Transition events exactly at the next-month boundary are excluded; October's current value is a separate partial-month snapshot, not October month-end.",
            "Historical values are reconstructed from currently accessible public PR records. Deleted/inaccessible records are unavailable; collection is not a transactional historical database snapshot.",
            "All open PRs are counted, including drafts and approved items awaiting other action. This is open queue size, not an exact number still requiring a code review.",
            "Changes after collection can change today's count. No causal AI attribution is measured by this series.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    snapshot = collect()
    args.output.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({
        "current_open_prs": snapshot["current"]["open_prs"],
        "drafts": snapshot["current"]["draft_prs"],
        "monthly_open_pr_stock": [(row["month"], row["open_prs"]) for row in snapshot["monthly_open_pr_stock"]],
        "reopened_candidates": len(snapshot["reopening_histories"]),
        "historical_records_scanned": snapshot["historical_records_scanned"],
        "finished_at": snapshot["collection_finished_at"],
    }, indent=2))


if __name__ == "__main__":
    main()
