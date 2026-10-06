"""Implementation of the search_listings tool."""

from __future__ import annotations

import json
import math
import re
from difflib import get_close_matches

import pandas as pd

DEFAULT_SEARCH_LIMIT = 5
MAX_SEARCH_LIMIT = 20
SEARCH_SORTS = {"price_asc", "price_desc", "newest"}

NYC_BOROUGHS = {"manhattan", "brooklyn", "queens", "bronx", "staten island"}
BOROUGH_ALIASES = {
    "manhattan": {"manhattan"},
    "new york": {"manhattan"},
    "new york county": {"manhattan"},
    "mn": {"manhattan"},
    "brooklyn": {"brooklyn"},
    "kings": {"brooklyn"},
    "kings county": {"brooklyn"},
    "bk": {"brooklyn"},
    "queens": {"queens"},
    "queens county": {"queens"},
    "qn": {"queens"},
    "bronx": {"bronx"},
    "the bronx": {"bronx"},
    "bronx county": {"bronx"},
    "bx": {"bronx"},
    "staten island": {"staten island"},
    "richmond": {"staten island"},
    "richmond county": {"staten island"},
    "si": {"staten island"},
    "new jersey": {"new jersey"},
    "nj": {"new jersey"},
    "nyc": NYC_BOROUGHS,
    "new york city": NYC_BOROUGHS,
}

NEIGHBORHOOD_ALIASES = {
    "bed stuy": "bedford stuyvesant",
    "bedstuy": "bedford stuyvesant",
    "fidi": "financial district",
    "lic": "long island city",
    "ues": "upper east side",
    "uws": "upper west side",
    "wburg": "williamsburg",
}


def normalize_search_text(value: object) -> str:
    """Normalize a user-facing location without changing the stored display value."""
    if value is None or pd.isna(value):
        return ""
    return re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()


def search_error(message: str, suggestion: str) -> str:
    """Return an actionable error that the model can relay or repair."""
    return json.dumps({"ok": False, "error": message, "suggestion": suggestion})


def json_value(value: object) -> object:
    """Convert pandas/numpy scalars and missing values to strict JSON values."""
    if value is None or pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def listing_search_result(listing_id: object, row: pd.Series) -> dict:
    """Build a compact result and exclude large/internal dataset fields."""
    unit = json_value(row.get("unit"))
    street = json_value(row.get("street"))
    address = f"{street}, Unit {unit}" if unit else str(street)
    borough = json_value(row.get("borough"))
    state = json_value(row.get("state"))
    zip_code = json_value(row.get("zip_code"))
    # A complete, map-ready address for commute_to. Without the borough/town, state and ZIP, Google can't place
    # addresses like "1-10 56th Avenue". NJ listings use the town (their borough is just "New Jersey").
    place = json_value(row.get("neighborhood")) if state == "NJ" else borough
    state_zip = " ".join(str(part) for part in (state, zip_code) if part)
    full_address = ", ".join(str(part) for part in (address, place, state_zip) if part)

    asking_rent = json_value(row.get("price"))
    effective_rent = json_value(row.get("effective_rent"))
    net_effective_rent = json_value(row.get("net_effective_price"))
    if not net_effective_rent or net_effective_rent == asking_rent:
        net_effective_rent = None

    return {
        "listing_id": json_value(listing_id),
        "address": address,
        "full_address": full_address,
        "street": street,
        "unit": unit,
        "neighborhood": json_value(row.get("neighborhood")),
        "borough": borough,
        "state": state,
        "zip_code": zip_code,
        "bedrooms": json_value(row.get("bedrooms")),
        "bathrooms": json_value(row.get("bathrooms")),
        "effective_rent": effective_rent,
        "asking_rent": asking_rent,
        "net_effective_rent": net_effective_rent,
        "furnished": bool(row.get("furnished", False)),
        "is_new_development": bool(row.get("is_new_development", False)),
        "available_date": json_value(row.get("available_date")),
        "created_at_utc": json_value(row.get("created_at_utc")),
        "last_publish_month": json_value(row.get("last_publish_month")),
        "url": json_value(row.get("url")),
    }


