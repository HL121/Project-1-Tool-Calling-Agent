"""The tools the harness can run, and the JSON that describes them to the model."""

from __future__ import annotations

import json
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from difflib import get_close_matches
from numbers import Real
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from scripts.tool4.building_violations import (
    check_building_violations as query_hpd_building_violations,
)


# --- Tool: commute_to ---

# Google Routes API, which is necessary to get map and transportation information.
ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"

# The mode names the model uses
travel_modes = {
    "transit": "TRANSIT",
    "walk": "WALK",
    "bicycle": "BICYCLE",
    "drive": "DRIVE",
}

# Define users transportation preferences.
preferences = {
    "subway": {"allowedTravelModes": ["SUBWAY"]}, # only take subway
    "bus": {"allowedTravelModes": ["BUS"]}, # only take bus
    "less_walking": {"routingPreference": "LESS_WALKING"}, # walk less
    "fewer_transfers": {"routingPreference": "FEWER_TRANSFERS"}, # less transit
}

# Ask Google for only the fields we summarize want
fields = ",".join([
    "routes.duration",
    "routes.distanceMeters", 
    "routes.legs.steps.travelMode", 
    "routes.legs.steps.staticDuration",
    "routes.legs.steps.transitDetails",
])


# Build a formatted request to the Google Routes API.
def format_request(origin: str, destination: str, mode: str,
                  transit_preference: str | None = None,
                  departure_time: str | None = None,
                  arrive_by: str | None = None) -> dict:
    request = {
        "origin": {"address": origin},
        "destination": {"address": destination},
        "travelMode": travel_modes[mode],
    }

    if mode == "transit":
        request["computeAlternativeRoutes"] = True # set to true in order to return multiple routes.
        if transit_preference:
            request["transitPreferences"] = preferences[transit_preference]
        if arrive_by:
            request["arrivalTime"] = arrive_by  

    if mode == "drive":
    # Use live traffic
        request["routingPreference"] = "TRAFFIC_AWARE"

    if departure_time and "arrivalTime" not in request:
        request["departureTime"] = departure_time

    return request

# Clean up the route information returned by the Google Routes API.
def clean_route_info(route: dict) -> dict:
    summary = {
        "duration_min": round(int(route["duration"].rstrip("s")) / 60),
        "distance_mi": round(route.get("distanceMeters", 0) / 1609.34, 1),
    }

    steps = [step for leg in route.get("legs", []) for step in leg.get("steps", [])]
    # Only the ride steps carry transitDetails (line, stops, departure time).
    rides = [step["transitDetails"] for step in steps if "transitDetails" in step]
    if not rides:
        # if walk/bicycle/drive, just return the summary with duration and distance.
        return summary

    # Walk through the steps in order and build a readable route.
    route_steps = []
    current_walk_time = 0  # seconds walked since the last ride
    total_walk_time = 0    # seconds walked over the whole trip
    for step in steps:
        ride = step.get("transitDetails")
        if not ride:
            # keep adding until the next ride starts.
            current_walk_time += int(step.get("staticDuration", "0s").rstrip("s"))
            continue
        # if a ride starts, so write out the walk before it as one "Walk N min".
        if current_walk_time:
            route_steps.append(f"Walk {round(current_walk_time / 60)} min")
            total_walk_time += current_walk_time
            current_walk_time = 0
        # Describe the ride: line name, vehicle type, where to get on and off, how many stops.
        transit_line = ride.get("transitLine", {})
        stop_info = ride.get("stopDetails", {})
        line_name = transit_line.get("nameShort") or transit_line.get("name", "Transit")  # some bus lines have no short name
        vehicle = transit_line.get("vehicle", {}).get("type", "").lower()
        route_steps.append(
            f"{line_name} {vehicle} ({stop_info.get('departureStop', {}).get('name', '?')} to "
            f"{stop_info.get('arrivalStop', {}).get('name', '?')}, {ride.get('stopCount', '?')} stops)"
        )
    # The walk after the last ride (to the destination) has no ride after it, append here.
    if current_walk_time:
        route_steps.append(f"Walk {round(current_walk_time / 60)} min")
        total_walk_time += current_walk_time

    summary.update({
        "transfers": len(rides) - 1,  # 1 ride = 0 transfers, 2 rides = 1 transfer
        "walking_min": round(total_walk_time / 60),
        # Local time of the first ride, e.g. "8:12 AM"; tells the user when to be at the stop.
        "first_boarding": rides[0].get("localizedValues", {}).get("departureTime", {}).get("time", {}).get("text"),
        "route": " -> ".join(route_steps),
    })

    # Google's duration skips the wait at the first stop, so also work out when the user actually arrives:
    # when the last ride gets in, plus the walk from that stop to the destination.
    last_arrival = rides[-1].get("stopDetails", {}).get("arrivalTime")  # UTC, e.g. "2026-10-04T20:28:33Z"
    if last_arrival:
        arrival = datetime.fromisoformat(last_arrival.replace("Z", "+00:00")) + timedelta(seconds=current_walk_time)
        summary["arrive_at"] = arrival.astimezone(ZoneInfo("America/New_York")).strftime("%I:%M %p").lstrip("0")
        summary["_arrival"] = arrival.isoformat()
    return summary


