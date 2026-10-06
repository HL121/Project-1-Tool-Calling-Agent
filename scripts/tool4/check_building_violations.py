"""Agent-facing validation for the check_building_violations tool."""

from __future__ import annotations

import json

from scripts.tool4.building_violations import (
    check_building_violations as query_hpd_building_violations,
)

MAX_HPD_LISTINGS = 50


def check_building_violations(listing_ids: list[int]) -> str:
    """Return public HPD violation summaries for one or more known listings."""
    if not isinstance(listing_ids, list) or not listing_ids:
        return json.dumps({
            "ok": False,
            "error": "listing_ids must be a non-empty list.",
            "suggestion": "Pass one or more listing_id values returned by search_listings.",
        })
    if len(listing_ids) > MAX_HPD_LISTINGS:
        return json.dumps({
            "ok": False,
            "error": f"listing_ids cannot contain more than {MAX_HPD_LISTINGS} ids.",
            "suggestion": "Split the listings into smaller batches and retry.",
        })
    if any(isinstance(value, bool) or not isinstance(value, int) for value in listing_ids):
        return json.dumps({
            "ok": False,
            "error": "Every listing_id must be an integer.",
            "suggestion": "Use the unmodified listing_id values returned by search_listings.",
        })
    if len(set(listing_ids)) != len(listing_ids):
        return json.dumps({
            "ok": False,
            "error": "listing_ids cannot contain duplicates.",
            "suggestion": "Remove repeated ids and retry the HPD check.",
        })

    result = query_hpd_building_violations(listing_ids)
    return json.dumps(result, ensure_ascii=False)



