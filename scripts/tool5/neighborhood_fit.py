"""Implementation of the check_neighborhood_fit tool."""

from __future__ import annotations

import json
import math
import os
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests

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
def check_neighborhood_fit(listings: pd.DataFrame, listing_id: int, wants: list[str] | None = None, avoids: list[str] | None = None) -> str:
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



