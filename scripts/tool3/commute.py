"""Implementation of the commute_to tool."""

from __future__ import annotations

import json
import math
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests


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
        cleaned = [clean_route_info(route) for route in routes]
        if mode == "transit":
            # Google sometimes offers "just walk" as a transit route; keep it only if there is no bus or subway option
            rides = [r for r in cleaned if "transfers" in r]
            cleaned = rides or cleaned
        cleaned = sorted(cleaned, key=lambda r: (r.get("_arrival", ""), r["duration_min"]), reverse=bool(arrive_by))
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




