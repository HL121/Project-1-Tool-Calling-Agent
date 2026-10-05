#!/usr/bin/env python3
"""Batch lookup and summarization of NYC HPD housing-maintenance violations."""

from __future__ import annotations

import json
import re
import socket
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LISTINGS_PATH = PROJECT_ROOT / "data/nyc_rental_listings_clean.csv"
DEFAULT_CATEGORY_RULES_PATH = PROJECT_ROOT / "data/hpd_analysis/hpd_violation_category_rules.json"
HPD_ENDPOINT = "https://data.cityofnewyork.us/resource/wvxf-dwi5.json"
MAX_LISTINGS_PER_REQUEST = 50
HPD_PAGE_SIZE = 1000
HPD_FIELDS = (
    "violationid,buildingid,boro,housenumber,streetname,zip,apartment,story,"
    "class,inspectiondate,approveddate,currentstatus,currentstatusdate,novdescription"
)

BOROUGH_NAMES = {
    "bronx": "BRONX",
    "brooklyn": "BROOKLYN",
    "manhattan": "MANHATTAN",
    "queens": "QUEENS",
    "staten island": "STATEN ISLAND",
}
HPD_DIRECTIONALS = {"N": "NORTH", "S": "SOUTH", "E": "EAST", "W": "WEST"}
HPD_STREET_SUFFIXES = {
    "AVE": "AVENUE",
    "BLVD": "BOULEVARD",
    "CIR": "CIRCLE",
    "CT": "COURT",
    "DR": "DRIVE",
    "EXPY": "EXPRESSWAY",
    "HWY": "HIGHWAY",
    "LN": "LANE",
    "PKWY": "PARKWAY",
    "PL": "PLACE",
    "PLZ": "PLAZA",
    "RD": "ROAD",
    "ST": "STREET",
    "TER": "TERRACE",
    "TRL": "TRAIL",
    "TPKE": "TURNPIKE",
}
CLOSED_STATUSES = {"VIOLATION CLOSED", "VIOLATION DISMISSED"}
RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}
HPD_MAX_ATTEMPTS = 3
HPD_RETRY_BACKOFF_SECONDS = 0.5
SUMMARY_WINDOWS_YEARS = (1, 3, 5)


