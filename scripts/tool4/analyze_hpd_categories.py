#!/usr/bin/env python3
"""Analyze frequent HPD NOV descriptions against the local category rules."""

from __future__ import annotations

import argparse
import json
import re
import socket
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[2]
ENDPOINT = "https://data.cityofnewyork.us/resource/wvxf-dwi5.json"
DEFAULT_RULES = ROOT / "data/hpd_analysis/hpd_violation_category_rules.json"


def load_rules(path: Path) -> tuple[dict, list[tuple[str, list[re.Pattern]]]]:
    with path.open(encoding="utf-8") as handle:
        document = json.load(handle)
    compiled = []
    seen = set()
    for category in document["categories"]:
        name = category["name"]
        if name in seen:
            raise ValueError(f"Duplicate category: {name}")
        seen.add(name)
        compiled.append((name, [re.compile(pattern, re.IGNORECASE) for pattern in category["patterns"]]))
    return document, compiled


def _request_json(params: dict, timeout: float) -> list[dict]:
    request = Request(
        f"{ENDPOINT}?{urlencode(params)}",
        headers={"Accept": "application/json", "User-Agent": "nyc-rental-agent-category-analysis/1.0"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            rows = json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"HPD API returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, socket.timeout) as exc:
        detail = exc.reason if isinstance(exc, URLError) else exc
        raise RuntimeError(f"HPD API request failed: {detail}") from exc
    if not isinstance(rows, list):
        raise RuntimeError("HPD API returned an unexpected response")
    return rows


def fetch_frequent_descriptions(sample_size: int, limit: int, timeout: float) -> list[dict]:
    """Count descriptions locally from a recent HPD sample.

    Socrata's full-table GROUP BY over this 11M-row dataset routinely times out, so
    bounded pagination is used to keep this analysis reproducible.
    """
    counts: Counter[str] = Counter()
    page_size = 5000
    for offset in range(0, sample_size, page_size):
        rows = _request_json(
            {
                "$select": "novdescription",
                "$where": "novdescription IS NOT NULL",
                "$order": "violationid DESC",
                "$limit": min(page_size, sample_size - offset),
                "$offset": offset,
            },
            timeout,
        )
        counts.update(row["novdescription"] for row in rows if row.get("novdescription"))
        if len(rows) < min(page_size, sample_size - offset):
            break
    return [
        {"novdescription": description, "frequency": frequency}
        for description, frequency in counts.most_common(limit)
    ]


def fetch_full_dataset_aggregation(limit: int, timeout: float) -> list[dict]:
    """Optional full-dataset aggregation; this may time out on Socrata."""
    params = {
        "$select": "novdescription,count(*) AS frequency",
        "$where": "novdescription IS NOT NULL",
        "$group": "novdescription",
        "$order": "frequency DESC",
        "$limit": limit,
    }
    return _request_json(params, timeout)


def classify(description: str, rules: list[tuple[str, list[re.Pattern]]]) -> tuple[str, list[str]]:
    matches = [name for name, patterns in rules if any(pattern.search(description) for pattern in patterns)]
    return (matches[0] if matches else "other", matches)


def analyze(rows: list[dict], rules: list[tuple[str, list[re.Pattern]]], source_scope: str) -> tuple[list[dict], dict]:
    output = []
    category_frequency: Counter[str] = Counter()
    total_frequency = 0
    other_frequency = 0
    for rank, row in enumerate(rows, start=1):
        description = row["novdescription"]
        frequency = int(row["frequency"])
        primary, matches = classify(description, rules)
        total_frequency += frequency
        category_frequency[primary] += frequency
        if primary == "other":
            other_frequency += frequency
        output.append({
            "rank": rank,
            "frequency": frequency,
            "primary_category": primary,
            "all_matching_categories": "|".join(matches),
            "novdescription": description,
        })
    classified_frequency = total_frequency - other_frequency
    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": ENDPOINT,
        "source_scope": source_scope,
        "description_limit": len(rows),
        "weighted_violation_count": total_frequency,
        "classified_violation_count": classified_frequency,
        "other_violation_count": other_frequency,
        "weighted_coverage_rate": round(classified_frequency / total_frequency, 6) if total_frequency else 0,
        "category_frequency": dict(category_frequency.most_common()),
        "top_other": [row for row in output if row["primary_category"] == "other"][:50],
    }
    return output, summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=2000, help="Number of most frequent distinct descriptions")
    parser.add_argument("--sample-size", type=int, default=100000, help="Number of newest HPD rows to sample")
    parser.add_argument("--full-aggregation", action="store_true", help="Try a full 11M-row server-side GROUP BY (may time out)")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.limit <= 0:
        raise ValueError("--limit must be positive")
    _, rules = load_rules(args.rules)
    if args.full_aggregation:
        source_rows = fetch_full_dataset_aggregation(args.limit, args.timeout)
        scope = "full_dataset_server_aggregation"
    else:
        source_rows = fetch_frequent_descriptions(args.sample_size, args.limit, args.timeout)
        scope = f"newest_{args.sample_size}_violations_by_violationid"
    _, summary = analyze(source_rows, rules, scope)
    print(json.dumps({
        "source_scope": summary["source_scope"],
        "analyzed_description_count": summary["description_limit"],
        "weighted_violation_count": summary["weighted_violation_count"],
        "weighted_coverage_rate": summary["weighted_coverage_rate"],
        "other_violation_count": summary["other_violation_count"],
        "top_other": summary["top_other"][:20],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