# estimate the commute from a given address to a destination
def commute_to(origin: str, destination: str,
               modes: list[str] | None = None,
               transit_preference: str | None = None,
               departure_time: str | None = None,
               arrive_by: str | None = None) -> str:
    # Error 1: bad argument values. Say what is allowed so the model can fix the call.
    modes = modes or list(travel_modes)  # no modes given -> check all of them, the user may bike, ride or drive
    if any(mode not in travel_modes for mode in modes) or (transit_preference and transit_preference not in preferences):
        return json.dumps({"error": f"modes must be from {list(travel_modes)}; "
                                    f"transit_preference must be one of {list(preferences)} or left out."})

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": os.environ.get("GOOGLE_MAPS_API_KEY"),
        "X-Goog-FieldMask": fields,
    }

    # Check one travel mode and return its best route (plus backups for transit).
    def check_mode(mode: str) -> dict:
        request = format_request(origin, destination, mode, transit_preference, departure_time, arrive_by)
        response = requests.post(ROUTES_URL, json=request, headers=headers, timeout=10)
        response.raise_for_status()  # turn a 4xx/5xx from Google into a RequestException

        routes = response.json().get("routes", [])
        if not routes:
            # This mode cannot make the trip, but another mode still might.
            return {"mode": mode, "note": f"Google Maps has no {mode} route for this trip."}

        # Earliest arrival first (a short ride that leaves in 30 min is not the best option).
        # With arrive_by, latest arrival first instead, so the user can leave as late as possible.
        # Walk/bike/drive have no arrival time, so they fall back to duration.
        cleaned = sorted((clean_route_info(route) for route in routes),
                         key=lambda r: (r.get("_arrival", ""), r["duration_min"]),
                         reverse=bool(arrive_by))
        # The same line at a later time is not a real alternative, so keep each route once.
        unique = []
        for route in cleaned:
            route.pop("_arrival", None)
            if route.get("route") not in [u.get("route") for u in unique]:
                unique.append(route)
        return {"mode": mode, **unique[0], "alternatives": unique[1:3]}

    try:
        # One request per mode, since Google takes one travelMode per request; send them at the same time.
        with ThreadPoolExecutor() as pool:
            options = list(pool.map(check_mode, modes))
    except requests.RequestException as e:
        # Error 3: the model cannot see an exception. Return something it can reason about.
        return json.dumps({"error": f"Google Routes API failed: {e}"})

    # Error 2: no mode found a route, so the address itself is most likely the problem.
    found = [option for option in options if "duration_min" in option]
    if not found:
        return json.dumps({"error": f"Google Maps could not find a route from '{origin}' to '{destination}'. "
                                    "If the address looks incomplete, ask the user for the street number, street "
                                    "and borough. If it already looks complete, tell the user this place cannot "
                                    "be found on the map right now; do not guess a commute time."})

    result = {"origin": origin, "destination": destination, "options": options}
    # Only worth naming the fastest mode when there is more than one to compare.
    if len(found) > 1:
        result["fastest_mode"] = min(found, key=lambda o: o["duration_min"])["mode"]
    return json.dumps(result)



listings = pd.read_csv(
    Path(__file__).parent / "data" / "nyc_rental_listings_clean.csv",
    dtype={"zip_code": "string"},
    parse_dates=["created_at_utc", "available_date"],
).set_index("id")


# --- Tool: check_building_violations ---

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


