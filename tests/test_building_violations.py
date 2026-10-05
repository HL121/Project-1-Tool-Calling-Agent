import tempfile
import unittest
import socket
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from scripts.tool4.building_violations import (
    HPDRequestError,
    _request_hpd_page,
    fetch_hpd_violations_batch,
    get_listing_addresses,
    normalize_hpd_addresses,
    summarize_violations_batch,
)
from urllib.request import Request


class BuildingViolationTests(unittest.TestCase):
    def test_load_preserves_order_duplicates_and_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "listings.csv"
            pd.DataFrame([
                {"id": 1, "street": "327 East 83rd Street", "normalized_address": "327 E 83RD ST", "zip_code": "10028", "borough": "Manhattan", "state": "NY"},
                {"id": 2, "street": "155 Washington Street", "normalized_address": "155 WASHINGTON ST", "zip_code": "07302", "borough": "New Jersey", "state": "NJ"},
            ]).to_csv(path, index=False)
            rows = get_listing_addresses([2, 99, 1, 1], path)
        self.assertEqual([r["listing_id"] for r in rows], [2, 99, 1, 1])
        self.assertEqual(rows[1]["status"], "listing_not_found")
        self.assertEqual(rows[0]["zip_code"], "07302")

    def test_normalization_and_building_dedup_key(self):
        rows = normalize_hpd_addresses([
            {"listing_id": 1, "status": "ready", "street": "327 East 83rd Street", "normalized_address": "327 E 83RD ST", "zip_code": "10028", "borough": "Manhattan", "state": "NY"},
            {"listing_id": 2, "status": "ready", "street": "327 E 83rd St", "normalized_address": "327 E 83RD ST", "zip_code": "10028", "borough": "Manhattan", "state": "NY"},
            {"listing_id": 3, "status": "ready", "street": "155 Washington Street", "normalized_address": "155 WASHINGTON ST", "zip_code": "07302", "borough": "New Jersey", "state": "NJ"},
        ])
        self.assertEqual(rows[0]["street_name"], "EAST 83 STREET")
        self.assertEqual(rows[0]["building_key"], rows[1]["building_key"])
        self.assertEqual(rows[2]["status"], "unsupported_location")

    def test_light_hpd_normalization_does_not_expand_saint(self):
        rows = normalize_hpd_addresses([
            {"listing_id": 1, "status": "ready", "street": "10 St Nicholas Avenue", "normalized_address": "10 ST NICHOLAS AVE", "zip_code": "10026", "borough": "Manhattan", "state": "NY"},
        ])
        self.assertEqual(rows[0]["street_name"], "ST NICHOLAS AVENUE")

    @patch("scripts.tool4.building_violations._fetch_all_pages")
    def test_fetches_each_building_only_once(self, fetch):
        fetch.return_value = [{"violationid": "1", "buildingid": "10"}]
        address = {"status": "normalized", "building_key": "key", "borough": "MANHATTAN", "zip_code": "10028", "house_number": "327", "street_name": "EAST 83 STREET"}
        result = fetch_hpd_violations_batch([address, dict(address)], max_workers=2)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result["key"]["building_ids"], ["10"])

    @patch("scripts.tool4.building_violations.time.sleep")
    @patch("scripts.tool4.building_violations.urlopen")
    def test_timeout_retries_are_bounded(self, urlopen, sleep):
        urlopen.side_effect = socket.timeout("read timed out")
        with self.assertRaises(HPDRequestError) as caught:
            _request_hpd_page(
                Request("https://example.test"),
                timeout=1,
                max_attempts=3,
                backoff_seconds=0.01,
            )
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(caught.exception.error_type, "timeout")
        self.assertEqual(caught.exception.attempts, 3)
        self.assertTrue(caught.exception.retryable)

    @patch("scripts.tool4.building_violations._fetch_all_pages")
    def test_one_timeout_does_not_abort_batch(self, fetch):
        def side_effect(address, endpoint, timeout):
            if address["building_key"] == "timeout":
                raise HPDRequestError(
                    "HPD API request timed out",
                    error_type="timeout",
                    retryable=True,
                    attempts=3,
                )
            return [{"violationid": "1", "buildingid": "10"}]

        fetch.side_effect = side_effect
        base = {
            "status": "normalized",
            "borough": "MANHATTAN",
            "zip_code": "10028",
            "house_number": "327",
            "street_name": "EAST 83 STREET",
        }
        result = fetch_hpd_violations_batch([
            {**base, "building_key": "success"},
            {**base, "building_key": "timeout"},
        ], max_workers=2)
        self.assertEqual(result["success"]["status"], "success")
        self.assertEqual(result["timeout"]["status"], "api_error")
        self.assertEqual(result["timeout"]["error_type"], "timeout")
        self.assertEqual(result["timeout"]["attempts"], 3)

        summarized = summarize_violations_batch(
            [{
                **base,
                "listing_id": 2,
                "building_key": "timeout",
            }],
            result,
        )
        self.assertEqual(summarized["results"][0]["error_type"], "timeout")
        self.assertEqual(summarized["results"][0]["attempts"], 3)
        self.assertTrue(summarized["results"][0]["retryable"])

    def test_summary_maps_shared_building_and_classifies(self):
        listings = [
            {"listing_id": 1, "status": "normalized", "building_key": "key", "house_number": "327", "street_name": "EAST 83 STREET", "borough": "MANHATTAN", "zip_code": "10028"},
            {"listing_id": 2, "status": "normalized", "building_key": "key", "house_number": "327", "street_name": "EAST 83 STREET", "borough": "MANHATTAN", "zip_code": "10028"},
        ]
        records = [
            {"violationid": "1", "buildingid": "10", "class": "C", "inspectiondate": "2025-01-02T00:00:00.000", "currentstatus": "NOV SENT OUT", "novdescription": "MICE AND ROACH CONDITION"},
            {"violationid": "2", "buildingid": "10", "class": "B", "inspectiondate": "2024-01-02T00:00:00.000", "currentstatus": "VIOLATION CLOSED", "novdescription": "MICE CONDITION"},
            {"violationid": "3", "buildingid": "10", "class": "A", "inspectiondate": "2022-01-02T00:00:00.000", "currentstatus": "VIOLATION DISMISSED", "novdescription": "ROACH CONDITION"},
        ]
        output = summarize_violations_batch(listings, {"key": {"status": "success", "building_ids": ["10"], "records": records}}, as_of=date(2026, 1, 1))
        self.assertEqual(output["matched_listing_count"], 2)
        self.assertEqual(output["unique_building_count"], 1)
        windows = output["results"][0]["summary"]["time_windows"]
        self.assertEqual(set(windows), {"1_year", "3_years", "5_years"})
        self.assertEqual(windows["1_year"]["by_category"], {"pests_bedbugs": 1})
        self.assertEqual(windows["3_years"]["by_category"], {"pests_bedbugs": 2})
        self.assertEqual(windows["5_years"]["by_category"], {"pests_bedbugs": 3})
        self.assertEqual(windows["1_year"]["by_severity"], {"C": 1})
        self.assertEqual(windows["3_years"]["by_severity"], {"B": 1, "C": 1})
        self.assertEqual(windows["5_years"]["by_severity"], {"A": 1, "B": 1, "C": 1})


if __name__ == "__main__":
    unittest.main()
