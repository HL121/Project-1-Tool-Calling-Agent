from __future__ import annotations

import json

import pandas as pd
import pytest

import tools as tools_module
from tools import TOOL_MAP, TOOLS, rank_listings, search_listings


def make_listing(
    listing_id: int,
    rent: float,
    *,
    neighborhood: str = "Example Heights",
    borough: str = "Brooklyn",
    bedrooms: float = 1,
) -> dict:
    return {
        "id": listing_id,
        "street": f"{listing_id} Example Street",
        "unit": "2A",
        "neighborhood": neighborhood,
        "borough": borough,
        "state": "NY",
        "zip_code": "11211",
        "bedrooms": bedrooms,
        "bathrooms": 1,
        "price": rent,
        "net_effective_price": 0,
        "effective_rent": rent,
        "furnished": False,
        "is_new_development": False,
        "available_date": pd.Timestamp("2026-09-01"),
        "created_at_utc": pd.Timestamp("2026-08-01T12:00:00Z"),
        "last_publish_month": "2026-08",
        "url": f"https://example.com/{listing_id}",
    }


@pytest.fixture
def ranking_listings(monkeypatch) -> pd.DataFrame:
    rows = [
        make_listing(1, 2400),
        make_listing(2, 3100),
        make_listing(3, 2750, neighborhood="Other Heights"),
    ]
    rows.extend(make_listing(100 + index, 2500 + index * 50) for index in range(30))
    frame = pd.DataFrame(rows).set_index("id")
    monkeypatch.setattr(tools_module, "listings", frame)
    return frame


def parse_rank(**kwargs) -> dict:
    return json.loads(rank_listings(**kwargs))


def test_price_only_ranking_is_market_relative_and_explainable(ranking_listings):
    result = parse_rank(listing_ids=[1, 2])

    assert result["ok"] is True
    assert result["ranking_scope"] == ["price_value"]
    assert result["weights_used"] == {"price_value": 1.0}
    assert [item["listing_id"] for item in result["rankings"]] == [1, 2]
    assert result["rankings"][0]["market_context"]["sample_size"] == 30
    assert result["rankings"][0]["market_context"]["comparison_scope"] == (
        "same neighborhood and bedrooms"
    )
    assert result["rankings"][0]["score_breakdown"]["price_value"] > (
        result["rankings"][1]["score_breakdown"]["price_value"]
    )
    assert result["missing_signals"] == {
        "commute": [1, 2],
        "neighborhood_fit": [1, 2],
    }


def test_verified_signals_can_change_order_and_dealbreaker_sorts_last(ranking_listings):
    result = parse_rank(
        listing_ids=[1, 2],
        price_weight=0.2,
        commute_weight=0.4,
        neighborhood_weight=0.4,
        max_commute_minutes=60,
        commute_summaries=[
            {"listing_id": 1, "duration_min": 55},
            {"listing_id": 2, "duration_min": 12},
        ],
        neighborhood_summaries=[
            {"listing_id": 1, "wants_met": 0, "wants_total": 2, "dealbreakers_hit": 1},
            {"listing_id": 2, "wants_met": 2, "wants_total": 2, "dealbreakers_hit": 0},
        ],
    )

    assert result["ranking_scope"] == ["price_value", "commute", "neighborhood_fit"]
    assert [item["listing_id"] for item in result["rankings"]] == [2, 1]
    assert result["rankings"][1]["dealbreaker_flag"] is True
    assert "dealbreaker" in result["rankings"][1]["warnings"][0]


def test_avoid_only_neighborhood_summary_has_defined_score(ranking_listings):
    result = parse_rank(
        listing_ids=[1, 2],
        price_weight=0,
        commute_weight=0,
        neighborhood_weight=1,
        neighborhood_summaries=[
            {"listing_id": 1, "wants_met": 0, "wants_total": 0, "dealbreakers_hit": 0},
            {"listing_id": 2, "wants_met": 0, "wants_total": 0, "dealbreakers_hit": 1},
        ],
    )

    assert [item["listing_id"] for item in result["rankings"]] == [1, 2]
    assert result["rankings"][0]["score_breakdown"]["neighborhood_fit"] == 100
    assert result["rankings"][1]["score_breakdown"]["neighborhood_fit"] == 50


def test_partial_signal_is_excluded_for_every_candidate(ranking_listings):
    result = parse_rank(
        listing_ids=[1, 2],
        price_weight=0.5,
        commute_weight=0.5,
        neighborhood_weight=0,
        commute_summaries=[{"listing_id": 1, "duration_min": 10}],
    )

    assert result["ranking_scope"] == ["price_value"]
    assert result["weights_used"] == {"price_value": 1.0}
    assert result["missing_signals"] == {"commute": [2]}


@pytest.mark.parametrize(
    ("arguments", "error_fragment"),
    [
        ({"listing_ids": [1]}, "2-10"),
        ({"listing_ids": [1, 1]}, "duplicates"),
        ({"listing_ids": [1, 999]}, "Unknown"),
        ({"listing_ids": [1, 2], "price_weight": -1}, "Weights"),
        (
            {
                "listing_ids": [1, 2],
                "commute_summaries": [{"listing_id": 1, "duration_min": -5}],
            },
            "duration_min",
        ),
        (
            {
                "listing_ids": [1, 2],
                "neighborhood_summaries": [
                    {"listing_id": 1, "wants_met": 2, "wants_total": 1, "dealbreakers_hit": 0}
                ],
            },
            "wants_met",
        ),
        (
            {
                "listing_ids": [1, 2],
                "neighborhood_summaries": [
                    {
                        "listing_id": 1,
                        "wants_met": 0.5,
                        "wants_total": 1,
                        "dealbreakers_hit": 0,
                    }
                ],
            },
            "whole numbers",
        ),
    ],
)
def test_invalid_rank_calls_return_actionable_errors(
    ranking_listings, arguments, error_fragment
):
    result = parse_rank(**arguments)

    assert result["ok"] is False
    assert error_fragment in result["error"]
    assert result["suggestion"]


def test_rank_tool_is_registered_for_the_agent():
    declared_names = [tool["function"]["name"] for tool in TOOLS]

    assert "rank_listings" in declared_names
    assert TOOL_MAP["rank_listings"] is rank_listings


def test_real_search_results_can_flow_into_price_ranking():
    search_result = json.loads(
        search_listings(
            borough="Brooklyn",
            neighborhood="Williamsburg",
            min_bedrooms=1,
            max_bedrooms=1,
            max_price=3500,
            limit=3,
        )
    )
    listing_ids = [result["listing_id"] for result in search_result["results"]]

    result = parse_rank(listing_ids=listing_ids)

    assert result["ok"] is True
    assert len(result["rankings"]) == 3
    assert {item["listing_id"] for item in result["rankings"]} == set(listing_ids)
    assert all(item["market_context"]["sample_size"] >= 20 for item in result["rankings"])
