from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]


def test_official_station_coordinates_cover_all_station_codes():
    coordinates = pd.read_csv(
        ROOT / "config" / "station_coordinates.csv", dtype={"canonical_station_id": str})
    metrics = pd.read_parquet(ROOT / "data" / "processed" / "metro" / "station_metrics.parquet")
    assert len(coordinates) == 114
    assert coordinates.canonical_station_id.nunique() == 114
    assert coordinates[["latitude", "longitude"]].notna().all().all()
    assert coordinates.latitude.between(35.0, 35.4).all()
    assert coordinates.longitude.between(128.9, 129.2).all()
    assert set(metrics.canonical_station_id).issubset(set(coordinates.canonical_station_id))
    assert coordinates.source_date.eq("2021-02-26").all()
