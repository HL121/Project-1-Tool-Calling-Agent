# Stoop — NYC Rental Agent

Stoop is a web-based rental assistant that helps users search for apartments,
compare candidates, check commutes and neighborhood preferences, review public
building records, and rank a shortlist. It combines a static June–August 2026
rental dataset with Google Maps Platform and NYC Open Data.

The listing dataset is a historical candidate pool, not live inventory. Users
should confirm current availability and lease terms through the listing links.

## Who it is for

Stoop is designed for students, workers, and other NYC-area renters who want to
compare more than rent alone. It is especially useful when commute time,
nearby amenities, neighborhood dealbreakers, and building maintenance history
matter to the decision.

## Tools

- `search_listings` searches approximately 70,000 NYC and New Jersey rental
  candidates by location, rent, bedrooms, bathrooms, furnishing, and new
  development status.
- `rank_listings` ranks 2–10 known listings using market-relative price value
  and, when available, verified commute and neighborhood-fit results.
- `commute_to` uses the Google Routes API to compare transit, walking,
  bicycling, and driving options from an apartment to a user-provided
  destination.
- `check_building_violations` uses NYC Open Data to summarize public HPD
  maintenance violations by recency, severity, status, and category.
- `check_neighborhood_fit` uses Google Places to check whether a listing is near
  amenities the user wants and away from explicitly stated dealbreakers.

## How to use it

Open the deployed Stoop website and enter a request in the chat using normal
language. Stoop shows every tool call and its arguments, remembers earlier
listings and preferences within the same session, and supports follow-ups such
as "rank those three" or "check the second one."

## Sample queries

Run Queries 1 and 2 in the same chat so Query 2 also demonstrates session
memory.

1. **Search, commute, and ranking**

   > I'm a student at Columbia University. Please help me find three apartments
   > under $3,000 per month in Morningside Heights, Manhattanville, or South
   > Harlem. My top priority is commute time, so please prioritize apartments
   > that are as close to Columbia University as possible.

2. **Building records, neighborhood evaluation, and session memory**

   > For these apartments, please look into the building and unit records,
   > including any reported issues or complaints. Also, tell me about the
   > surrounding community and neighborhood environment.

3. **Error handling**

   > I want to know the commute time from address: "AAA" to Columbia University,
   > and also the history records for this apartment.
