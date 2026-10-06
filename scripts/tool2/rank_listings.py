"""Implementation of the rank_listings tool."""

from __future__ import annotations

import json
import math
from numbers import Real

import pandas as pd

from scripts.tool1.search_listings import listing_search_result

MIN_COMPARABLES = 20
MIN_RANK_LISTINGS = 2
MAX_RANK_LISTINGS = 10
DEFAULT_MAX_COMMUTE_MINUTES = 60


def ranking_error(message: str, suggestion: str) -> str:
    """Return a structured error that lets the model repair a ranking call."""
    return json.dumps({"ok": False, "error": message, "suggestion": suggestion})


def is_finite_number(value: object, *, positive: bool = False) -> bool:
    """Accept JSON numbers but reject booleans, NaN, infinities, and bad ranges."""
    if isinstance(value, bool) or not isinstance(value, Real):
        return False
    if not math.isfinite(value):
        return False
    return value > 0 if positive else value >= 0


def price_value_metrics(
    listings: pd.DataFrame, listing_id: int, ranked_ids: set[int]
) -> dict | None:
    """Describe one listing's rent relative to location/bedroom comparables."""
    target = listings.loc[listing_id]
    target_rent = target.get("effective_rent")
    if not is_finite_number(target_rent, positive=True):
        return None

    eligible = listings[
        listings["effective_rent"].notna()
        & (listings["effective_rent"] > 0)
        & ~listings.index.isin(ranked_ids)
    ]
    neighborhood_comps = eligible[
        (eligible["neighborhood"] == target["neighborhood"])
        & (eligible["bedrooms"] == target["bedrooms"])
    ]

    if len(neighborhood_comps) >= MIN_COMPARABLES:
        comparables = neighborhood_comps
        comparison_scope = "same neighborhood and bedrooms"
    else:
        comparables = eligible[
            (eligible["borough"] == target["borough"])
            & (eligible["bedrooms"] == target["bedrooms"])
        ]
        comparison_scope = "same borough and bedrooms"

    rents = pd.to_numeric(comparables["effective_rent"], errors="coerce").dropna()
    rents = rents[rents > 0]
    if rents.empty:
        return None

    target_rent = float(target_rent)
    median = float(rents.median())
    percentile = float(
        ((rents < target_rent).sum() + 0.5 * (rents == target_rent).sum())
        / len(rents)
        * 100
    )
    value_score = max(0.0, min(100.0, 100.0 - percentile))

    if percentile <= 10:
        price_label = "well below comparable market"
    elif percentile <= 35:
        price_label = "somewhat below comparable market"
    elif percentile <= 65:
        price_label = "in line with comparable market"
    elif percentile <= 90:
        price_label = "somewhat above comparable market"
    else:
        price_label = "well above comparable market"

    difference_pct = ((target_rent - median) / median * 100) if median else None
    return {
        "price_value_score": round(value_score, 1),
        "price_label": price_label,
        "comparison_scope": comparison_scope,
        "sample_size": int(len(rents)),
        "median_effective_rent": round(median, 2),
        "q1_effective_rent": round(float(rents.quantile(0.25)), 2),
        "q3_effective_rent": round(float(rents.quantile(0.75)), 2),
        "price_percentile": round(percentile, 1),
        "difference_from_median_pct": (
            round(float(difference_pct), 1) if difference_pct is not None else None
        ),
    }


def validate_summary_list(
    summaries: list[dict] | None,
    listing_ids: list[int],
    summary_name: str,
) -> tuple[dict[int, dict], str | None]:
    """Validate summary ownership and uniqueness before any score is computed."""
    if summaries is None:
        return {}, None
    if not isinstance(summaries, list):
        return {}, f"{summary_name} must be a list or omitted."

    requested_ids = set(listing_ids)
    result: dict[int, dict] = {}
    for position, summary in enumerate(summaries):
        if not isinstance(summary, dict):
            return {}, f"{summary_name}[{position}] must be an object."
        summary_id = summary.get("listing_id")
        if isinstance(summary_id, bool) or not isinstance(summary_id, int):
            return {}, f"{summary_name}[{position}].listing_id must be an integer."
        if summary_id not in requested_ids:
            return {}, (
                f"{summary_name} contains listing_id {summary_id}, which is not in listing_ids."
            )
        if summary_id in result:
            return {}, f"{summary_name} contains duplicate listing_id {summary_id}."
        result[summary_id] = summary
    return result, None


