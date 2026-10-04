"""The tools the harness can run, and the JSON that describes them to the model."""

import json
import math
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests



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



# --- Tool: check_neighborhood_fit ---

# Google Places Text Search: find places near the apartment from a plain phrase like "dog park".
PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
want_radius = 800   # meters, about a 10-minute walk
avoid_radius = 150  # meters, about the same block


listings = pd.read_csv(
    Path(__file__).parent / "data" / "nyc_rental_listings_clean.csv",
    dtype={"zip_code": "string"},
).set_index("id")


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
TOOL_MAP = {"commute_to": commute_to, "check_neighborhood_fit": check_neighborhood_fit}


def run_tool(name: str, args: dict) -> str:
    """Run one tool call. Models invent tool names and arguments; never let that crash the loop."""
    if name not in TOOL_MAP:
        return json.dumps({"error": f"Unknown tool '{name}'. Available: {list(TOOL_MAP)}"})
    try:
        return TOOL_MAP[name](**args)
    except TypeError as e:
        return json.dumps({"error": f"Bad arguments for {name}: {e}"})
