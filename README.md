# NYC Rental Agent: `commute_to` and `check_neighborhood_fit`

Both tools live in `tools.py` and share the `GOOGLE_MAPS_API_KEY` environment variable. The Google Cloud project needs **Routes API** and **Places API (New)** enabled. To run locally, put `GOOGLE_MAPS_API_KEY=...` in `.env`, then run `uv run --env-file .env python app.py`.

## Tool 3: `commute_to`

Gets commute options and times from an address to a destination, using the Google Routes API.

### Parameters

| Parameter | Required | Description |
| --- | --- | --- |
| `origin` | Yes | Full listing address, e.g. `610 West 150th Street, New York, NY` |
| `destination` | Yes | Place name or address, e.g. `Columbia University, New York, NY` |
| `modes` | No | List of `transit` / `walk` / `bicycle` / `drive`; defaults to all four |
| `transit_preference` | No | `subway` / `bus` / `less_walking` / `fewer_transfers`; transit only |
| `departure_time` | No | RFC3339, e.g. `2026-10-05T08:30:00-04:00`; defaults to now |
| `arrive_by` | No | Latest arrival time, same format; transit only; takes priority if both times are given |

### Logic

1. If `modes` or `transit_preference` is given, validate it and return an error listing the valid values if it is invalid. If omitted, `modes` defaults to all four and no transit preference is applied.
2. Send one Routes API request per mode, all in parallel.
3. Each transit route is condensed to: duration, distance, transfers, walking minutes, first boarding time, **actual arrival time**, and a one-line route description. Consecutive walking steps are merged.
4. Transit routes are sorted by **arrival time**, earliest first, since Google's `duration` excludes waiting time. With `arrive_by`, the latest arrival comes first so the user can leave as late as possible. Routes that differ only by departure are deduplicated, with up to 2 alternatives.
5. When two or more modes return routes, `fastest_mode` is included.

Errors: invalid parameters; no route for any mode (ask the user for a fuller address, never make up times); API request failure.

### Example output

`commute_to("610 West 150th Street, New York, NY", "Columbia University, New York, NY", modes=["transit", "bicycle"])`

```json
{
  "origin": "610 West 150th Street, New York, NY",
  "destination": "Columbia University, New York, NY",
  "options": [
    {
      "mode": "transit",
      "duration_min": 19,
      "distance_mi": 1.7,
      "transfers": 0,
      "walking_min": 4,
      "first_boarding": "4:12 PM",
      "route": "Walk 2 min -> M4 bus (Broadway/W 150 St to Broadway/W 120 St, 12 stops) -> Walk 2 min",
      "arrive_at": "4:30 PM",
      "alternatives": [
        {
          "duration_min": 26, "transfers": 1, "walking_min": 11, "first_boarding": "4:14 PM", "arrive_at": "4:33 PM",
          "route": "Walk 7 min -> 1 Line subway (145 St to 96 St, 2 stops) -> Walk 1 min -> 1 Line subway (96 St to 116 St - Columbia University, 3 stops) -> Walk 2 min"
        }
      ]
    },
    {"mode": "bicycle", "duration_min": 14, "distance_mi": 1.8, "alternatives": []}
  ],
  "fastest_mode": "bicycle"
}
```

## Tool 5: `check_neighborhood_fit`

Checks whether a listing's surroundings match the user's lifestyle: places they want nearby (wants) and places they don't want next door (avoids). Listing coordinates come from `data/nyc_rental_listings_clean.csv`; nearby places come from Google Places Text Search.

### Parameters

| Parameter | Required | Description |
| --- | --- | --- |
| `listing_id` | Yes | The CSV `id`, from search results |
| `wants` | No | Free-text places to have nearby, e.g. `["fitness gym", "laundromat", "dog run", "Trader Joe's"]` |
| `avoids` | No | Free-text dealbreakers, e.g. `["nightclub", "fire station"]` |

Both are optional, but at least one is required. Specific English terms work best, e.g. `fitness gym` instead of `gym`, `nightclub` instead of `bar` (a quiet bar isn't noisy).

### Logic

1. If both `wants` and `avoids` are empty, return an error asking the agent to ask about the user's lifestyle first.
2. Look up the listing's coordinates by `listing_id`; return an error if not found.
3. Run one text search per item around the listing, all in parallel, compute distances from coordinates, and keep the nearest result.
4. A want is met within **800 m** (~10 min walk); an avoid is hit only within **150 m** (about the same block). The nearest place is returned even when out of range, so the agent can say how far the closest one is.
5. Return per-item results plus `wants_met`, `wants_total`, and `dealbreakers_hit` for ranking.

Errors: no preferences; unknown `listing_id`; API request failure.

### Example output

`check_neighborhood_fit(5096445, wants=["gym", "laundromat", "dog park"], avoids=["bar", "fire station"])`

```json
{
  "listing": "610 West 150th Street #2H, Manhattan",
  "wants": [
    {"want": "gym", "met": true, "nearest": {"name": "Parco Harlem Gym", "distance": "295 meters"}},
    {"want": "laundromat", "met": true, "nearest": {"name": "Miss Bubble Laundromat", "distance": "70 meters"}},
    {"want": "dog park", "met": true, "nearest": {"name": "142nd Street Dog Run 🐕", "distance": "633 meters"}}
  ],
  "avoids": [
    {"avoid": "bar", "hit": true, "nearest": {"name": "Uptown Bourbon", "distance": "54 meters"}},
    {"avoid": "fire station", "hit": false, "nearest": {"name": "FDNY Engine 80/Ladder 23", "distance": "888 meters"}}
  ],
  "wants_met": 3,
  "wants_total": 3,
  "dealbreakers_hit": 1
}
```

## Agent prompt design notes for `app.py`

Tool usage is documented in the `TOOLS` descriptions in `tools.py`. The system prompt could cover:

- **Search results include the full address and `id`**: `commute_to` uses the address, `check_neighborhood_fit` uses the `id`. When the user says "the second one", the model takes the value from the conversation history.
- **Include the current NYC time**: so the model can fill in the right date and time zone for `arrive_by` when the user says "I need to be there by 9 tomorrow".
- **Ask about lifestyle first**: if the user hasn't mentioned it, ask once; don't ask again. With no preferences, skip `check_neighborhood_fit` and ignore surroundings in ranking. `wants` and `avoids` must come from what the user said, never made up, and carry through the conversation.
- **Ranking fields**: commute time, `wants_met`, and `dealbreakers_hit`. Listings with dealbreakers rank lower.
- **Explain results in natural language**: mention routes, arrival times, place names and distances, tied back to the user's habits; note that driving excludes parking time; don't show raw counts or invent times or places the tool didn't return.
