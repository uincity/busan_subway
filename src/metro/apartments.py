from __future__ import annotations

import hashlib
import io
import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

REQUIRED_COLUMNS = ["kapt_code", "complex_name", "latitude", "longitude", "households"]
OPTIONAL_COLUMNS = [
    "road_address", "legal_address", "sigungu", "dong", "jibun", "buildings",
    "approval_date", "apartment_age", "age_group", "parking_total",
    "parking_per_household", "is_500plus", "is_1000plus", "is_under_10years",
    "is_over_20years", "is_over_30years",
]
SNAPSHOT_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS
EARTH_RADIUS_M = 6_371_008.8


class ApartmentDataError(ValueError):
    pass


@dataclass
class ApartmentBundle:
    apartments: pd.DataFrame
    conflicts: pd.DataFrame
    quality: dict


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bytes_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def resolve_apartment_path(root: Path) -> tuple[Path | None, str]:
    """Resolve configured source first, then the deployable project snapshot."""
    config_path = root / "config" / "apartment_data.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    explicit = os.getenv("BUSAN_METRO_APARTMENT_PARQUET") or config.get("source_path")
    if explicit:
        candidate = Path(os.path.expandvars(str(explicit))).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        if candidate.is_file():
            return candidate.resolve(), "설정 경로"
    snapshot = root / str(config.get("snapshot_path", "data/processed/apartments/kapt_master.parquet"))
    if snapshot.is_file():
        return snapshot.resolve(), "배포용 스냅샷"
    return None, "파일 없음"


def read_apartment_parquet(source: Path | bytes) -> pd.DataFrame:
    return pd.read_parquet(io.BytesIO(source) if isinstance(source, bytes) else source)


def validate_apartments(raw: pd.DataFrame, station_coordinates: pd.DataFrame) -> ApartmentBundle:
    missing = sorted(set(REQUIRED_COLUMNS) - set(raw.columns))
    if missing:
        raise ApartmentDataError("필수 열이 없습니다: " + ", ".join(missing))

    frame = raw[[c for c in SNAPSHOT_COLUMNS if c in raw.columns]].copy()
    input_rows = len(frame)
    frame["kapt_code"] = frame["kapt_code"].astype("string").str.strip()
    frame.loc[frame.kapt_code.eq(""), "kapt_code"] = pd.NA
    missing_keys = int(frame.kapt_code.isna().sum())
    frame = frame.dropna(subset=["kapt_code"])
    exact_duplicates = int(frame.duplicated().sum())
    frame = frame.drop_duplicates()

    duplicate_codes = frame.loc[frame.kapt_code.duplicated(keep=False), "kapt_code"].unique().tolist()
    conflicts = frame[frame.kapt_code.isin(duplicate_codes)].sort_values("kapt_code").copy()
    frame = frame[~frame.kapt_code.isin(duplicate_codes)].copy()

    frame["latitude"] = pd.to_numeric(frame.latitude, errors="coerce")
    frame["longitude"] = pd.to_numeric(frame.longitude, errors="coerce")
    frame["households"] = pd.to_numeric(frame.households, errors="coerce")
    if "approval_date" in frame:
        frame["approval_date"] = pd.to_datetime(frame.approval_date, errors="coerce")

    numeric_bad = frame.latitude.isna() | frame.longitude.isna()
    range_bad = ~frame.latitude.between(-90, 90) | ~frame.longitude.between(-180, 180)
    swapped_likely = frame.latitude.between(127, 131) & frame.longitude.between(33, 37)

    stations = station_coordinates.dropna(subset=["latitude", "longitude"])
    nearest_station_m = nearest_distance_m(
        frame[["latitude", "longitude"]], stations[["latitude", "longitude"]]
    )
    frame["nearest_station_distance_m"] = nearest_station_m
    location_outlier = pd.Series(nearest_station_m > 100_000, index=frame.index)
    household_missing = frame.households.isna()
    household_invalid = frame.households.lt(0)
    valid_mask = ~(numeric_bad | range_bad | swapped_likely | location_outlier | household_missing | household_invalid)
    frame["is_valid_for_linkage"] = valid_mask
    frame["is_500plus_calculated"] = frame.households.ge(500)

    mismatch_500 = 0
    if "is_500plus" in frame:
        supplied = frame.is_500plus.astype("boolean")
        mismatch_500 = int((supplied.notna() & supplied.ne(frame.is_500plus_calculated)).sum())

    quality = {
        "input_rows": input_rows,
        "input_unique_complexes": int(raw.kapt_code.astype("string").nunique(dropna=True)),
        "missing_kapt_code_rows": missing_keys,
        "exact_duplicate_rows_removed": exact_duplicates,
        "conflicting_kapt_codes": len(duplicate_codes),
        "conflict_rows_excluded": len(conflicts),
        "coordinate_numeric_missing": int(numeric_bad.sum()),
        "coordinate_range_invalid": int(range_bad.sum()),
        "coordinate_swapped_likely": int(swapped_likely.sum()),
        "location_outliers_over_100km_from_station": int(location_outlier.sum()),
        "households_missing": int(household_missing.sum()),
        "households_invalid": int(household_invalid.sum()),
        "is_500plus_mismatches": mismatch_500,
        "valid_complexes": int(valid_mask.sum()),
        "complexes_500plus": int((valid_mask & frame.households.ge(500)).sum()),
        "approval_date_missing_or_invalid": int(frame.approval_date.isna().sum()) if "approval_date" in frame else len(frame),
    }
    return ApartmentBundle(frame.reset_index(drop=True), conflicts.reset_index(drop=True), quality)


