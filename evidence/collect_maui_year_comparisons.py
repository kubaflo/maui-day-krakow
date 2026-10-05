#!/usr/bin/env python3
"""Derive matched 2024, 2025 and 2026 comparisons from dated public snapshots."""

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path


YEARS = ("2024", "2025", "2026")
PUBLIC_ROOT = "https://kubaflo.github.io/maui-day-krakow/evidence/"


def comparison(values):
    if any(value <= 0 for value in values):
        raise ValueError("Comparison baselines must be positive")
    current = values[-1]
    return {
        "vs_" + year: {
            "ratio": round(current / values[index], 3),
            "percent_change": round(100 * (current / values[index] - 1), 1),
        }
        for index, year in enumerate(YEARS[:-1])
    }


def derive(root, backlog_path):
    source_paths = (
        root / "maui-ai-historical-2026-10-05.json",
        root / "maui-pr-resolution-2026-10-05.json",
        backlog_path,
    )
    historical, resolution, backlog = [
        json.loads(path.read_text()) for path in source_paths
    ]
    history = historical["years"]
    calendar = resolution["calendar_summary"]
    definitions = (
        ("merged_prs", "Merged PRs", "merged", history, "mergedAt", False),
        ("community_merges", "Community-labeled merged PRs", "community_merged", history, "mergedAt", False),
        ("community_median_days", "Community PR median days to merge", "community_median_days_to_merge", history, "mergedAt", True),
        ("closed_without_merge", "Closed without merging", "closed_without_merge", calendar, "latest closedAt of currently nonmerged closed PRs", False),
        ("terminal_decisions", "Merged plus closed without merging", "terminal_decisions", calendar, "mergedAt or latest closedAt", False),
    )
    activity = {}
    for key, label, field, source, date_bucket, lower in definitions:
        values = [source[year][field] for year in YEARS]
        activity[key] = {
            "label": label,
            "values": values,
            "date_bucket": date_bucket,
            "unit": "days" if key == "community_median_days" else "PRs",
            "lower_is_better": lower,
            "comparisons": comparison(values),
        }
    for year in YEARS:
        if history[year]["period"] != {
            "from": f"{year}-01-01", "through": f"{year}-09-30",
        }:
            raise ValueError(f"Unexpected activity period for {year}")
        if calendar[year]["terminal_decisions"] != (
            calendar[year]["merged"] + calendar[year]["closed_without_merge"]
        ):
            raise ValueError(f"Terminal-decision arithmetic failed for {year}")
    stocks = {row["month"]: row["open_prs"] for row in backlog["monthly_open_pr_stock"]}
    september = [stocks[year + "-09"] for year in YEARS]
    return {
        "repository": "dotnet/maui",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "years": [int(year) for year in YEARS],
        "activity_period": "January 1 through September 30 inclusive, UTC, in each year",
        "activity": activity,
        "backlog": {
            "label": "September month-end open PR stock",
            "values": september,
            "cutoff_exclusive_utc": [year + "-10-01T00:00:00Z" for year in YEARS],
            "unit": "PRs",
            "lower_is_better": True,
            "comparisons": comparison(september),
            "current_october_snapshot": {
                "open_prs": backlog["current"]["open_prs"],
                "draft_prs": backlog["current"]["draft_prs"],
                "collected_at": backlog["current"]["collected_at"],
                "scope": backlog["current"]["scope"],
            },
        },
        "sources": [
            {
                "url": PUBLIC_ROOT + path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in source_paths
        ],
        "methodology": [
            "Activity comparisons use matched January-September UTC date buckets, not whole-year versus partial-year comparisons.",
            "Community membership is the current community label at each source snapshot, not a historical or immutable identity classification.",
            "Community timing is the median from PR creation to merge among community-labeled PRs merged in the matched period; it is not issue-to-fix time or an all-PR speed claim.",
            "Community timing comparisons use the supplied unrounded source medians; visible slide labels may round days to one decimal.",
            "Terminal decisions equal merged plus closed-without-merge PRs. Closure reasons, quality and AI causality were not measured; reopening can change the latest-closure metadata.",
            "Backlog comparisons use reconstructed September month-end stock for every year. Today's October all-open count is separate and includes drafts, not an exact review-required count.",
            "Counts can include ports and automation. Different source snapshots were collected at different times and are not a transactional historical database.",
            "These historical cohorts are not no-AI versus AI cohorts. No causal AI impact, AI-only fraction, calibrated satisfaction rate or AI-caused regression rate is inferred.",
            "2024 AI coding/review participation is unmeasured, not zero. The separate approximately 99 percent adoption statement remains a maintainer-reported estimate.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--backlog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    snapshot = derive(args.root, args.backlog)
    args.output.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({
        "years": snapshot["years"],
        "activity": snapshot["activity"],
        "backlog": snapshot["backlog"],
    }, indent=2))


if __name__ == "__main__":
    main()
