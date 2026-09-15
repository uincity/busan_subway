from __future__ import annotations

import hashlib
import io
import os
import shutil
import tempfile
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .apartments import EARTH_RADIUS_M

ENTRANCE_COLUMNS = [
    "entrance_id", "kapt_code", "entrance_name", "latitude", "longitude",
    "entrance_type", "access_status", "access_note", "verification_status",
    "enabled", "source_type", "source_reference", "verified_at", "created_at",
    "updated_at", "notes",
]
ENTRANCE_TYPES = ["보행 전용", "보행·차량 겸용", "차량 전용", "미확인"]
ACCESS_STATUSES = ["상시 통행", "시간제한", "입주민 전용", "폐쇄", "미확인"]
VERIFICATION_STATUSES = ["미확인", "사용자 확인"]
SOURCE_TYPES = ["현장 확인", "지도 확인", "기타"]
CALCULABLE_TYPES = {"보행 전용", "보행·차량 겸용"}
DEFAULT_ACCESS = {"상시 통행"}


class EntranceDataError(ValueError):
    pass


class EntranceConflictError(RuntimeError):
    pass


def empty_entrances() -> pd.DataFrame:
    return pd.DataFrame(columns=ENTRANCE_COLUMNS)


def entrance_revision(path: Path) -> str:
    if not path.exists():
        return "missing"
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_entrances(raw: pd.DataFrame) -> pd.DataFrame:
    missing = sorted(set(ENTRANCE_COLUMNS) - set(raw.columns))
    if missing:
        raise EntranceDataError("필수 열이 없습니다: " + ", ".join(missing))
    frame = raw[ENTRANCE_COLUMNS].copy()
    for col in [c for c in ENTRANCE_COLUMNS if c not in {"latitude", "longitude", "enabled"}]:
        frame[col] = frame[col].fillna("").astype(str).str.strip()
    frame["latitude"] = pd.to_numeric(frame.latitude, errors="coerce")
    frame["longitude"] = pd.to_numeric(frame.longitude, errors="coerce")
    if frame.enabled.dtype == bool:
        frame["enabled"] = frame.enabled.astype(bool)
    else:
        values = frame.enabled.astype(str).str.strip().str.lower()
        invalid_bool = ~values.isin({"true", "false", "1", "0"})
        if invalid_bool.any():
            raise EntranceDataError("enabled는 true/false여야 합니다.")
        frame["enabled"] = values.isin({"true", "1"})
    problems: list[str] = []
    if frame.entrance_id.eq("").any() or frame.entrance_id.duplicated().any():
        problems.append("entrance_id가 비어 있거나 중복되었습니다")
    if frame.kapt_code.eq("").any(): problems.append("kapt_code가 비어 있습니다")
    if (frame.entrance_name.eq("")).any(): problems.append("entrance_name이 비어 있습니다")
    if (frame.latitude.isna() | ~frame.latitude.between(-90, 90)).any(): problems.append("위도 범위가 잘못되었습니다")
    if (frame.longitude.isna() | ~frame.longitude.between(-180, 180)).any(): problems.append("경도 범위가 잘못되었습니다")
    if (~frame.entrance_type.isin(ENTRANCE_TYPES)).any(): problems.append("entrance_type 값이 잘못되었습니다")
    if (~frame.access_status.isin(ACCESS_STATUSES)).any(): problems.append("access_status 값이 잘못되었습니다")
    if (~frame.verification_status.isin(VERIFICATION_STATUSES)).any(): problems.append("verification_status 값이 잘못되었습니다")
    if (~frame.source_type.isin(SOURCE_TYPES)).any(): problems.append("source_type 값이 잘못되었습니다")
    if problems:
        raise EntranceDataError("; ".join(problems))
    return frame.reset_index(drop=True)


def read_entrances(path: Path) -> tuple[pd.DataFrame, str]:
    if not path.exists():
        return empty_entrances(), "missing"
    return normalize_entrances(pd.read_csv(path, dtype={"entrance_id": str, "kapt_code": str})), entrance_revision(path)


def save_entrances_atomic(frame: pd.DataFrame, path: Path, expected_revision: str | None = None) -> str:
    clean = normalize_entrances(frame)
    current = entrance_revision(path)
    if expected_revision is not None and current != expected_revision:
        raise EntranceConflictError("다른 세션에서 출입구 파일을 변경했습니다. 다시 불러온 뒤 병합해 주세요.")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        clean.to_csv(temp, index=False, encoding="utf-8-sig")
        if path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        os.replace(temp, path)
    finally:
        if temp.exists(): temp.unlink()
    return entrance_revision(path)


def parse_entrance_csv(content: bytes) -> pd.DataFrame:
    try:
        return normalize_entrances(pd.read_csv(io.BytesIO(content), dtype={"entrance_id": str, "kapt_code": str}))
    except Exception as exc:
        if isinstance(exc, EntranceDataError): raise
        raise EntranceDataError(f"CSV를 읽을 수 없습니다: {exc}") from exc


def new_entrance_id() -> str:
    return "ent_" + uuid.uuid4().hex


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def haversine_m(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, [lat1, lon1, lat2, lon2])
    a = np.sin((lat2-lat1)/2)**2 + np.cos(lat1)*np.cos(lat2)*np.sin((lon2-lon1)/2)**2
    return 2 * EARTH_RADIUS_M * np.arctan2(np.sqrt(a), np.sqrt(1-a))