def nearest_distance_m(points: pd.DataFrame, stations: pd.DataFrame) -> np.ndarray:
    if points.empty or stations.empty:
        return np.full(len(points), np.nan)
    p_lat = np.radians(points.latitude.to_numpy(float))[:, None]
    p_lon = np.radians(points.longitude.to_numpy(float))[:, None]
    s_lat = np.radians(stations.latitude.to_numpy(float))[None, :]
    s_lon = np.radians(stations.longitude.to_numpy(float))[None, :]
    a = np.sin((s_lat - p_lat) / 2) ** 2 + np.cos(p_lat) * np.cos(s_lat) * np.sin((s_lon - p_lon) / 2) ** 2
    return (2 * EARTH_RADIUS_M * np.arctan2(np.sqrt(a), np.sqrt(1 - a))).min(axis=1)


def build_station_links(apartments: pd.DataFrame, stations: pd.DataFrame) -> pd.DataFrame:
    """Build the full WGS84 great-circle distance matrix without radius filtering."""
    homes = apartments[apartments.is_valid_for_linkage].copy().reset_index(drop=True)
    stops = stations.dropna(subset=["latitude", "longitude"]).copy().reset_index(drop=True)
    if homes.empty or stops.empty:
        return pd.DataFrame(columns=["canonical_station_id", "kapt_code", "distance_m", "nearest_station_rank", "is_nearest_station"])
    hlat = np.radians(homes.latitude.to_numpy(float))[:, None]
    hlon = np.radians(homes.longitude.to_numpy(float))[:, None]
    slat = np.radians(stops.latitude.to_numpy(float))[None, :]
    slon = np.radians(stops.longitude.to_numpy(float))[None, :]
    a = np.sin((slat - hlat) / 2) ** 2 + np.cos(hlat) * np.cos(slat) * np.sin((slon - hlon) / 2) ** 2
    distances = 2 * EARTH_RADIUS_M * np.arctan2(np.sqrt(a), np.sqrt(1 - a))
    ranks = distances.argsort(axis=1).argsort(axis=1) + 1
    return pd.DataFrame({
        "kapt_code": np.repeat(homes.kapt_code.to_numpy(), len(stops)),
        "canonical_station_id": np.tile(stops.canonical_station_id.astype(str).to_numpy(), len(homes)),
        "distance_m": distances.ravel(),
        "nearest_station_rank": ranks.ravel(),
        "is_nearest_station": ranks.ravel() == 1,
    })[["canonical_station_id", "kapt_code", "distance_m", "nearest_station_rank", "is_nearest_station"]]


def build_artifacts(root: Path, source_path: Path | None = None) -> dict:
    source = source_path or (root / "data" / "interim" / "kapt_clean.parquet")
    if not source.is_file():
        raise FileNotFoundError(source)
    coordinates = pd.read_csv(root / "config" / "station_coordinates.csv", dtype={"canonical_station_id": str})
    metrics = pd.read_parquet(root / "data" / "processed" / "metro" / "station_metrics.parquet")
    stations = metrics[["canonical_station_id", "station_name", "line_id"]].merge(
        coordinates[["canonical_station_id", "latitude", "longitude"]], on="canonical_station_id", validate="one_to_one"
    )
    bundle = validate_apartments(read_apartment_parquet(source), stations)
    links = build_station_links(bundle.apartments, stations)
    output = root / "data" / "processed" / "apartments"
    report_dir = root / "reports" / "apartments"
    output.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    snapshot = bundle.apartments.copy()
    snapshot.to_parquet(output / "kapt_master.parquet", index=False)
    links.to_parquet(output / "station_apartment_links.parquet", index=False)
    links[links.nearest_station_rank.le(3)].to_parquet(output / "apartment_nearest_stations.parquet", index=False)
    bundle.conflicts.to_csv(report_dir / "kapt_conflicts.csv", index=False, encoding="utf-8-sig")
    default_links = links.merge(snapshot[["kapt_code", "households"]], on="kapt_code")
    default_links = default_links[default_links.distance_m.le(300) & default_links.households.ge(500)]
    metadata = {
        "source_path": str(source.resolve()), "extracted_at": date.today().isoformat(),
        "source_sha256": file_sha256(source), **bundle.quality,
        "station_count": int(stations.canonical_station_id.nunique()),
        "distance_method": "WGS84 haversine great-circle; mean Earth radius 6371008.8m",
        "all_station_complex_pairs": len(links),
        "default_300m_500plus_links": len(default_links),
        "default_300m_500plus_unique_complexes": int(default_links.kapt_code.nunique()),
    }
    (report_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame([{"metric": k, "value": v} for k, v in metadata.items()]).to_csv(
        report_dir / "quality_summary.csv", index=False, encoding="utf-8-sig"
    )
    return metadata

