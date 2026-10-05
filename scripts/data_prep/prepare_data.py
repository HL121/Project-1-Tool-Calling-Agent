#!/usr/bin/env python3
"""Build a rental candidate pool from the three most recent source snapshots."""

from __future__ import annotations

import argparse
import math
import re
import unicodedata
from pathlib import Path

import pandas as pd

try:
    from .spatial_location import SpatialLocationIndex
except ImportError:  # Support direct execution: python3 scripts/data_prep/prepare_data.py
    from spatial_location import SpatialLocationIndex


MONTH_PATTERN = re.compile(r"(?P<year>20\d{2})-(?P<month>[A-Za-z]{3})")
SOURCE_MONTHS = {"2026-06", "2026-07", "2026-08"}
EXCLUDED_BUILDING_TYPES = {"COMMERCIAL", "LAND"}
MIN_EFFECTIVE_RENT = 500
OUTPUT_COLUMNS_REMOVED = [
    "month",
    "sqft",
    "lease_months",
    "months_free",
    "has_videos",
    "has_3d_tour",
    "media_asset_count",
    "lead_photo_id",
    "photo_ids",
    "open_house_start_utc",
    "open_house_end_utc",
    "open_house_appointment_only",
    "no_fee",
]
STREET_SUFFIXES = {
    "AVENUE": "AVE",
    "BOULEVARD": "BLVD",
    "CIRCLE": "CIR",
    "COURT": "CT",
    "DRIVE": "DR",
    "EXPRESSWAY": "EXPY",
    "HIGHWAY": "HWY",
    "LANE": "LN",
    "PARKWAY": "PKWY",
    "PLACE": "PL",
    "PLAZA": "PLZ",
    "ROAD": "RD",
    "STREET": "ST",
    "TERRACE": "TER",
    "TRAIL": "TRL",
    "TURNPIKE": "TPKE",
    "TNPK": "TPKE",
    "TPK": "TPKE",
}
DIRECTIONALS = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W"}
ORDINAL_WORDS = {
    "FIRST": "1ST",
    "SECOND": "2ND",
    "THIRD": "3RD",
    "FOURTH": "4TH",
    "FIFTH": "5TH",
    "SIXTH": "6TH",
    "SEVENTH": "7TH",
    "EIGHTH": "8TH",
    "NINTH": "9TH",
    "TENTH": "10TH",
    "ELEVENTH": "11TH",
    "TWELFTH": "12TH",
    "THIRTEENTH": "13TH",
    "FOURTEENTH": "14TH",
    "FIFTEENTH": "15TH",
    "SIXTEENTH": "16TH",
    "SEVENTEENTH": "17TH",
    "EIGHTEENTH": "18TH",
    "NINETEENTH": "19TH",
    "TWENTIETH": "20TH",
}


def source_month(path: Path) -> pd.Timestamp:
    match = MONTH_PATTERN.search(path.name)
    if not match:
        raise ValueError(f"Cannot parse source month from {path.name}")
    return pd.to_datetime(
        f"{match.group('year')}-{match.group('month')}-01", format="%Y-%b-%d"
    )