def rank_listings(
    listings: pd.DataFrame,
    *,
    listing_ids: list[int],
    price_weight: float = 0.5,
    commute_weight: float = 0.3,
    neighborhood_weight: float = 0.2,
    commute_summaries: list[dict] | None = None,
    neighborhood_summaries: list[dict] | None = None,
    max_commute_minutes: float = DEFAULT_MAX_COMMUTE_MINUTES,
) -> str:
    """Rank rental candidates using market value and optional verified signals.

    External summaries must come from earlier tool calls. A signal is included in
    the composite score only when every candidate has that signal, preventing a
    missing value from helping or hurting one listing unfairly.
    """
    if not isinstance(listing_ids, list):
        return ranking_error(
            "listing_ids must be a list.",
            f"Pass {MIN_RANK_LISTINGS}-{MAX_RANK_LISTINGS} ids from search_listings.",
        )
    if not MIN_RANK_LISTINGS <= len(listing_ids) <= MAX_RANK_LISTINGS:
        return ranking_error(
            f"listing_ids must contain {MIN_RANK_LISTINGS}-{MAX_RANK_LISTINGS} ids.",
            "Shortlist the search results, then retry rank_listings.",
        )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in listing_ids):
        return ranking_error(
            "Every listing_id must be an integer.",
            "Use the listing_id values returned by search_listings.",
        )
    if len(set(listing_ids)) != len(listing_ids):
        return ranking_error(
            "listing_ids cannot contain duplicates.",
            "Remove repeated ids and retry rank_listings.",
        )

    unknown_ids = [listing_id for listing_id in listing_ids if listing_id not in listings.index]
    if unknown_ids:
        return ranking_error(
            f"Unknown listing_ids: {unknown_ids}.",
            "Use current ids returned by search_listings.",
        )

    raw_weights = {
        "price_value": price_weight,
        "commute": commute_weight,
        "neighborhood_fit": neighborhood_weight,
    }
    invalid_weights = [
        name for name, value in raw_weights.items() if not is_finite_number(value)
    ]
    if invalid_weights:
        return ranking_error(
            f"Weights must be finite non-negative numbers: {invalid_weights}.",
            "Correct the weights and retry rank_listings.",
        )
    if not is_finite_number(max_commute_minutes, positive=True):
        return ranking_error(
            "max_commute_minutes must be a positive number.",
            "Use the user's maximum acceptable commute, or omit it for the 60-minute default.",
        )

    commute_by_id, summary_error = validate_summary_list(
        commute_summaries, listing_ids, "commute_summaries"
    )
    if summary_error:
        return ranking_error(summary_error, "Pass only verified commute summaries for these ids.")
    neighborhood_by_id, summary_error = validate_summary_list(
        neighborhood_summaries, listing_ids, "neighborhood_summaries"
    )
    if summary_error:
        return ranking_error(
            summary_error, "Pass only verified neighborhood summaries for these ids."
        )

    for listing_id, summary in commute_by_id.items():
        duration = summary.get("duration_min")
        if not is_finite_number(duration):
            return ranking_error(
                f"commute_summaries for listing {listing_id} needs a non-negative duration_min.",
                "Copy duration_min from the user-requested mode in commute_to.",
            )

    for listing_id, summary in neighborhood_by_id.items():
        required = ("wants_met", "wants_total", "dealbreakers_hit")
        if any(field not in summary for field in required):
            return ranking_error(
                f"neighborhood_summaries for listing {listing_id} is missing a required count.",
                "Copy wants_met, wants_total, and dealbreakers_hit from check_neighborhood_fit.",
            )
        wants_met = summary["wants_met"]
        wants_total = summary["wants_total"]
        dealbreakers = summary["dealbreakers_hit"]
        if not all(is_finite_number(value) for value in (wants_met, wants_total, dealbreakers)):
            return ranking_error(
                f"Neighborhood counts for listing {listing_id} must be non-negative numbers.",
                "Use the numeric counts returned by check_neighborhood_fit.",
            )
        if not all(float(value).is_integer() for value in (wants_met, wants_total, dealbreakers)):
            return ranking_error(
                f"Neighborhood counts for listing {listing_id} must be whole numbers.",
                "Use the unmodified counts returned by check_neighborhood_fit.",
            )
        if wants_met > wants_total:
            return ranking_error(
                f"wants_met cannot exceed wants_total for listing {listing_id}.",
                "Use the unmodified counts returned by check_neighborhood_fit.",
            )

    ranked_id_set = set(listing_ids)
    price_by_id = {
        listing_id: price_value_metrics(listings, listing_id, ranked_id_set)
        for listing_id in listing_ids
    }

    missing_signals: dict[str, list[int]] = {}
    price_missing = [listing_id for listing_id, value in price_by_id.items() if value is None]
    commute_missing = [listing_id for listing_id in listing_ids if listing_id not in commute_by_id]
    neighborhood_missing = [
        listing_id for listing_id in listing_ids if listing_id not in neighborhood_by_id
    ]

    dimension_available = {
        "price_value": not price_missing,
        "commute": not commute_missing,
        "neighborhood_fit": not neighborhood_missing,
    }
    if price_weight > 0 and price_missing:
        missing_signals["price_value"] = price_missing
    if commute_weight > 0 and commute_missing:
        missing_signals["commute"] = commute_missing
    if neighborhood_weight > 0 and neighborhood_missing:
        missing_signals["neighborhood_fit"] = neighborhood_missing

    active_weights = {
        name: weight
        for name, weight in raw_weights.items()
        if weight > 0 and dimension_available[name]
    }
    active_weight_total = sum(active_weights.values())
    if active_weight_total <= 0:
        return ranking_error(
            "No complete, positively weighted ranking dimension is available.",
            "Provide valid weights or complete verified summaries for every listing.",
        )
    normalized_weights = {
        name: weight / active_weight_total for name, weight in active_weights.items()
    }

    records: list[dict] = []
    neighborhood_complete = dimension_available["neighborhood_fit"]
    for listing_id in listing_ids:
        listing = listings.loc[listing_id]
        price_metrics = price_by_id[listing_id]
        price_score = price_metrics["price_value_score"] if price_metrics else None

        commute_summary = commute_by_id.get(listing_id)
        commute_score = None
        if commute_summary is not None:
            duration = float(commute_summary["duration_min"])
            commute_score = round(
                max(0.0, min(100.0, 100.0 * (1.0 - duration / max_commute_minutes))),
                1,
            )

        neighborhood_summary = neighborhood_by_id.get(listing_id)
        neighborhood_score = None
        dealbreakers_hit = 0
        if neighborhood_summary is not None:
            wants_met = float(neighborhood_summary["wants_met"])
            wants_total = float(neighborhood_summary["wants_total"])
            dealbreakers_hit = int(neighborhood_summary["dealbreakers_hit"])
            base_score = 100.0 if wants_total == 0 else wants_met / wants_total * 100.0
            neighborhood_score = round(max(0.0, base_score - 50.0 * dealbreakers_hit), 1)

        scores = {
            "price_value": price_score,
            "commute": commute_score,
            "neighborhood_fit": neighborhood_score,
        }
        overall_score = sum(
            float(scores[name]) * weight for name, weight in normalized_weights.items()
        )

        reasons: list[str] = []
        warnings: list[str] = []
        if price_metrics:
            difference = price_metrics["difference_from_median_pct"]
            reasons.append(
                f"Price is {abs(difference):.1f}% "
                f"{'below' if difference < 0 else 'above' if difference > 0 else 'at'} "
                "the comparable median."
            )
            if price_metrics["comparison_scope"] == "same borough and bedrooms":
                warnings.append("Neighborhood sample was small, so borough-level comparables were used.")
        if commute_summary is not None:
            reasons.append(f"Selected commute is {commute_summary['duration_min']} minutes.")
        if neighborhood_summary is not None:
            reasons.append(
                f"Meets {neighborhood_summary['wants_met']} of "
                f"{neighborhood_summary['wants_total']} stated neighborhood wants."
            )
        if dealbreakers_hit:
            warnings.append(f"Hits {dealbreakers_hit} stated neighborhood dealbreaker(s).")

        listing_info = listing_search_result(listing_id, listing)
        records.append(
            {
                "rank": None,
                "listing_id": listing_id,
                "listing": {
                    "address": listing_info["address"],
                    "neighborhood": listing_info["neighborhood"],
                    "borough": listing_info["borough"],
                    "bedrooms": listing_info["bedrooms"],
                    "bathrooms": listing_info["bathrooms"],
                    "effective_rent": listing_info["effective_rent"],
                    "url": listing_info["url"],
                },
                "overall_score": round(overall_score, 1),
                "score_breakdown": scores,
                "market_context": price_metrics,
                "commute_summary": commute_summary,
                "neighborhood_summary": neighborhood_summary,
                "dealbreaker_flag": bool(dealbreakers_hit),
                "reasons": reasons,
                "warnings": warnings,
            }
        )

    records.sort(
        key=lambda record: (
            record["dealbreaker_flag"] if neighborhood_complete else False,
            -record["overall_score"],
            record["listing"]["effective_rent"],
            record["listing_id"],
        )
    )
    for rank, record in enumerate(records, start=1):
        record["rank"] = rank

    return json.dumps(
        {
            "ok": True,
            "ranking_scope": list(normalized_weights),
            "weights_used": {
                name: round(weight, 4) for name, weight in normalized_weights.items()
            },
            "rankings": records,
            "missing_signals": missing_signals,
            "methodology": {
                "minimum_neighborhood_comparables": MIN_COMPARABLES,
                "default_max_commute_minutes": DEFAULT_MAX_COMMUTE_MINUTES,
                "dealbreakers_sort_after_clear_options": neighborhood_complete,
            },
            "inventory_note": (
                "Rankings use the static June-August 2026 candidate dataset and optional "
                "tool summaries. Confirm current availability and terms at each listing URL."
            ),
        }
    )