def validate_candidate(candidate: dict, apartments: pd.DataFrame, entrances: pd.DataFrame,
                       duplicate_tolerance_m: float = 2.0, far_warning_m: float = 1000.0) -> dict:
    code = str(candidate.get("kapt_code", "")).strip()
    match = apartments[apartments.kapt_code.astype(str).eq(code)]
    errors, warnings = [], []
    if match.empty: errors.append("kapt_code가 현재 아파트 마스터에 없습니다.")
    try: lat, lon = float(candidate.get("latitude")), float(candidate.get("longitude"))
    except (TypeError, ValueError):
        return {"errors": errors + ["좌표는 숫자여야 합니다."], "warnings": warnings, "center_distance_m": None}
    if not (-90 <= lat <= 90 and -180 <= lon <= 180): errors.append("위도·경도 범위를 벗어났습니다.")
    if 127 <= lat <= 131 and 33 <= lon <= 37: errors.append("부산 좌표에서 위도·경도가 뒤바뀐 것으로 보입니다.")
    center_distance = None
    if not match.empty and not errors:
        center_distance = float(haversine_m(match.iloc[0].latitude, match.iloc[0].longitude, lat, lon))
        if center_distance > far_warning_m:
            warnings.append(f"중심점에서 {center_distance:,.1f}m 떨어져 있습니다(점검 기준 {far_warning_m:,.0f}m). 단지 경계 판정 결과는 아닙니다.")
    peers = entrances[entrances.kapt_code.astype(str).eq(code)]
    current_id = str(candidate.get("entrance_id", ""))
    peers = peers[~peers.entrance_id.astype(str).eq(current_id)]
    if not peers.empty and not errors:
        distances = haversine_m(peers.latitude.to_numpy(float), peers.longitude.to_numpy(float), lat, lon)
        if np.any(distances <= duplicate_tolerance_m):
            errors.append(f"동일 단지의 기존 출입구와 {duplicate_tolerance_m:g}m 이내로 중복됩니다.")
    return {"errors": errors, "warnings": warnings, "center_distance_m": center_distance}


def eligible_entrances(entrances: pd.DataFrame, include_restricted: bool = False) -> pd.DataFrame:
    allowed = DEFAULT_ACCESS | ({"시간제한", "입주민 전용"} if include_restricted else set())
    return entrances[
        entrances.enabled.astype(bool)
        & entrances.verification_status.eq("사용자 확인")
        & entrances.entrance_type.isin(CALCULABLE_TYPES)
        & entrances.access_status.isin(allowed)
    ].copy()


def build_effective_station_links(apartments: pd.DataFrame, stations: pd.DataFrame, entrances: pd.DataFrame,
                                  *, mode: str = "entrance_preferred", include_restricted: bool = False) -> pd.DataFrame:
    homes = apartments[apartments.is_valid_for_linkage].copy().reset_index(drop=True)
    stops = stations.dropna(subset=["latitude", "longitude"]).copy().reset_index(drop=True)
    rows: list[dict] = []
    valid = eligible_entrances(entrances, include_restricted)
    by_code = {k: v for k, v in valid.groupby("kapt_code")}
    for _, home in homes.iterrows():
        gates = by_code.get(str(home.kapt_code)) if mode == "entrance_preferred" else None
        for _, stop in stops.iterrows():
            center = float(haversine_m(home.latitude, home.longitude, stop.latitude, stop.longitude))
            selected = None
            entrance_distance = np.nan
            if gates is not None and not gates.empty:
                ds = haversine_m(gates.latitude.to_numpy(float), gates.longitude.to_numpy(float), stop.latitude, stop.longitude)
                selected = gates.iloc[int(np.argmin(ds))]
                entrance_distance = float(np.min(ds))
            rows.append({
                "canonical_station_id": str(stop.canonical_station_id), "kapt_code": str(home.kapt_code),
                "center_distance_m": center, "entrance_distance_m": entrance_distance,
                "effective_distance_m": entrance_distance if selected is not None else center,
                "coordinate_basis": "보행 출입구" if selected is not None else "단지 중심",
                "selected_entrance_id": None if selected is None else selected.entrance_id,
                "selected_entrance_name": None if selected is None else selected.entrance_name,
                "selected_access_status": None if selected is None else selected.access_status,
                "selected_verified_at": None if selected is None else selected.verified_at,
                "selected_latitude": float(home.latitude if selected is None else selected.latitude),
                "selected_longitude": float(home.longitude if selected is None else selected.longitude),
            })
    out = pd.DataFrame(rows)
    if out.empty: return out
    out["nearest_station_rank"] = out.groupby("kapt_code")["effective_distance_m"].rank(method="first").astype(int)
    out["is_nearest_station"] = out.nearest_station_rank.eq(1)
    out["distance_m"] = out.effective_distance_m
    return out


def merge_import(existing: pd.DataFrame, incoming: pd.DataFrame, action: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    incoming = normalize_entrances(incoming)
    existing = normalize_entrances(existing) if len(existing) else empty_entrances()
    overlap = set(existing.entrance_id) & set(incoming.entrance_id)
    changes = incoming.assign(change=incoming.entrance_id.map(lambda x: "수정" if x in overlap else "추가"))
    if overlap and action == "새 ID만 추가":
        incoming = incoming[~incoming.entrance_id.isin(overlap)]
        result = pd.concat([existing, incoming], ignore_index=True)
    elif action == "동일 ID 업데이트":
        result = pd.concat([existing[~existing.entrance_id.isin(incoming.entrance_id)], incoming], ignore_index=True)
    else:
        result = pd.concat([existing, incoming[~incoming.entrance_id.isin(overlap)]], ignore_index=True)
    return normalize_entrances(result) if len(result) else empty_entrances(), changes
