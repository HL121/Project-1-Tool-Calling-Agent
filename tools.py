"""The tools the harness can run, and the JSON that describes them to the model."""

import json
import os
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

    # Walk through the steps in order and build a readable route, e.g.
    # "Walk 5 min -> Q subway (86 St to 72 St, 2 stops) -> 1 subway (...) -> Walk 4 min".
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
    return summary


def commute_to(origin: str, destination: str,
               modes: list[str] | None = None,
               transit_preference: str | None = None,
               departure_time: str | None = None,
               arrive_by: str | None = None) -> str:
    """Estimate the commute from an apartment to a destination, by one or more travel modes."""
    # Error 1: bad argument values. Say what is allowed so the model can fix the call.
    modes = modes or ["transit"]
    if any(mode not in travel_modes for mode in modes) or (transit_preference and transit_preference not in preferences):
        return json.dumps({"error": f"modes must be from {list(travel_modes)}; "
                                    f"transit_preference must be one of {list(preferences)} or left out."})

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": os.environ.get("GOOGLE_MAPS_API_KEY"),
        "X-Goog-FieldMask": fields,
    }

    options = []
    try:
        # One request per mode, since Google takes one travelMode per request.
        for mode in modes:
            request = format_request(origin, destination, mode, transit_preference, departure_time, arrive_by)
            response = requests.post(ROUTES_URL, json=request, headers=headers, timeout=10)
            response.raise_for_status()  # turn a 4xx/5xx from Google into a RequestException

            routes = response.json().get("routes", [])
            if not routes:
                # This mode cannot make the trip, but another mode still might, so keep going.
                options.append({"mode": mode, "note": f"Google Maps has no {mode} route for this trip."})
                continue

            # Fastest route first; keep up to 2 others as backups.
            cleaned = sorted((clean_route_info(route) for route in routes), key=lambda r: r["duration_min"])
            options.append({"mode": mode, **cleaned[0], "alternatives": cleaned[1:3]})
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

    return json.dumps({
        "origin": origin,
        "destination": destination,
        "options": options,
        "fastest_mode": min(found, key=lambda o: o["duration_min"])["mode"],
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
                        "description": "Travel modes to check. Leave out to check transit only; pass several when the user wants to compare ways to get there.",
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
]

# What the harness runs: tool name -> Python function.
TOOL_MAP = {"commute_to": commute_to}


def run_tool(name: str, args: dict) -> str:
    """Run one tool call. Models invent tool names and arguments; never let that crash the loop."""
    if name not in TOOL_MAP:
        return json.dumps({"error": f"Unknown tool '{name}'. Available: {list(TOOL_MAP)}"})
    try:
        return TOOL_MAP[name](**args)
    except TypeError as e:
        return json.dumps({"error": f"Bad arguments for {name}: {e}"})
