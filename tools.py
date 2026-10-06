"""Tool registrations, schemas, and thin adapters used by the agent harness."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from scripts.tool1.search_listings import (
    DEFAULT_SEARCH_LIMIT,
    MAX_SEARCH_LIMIT,
    listing_search_result,
    normalize_search_text,
    search_listings as _search_listings,
)
from scripts.tool2.rank_listings import (
    DEFAULT_MAX_COMMUTE_MINUTES,
    MAX_RANK_LISTINGS,
    MIN_COMPARABLES,
    MIN_RANK_LISTINGS,
    rank_listings as _rank_listings,
)
from scripts.tool3.commute import (
    clean_route_info,
    commute_to,
    format_request,
    preferences,
    travel_modes,
)
from scripts.tool4.check_building_violations import (
    MAX_HPD_LISTINGS,
    check_building_violations,
)
from scripts.tool5.neighborhood_fit import (
    check_neighborhood_fit as _check_neighborhood_fit,
    distance_m,
    find_nearest,
)


listings = pd.read_csv(
    Path(__file__).parent / "data" / "nyc_rental_listings_clean.csv",
    dtype={"zip_code": "string"},
    parse_dates=["created_at_utc", "available_date"],
).set_index("id")


def search_listings(
    borough: str | None = None,
    neighborhood: str | None = None,
    min_bedrooms: float | None = None,
    max_bedrooms: float | None = None,
    min_bathrooms: float | None = None,
    max_bathrooms: float | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    furnished: bool | None = None,
    new_development_only: bool = False,
    sort_by: str = "price_asc",
    limit: int = DEFAULT_SEARCH_LIMIT,
) -> str:
    """Delegate listing search to Tool 1 using the current listing dataset."""
    return _search_listings(
        listings,
        borough=borough,
        neighborhood=neighborhood,
        min_bedrooms=min_bedrooms,
        max_bedrooms=max_bedrooms,
        min_bathrooms=min_bathrooms,
        max_bathrooms=max_bathrooms,
        min_price=min_price,
        max_price=max_price,
        furnished=furnished,
        new_development_only=new_development_only,
        sort_by=sort_by,
        limit=limit,
    )


def rank_listings(
    listing_ids: list[int],
    price_weight: float = 0.5,
    commute_weight: float = 0.3,
    neighborhood_weight: float = 0.2,
    commute_summaries: list[dict] | None = None,
    neighborhood_summaries: list[dict] | None = None,
    max_commute_minutes: float = DEFAULT_MAX_COMMUTE_MINUTES,
) -> str:
    """Delegate ranking to Tool 2 using the current listing dataset."""
    return _rank_listings(
        listings,
        listing_ids=listing_ids,
        price_weight=price_weight,
        commute_weight=commute_weight,
        neighborhood_weight=neighborhood_weight,
        commute_summaries=commute_summaries,
        neighborhood_summaries=neighborhood_summaries,
        max_commute_minutes=max_commute_minutes,
    )


def check_neighborhood_fit(listing_id: int, wants: list[str] | None = None, avoids: list[str] | None = None) -> str:
    """Delegate neighborhood checks to Tool 5 using the current listing dataset."""
    return _check_neighborhood_fit(listings, listing_id, wants=wants, avoids=avoids)


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_listings",
            "description": (
                "Search the rental candidate pool when the user wants apartments or asks "
                "for recommendations. Apply only constraints the user stated; do not invent "
                "a neighborhood, budget, bedroom count, furnishing choice, or new-development "
                "preference. Price means monthly effective rent. The results include listing_id "
                "values for follow-up tools, listing URLs, and an inventory freshness warning."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "borough": {
                        "type": "string",
                        "description": (
                            "Broad location such as Manhattan, Brooklyn, Queens, Bronx, "
                            "Staten Island, NYC, or New Jersey. Common abbreviations are accepted."
                        ),
                    },
                    "neighborhood": {
                        "type": "string",
                        "description": (
                            "One neighborhood requested by the user, such as Williamsburg, "
                            "Chelsea, Bed-Stuy, UES, or Jersey City. Matching is case-insensitive "
                            "and can map common names to official dataset areas."
                        ),
                    },
                    "min_bedrooms": {
                        "type": "number",
                        "minimum": 0,
                        "description": "Minimum bedrooms. A studio is 0 bedrooms.",
                    },
                    "max_bedrooms": {
                        "type": "number",
                        "minimum": 0,
                        "description": "Maximum bedrooms. Use 0 when the user specifically wants a studio.",
                    },
                    "min_bathrooms": {
                        "type": "number",
                        "minimum": 0,
                        "description": "Minimum full bathrooms, only when the user states this preference.",
                    },
                    "max_bathrooms": {
                        "type": "number",
                        "minimum": 0,
                        "description": "Maximum full bathrooms, only when the user states this preference.",
                    },
                    "min_price": {
                        "type": "number",
                        "minimum": 0,
                        "description": "Minimum monthly effective rent in US dollars.",
                    },
                    "max_price": {
                        "type": "number",
                        "minimum": 0,
                        "description": "Maximum monthly effective rent in US dollars.",
                    },
                    "furnished": {
                        "type": "boolean",
                        "description": (
                            "True for furnished-only or false for unfurnished-only. Omit when "
                            "the user has not stated a furnishing preference."
                        ),
                    },
                    "new_development_only": {
                        "type": "boolean",
                        "description": (
                            "True only if the user requires a new development; otherwise omit or false."
                        ),
                    },
                    "sort_by": {
                        "type": "string",
                        "enum": ["price_asc", "price_desc", "newest"],
                        "description": (
                            "How to order results. Use price_asc by default, price_desc only when "
                            "the user asks for the most expensive, and newest for recent listings."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_SEARCH_LIMIT,
                        "description": (
                            f"How many results to return; default {DEFAULT_SEARCH_LIMIT}, "
                            f"maximum {MAX_SEARCH_LIMIT}. If the user requests a "
                            "specific number, always pass that exact number."
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rank_listings",
            "description": (
                "Required whenever the user asks to rank, compare value, prioritize, "
                "choose between, or identify the best among 2-10 known rental listings. "
                "Do not manually rank listings outside this tool. It always uses "
                "market-relative effective rent. It can also use commute and neighborhood "
                "summaries, but only pass those summaries when they came from actual earlier "
                "commute_to or check_neighborhood_fit calls; never invent them. If optional "
                "summaries are missing for any candidate, that dimension is excluded for all "
                "candidates and reported in missing_signals."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "listing_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": MIN_RANK_LISTINGS,
                        "maxItems": MAX_RANK_LISTINGS,
                        "uniqueItems": True,
                        "description": "The shortlist of listing_id values from search_listings.",
                    },
                    "price_weight": {
                        "type": "number",
                        "minimum": 0,
                        "description": "Weight for market price value. Defaults to 0.5.",
                    },
                    "commute_weight": {
                        "type": "number",
                        "minimum": 0,
                        "description": "Weight for verified commute time. Defaults to 0.3.",
                    },
                    "neighborhood_weight": {
                        "type": "number",
                        "minimum": 0,
                        "description": "Weight for verified lifestyle fit. Defaults to 0.2.",
                    },
                    "commute_summaries": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "listing_id": {"type": "integer"},
                                "duration_min": {"type": "number", "minimum": 0},
                            },
                            "required": ["listing_id", "duration_min"],
                            "additionalProperties": False,
                        },
                        "description": (
                            "One verified duration for each candidate, copied from the travel "
                            "mode the user cares about in commute_to. Omit for price-only ranking."
                        ),
                    },
                    "neighborhood_summaries": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "listing_id": {"type": "integer"},
                                "wants_met": {"type": "number", "minimum": 0},
                                "wants_total": {"type": "number", "minimum": 0},
                                "dealbreakers_hit": {"type": "number", "minimum": 0},
                            },
                            "required": [
                                "listing_id",
                                "wants_met",
                                "wants_total",
                                "dealbreakers_hit",
                            ],
                            "additionalProperties": False,
                        },
                        "description": (
                            "Verified aggregate counts from check_neighborhood_fit for each "
                            "candidate. Omit when neighborhood fit was not checked."
                        ),
                    },
                    "max_commute_minutes": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "description": (
                            "User's maximum acceptable commute in minutes. Defaults to 60."
                        ),
                    },
                },
                "required": ["listing_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "commute_to",
            "description": "Get the commute from an apartment address to a specific user-provided destination, such as school or work. "
            "Returns minutes and miles for each travel mode (transit, walk, bicycle, drive); for transit it also returns the subway/bus lines, transfers, walking time and backup routes. "
            "Use only when the destination was stated by the user or unambiguously established earlier. Do not use this tool to find whether a listing is near a subway station or bus stop, and never invent a station as the destination; use check_neighborhood_fit for proximity to transit facilities. It handles one apartment per call, so to compare several apartments, call it once for each.",
            "parameters": {
                "type": "object",
                "properties": {
                    "origin": {
                        "type": "string",
                        "description": "Full street address of the apartment, e.g. '327 East 83rd Street, New York, NY'.",
                    },
                    "destination": {
                        "type": "string",
                        "description": "A destination explicitly provided by the user or already established in the conversation, e.g. 'Columbia University, New York, NY'. Never invent a station or destination.",
                    },
                    "modes": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(travel_modes)},
                        "description": "Travel modes to check. Leave out to check all of them; pass only the ones the user cares about, e.g. ['transit'] for 'by subway' or ['drive'] for 'I have a car'.",
                    },
                    "transit_preference": {
                        "type": "string",
                        "enum": list(preferences),
                        "description": "Transit only. Set it only when the user asks, e.g. 'subway only' or 'I don't want to transfer'.",
                    },
                    "departure_time": {
                        "type": "string",
                        "description": "When the user leaves, in RFC3339 with the New York offset, e.g. '2026-10-05T08:30:00-04:00'. Leave out to leave now.",
                    },
                    "arrive_by": {
                        "type": "string",
                        "description": "Transit only. When the user must arrive, same format as departure_time, e.g. for 'I need to be there by 9am'. Takes priority over departure_time.",
                    },
                },
                "required": ["origin", "destination"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_building_violations",
            "description": (
                "Check public NYC HPD Housing Maintenance Code violation records for one or more known rental listings. "
                "Use when the user asks about building violations, maintenance history, heat or hot-water problems, pests, mold,leaks, plumbing, or other HPD-recorded conditions."
                "Returns fixed 1-year, 3-year, and 5-year summaries by severity, status, and category, plus recent open Class B/C violations."
                "New Jersey listings are unsupported."
                "A result of no_public_records_or_address_match does not prove the building has no problems; it may also mean the address did not match HPD exactly. "
                "Pass all known listings in one batch instead of calling once per listing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "listing_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 1,
                        "maxItems": MAX_HPD_LISTINGS,
                        "uniqueItems": True,
                        "description": (
                            "One or more listing_id values returned by search_listings."
                        ),
                    },
                },
                "required": ["listing_ids"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_neighborhood_fit",
            "description": "Check a known listing against lifestyle wants and nearby dealbreakers the user explicitly stated. Use this tool, not commute_to, when the user asks whether a listing is near a subway station, train station, bus stop, or other local facility. For transit proximity, pass explicit wants such as ['subway station', 'bus stop']. Do not call any tool for a generic question such as 'How is the neighborhood?' until the user clarifies what matters, unless relevant explicit preferences are already established and still need evaluation. Never invent wants or avoids. Places they want are checked within walking distance (800 m) and dealbreakers right next door (150 m, about the same block). "
            "Returns the closest matching place and its distance for each item. "
            "Only call it after the user has told you about their lifestyle, and never invent wants or avoids. "
            "Each item is a Google Maps text search, so turn the user's words into short, specific English phrases for the kind of place that actually matters to them, "
            "e.g. 'I have a dog' -> wants ['dog run']; 'I work out' -> wants ['fitness gym']; 'I'm a light sleeper' -> avoids ['nightclub', 'fire station']"
            "(a loud club, not a quiet cocktail bar). Translate non-English requests into English. "
            "It handles one listing per call, so to compare several listings, call it once for each.",
            "parameters": {
                "type": "object",
                "properties": {
                    "listing_id": {
                        "type": "integer",
                        "description": "The id of a listing from search results, e.g. 5096445.",
                    },
                    "wants": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Places the user wants within walking distance, as short, specific English search phrases, e.g. ['fitness gym', 'laundromat', 'dog run', 'Trader Joe's'].",
                    },
                    "avoids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Places the user does not want right next door, as short, specific English search phrases for what actually bothers them, e.g. ['nightclub', 'fire station'] for noise.",
                    },
                },
                "required": ["listing_id"],
            },
        },
    },
]

# What the harness runs: tool name -> Python function.
TOOL_MAP = {
    "search_listings": search_listings,
    "rank_listings": rank_listings,
    "commute_to": commute_to,
    "check_building_violations": check_building_violations,
    "check_neighborhood_fit": check_neighborhood_fit,
}


def run_tool(name: str, args: dict) -> str:
    """Run one tool call. Models invent tool names and arguments; never let that crash the loop."""
    if name not in TOOL_MAP:
        return json.dumps({"error": f"Unknown tool '{name}'. Available: {list(TOOL_MAP)}"})
    try:
        return TOOL_MAP[name](**args)
    except TypeError as e:
        return json.dumps({"error": f"Bad arguments for {name}: {e}"})
