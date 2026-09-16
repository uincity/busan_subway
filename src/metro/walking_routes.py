from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .entrances import eligible_entrances, haversine_m

SCHEMA_VERSION = 1
CALCULATION_VERSION = "walking-v1"
DEFAULT_PROVIDER = "graphhopper"


class RouteProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class RouteConfig:
    provider: str = DEFAULT_PROVIDER
    method: str = "foot"
    version: str = CALCULATION_VERSION
    graphhopper_url: str = "https://graphhopper.com/api/1/route"
    graphhopper_key: str = ""

    @classmethod
    def from_env(cls) -> "RouteConfig":
        return cls(
            provider=os.getenv("BUSAN_METRO_ROUTE_PROVIDER", DEFAULT_PROVIDER).strip().lower(),
            method=os.getenv("BUSAN_METRO_ROUTE_METHOD", "foot").strip(),
            version=os.getenv("BUSAN_METRO_ROUTE_VERSION", CALCULATION_VERSION).strip(),
            graphhopper_url=os.getenv("GRAPHHOPPER_URL", "https://graphhopper.com/api/1/route").strip(),
            graphhopper_key=os.getenv("GRAPHHOPPER_API_KEY", "").strip(),
        )

    @property
    def available(self) -> bool:
        return self.provider == "graphhopper" and bool(self.graphhopper_key)