def load_sources(input_dir: Path) -> pd.DataFrame:
    all_files = sorted(input_dir.glob("*-nyc-rental-listings.csv"), key=source_month)
    files = [path for path in all_files if source_month(path).strftime("%Y-%m") in SOURCE_MONTHS]
    found_months = {source_month(path).strftime("%Y-%m") for path in files}
    if found_months != SOURCE_MONTHS:
        missing = sorted(SOURCE_MONTHS - found_months)
        raise FileNotFoundError(f"Missing required source months: {', '.join(missing)}")

    frames: list[pd.DataFrame] = []
    expected_columns: list[str] | None = None
    for path in files:
        frame = pd.read_csv(path, low_memory=False)
        if expected_columns is None:
            expected_columns = frame.columns.tolist()
        elif frame.columns.tolist() != expected_columns:
            raise ValueError(f"Schema mismatch in {path.name}")
        month = source_month(path)
        frame["month"] = month.strftime("%Y-%m")
        frame["_source_month"] = month
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def normalize_text(value: object) -> str | None:
    if pd.isna(value):
        return None
    text = unicodedata.normalize("NFKC", str(value)).upper().strip()
    text = re.sub(r"[^A-Z0-9 ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def normalize_address(value: object) -> str | None:
    text = normalize_text(value)
    if not text:
        return None
    words = text.split()
    words = [ORDINAL_WORDS.get(word, DIRECTIONALS.get(word, word)) for word in words]
    words[-1] = STREET_SUFFIXES.get(words[-1], words[-1])
    if len(words) >= 3 and words[-1] in set(STREET_SUFFIXES.values()) and words[-2].isdigit():
        number = int(words[-2])
        suffix = "TH" if 10 <= number % 100 <= 20 else {1: "ST", 2: "ND", 3: "RD"}.get(number % 10, "TH")
        words[-2] = f"{number}{suffix}"
    return " ".join(words)


def normalize_unit(value: object) -> str | None:
    if pd.isna(value):
        return None
    text = unicodedata.normalize("NFKC", str(value)).upper().strip()
    text = re.sub(r"^(?:APT(?:ARTMENT)?|UNIT|#)\s*[-:#]?\s*", "", text)
    text = re.sub(r"[^A-Z0-9]+", "", text)
    return text or None


def normalize_and_validate_zip(values: pd.Series, field_name: str = "zip_code") -> pd.Series:
    normalized = (
        values.astype("string")
        .str.strip()
        .str.replace(r"\.0$", "", regex=True)
        .str.zfill(5)
    )
    invalid = ~normalized.str.fullmatch(r"\d{5}", na=False)
    if invalid.any():
        samples = values.loc[invalid].astype("string").drop_duplicates().head(5).tolist()
        raise ValueError(
            f"{field_name} must contain exactly five digits after normalization; "
            f"found {int(invalid.sum())} invalid value(s), sample={samples}"
        )
    return normalized


def normalize_url(values: pd.Series) -> pd.Series:
    normalized = values.astype("string").str.strip().str.rstrip("/")
    normalized = normalized.str.replace("#", "%23", regex=False)
    return normalized.mask(normalized.eq(""))


def load_geocode_verification(path: Path) -> pd.DataFrame:
    verification = pd.read_csv(path, dtype={"zip_code": "string"})
    verification["zip_code"] = normalize_and_validate_zip(
        verification["zip_code"], "geocode verification zip_code"
    )
    verification["_street_key"] = verification["street"].map(normalize_text)
    verification["state"] = verification["state"].str.strip().str.upper()
    if verification.duplicated(["_street_key", "zip_code", "state"]).any():
        raise ValueError("Geocode verification contains duplicate address keys")
    return verification


def clean_location(
    candidates: pd.DataFrame,
    spatial_index: SpatialLocationIndex,
    geocode_verification: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    candidates = candidates.copy()
    candidates["zip_code"] = normalize_and_validate_zip(candidates["zip_code"])
    candidates["state"] = candidates["state"].astype("string").str.strip().str.upper()
    candidates["borough"] = candidates["borough"].astype("string").str.strip()
    candidates["neighborhood"] = candidates["neighborhood"].astype("string").str.strip()
    candidates["latitude"] = pd.to_numeric(candidates["latitude"], errors="coerce")
    candidates["longitude"] = pd.to_numeric(candidates["longitude"], errors="coerce")
    finite_coordinates = candidates["latitude"].map(
        lambda value: pd.notna(value) and math.isfinite(value)
    ) & candidates["longitude"].map(
        lambda value: pd.notna(value) and math.isfinite(value)
    )
    valid_coordinate = (
        finite_coordinates
        & candidates["latitude"].between(-90, 90)
        & candidates["longitude"].between(-180, 180)
    )
    unique_points = candidates.loc[
        valid_coordinate, ["latitude", "longitude"]
    ].drop_duplicates()
    point_matches: dict[tuple[float, float], list] = {}
    for point in unique_points.itertuples(index=False):
        key = (float(point.latitude), float(point.longitude))
        point_matches[key] = spatial_index.lookup(*key)

    matches = pd.Series(
        [[] for _ in range(len(candidates))], index=candidates.index, dtype="object"
    )
    if valid_coordinate.any():
        matches.loc[valid_coordinate] = candidates.loc[valid_coordinate].apply(
            lambda row: point_matches[
                (float(row["latitude"]), float(row["longitude"]))
            ],
            axis=1,
        )
    match_count = matches.map(len)
    candidates["_spatial_state"] = matches.map(
        lambda values: values[0].state if len(values) == 1 else pd.NA
    )
    candidates["_spatial_borough"] = matches.map(
        lambda values: values[0].borough if len(values) == 1 else pd.NA
    )
    candidates["_spatial_neighborhood"] = matches.map(
        lambda values: values[0].neighborhood if len(values) == 1 else pd.NA
    )

    # Only anomalous coordinates are eligible for address-geocoder validation.
    initial_one_match = match_count.eq(1)
    initial_issue = (
        ~initial_one_match
        | candidates["state"].ne(candidates["_spatial_state"])
        | candidates["borough"].ne(candidates["_spatial_borough"])
    )
    candidates["_street_key"] = candidates["street"].map(normalize_text)
    verification_lookup = {
        (row["_street_key"], row["zip_code"], row["state"]): row
        for _, row in geocode_verification.iterrows()
    }
    geocode_verified = pd.Series(False, index=candidates.index)
    geocode_cache_hits = pd.Series(False, index=candidates.index)
    for index in candidates.index[initial_issue]:
        row = candidates.loc[index]
        verification = verification_lookup.get(
            (row["_street_key"], row["zip_code"], row["state"])
        )
        if verification is None:
            continue
        geocode_cache_hits.loc[index] = True
        if (
            verification["match_status"] != "Match"
            or verification["match_type"] != "Exact"
            or pd.isna(verification["latitude"])
            or pd.isna(verification["longitude"])
        ):
            continue
        verified_matches = spatial_index.lookup(
            float(verification["latitude"]), float(verification["longitude"])
        )
        if len(verified_matches) != 1:
            continue
        verified_match = verified_matches[0]
        candidates.loc[index, "latitude"] = float(verification["latitude"])
        candidates.loc[index, "longitude"] = float(verification["longitude"])
        candidates.loc[index, "_spatial_state"] = verified_match.state
        candidates.loc[index, "_spatial_borough"] = verified_match.borough
        candidates.loc[index, "_spatial_neighborhood"] = verified_match.neighborhood
        match_count.loc[index] = 1
        geocode_verified.loc[index] = True

    # An exact address match validates the replacement coordinates and their
    # spatial result, so these rows may safely adopt the generated location.
    candidates.loc[geocode_verified, "state"] = candidates.loc[
        geocode_verified, "_spatial_state"
    ]
    candidates.loc[geocode_verified, "borough"] = candidates.loc[
        geocode_verified, "_spatial_borough"
    ]
    candidates.loc[geocode_verified, "neighborhood"] = candidates.loc[
        geocode_verified, "_spatial_neighborhood"
    ]

    one_match = match_count.eq(1)
    state_conflict = (
        one_match
        & candidates["state"].notna()
        & candidates["state"].ne(candidates["_spatial_state"])
    )
    borough_conflict = (
        one_match
        & ~state_conflict
        & candidates["borough"].notna()
        & candidates["borough"].ne(candidates["_spatial_borough"])
    )
    verified = one_match & ~state_conflict & ~borough_conflict
    neighborhood_updates = verified & candidates["neighborhood"].ne(
        candidates["_spatial_neighborhood"]
    )

    # Coordinates become authoritative only after state and borough agree. The
    # official boundary name then replaces the source neighborhood taxonomy.
    candidates.loc[verified, "state"] = candidates.loc[verified, "_spatial_state"]
    candidates.loc[verified, "borough"] = candidates.loc[verified, "_spatial_borough"]
    candidates.loc[verified, "neighborhood"] = candidates.loc[
        verified, "_spatial_neighborhood"
    ]

    reason = pd.Series(pd.NA, index=candidates.index, dtype="string")
    reason.loc[match_count.eq(0)] = "coordinate_not_in_official_boundary"
    reason.loc[match_count.gt(1)] = "coordinate_matches_multiple_boundaries"
    reason.loc[state_conflict] = "state_conflicts_with_spatial_result"
    reason.loc[borough_conflict] = "borough_conflicts_with_spatial_result"
    reason.loc[~valid_coordinate & ~geocode_verified] = "invalid_coordinates"
    anomaly_mask = reason.notna()
    anomaly_columns = [
        "id",
        "street",
        "unit",
        "zip_code",
        "latitude",
        "longitude",
        "state",
        "borough",
        "neighborhood",
        "_spatial_state",
        "_spatial_borough",
        "_spatial_neighborhood",
        "url",
    ]
    anomalies = candidates.loc[anomaly_mask, anomaly_columns].copy()
    anomalies = anomalies.rename(
        columns={
            "_spatial_state": "spatial_state",
            "_spatial_borough": "spatial_borough",
            "_spatial_neighborhood": "spatial_neighborhood",
        }
    )
    anomalies["issue"] = reason.loc[anomaly_mask].values
    anomalies["address_geocode_cache_hit"] = geocode_cache_hits.loc[anomaly_mask].values

    candidates = candidates.drop(
        columns=[
            "_spatial_state",
            "_spatial_borough",
            "_spatial_neighborhood",
            "_street_key",
        ]
    )
    return candidates, anomalies


def prepare(
    df: pd.DataFrame,
    spatial_index: SpatialLocationIndex,
    geocode_verification: pd.DataFrame,
) -> pd.DataFrame:
    candidates = df.copy()
    candidates["street"] = candidates["street"].astype("string").str.strip()
    candidates["unit"] = candidates["unit"].astype("string").str.strip()
    unit_is_placeholder = candidates["unit"].str.upper().eq("UNIT").fillna(False)
    candidates.loc[candidates["unit"].eq("") | unit_is_placeholder, "unit"] = pd.NA

    created = pd.to_datetime(candidates["created_at_utc"], errors="coerce", utc=True)
    source_start = candidates["_source_month"].dt.tz_localize("UTC")
    source_end = source_start + pd.offsets.MonthBegin(1)
    created_outside_source_month = created.notna() & (
        created.lt(source_start) | created.ge(source_end)
    )
    created_valid = created.notna() & ~created_outside_source_month
    candidates["created_at_utc"] = created.where(created_valid)

    available = pd.to_datetime(candidates["available_date"], errors="coerce", utc=True)
    available_outside_window = (
        available.notna()
        & candidates["created_at_utc"].notna()
        & (
            available.lt(candidates["created_at_utc"] - pd.Timedelta(days=365))
            | available.gt(candidates["created_at_utc"] + pd.Timedelta(days=365))
        )
    )
    available_valid = (
        available.notna()
        & candidates["created_at_utc"].notna()
        & ~available_outside_window
    )
    candidates["available_date"] = available.where(available_valid)
    candidates["normalized_address"] = candidates["street"].map(normalize_address)
    candidates["normalized_unit"] = candidates["unit"].map(normalize_unit)
    candidates["state"] = candidates["state"].astype("string").str.strip().str.upper()
    candidates["zip_code"] = normalize_and_validate_zip(candidates["zip_code"])
    candidates["url"] = normalize_url(candidates["url"])
    candidates["_normalized_url"] = candidates["url"]
    candidates["_created_sort"] = candidates["created_at_utc"]
    candidates = candidates.sort_values(
        ["_source_month", "_created_sort", "id"], ascending=[False, False, False]
    )

    # Deduplicate in priority order. Missing values never share a fallback key:
    # they proceed to the next stage and ultimately remain independent by ID.
    has_url = candidates["_normalized_url"].notna()
    candidates = pd.concat(
        [
            candidates.loc[has_url].drop_duplicates("_normalized_url", keep="first"),
            candidates.loc[~has_url],
        ],
        ignore_index=True,
    )
    candidates["_unit_key"] = pd.NA
    complete_key = (
        candidates["state"].notna()
        & candidates["zip_code"].str.fullmatch(r"\d{5}", na=False)
        & candidates["normalized_address"].notna()
        & candidates["normalized_unit"].notna()
    )
    candidates.loc[complete_key, "_unit_key"] = (
        candidates.loc[complete_key, "state"]
        + "|"
        + candidates.loc[complete_key, "zip_code"]
        + "|"
        + candidates.loc[complete_key, "normalized_address"]
        + "|"
        + candidates.loc[complete_key, "normalized_unit"]
    )
    candidates = candidates.sort_values(
        ["_source_month", "_created_sort", "id"], ascending=[False, False, False]
    )
    complete_key = complete_key.reindex(candidates.index)
    # Preserve the latest record's attributes while carrying forward an older
    # usable URL for the same complete address/unit key.
    valid_url_by_unit_key = (
        candidates.loc[complete_key & candidates["_normalized_url"].notna()]
        .drop_duplicates("_unit_key", keep="first")
        .set_index("_unit_key")["_normalized_url"]
    )
    missing_url_with_key = complete_key & candidates["_normalized_url"].isna()
    fallback_urls = candidates.loc[missing_url_with_key, "_unit_key"].map(
        valid_url_by_unit_key
    )
    candidates.loc[fallback_urls.index, "_normalized_url"] = fallback_urls
    candidates.loc[fallback_urls.index, "url"] = fallback_urls
    candidates = pd.concat(
        [
            candidates.loc[complete_key].drop_duplicates("_unit_key", keep="first"),
            candidates.loc[~complete_key],
        ],
        ignore_index=True,
    )
    candidates = candidates.sort_values(
        ["_source_month", "_created_sort", "id"], ascending=[False, False, False]
    )
    has_id = candidates["id"].notna()
    candidates = pd.concat(
        [
            candidates.loc[has_id].drop_duplicates("id", keep="first"),
            candidates.loc[~has_id],
        ],
        ignore_index=True,
    )
    candidates["last_publish_month"] = candidates["month"]

    # Price cleaning intentionally happens after the latest record is selected.
    candidates["price"] = pd.to_numeric(candidates["price"], errors="coerce")
    candidates["net_effective_price"] = pd.to_numeric(
        candidates["net_effective_price"], errors="coerce"
    )
    candidates["effective_rent"] = candidates["net_effective_price"].where(
        candidates["net_effective_price"] > 0, candidates["price"]
    )
    finite_price = candidates["effective_rent"].map(
        lambda value: pd.notna(value) and math.isfinite(value)
    )
    valid_price = finite_price & candidates["effective_rent"].gt(MIN_EFFECTIVE_RENT)
    candidates = candidates.loc[valid_price].copy()

    building_type_key = candidates["building_type"].astype("string").str.strip().str.upper()
    excluded_building_type = building_type_key.isin(EXCLUDED_BUILDING_TYPES)
    candidates = candidates.loc[~excluded_building_type].copy()

    bedrooms = pd.to_numeric(candidates["bedrooms"], errors="coerce")
    bathrooms = pd.to_numeric(candidates["bathrooms"], errors="coerce")
    half_baths = pd.to_numeric(candidates["half_baths"], errors="coerce")
    bedrooms_below_zero = bedrooms.lt(0)
    bathrooms_zero = bathrooms.eq(0)
    bathrooms_extreme_for_bedrooms = bathrooms.ge(7) & bathrooms.gt(bedrooms + 2)
    half_baths_at_least_five = half_baths.ge(5)
    invalid_room_count = (
        bedrooms_below_zero
        | bathrooms_zero
        | bathrooms_extreme_for_bedrooms
        | half_baths_at_least_five
    )
    candidates = candidates.loc[~invalid_room_count].copy()

    missing_url = candidates["_normalized_url"].isna()
    candidates = candidates.loc[~missing_url].copy()

    candidates, location_anomalies = clean_location(
        candidates, spatial_index, geocode_verification
    )
    candidates = candidates.drop(index=location_anomalies.index).copy()

    candidates["created_at_utc"] = candidates["created_at_utc"].dt.strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    candidates["available_date"] = candidates["available_date"].dt.strftime("%Y-%m-%d")

    candidates = candidates.sort_values(
        ["_source_month", "_created_sort", "effective_rent", "id"],
        ascending=[False, False, True, False],
    ).reset_index(drop=True)
    candidates = candidates.drop(
        columns=[
            "_source_month",
            "_normalized_url",
            "_unit_key",
            "_created_sort",
            *OUTPUT_COLUMNS_REMOVED,
        ],
        errors="ignore",
    )

    return candidates


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("data/firstmover-nyc-rental-listings"),
    )
    parser.add_argument("--output-dir", type=Path, default=Path("data"))
    parser.add_argument(
        "--nyc-boundaries",
        type=Path,
        default=Path("data/geography/nyc_nta_2020.geojson.gz"),
    )
    parser.add_argument(
        "--nj-boundaries",
        type=Path,
        default=Path("data/geography/nj_municipalities.geojson.gz"),
    )
    parser.add_argument(
        "--geocode-verification",
        type=Path,
        default=Path("data/geography/address_geocode_verification.csv"),
    )
    args = parser.parse_args()

    raw = load_sources(args.input_dir)
    spatial_index = SpatialLocationIndex(args.nyc_boundaries, args.nj_boundaries)
    geocode_verification = load_geocode_verification(args.geocode_verification)
    clean = prepare(raw, spatial_index, geocode_verification)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.output_dir / "nyc_rental_listings_clean.csv"
    clean.to_csv(csv_path, index=False)
    print(f"Wrote {len(clean)} rows to {csv_path}")


if __name__ == "__main__":
    main()