# --- Tool: search_listings ---

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

    asking_rent = json_value(row.get("price"))
    effective_rent = json_value(row.get("effective_rent"))
    net_effective_rent = json_value(row.get("net_effective_price"))
    if not net_effective_rent or net_effective_rent == asking_rent:
        net_effective_rent = None

    return {
        "listing_id": json_value(listing_id),
        "address": address,
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


# --- Tool: rank_listings ---

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


def price_value_metrics(listing_id: int, ranked_ids: set[int]) -> dict | None:
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
        listing_id: price_value_metrics(listing_id, ranked_id_set)
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


# --- Tool: check_neighborhood_fit ---

# Google Places Text Search: find places near the apartment from a plain phrase like "dog park".
PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
want_radius = 800   # meters, about a 10-minute walk
avoid_radius = 150  # meters, about the same block


# distance in meters between two points.
def distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> int:
    lat1, lng1, lat2, lng2 = map(math.radians, [lat1, lng1, lat2, lng2])
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return round(6371000 * 2 * math.asin(math.sqrt(a)))


# Find the closest place based on text searching.
def find_nearest(query: str, latitude: float, longitude: float) -> dict | None:
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": os.environ.get("GOOGLE_MAPS_API_KEY"),
        "X-Goog-FieldMask": "places.displayName,places.location",
    }
    request = {
        "textQuery": query,
        "locationBias": {"circle": {"center": {"latitude": latitude, "longitude": longitude}, "radius": want_radius}},
        "rankPreference": "DISTANCE",
        "pageSize": 5,
    }
    response = requests.post(PLACES_URL, json=request, headers=headers, timeout=10)
    response.raise_for_status()
    places = [
        {
            "name": place.get("displayName", {}).get("text", query),
            "distance": f"{distance_m(latitude, longitude, place['location']['latitude'], place['location']['longitude'])} meters",
        }
        for place in response.json().get("places", [])
    ]

    # Return the closest place found, or None if no places matched.
    return min(places, key=lambda p: int(p["distance"].split()[0]), default=None)


# Check whether the area around a listing has what the user wants and none of their dealbreakers.
def check_neighborhood_fit(listing_id: int, wants: list[str] | None = None, avoids: list[str] | None = None) -> str:
    # Error 1: nothing to check. The model should ask about the user's lifestyle instead of guessing.
    wants, avoids = wants or [], avoids or []
    if not wants and not avoids:
        return json.dumps({"error": "No wants or avoids given. Ask the user about their lifestyle or daily preference first, e.g. pets, gym, or noise."})

    # Error 2: the listing is not in our data.
    try:
        listing = listings.loc[int(listing_id)]
    except (KeyError, ValueError):
        return json.dumps({"error": f"Listing {listing_id} was not found. Use a valid id from search_listings results."})

    # Search every phrase at the same time instead of one by one.
    try:
        with ThreadPoolExecutor() as pool:
            nearest = list(pool.map(lambda q: find_nearest(q, listing["latitude"], listing["longitude"]), wants + avoids))
    except requests.RequestException as e:
        # Error 3: the model cannot see an exception. Return something it can reason about.
        return json.dumps({"error": f"Google Places API failed: {e}"})

    # A want is met within want_radius; a dealbreaker is hit only when it is really close (avoid_radius).
    want_results = [
        {"want": query, "met": bool(place and int(place["distance"].split()[0]) <= want_radius), "nearest": place}
        for query, place in zip(wants, nearest[:len(wants)])
    ]
    avoid_results = [
        {"avoid": query, "hit": bool(place and int(place["distance"].split()[0]) <= avoid_radius), "nearest": place}
        for query, place in zip(avoids, nearest[len(wants):])
    ]

    unit = f" #{listing['unit']}" if pd.notna(listing["unit"]) else ""
    return json.dumps({
        "listing": f"{listing['street']}{unit}, {listing['borough']}",
        "wants": want_results,
        "avoids": avoid_results,
        # Two numbers the ranking can use directly.
        "wants_met": sum(r["met"] for r in want_results),
        "wants_total": len(wants),
        "dealbreakers_hit": sum(r["hit"] for r in avoid_results),
    })


# What the model sees: the "set notes" in the screenplay.
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
                        "description": f"How many results to return; default {DEFAULT_SEARCH_LIMIT}, maximum {MAX_SEARCH_LIMIT}.",
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
                "Rank 2-10 known rental listings with an explainable score. Always uses "
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
            "description": "Get the commute from an apartment address to a place the user goes often, like school or work. "
            "Returns minutes and miles for each travel mode (transit, walk, bicycle, drive); for transit it also returns the subway/bus lines, transfers, walking time and backup routes. "
            "Use it when the user asks how long or how to get somewhere from an apartment. It handles one apartment per call, so to compare several apartments, call it once for each.",
            "parameters": {
                "type": "object",
                "properties": {
                    "origin": {
                        "type": "string",
                        "description": "Full street address of the apartment, e.g. '327 East 83rd Street, New York, NY'.",
                    },
                    "destination": {
                        "type": "string",
                        "description": "Where the user commutes to, as a place name or address, e.g. 'Columbia University, New York, NY'.",
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
            "description": "Check whether the area around one listing fits the user's lifestyle: places they want within walking distance (800 m) and dealbreakers right next door (150 m, about the same block). "
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
