from __future__ import annotations

import json

import pandas as pd
import pytest

import tools as tools_module
from tools import TOOL_MAP, TOOLS, search_listings


@pytest.fixture
def sample_listings(monkeypatch) -> pd.DataFrame:
    frame = pd.DataFrame(
        [
            {
                "id": 101,
                "street": "10 Bedford Ave",
                "unit": "2A",
                "neighborhood": "Williamsburg",
                "borough": "Brooklyn",
                "state": "NY",
                "zip_code": "11211",
                "bedrooms": 1,
                "bathrooms": 1,
                "price": 3600,
                "net_effective_price": 3300,
                "effective_rent": 3300,
                "furnished": False,
                "is_new_development": False,
                "available_date": pd.Timestamp("2026-09-01"),
                "created_at_utc": pd.Timestamp("2026-08-10T12:00:00Z"),
                "last_publish_month": "2026-08",
                "url": "https://example.com/101",
            },
            {
                "id": 102,
                "street": "20 Kent Ave",
                "unit": "8B",
                "neighborhood": "Williamsburg",
                "borough": "Brooklyn",
                "state": "NY",
                "zip_code": "11249",
                "bedrooms": 1,
                "bathrooms": 1.5,
                "price": 3200,
                "net_effective_price": 0,
                "effective_rent": 3200,
                "furnished": True,
                "is_new_development": True,
                "available_date": pd.NaT,
                "created_at_utc": pd.Timestamp("2026-08-15T12:00:00Z"),
                "last_publish_month": "2026-08",
                "url": "https://example.com/102",
            },
            {
                "id": 103,
                "street": "30 Broadway",
                "unit": "4C",
                "neighborhood": "SoHo-Little Italy-Hudson Square",
                "borough": "Manhattan",
                "state": "NY",
                "zip_code": "10013",
                "bedrooms": 2,
                "bathrooms": 2,
                "price": 6500,
                "net_effective_price": 0,
                "effective_rent": 6500,
                "furnished": False,
                "is_new_development": False,
                "available_date": pd.Timestamp("2026-10-01"),
                "created_at_utc": pd.Timestamp("2026-08-12T12:00:00Z"),
                "last_publish_month": "2026-08",
                "url": "https://example.com/103",
            },
            {
                "id": 104,
                "street": "40 Fulton St",
                "unit": None,
                "neighborhood": "Bedford-Stuyvesant (West)",
                "borough": "Brooklyn",
                "state": "NY",
                "zip_code": "11216",
                "bedrooms": 0,
                "bathrooms": 1,
                "price": 2400,
                "net_effective_price": 2250,
                "effective_rent": 2250,
                "furnished": False,
                "is_new_development": False,
                "available_date": pd.Timestamp("2026-09-15"),
                "created_at_utc": pd.Timestamp("2026-08-11T12:00:00Z"),
                "last_publish_month": "2026-08",
                "url": "https://example.com/104",
            },
        ]
    ).set_index("id")
    monkeypatch.setattr(tools_module, "listings", frame)
    return frame


def parse_result(**kwargs) -> dict:
    return json.loads(search_listings(**kwargs))


def test_effective_rent_filters_and_compact_result_fields(sample_listings):
    result = parse_result(
        borough="Brooklyn",
        neighborhood="williamsburg",
        min_bedrooms=1,
        max_bedrooms=1,
        min_bathrooms=1.5,
        max_bathrooms=1.5,
        min_price=3000,
        max_price=3250,
    )

    assert result["ok"] is True
    assert result["total_matches"] == 1
    assert result["results"][0]["listing_id"] == 102
    assert result["results"][0]["effective_rent"] == 3200
    assert result["results"][0]["address"] == "20 Kent Ave, Unit 8B"
    assert "normalized_address" not in result["results"][0]


def test_common_borough_and_neighborhood_aliases_work(sample_listings):
    result = parse_result(borough="Kings County", neighborhood="Bed-Stuy")

    assert result["ok"] is True
    assert [item["listing_id"] for item in result["results"]] == [104]
    assert result["matched_neighborhoods"] == ["Bedford-Stuyvesant (West)"]


def test_false_furnished_value_means_unfurnished_only(sample_listings):
    result = parse_result(
        borough="BK", neighborhood="Williamsburg", furnished=False, sort_by="price_asc"
    )

    assert [item["listing_id"] for item in result["results"]] == [101]


def test_newest_and_new_development_filters(sample_listings):
    result = parse_result(
        borough="Brooklyn", new_development_only=True, sort_by="newest"
    )

    assert [item["listing_id"] for item in result["results"]] == [102]


def test_unknown_neighborhood_returns_suggestions(sample_listings):
    result = parse_result(borough="Brooklyn", neighborhood="Williamsbarg")

    assert result["ok"] is True
    assert result["count"] == 0
    assert "Williamsburg" in result["suggested_neighborhoods"]


def test_unknown_borough_returns_suggestions(sample_listings):
    result = parse_result(borough="Brooklynn")

    assert result["ok"] is True
    assert result["count"] == 0
    assert "Brooklyn" in result["suggested_boroughs"]


@pytest.mark.parametrize(
    ("arguments", "error_fragment"),
    [
        ({"min_bedrooms": 3, "max_bedrooms": 1}, "min_bedrooms"),
        ({"min_bathrooms": 2, "max_bathrooms": 1}, "min_bathrooms"),
        ({"min_price": 4000, "max_price": 3000}, "min_price"),
        ({"max_price": -1}, "max_price"),
        ({"sort_by": "random"}, "sort_by"),
        ({"limit": 100}, "limit"),
        ({"furnished": "yes"}, "furnished"),
    ],
)
def test_invalid_arguments_return_actionable_json_errors(
    sample_listings, arguments, error_fragment
):
    result = parse_result(**arguments)

    assert result["ok"] is False
    assert error_fragment in result["error"]
    assert result["suggestion"]


def test_tool_is_registered_for_the_agent():
    declared_names = [tool["function"]["name"] for tool in TOOLS]

    assert "search_listings" in declared_names
    assert TOOL_MAP["search_listings"] is search_listings


def test_real_dataset_returns_valid_williamsburg_candidates():
    result = parse_result(
        borough="Brooklyn",
        neighborhood="Williamsburg",
        min_bedrooms=1,
        max_bedrooms=1,
        max_price=3500,
        sort_by="price_asc",
        limit=3,
    )

    assert result["ok"] is True
    assert result["count"] == 3
    assert result["total_matches"] >= 3
    assert all(item["neighborhood"] == "Williamsburg" for item in result["results"])
    assert all(item["borough"] == "Brooklyn" for item in result["results"])
    assert all(item["bedrooms"] == 1 for item in result["results"])
    assert all(item["effective_rent"] <= 3500 for item in result["results"])
    assert all(item["url"].startswith("https://") for item in result["results"])


def test_real_dataset_nyc_alias_excludes_new_jersey():
    result = parse_result(borough="NYC", sort_by="newest", limit=20)

    assert result["ok"] is True
    assert result["count"] == 20
    assert all(item["borough"] != "New Jersey" for item in result["results"])
