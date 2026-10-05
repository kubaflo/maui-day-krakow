#!/usr/bin/env python3
"""Extend verified month-end open PR history backwards to January 2024."""

import argparse
import collections
import datetime as dt
import hashlib
import json
from pathlib import Path
from urllib.parse import quote

import collect_maui_backlog_evidence as base


SEED_SHA256 = "5b5c1931b60a618ba036f070d575c1161d81c44a2cb8fffb499752fd131f3560"
SEED_URL = "https://kubaflo.github.io/maui-day-krakow/evidence/maui-open-pr-stock-2026-10-05.json"


def boundaries():
    year, month = 2024, 1
    points = []
    while (year, month) <= (2026, 9):
        following_year, following_month = base.month_after(year, month)
        points.append((
            f"{year}-{month:02d}",
            dt.datetime(following_year, following_month, 1, tzinfo=base.UTC),
        ))
        year, month = following_year, following_month
    return points


def collect(seed_path):
    started = base.stamp(dt.datetime.now(base.UTC))
    seed_bytes = seed_path.read_bytes()
    if hashlib.sha256(seed_bytes).hexdigest() != SEED_SHA256:
        raise RuntimeError("The seed is not the verified immutable public snapshot")
    seed = json.loads(seed_bytes)
    if seed["repository"] != "dotnet/maui":
        raise RuntimeError("Unexpected seed repository")
    if base.instant(seed["collection_finished_at"]) >= base.instant(started):
        raise RuntimeError("The seed collection must precede this extension")
    points = boundaries()
    last_boundary = points[-1][1]
    seed_first_boundary = base.instant(seed["monthly_open_pr_stock"][0]["cutoff_exclusive_utc"])
    if seed_first_boundary != dt.datetime(2025, 2, 1, tzinfo=base.UTC):
        raise RuntimeError("Unexpected seed history boundary")
    records = {node["number"]: node for node in seed["historical_candidate_records"]}
    if len(records) != seed["historical_records_scanned"]:
        raise RuntimeError("The seed candidate archive is incomplete")

    year, month = points[0][1].year, points[0][1].month
    missing_records = 0
    while dt.datetime(year, month, 1, tzinfo=base.UTC) < seed_first_boundary:
        next_year, next_month = base.month_after(year, month)
        bucket = base.closed_records(
            dt.datetime(year, month, 1, tzinfo=base.UTC),
            dt.datetime(next_year, next_month, 1, tzinfo=base.UTC),
            last_boundary,
        )
        missing_records += len(bucket)
        records.update({node["number"]: node for node in bucket})
        year, month = next_year, next_month

    recent_end = dt.datetime.now(base.UTC).replace(microsecond=0) + dt.timedelta(seconds=1)
    recent = base.closed_records(
        base.instant(seed["collection_finished_at"]), recent_end, last_boundary,
    )
    records.update({node["number"]: node for node in recent})
    open_records = base.current_open()
    records.update({
        node["number"]: node for node in open_records
        if base.instant(node["createdAt"]) < last_boundary
    })
    reopened = base.histories(
        node for node in records.values() if base.has_reopening(node)
    )

    queries = {}
    for index, (_, boundary) in enumerate(points):
        date = boundary.date().isoformat()
        queries[f"open_{index}"] = f"repo:dotnet/maui is:pr is:open created:<{date}"
        queries[f"terminal_{index}"] = (
            f"repo:dotnet/maui is:pr is:closed created:<{date} closed:>={date}"
        )
    fields = [
        f"{alias}:search(query:{json.dumps(query)},type:ISSUE,first:1) {{issueCount}}"
        for alias, query in queries.items()
    ]
    counts = {}
    for offset in range(0, len(fields), 20):
        counts.update(base.api("query{" + "\n".join(fields[offset:offset + 20]) + "}"))

    monthly = []
    for index, (month, boundary) in enumerate(points):
        open_count = counts[f"open_{index}"]["issueCount"]
        terminal_count = counts[f"terminal_{index}"]["issueCount"]
        baseline = sum(base.contribution(node, boundary) for node in records.values())
        if baseline != open_count + terminal_count:
            raise RuntimeError(f"Candidate archive and live baseline disagree for {month}")
        if any(
            base.contribution(node, boundary)
            != base.contribution(records[node["number"]], boundary)
            for node in reopened
        ):
            raise RuntimeError(f"Reopened metadata changed during collection for {month}")
        adjustment = sum(
            base.contribution(node, boundary, True) - base.contribution(node, boundary)
            for node in reopened
        )
        stock = baseline + adjustment
        if stock < 0 or adjustment > 0:
            raise RuntimeError(f"Invalid reconstructed stock for {month}")
        monthly.append({
            "month": month,
            "cutoff_exclusive_utc": base.stamp(boundary),
            "latest_metadata_baseline": baseline,
            "reopening_adjustment": adjustment,
            "open_prs": stock,
            "current_open_created_before": open_count,
            "current_terminal_closed_at_or_after": terminal_count,
            "queries": {
                name: {
                    "query": queries[f"{name}_{index}"],
                    "url": "https://github.com/dotnet/maui/pulls?q="
                    + quote(queries[f"{name}_{index}"], safe=""),
                }
                for name in ("open", "terminal")
            },
        })
    previous = {row["month"]: row["open_prs"] for row in seed["monthly_open_pr_stock"]}
    if any(row["open_prs"] != previous[row["month"]] for row in monthly if row["month"] in previous):
        raise RuntimeError("The previously verified historical stocks changed")

    latest_open = base.current_open()
    covered = {node["number"] for node in reopened}
    if any(
        base.has_reopening(node)
        and base.instant(node["createdAt"]) < last_boundary
        and node["number"] not in covered
        for node in latest_open
    ):
        raise RuntimeError("An unscanned PR reopened during collection; rerun")
    current = {
        "collected_at": base.stamp(dt.datetime.now(base.UTC)),
        "open_prs": len(latest_open),
        "draft_prs": sum(node["isDraft"] for node in latest_open),
        "review_decisions": dict(collections.Counter(
            node["reviewDecision"] or "UNSPECIFIED" for node in latest_open
        )),
        "source_url": seed["current"]["source_url"],
        "scope": seed["current"]["scope"],
        "pull_requests": latest_open,
    }
    return {
        "repository": "dotnet/maui",
        "collection_started_at": started,
        "collection_finished_at": base.stamp(dt.datetime.now(base.UTC)),
        "current": current,
        "monthly_open_pr_stock": monthly,
        "reopening_histories": reopened,
        "historical_records_scanned": len(records),
        "historical_candidate_records": sorted(records.values(), key=lambda node: node["number"]),
        "seed_snapshot": {
            "url": SEED_URL,
            "sha256": SEED_SHA256,
            "collected_at": seed["collection_finished_at"],
            "candidate_records": seed["historical_records_scanned"],
        },
        "extension": {
            "missing_closure_interval_start": base.stamp(points[0][1]),
            "missing_closure_interval_end_exclusive": base.stamp(seed_first_boundary),
            "additional_terminal_records": missing_records,
            "recent_terminal_records_refreshed": len(recent),
            "recent_terminal_refresh_end_exclusive": base.stamp(recent_end),
        },
        "methodology": seed["methodology"] + [
            "This extension preserves the immutable seed, scans its missing 2024-February through 2025-January terminal interval and refreshes recent terminal and currently open candidates.",
            "Every reopened candidate's complete public timeline is fetched again. All 33 historical baseline totals are cross-checked against live aggregate searches and the combined fully paginated archive.",
            "All 21 previously reported 2025-2026 month-end stocks must match exactly; any disagreement fails collection rather than silently revising the published seed.",
            "Historical month-end stocks are Jan 2024 through Sep 2026. Matched-year backlog comparisons use the September endpoint for each year; today's October count remains separate.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    snapshot = collect(args.seed)
    args.output.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({
        "current": snapshot["current"]["open_prs"],
        "drafts": snapshot["current"]["draft_prs"],
        "months": [(row["month"], row["open_prs"]) for row in snapshot["monthly_open_pr_stock"]],
        "candidate_records": snapshot["historical_records_scanned"],
        "reopened_histories": len(snapshot["reopening_histories"]),
        "finished_at": snapshot["collection_finished_at"],
    }, indent=2))


if __name__ == "__main__":
    main()
