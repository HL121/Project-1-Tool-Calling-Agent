"""Dependency-free point-in-polygon lookup for official location boundaries."""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class LocationMatch:
    state: str
    borough: str
    neighborhood: str


def _polygon_parts(geometry: dict) -> Iterable[list]:
    if geometry["type"] == "Polygon":
        yield geometry["coordinates"]
    elif geometry["type"] == "MultiPolygon":
        yield from geometry["coordinates"]


def _bbox(polygon: list) -> tuple[float, float, float, float]:
    points = [point for ring in polygon for point in ring]
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _on_segment(
    x: float,
    y: float,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    tolerance: float = 1e-10,
) -> bool:
    cross = (y - y1) * (x2 - x1) - (x - x1) * (y2 - y1)
    if abs(cross) > tolerance:
        return False
    return (
        min(x1, x2) - tolerance <= x <= max(x1, x2) + tolerance
        and min(y1, y2) - tolerance <= y <= max(y1, y2) + tolerance
    )


def _inside_ring(x: float, y: float, ring: list) -> bool:
    inside = False
    previous = len(ring) - 1
    for current in range(len(ring)):
        x1, y1 = ring[previous][:2]
        x2, y2 = ring[current][:2]
        if _on_segment(x, y, x1, y1, x2, y2):
            return True
        if (y1 > y) != (y2 > y):
            crossing_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < crossing_x:
                inside = not inside
        previous = current
    return inside


def _inside_polygon(x: float, y: float, polygon: list) -> bool:
    return _inside_ring(x, y, polygon[0]) and not any(
        _inside_ring(x, y, hole) for hole in polygon[1:]
    )


def _load_geojson(path: Path) -> dict:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def _nj_municipality(properties: dict) -> str:
    name = properties["GNIS_NAME"]
    for prefix in ("City of ", "Township of ", "Town of ", "Borough of ", "Village of "):
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


class SpatialLocationIndex:
    def __init__(self, nyc_path: Path, nj_path: Path):
        self.entries: list[tuple[tuple[float, float, float, float], list, LocationMatch]] = []
        nyc = _load_geojson(nyc_path)
        for feature in nyc["features"]:
            properties = feature["properties"]
            match = LocationMatch("NY", properties["boroname"], properties["ntaname"])
            for polygon in _polygon_parts(feature["geometry"]):
                self.entries.append((_bbox(polygon), polygon, match))

        nj = _load_geojson(nj_path)
        for feature in nj["features"]:
            properties = feature["properties"]
            match = LocationMatch("NJ", "New Jersey", _nj_municipality(properties))
            for polygon in _polygon_parts(feature["geometry"]):
                self.entries.append((_bbox(polygon), polygon, match))

    def lookup(self, latitude: float, longitude: float) -> list[LocationMatch]:
        matches: list[LocationMatch] = []
        for bounds, polygon, match in self.entries:
            if not (
                bounds[0] <= longitude <= bounds[2]
                and bounds[1] <= latitude <= bounds[3]
            ):
                continue
            if _inside_polygon(longitude, latitude, polygon) and match not in matches:
                matches.append(match)
        return matches
