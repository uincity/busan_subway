from pathlib import Path
import uuid

import numpy as np
import pandas as pd
import pytest

from src.metro.apartments import EARTH_RADIUS_M, validate_apartments
from src.metro.entrances import (
    ENTRANCE_COLUMNS, EntranceConflictError, build_effective_station_links,
    empty_entrances, entrance_revision, read_entrances, save_entrances_atomic,
)


def offset(m):
    return np.degrees(m / (EARTH_RADIUS_M * np.cos(np.radians(35.0))))


def stations():
    return pd.DataFrame([
        {"canonical_station_id": "A", "station_name": "서", "line_id": "1", "latitude": 35.0, "longitude": 129.0},
        {"canonical_station_id": "B", "station_name": "동", "line_id": "1", "latitude": 35.0, "longitude": 129.01},
    ])


def apartments(center_m=400):
    raw = pd.DataFrame([{"kapt_code": "K1", "complex_name": "테스트", "latitude": 35.0,
                         "longitude": 129.0 + offset(center_m), "households": 700}])
    return validate_apartments(raw, stations()).apartments


def gate(gate_id, meters, **updates):
    row = dict(zip(ENTRANCE_COLUMNS, [""] * len(ENTRANCE_COLUMNS)))
    row.update({"entrance_id": gate_id, "kapt_code": "K1", "entrance_name": gate_id,
                "latitude": 35.0, "longitude": 129.0 + offset(meters),
                "entrance_type": "보행 전용", "access_status": "상시 통행", "access_note": "",
                "verification_status": "사용자 확인", "enabled": True, "source_type": "기타",
                "source_reference": "test", "verified_at": "2026-01-01", "created_at": "x",
                "updated_at": "x", "notes": ""})
    row.update(updates)
    return row


def test_no_entrance_equals_center_and_multiple_gates_are_station_specific():
    homes = apartments()
    base = build_effective_station_links(homes, stations(), empty_entrances())
    assert np.allclose(base.center_distance_m, base.effective_distance_m)
    gates = pd.DataFrame([gate("west", 100), gate("east", 900)])
    links = build_effective_station_links(homes, stations(), gates)
    assert links.set_index("canonical_station_id").selected_entrance_id.to_dict() == {"A": "west", "B": "east"}


def test_exclusions_and_restricted_option_and_disable_delete_recalculate():
    homes = apartments()
    excluded = pd.DataFrame([
        gate("car", 50, entrance_type="차량 전용"), gate("closed", 60, access_status="폐쇄"),
        gate("unknown", 70, verification_status="미확인"), gate("timed", 80, access_status="시간제한"),
        gate("resident", 90, access_status="입주민 전용"), gate("off", 40, enabled=False),
    ])
    default = build_effective_station_links(homes, stations().iloc[:1], excluded)
    assert default.iloc[0].coordinate_basis == "단지 중심"
    allowed = build_effective_station_links(homes, stations().iloc[:1], excluded, include_restricted=True)
    assert allowed.iloc[0].selected_entrance_id == "timed"
    deleted = excluded[~excluded.entrance_id.isin(["timed", "resident"])]
    assert build_effective_station_links(homes, stations().iloc[:1], deleted).iloc[0].coordinate_basis == "단지 중심"


def test_radius_changes_and_no_duplicate_households():
    station = stations().iloc[:1]
    inside = build_effective_station_links(apartments(400), station, pd.DataFrame([gate("in", 250)]))
    assert inside.iloc[0].center_distance_m > 300 and inside.iloc[0].effective_distance_m <= 300
    outside = build_effective_station_links(apartments(200), station, pd.DataFrame([gate("out", 350)]))
    assert outside.iloc[0].center_distance_m <= 300 and outside.iloc[0].effective_distance_m > 300
    many = build_effective_station_links(apartments(400), station, pd.DataFrame([gate("a", 100), gate("b", 150)]))
    assert len(many) == 1
    total = apartments(400)[apartments(400).kapt_code.isin(many.kapt_code)].drop_duplicates("kapt_code").households.sum()
    assert total == 700


def test_atomic_save_reload_backup_and_conflict():
    folder = Path(".tmp") / ("entrance-test-" + uuid.uuid4().hex)
    folder.mkdir(parents=True)
    path = folder / "entrances.csv"
    first = pd.DataFrame([gate("a", 100)])
    rev1 = save_entrances_atomic(first, path, "missing")
    loaded, loaded_rev = read_entrances(path)
    assert loaded.iloc[0].entrance_id == "a" and loaded_rev == rev1
    second = pd.DataFrame([gate("b", 120)])
    rev2 = save_entrances_atomic(second, path, rev1)
    assert rev2 != rev1 and path.with_suffix(".csv.bak").exists()
    with pytest.raises(EntranceConflictError):
        save_entrances_atomic(first, path, rev1)
    assert entrance_revision(path) == rev2