def persistent_db_path(root: Path) -> Path:
    override = os.getenv("BUSAN_METRO_ROUTE_DB", "").strip()
    return Path(override).expanduser() if override else root / "data" / "persistent" / "walking_routes.sqlite3"


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS walking_routes (
          route_key TEXT PRIMARY KEY,
          station_id TEXT NOT NULL, line_id TEXT NOT NULL,
          station_entrance_id TEXT NOT NULL, station_lat REAL NOT NULL, station_lon REAL NOT NULL,
          kapt_code TEXT NOT NULL, apartment_entrance_id TEXT NOT NULL,
          apartment_lat REAL NOT NULL, apartment_lon REAL NOT NULL,
          access_status TEXT, include_restricted INTEGER NOT NULL,
          distance_m REAL, duration_s REAL, route_json TEXT,
          provider TEXT NOT NULL, method TEXT NOT NULL, calculation_version TEXT NOT NULL,
          input_hash TEXT NOT NULL, status TEXT NOT NULL,
          error_message TEXT, calculated_at TEXT, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_routes_lookup
          ON walking_routes(station_id, line_id, include_restricted, kapt_code, status);
        """
    )
    return db


def _stable_hash(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_candidates(apartments: pd.DataFrame, station: pd.Series, entrances: pd.DataFrame, *,
                     include_restricted: bool, mode: str = "entrance_preferred",
                     candidate_radius_m: float = 1600) -> pd.DataFrame:
    """Return only one station's plausible apartments and every permitted entrance.

    Straight-line distance is used only as a conservative candidate filter, never as a
    walking-distance result.
    """
    homes = apartments[apartments.is_valid_for_linkage].copy()
    center_d = haversine_m(homes.latitude.to_numpy(float), homes.longitude.to_numpy(float),
                           float(station.latitude), float(station.longitude))
    homes = homes.assign(center_distance_m=center_d)
    homes = homes[homes.center_distance_m.le(candidate_radius_m)].copy()
    valid = eligible_entrances(entrances, include_restricted) if mode == "entrance_preferred" else entrances.iloc[0:0]
    grouped = {str(k): v for k, v in valid.groupby(valid.kapt_code.astype(str))}
    rows = []
    for home in homes.itertuples(index=False):
        gates = grouped.get(str(home.kapt_code))
        if gates is None or gates.empty:
            gates = pd.DataFrame([{
                "entrance_id": "center", "latitude": home.latitude, "longitude": home.longitude,
                "access_status": "단지 중심", "entrance_name": "단지 중심",
            }])
        for gate in gates.itertuples(index=False):
            payload = {
                "station_id": str(station.canonical_station_id), "line_id": str(station.line_id),
                "station_entrance_id": f"station-center:{station.canonical_station_id}",
                "station_lat": round(float(station.latitude), 7), "station_lon": round(float(station.longitude), 7),
                "kapt_code": str(home.kapt_code), "apartment_entrance_id": str(gate.entrance_id),
                "apartment_lat": round(float(gate.latitude), 7), "apartment_lon": round(float(gate.longitude), 7),
                "access_status": str(gate.access_status), "include_restricted": bool(include_restricted),
            }
            input_hash = _stable_hash(payload)
            identity = {
                "station_id": payload["station_id"], "line_id": payload["line_id"],
                "station_entrance_id": payload["station_entrance_id"], "kapt_code": payload["kapt_code"],
                "apartment_entrance_id": payload["apartment_entrance_id"],
                "include_restricted": payload["include_restricted"],
            }
            rows.append(payload | {"center_distance_m": float(home.center_distance_m),
                                   "input_hash": input_hash, "route_key": _stable_hash(identity)})
    return pd.DataFrame(rows)


def read_results(db_path: Path, candidates: pd.DataFrame, config: RouteConfig) -> pd.DataFrame:
    if candidates.empty:
        return candidates.assign(status=pd.Series(dtype=str))
    with connect(db_path) as db:
        marks = ",".join("?" for _ in candidates.route_key)
        stored = pd.read_sql_query(
            f"SELECT * FROM walking_routes WHERE route_key IN ({marks})", db,
            params=candidates.route_key.tolist(),
        )
    merged = candidates.merge(stored, on="route_key", how="left", suffixes=("", "_stored"))
    merged["result_status"] = np.where(merged.status.isna(), "미계산", merged.status)
    incompatible = merged.status.eq("완료") & (
        merged.provider.ne(config.provider) | merged.method.ne(config.method)
        | merged.calculation_version.ne(config.version) | merged.input_hash_stored.ne(merged.input_hash)
    )
    merged.loc[incompatible, "result_status"] = "갱신 필요"
    return merged


def best_results(results: pd.DataFrame) -> pd.DataFrame:
    if results.empty:
        return results
    ready = results[results.result_status.eq("완료") & results.distance_m.gt(0) & results.route_json.notna()].copy()
    if ready.empty:
        return ready
    return ready.sort_values("distance_m").drop_duplicates("kapt_code", keep="first")


def _graphhopper_route(config: RouteConfig, start_lat: float, start_lon: float,
                       end_lat: float, end_lon: float) -> tuple[float, float, list[list[float]]]:
    if not config.available:
        raise RouteProviderError("GRAPHHOPPER_API_KEY가 설정되지 않았습니다.")
    query = urllib.parse.urlencode({
        "point": [f"{start_lat},{start_lon}", f"{end_lat},{end_lon}"],
        "profile": config.method, "points_encoded": "false", "key": config.graphhopper_key,
    }, doseq=True)
    request = urllib.request.Request(f"{config.graphhopper_url}?{query}", headers={"User-Agent": "busan-metro/1"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.load(response)
        path = data["paths"][0]
        coords = path["points"]["coordinates"]
        distance, duration = float(path["distance"]), float(path["time"]) / 1000
    except Exception as exc:
        raise RouteProviderError(str(exc)) from exc
    if distance <= 0 or duration <= 0 or len(coords) < 2:
        raise RouteProviderError("경로 제공자가 유효하지 않은 거리·시간·좌표를 반환했습니다.")
    return distance, duration, coords


def calculate_pending(db_path: Path, candidates: pd.DataFrame, config: RouteConfig, *,
                      retry_failed: bool = False, limit: int | None = None,
                      progress=None) -> dict[str, int]:
    results = read_results(db_path, candidates, config)
    wanted = results.result_status.isin(["미계산", "갱신 필요"] + (["실패"] if retry_failed else []))
    todo = results[wanted].copy()
    if limit is not None:
        todo = todo.head(limit)
    counts = {"완료": 0, "실패": 0, "건너뜀": int(len(results) - len(todo))}
    now = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect(db_path) as db:
        for index, row in enumerate(todo.itertuples(index=False), 1):
            # BEGIN IMMEDIATE + status check prevents duplicate work across sessions/processes.
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT status, input_hash, updated_at FROM walking_routes WHERE route_key=?", (row.route_key,)).fetchone()
            if current and current["status"] == "계산 중":
                db.rollback(); counts["건너뜀"] += 1; continue
            values = (row.route_key, row.station_id, row.line_id, row.station_entrance_id,
                      row.station_lat, row.station_lon, row.kapt_code, row.apartment_entrance_id,
                      row.apartment_lat, row.apartment_lon, row.access_status, int(row.include_restricted),
                      config.provider, config.method, config.version, row.input_hash, "계산 중", now())
            db.execute("""INSERT INTO walking_routes
              (route_key,station_id,line_id,station_entrance_id,station_lat,station_lon,kapt_code,
               apartment_entrance_id,apartment_lat,apartment_lon,access_status,include_restricted,
               provider,method,calculation_version,input_hash,status,updated_at)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(route_key) DO UPDATE SET status=excluded.status,error_message=NULL,updated_at=excluded.updated_at""", values)
            db.commit()
            try:
                distance, duration, coords = _graphhopper_route(
                    config, row.station_lat, row.station_lon, row.apartment_lat, row.apartment_lon)
                db.execute("""UPDATE walking_routes SET distance_m=?,duration_s=?,route_json=?,status='완료',
                           error_message=NULL,calculated_at=?,updated_at=? WHERE route_key=?""",
                           (distance, duration, json.dumps(coords, separators=(",", ":")), now(), now(), row.route_key))
                counts["완료"] += 1
            except Exception as exc:
                db.execute("""UPDATE walking_routes SET distance_m=NULL,duration_s=NULL,route_json=NULL,status='실패',
                           error_message=?,calculated_at=?,updated_at=? WHERE route_key=?""",
                           (str(exc)[:1000], now(), now(), row.route_key))
                counts["실패"] += 1
            db.commit()
            if progress:
                progress(index, len(todo), counts)
            time.sleep(0.05)
    return counts


def status_counts(results: pd.DataFrame) -> dict[str, int]:
    if results.empty:
        return {}
    return results.result_status.value_counts().astype(int).to_dict()