class HPDRequestError(RuntimeError):
    """A normalized per-building HPD request failure."""

    def __init__(
        self,
        message: str,
        *,
        error_type: str,
        retryable: bool,
        attempts: int,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable
        self.attempts = attempts


def _load_category_patterns(path: Path = DEFAULT_CATEGORY_RULES_PATH) -> list[tuple[str, list[re.Pattern]]]:
    with path.open(encoding="utf-8") as handle:
        document = json.load(handle)
    return [
        (category["name"], [re.compile(pattern, re.IGNORECASE) for pattern in category["patterns"]])
        for category in document["categories"]
    ]


CATEGORY_PATTERNS = _load_category_patterns()


def get_listing_addresses(
    listing_ids: list[int],
    listings_path: str | Path = DEFAULT_LISTINGS_PATH,
) -> list[dict]:
    """Load the listing index once and return one result for each requested ID."""
    if not isinstance(listing_ids, list) or not listing_ids:
        raise ValueError("listing_ids must be a non-empty list")
    if len(listing_ids) > MAX_LISTINGS_PER_REQUEST:
        raise ValueError(f"A maximum of {MAX_LISTINGS_PER_REQUEST} listing IDs is allowed")

    normalized_ids: list[int] = []
    for value in listing_ids:
        if isinstance(value, bool):
            raise ValueError("listing_ids must contain integers")
        try:
            normalized_ids.append(int(value))
        except (TypeError, ValueError) as exc:
            raise ValueError("listing_ids must contain integers") from exc

    columns = ["id", "street", "normalized_address", "zip_code", "borough", "state"]
    frame = pd.read_csv(listings_path, dtype={"zip_code": "string"}, usecols=columns)
    matches = frame.loc[frame["id"].isin(set(normalized_ids))]
    by_id = {int(row["id"]): row for _, row in matches.iterrows()}

    results = []
    for listing_id in normalized_ids:
        row = by_id.get(listing_id)
        if row is None:
            results.append({"listing_id": listing_id, "status": "listing_not_found"})
            continue
        results.append(
            {
                "listing_id": listing_id,
                "status": "ready",
                "street": str(row["street"]).strip(),
                "normalized_address": str(row["normalized_address"]).strip(),
                "zip_code": str(row["zip_code"]).strip(),
                "borough": str(row["borough"]).strip(),
                "state": str(row["state"]).strip().upper(),
            }
        )
    return results


def normalize_hpd_addresses(listings: list[dict]) -> list[dict]:
    """Split normalized_address and lightly adapt only the HPD query fields."""
    results = []
    for listing in listings:
        item = dict(listing)
        if item.get("status") != "ready":
            results.append(item)
            continue
        if item.get("state") != "NY" or item.get("borough", "").lower() not in BOROUGH_NAMES:
            item.update(status="unsupported_location", error="HPD covers New York City only")
            results.append(item)
            continue

        normalized_address = item.get("normalized_address", "")
        match = re.fullmatch(
            r"(\d+[A-Z]?(?:-\d+[A-Z]?)?)\s+(.+)", normalized_address
        )
        if not match:
            item.update(
                status="address_parse_error",
                error="Could not split normalized_address into house number and street name",
            )
            results.append(item)
            continue

        house_number, normalized_street_name = match.groups()
        words = normalized_street_name.split()
        words = [re.sub(r"(?<=\d)(?:ST|ND|RD|TH)$", "", word) for word in words]
        if words and words[0] in HPD_DIRECTIONALS:
            words[0] = HPD_DIRECTIONALS[words[0]]
        if words:
            words[-1] = HPD_STREET_SUFFIXES.get(words[-1], words[-1])
        street_name = " ".join(words)
        borough = BOROUGH_NAMES[item["borough"].lower()]
        zip_code = item["zip_code"].zfill(5)
        building_key = "|".join((item["state"], zip_code, normalized_address))
        item.update(
            status="normalized",
            house_number=house_number,
            street_name=street_name,
            borough=borough,
            zip_code=zip_code,
            building_key=building_key,
        )
        results.append(item)
    return results


def _soql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _request_hpd_page(
    request: Request,
    timeout: float,
    *,
    max_attempts: int = HPD_MAX_ATTEMPTS,
    backoff_seconds: float = HPD_RETRY_BACKOFF_SECONDS,
) -> list[dict]:
    """Fetch one page with bounded retries for transient failures."""
    for attempt in range(1, max_attempts + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                page = json.load(response)
            if not isinstance(page, list):
                raise HPDRequestError(
                    "HPD API returned an unexpected response",
                    error_type="invalid_response",
                    retryable=False,
                    attempts=attempt,
                )
            return page
        except HTTPError as exc:
            retryable = exc.code in RETRYABLE_HTTP_STATUSES
            if retryable and attempt < max_attempts:
                time.sleep(backoff_seconds * (2 ** (attempt - 1)))
                continue
            raise HPDRequestError(
                f"HPD API returned HTTP {exc.code}",
                error_type=f"http_{exc.code}",
                retryable=retryable,
                attempts=attempt,
            ) from exc
        except (socket.timeout, TimeoutError) as exc:
            if attempt < max_attempts:
                time.sleep(backoff_seconds * (2 ** (attempt - 1)))
                continue
            raise HPDRequestError(
                "HPD API request timed out",
                error_type="timeout",
                retryable=True,
                attempts=attempt,
            ) from exc
        except URLError as exc:
            if attempt < max_attempts:
                time.sleep(backoff_seconds * (2 ** (attempt - 1)))
                continue
            raise HPDRequestError(
                f"HPD API request failed: {exc.reason}",
                error_type="network_error",
                retryable=True,
                attempts=attempt,
            ) from exc
        except json.JSONDecodeError as exc:
            raise HPDRequestError(
                "HPD API returned invalid JSON",
                error_type="invalid_json",
                retryable=False,
                attempts=attempt,
            ) from exc
        except OSError as exc:
            if attempt < max_attempts:
                time.sleep(backoff_seconds * (2 ** (attempt - 1)))
                continue
            raise HPDRequestError(
                f"HPD API request failed: {exc}",
                error_type="network_error",
                retryable=True,
                attempts=attempt,
            ) from exc
    raise AssertionError("unreachable")


def _fetch_all_pages(address: dict, endpoint: str, timeout: float) -> list[dict]:
    where = " AND ".join(
        (
            f"boro={_soql_literal(address['borough'])}",
            f"zip={_soql_literal(address['zip_code'])}",
            f"housenumber={_soql_literal(address['house_number'])}",
            f"streetname={_soql_literal(address['street_name'])}",
        )
    )
    records: list[dict] = []
    offset = 0
    while True:
        params = {
            "$select": HPD_FIELDS,
            "$where": where,
            "$order": "violationid",
            "$limit": HPD_PAGE_SIZE,
            "$offset": offset,
        }
        request = Request(
            f"{endpoint}?{urlencode(params)}",
            headers={"Accept": "application/json", "User-Agent": "nyc-rental-agent/1.0"},
        )
        page = _request_hpd_page(request, timeout)
        records.extend(page)
        if len(page) < HPD_PAGE_SIZE:
            return records
        offset += HPD_PAGE_SIZE


def fetch_hpd_violations_batch(
    addresses: list[dict],
    *,
    endpoint: str = HPD_ENDPOINT,
    timeout: float = 15,
    max_workers: int = 5,
) -> dict[str, dict]:
    """Fetch each unique normalized building once; isolate per-building errors."""
    unique = {
        item["building_key"]: item
        for item in addresses
        if item.get("status") == "normalized"
    }
    results: dict[str, dict] = {}
    if not unique:
        return results

    workers = max(1, min(max_workers, len(unique)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_fetch_all_pages, address, endpoint, timeout): key
            for key, address in unique.items()
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                records = future.result()
                building_ids = sorted({r.get("buildingid") for r in records if r.get("buildingid")})
                results[key] = {
                    "status": "success" if records else "no_public_records_or_address_match",
                    "building_ids": building_ids,
                    "records": records,
                }
            except HPDRequestError as exc:
                results[key] = {
                    "status": "api_error",
                    "error": str(exc),
                    "error_type": exc.error_type,
                    "retryable": exc.retryable,
                    "attempts": exc.attempts,
                    "records": [],
                }
            except Exception as exc:
                # Preserve the rest of the batch even if one worker has an
                # unexpected implementation or data error.
                results[key] = {
                    "status": "internal_error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "retryable": False,
                    "records": [],
                }
    return results


def _parse_date(value: object) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _subtract_years(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:  # February 29
        return day.replace(year=day.year - years, day=28)


def _category(description: str) -> str:
    for name, patterns in CATEGORY_PATTERNS:
        if any(pattern.search(description) for pattern in patterns):
            return name
    return "other"


def _summarize_records(records: list[dict], as_of: date) -> dict:
    def is_open(record: dict) -> bool:
        return str(record.get("currentstatus", "")).upper() not in CLOSED_STATUSES

    open_all = [r for r in records if is_open(r)]
    time_windows = {}
    records_by_window = {}
    for years in SUMMARY_WINDOWS_YEARS:
        cutoff = _subtract_years(as_of, years)
        window_records = [
            record
            for record in records
            if (_parse_date(record.get("inspectiondate")) or date.min) >= cutoff
        ]
        records_by_window[years] = window_records
        window_open = [record for record in window_records if is_open(record)]
        label = "1_year" if years == 1 else f"{years}_years"
        time_windows[label] = {
            "total": len(window_records),
            "open_or_pending": len(window_open),
            "by_severity": dict(
                sorted(Counter(record.get("class", "UNKNOWN") for record in window_records).items())
            ),
            "by_status": dict(
                sorted(Counter(record.get("currentstatus", "UNKNOWN") for record in window_records).items())
            ),
            "by_category": dict(
                sorted(Counter(_category(record.get("novdescription", "")) for record in window_records).items())
            ),
        }

    open_recent = [record for record in records_by_window[5] if is_open(record)]
    serious = sorted(
        (r for r in open_recent if r.get("class") in {"B", "C"}),
        key=lambda r: (_parse_date(r.get("inspectiondate")) or date.min, r.get("class", "")),
        reverse=True,
    )[:10]
    return {
        "as_of": as_of.isoformat(),
        "all_time": {
            "total": len(records),
            "open_or_pending": len(open_all),
            "closed_or_dismissed": len(records) - len(open_all),
        },
        "time_windows": time_windows,
        "recent_serious_open_violations": [
            {
                "violation_id": r.get("violationid"),
                "class": r.get("class"),
                "inspection_date": r.get("inspectiondate"),
                "status": r.get("currentstatus"),
                "category": _category(r.get("novdescription", "")),
                "description": r.get("novdescription"),
            }
            for r in serious
        ],
    }


def summarize_violations_batch(
    listings: list[dict],
    building_results: dict[str, dict],
    *,
    as_of: date | None = None,
) -> dict:
    """Summarize unique buildings and map their results back to listing order."""
    as_of = as_of or date.today()
    summary_cache: dict[str, dict] = {}
    results = []
    matched = 0
    for listing in listings:
        status = listing.get("status")
        output = {"listing_id": listing.get("listing_id"), "status": status}
        key = listing.get("building_key")
        if status != "normalized" or not key:
            if listing.get("error"):
                output["error"] = listing["error"]
            results.append(output)
            continue

        building = building_results.get(key, {"status": "api_error", "error": "Missing building result"})
        output["status"] = building["status"]
        output["matched_address"] = f"{listing['house_number']} {listing['street_name']}, {listing['borough']} {listing['zip_code']}"
        output["building_ids"] = building.get("building_ids", [])
        if building["status"] == "success":
            matched += 1
            if key not in summary_cache:
                summary_cache[key] = _summarize_records(building["records"], as_of)
            output["summary"] = summary_cache[key]
        elif building.get("error"):
            output["error"] = building["error"]
            for field in ("error_type", "retryable", "attempts"):
                if field in building:
                    output[field] = building[field]
        results.append(output)

    return {
        "requested_count": len(listings),
        "matched_listing_count": matched,
        "unique_building_count": len({x.get("building_key") for x in listings if x.get("building_key")}),
        "results": results,
        "disclaimer": (
            "HPD records are public administrative records. No returned rows may mean either that "
            "no public violation rows were found or that the normalized address did not match HPD exactly."
        ),
    }


def check_building_violations(
    listing_ids: list[int],
    *,
    listings_path: str | Path = DEFAULT_LISTINGS_PATH,
) -> dict:
    """Public tool entry point for batch HPD violation checks."""
    listings = get_listing_addresses(listing_ids, listings_path)
    addresses = normalize_hpd_addresses(listings)
    building_results = fetch_hpd_violations_batch(addresses)
    return summarize_violations_batch(
        addresses,
        building_results,
    )