def neighborhood_suggestions(frame: pd.DataFrame, query: str) -> list[str]:
    """Suggest stored neighborhood names close to a query within current filters."""
    names = sorted(frame["neighborhood"].dropna().astype(str).unique())
    normalized_to_name = {normalize_search_text(name): name for name in names}
    close_keys = get_close_matches(query, normalized_to_name, n=5, cutoff=0.35)
    return [normalized_to_name[key] for key in close_keys]


def search_listings(
    listings: pd.DataFrame,
    *,
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
    """Search the cleaned rental candidate pool using only stated constraints.

    Price filtering and ordering always use ``effective_rent``. Location input is
    case-insensitive and accepts common borough/neighborhood aliases. The return
    value is JSON text because the shared tool harness sends strings to Gemini.
    """
    numeric_args = {
        "min_bedrooms": min_bedrooms,
        "max_bedrooms": max_bedrooms,
        "min_bathrooms": min_bathrooms,
        "max_bathrooms": max_bathrooms,
        "min_price": min_price,
        "max_price": max_price,
    }
    for name, value in numeric_args.items():
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            return search_error(
                f"{name} must be a non-negative number.",
                f"Correct {name} or leave it out, then retry search_listings.",
            )

    if (
        min_bedrooms is not None
        and max_bedrooms is not None
        and min_bedrooms > max_bedrooms
    ):
        return search_error(
            "min_bedrooms cannot be greater than max_bedrooms.",
            "Correct the bedroom range and retry search_listings.",
        )
    if (
        min_bathrooms is not None
        and max_bathrooms is not None
        and min_bathrooms > max_bathrooms
    ):
        return search_error(
            "min_bathrooms cannot be greater than max_bathrooms.",
            "Correct the bathroom range and retry search_listings.",
        )
    if min_price is not None and max_price is not None and min_price > max_price:
        return search_error(
            "min_price cannot be greater than max_price.",
            "Correct the effective-rent range and retry search_listings.",
        )
    if furnished is not None and not isinstance(furnished, bool):
        return search_error(
            "furnished must be true, false, or omitted.",
            "Use true for furnished-only, false for unfurnished-only, or omit it.",
        )
    if not isinstance(new_development_only, bool):
        return search_error(
            "new_development_only must be true or false.",
            "Use true only when the user specifically asks for a new development.",
        )
    if sort_by not in SEARCH_SORTS:
        return search_error(
            f"sort_by must be one of {sorted(SEARCH_SORTS)}.",
            "Choose price_asc, price_desc, or newest.",
        )
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_SEARCH_LIMIT:
        return search_error(
            f"limit must be an integer from 1 to {MAX_SEARCH_LIMIT}.",
            f"Choose a result limit between 1 and {MAX_SEARCH_LIMIT}.",
        )
    if borough is not None and not isinstance(borough, str):
        return search_error(
            "borough must be a place name or omitted.",
            "Use a borough such as Brooklyn, Manhattan, NYC, or New Jersey.",
        )
    if neighborhood is not None and not isinstance(neighborhood, str):
        return search_error(
            "neighborhood must be a place name or omitted.",
            "Use one neighborhood name, such as Williamsburg or Chelsea.",
        )

    matches = listings
    applied_filters: dict[str, object] = {}

    if borough and borough.strip():
        borough_query = normalize_search_text(borough)
        if not borough_query:
            return search_error(
                "borough does not contain a searchable place name.",
                "Use a borough such as Brooklyn, Manhattan, NYC, or New Jersey.",
            )
        accepted_boroughs = BOROUGH_ALIASES.get(borough_query, {borough_query})
        borough_keys = matches["borough"].map(normalize_search_text)
        matches = matches[borough_keys.isin(accepted_boroughs)]
        applied_filters["borough"] = borough.strip()
        if matches.empty:
            available_boroughs = sorted(listings["borough"].dropna().astype(str).unique())
            key_to_name = {
                normalize_search_text(name): name for name in available_boroughs
            }
            suggestions = [
                key_to_name[key]
                for key in get_close_matches(borough_query, key_to_name, n=3, cutoff=0.35)
            ]
            return json.dumps(
                {
                    "ok": True,
                    "message": (
                        f"No borough in the candidate pool matched '{borough.strip()}'. "
                        "Ask the user whether one of the suggested locations is acceptable."
                    ),
                    "applied_filters": {},
                    "suggested_boroughs": suggestions,
                    "total_matches": 0,
                    "count": 0,
                    "results": [],
                }
            )

    matched_neighborhoods: list[str] = []
    if neighborhood and neighborhood.strip():
        original_query = neighborhood.strip()
        neighborhood_query = normalize_search_text(original_query)
        if not neighborhood_query:
            return search_error(
                "neighborhood does not contain a searchable place name.",
                "Use a neighborhood such as Williamsburg, Chelsea, or Jersey City.",
            )
        neighborhood_query = NEIGHBORHOOD_ALIASES.get(neighborhood_query, neighborhood_query)
        neighborhood_keys = matches["neighborhood"].map(normalize_search_text)

        exact_mask = neighborhood_keys == neighborhood_query
        if exact_mask.any():
            neighborhood_mask = exact_mask
        else:
            query_tokens = neighborhood_query.split()
            neighborhood_mask = neighborhood_keys.map(
                lambda value: all(token in value for token in query_tokens)
            )

        if not neighborhood_mask.any():
            suggestions = neighborhood_suggestions(matches, neighborhood_query)
            return json.dumps(
                {
                    "ok": True,
                    "message": (
                        f"No neighborhood in the candidate pool matched '{original_query}'. "
                        "Ask the user whether one of the suggested official area names is acceptable."
                    ),
                    "applied_filters": applied_filters,
                    "suggested_neighborhoods": suggestions,
                    "total_matches": 0,
                    "count": 0,
                    "results": [],
                }
            )

        matches = matches[neighborhood_mask]
        matched_neighborhoods = sorted(matches["neighborhood"].dropna().astype(str).unique())
        applied_filters["neighborhood"] = original_query

    if min_bedrooms is not None:
        matches = matches[matches["bedrooms"] >= min_bedrooms]
        applied_filters["min_bedrooms"] = min_bedrooms
    if max_bedrooms is not None:
        matches = matches[matches["bedrooms"] <= max_bedrooms]
        applied_filters["max_bedrooms"] = max_bedrooms
    if min_bathrooms is not None:
        matches = matches[matches["bathrooms"] >= min_bathrooms]
        applied_filters["min_bathrooms"] = min_bathrooms
    if max_bathrooms is not None:
        matches = matches[matches["bathrooms"] <= max_bathrooms]
        applied_filters["max_bathrooms"] = max_bathrooms
    if min_price is not None:
        matches = matches[matches["effective_rent"] >= min_price]
        applied_filters["min_price"] = min_price
    if max_price is not None:
        matches = matches[matches["effective_rent"] <= max_price]
        applied_filters["max_price"] = max_price
    if furnished is not None:
        matches = matches[matches["furnished"] == furnished]
        applied_filters["furnished"] = furnished
    if new_development_only:
        matches = matches[matches["is_new_development"]]
        applied_filters["new_development_only"] = True

    if sort_by == "newest":
        matches = matches.sort_values(
            ["created_at_utc", "effective_rent"],
            ascending=[False, True],
            na_position="last",
            kind="stable",
        )
    else:
        matches = matches.sort_values(
            ["effective_rent", "created_at_utc"],
            ascending=[sort_by == "price_asc", False],
            na_position="last",
            kind="stable",
        )

    total_matches = len(matches)
    selected = matches.head(limit)
    results = [listing_search_result(listing_id, row) for listing_id, row in selected.iterrows()]

    if not results:
        message = (
            "No listings matched every condition. Ask which constraint the user "
            "would prefer to relax, such as price, bedrooms, location, or amenities."
        )
    elif total_matches > len(results):
        message = f"Showing {len(results)} of {total_matches} matching listings."
    else:
        message = f"Found {total_matches} matching listing{'s' if total_matches != 1 else ''}."

    return json.dumps(
        {
            "ok": True,
            "message": message,
            "inventory_note": (
                "These are candidates from the June-August 2026 dataset, not a guarantee "
                "of current availability. Confirm the latest status at each listing URL."
            ),
            "applied_filters": applied_filters,
            "matched_neighborhoods": matched_neighborhoods,
            "sort_by": sort_by,
            "total_matches": total_matches,
            "count": len(results),
            "results": results,
        }
    )



