import numpy as np
import pandas as pd

from src.metro.apartments import EARTH_RADIUS_M, build_station_links, validate_apartments


def _stations():
    return pd.DataFrame([
        {"canonical_station_id": "A", "station_name": "가", "line_id": "1", "latitude": 35.0, "longitude": 129.0},
        {"canonical_station_id": "B", "station_name": "나", "line_id": "2", "latitude": 35.0, "longitude": 129.004},
    ])


def _longitude_offset(distance_m):
    return np.degrees(distance_m / (EARTH_RADIUS_M * np.cos(np.radians(35.0))))


def test_unrounded_distance_and_household_boundaries():
    raw = pd.DataFrame([
        {"kapt_code": "included", "complex_name": "포함", "latitude": 35.0, "longitude": 129.0 + _longitude_offset(300.0), "households": 500},
        {"kapt_code": "distance_out", "complex_name": "거리제외", "latitude": 35.0, "longitude": 129.0 + _longitude_offset(300.01), "households": 500},
        {"kapt_code": "household_out", "complex_name": "세대제외", "latitude": 35.0, "longitude": 129.0, "households": 499},
    ])
    bundle = validate_apartments(raw, _stations())
    links = build_station_links(bundle.apartments, _stations())
    at_a = links[links.canonical_station_id.eq("A")].merge(bundle.apartments[["kapt_code", "households"]], on="kapt_code")
    selected = at_a[at_a.distance_m.le(300) & at_a.households.ge(500)]
    assert selected.kapt_code.tolist() == ["included"]
    assert round(at_a.set_index("kapt_code").at["distance_out", "distance_m"], 1) == 300.0


def test_one_complex_is_preserved_for_each_nearby_station_and_deduplicates_totals():
    stations = _stations()
    midpoint = (stations.longitude.min() + stations.longitude.max()) / 2
    raw = pd.DataFrame([{"kapt_code": "shared", "complex_name": "공유", "latitude": 35.0, "longitude": midpoint, "households": 700}])
    bundle = validate_apartments(raw, stations)
    links = build_station_links(bundle.apartments, stations)
    nearby = links[links.distance_m.le(300)]
    assert set(nearby.canonical_station_id) == {"A", "B"}
    assert nearby.kapt_code.nunique() == 1
    total = bundle.apartments[bundle.apartments.kapt_code.isin(nearby.kapt_code)].drop_duplicates("kapt_code").households.sum()
    assert total == 700


def test_conflicting_key_is_excluded_without_averaging():
    raw = pd.DataFrame([
        {"kapt_code": "collision", "complex_name": "갑", "latitude": 35.0, "longitude": 129.0, "households": 500},
        {"kapt_code": "collision", "complex_name": "을", "latitude": 35.1, "longitude": 129.1, "households": 900},
    ])
    bundle = validate_apartments(raw, _stations())
    assert bundle.apartments.empty
    assert len(bundle.conflicts) == 2
    assert bundle.quality["conflicting_kapt_codes"] == 1

