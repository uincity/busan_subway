from pathlib import Path
import uuid

import pandas as pd

from src.metro.apartments import validate_apartments
from src.metro.entrances import ENTRANCE_COLUMNS, empty_entrances
from src.metro.walking_routes import (
    RouteConfig, best_results, build_candidates, calculate_pending,
    mark_apartments_stale, read_results,
)


def _station():
    return pd.Series({"canonical_station_id": "213", "line_id": "2", "station_name": "대연",
                      "latitude": 35.135153, "longitude": 129.092161})


def _apartments():
    raw = pd.DataFrame([{"kapt_code": "K1", "complex_name": "테스트", "latitude": 35.136,
                         "longitude": 129.093, "households": 700}])
    return validate_apartments(raw, pd.DataFrame([_station()])).apartments


def _gate(access="시간제한", longitude=129.0928):
    row = dict.fromkeys(ENTRANCE_COLUMNS, "")
    row.update({"entrance_id": "gate-1", "kapt_code": "K1", "entrance_name": "정문",
                "latitude": 35.1358, "longitude": longitude, "entrance_type": "보행 전용",
                "access_status": access, "verification_status": "사용자 확인", "enabled": True,
                "source_type": "기타"})
    return pd.DataFrame([row])


def test_restricted_conditions_are_separate_and_default_includes_gate():
    homes = _apartments()
    without = build_candidates(homes, _station(), _gate(), include_restricted=False)
    with_restricted = build_candidates(homes, _station(), _gate(), include_restricted=True)
    assert without.iloc[0].apartment_entrance_id == "center"
    assert with_restricted.iloc[0].apartment_entrance_id == "gate-1"
    assert without.iloc[0].route_key != with_restricted.iloc[0].route_key


def test_persistent_reuse_stale_detection_and_failed_not_zero(monkeypatch):
    db = Path(".tmp") / f"routes-{uuid.uuid4().hex}.sqlite3"
    config = RouteConfig(graphhopper_key="test", request_interval_s=0)
    candidates = build_candidates(_apartments(), _station(), _gate(), include_restricted=True)
    calls = []
    def fake_route(*args):
        calls.append(1)
        return 321.0, 240.0, [[129.092161, 35.135153], [129.0928, 35.1358]]
    monkeypatch.setattr("src.metro.walking_routes._graphhopper_route", fake_route)
    assert calculate_pending(db, candidates, config)["완료"] == 1
    assert calculate_pending(db, candidates, config)["완료"] == 0
    assert len(calls) == 1
    loaded = read_results(db, candidates, config)
    assert best_results(loaded).iloc[0].distance_m == 321.0

    changed = build_candidates(_apartments(), _station(), _gate(longitude=129.094), include_restricted=True)
    assert read_results(db, changed, config).iloc[0].result_status == "갱신 필요"

    def fail(*args):
        raise RuntimeError("provider down")
    monkeypatch.setattr("src.metro.walking_routes._graphhopper_route", fail)
    assert calculate_pending(db, changed, config)["실패"] == 1
    failed = read_results(db, changed, config).iloc[0]
    assert failed.result_status == "실패" and pd.isna(failed.distance_m)


def test_correction_marks_only_related_routes_and_recalculates_once(monkeypatch):
    db = Path(".tmp") / f"routes-{uuid.uuid4().hex}.sqlite3"
    config = RouteConfig(graphhopper_key="test", request_interval_s=0)
    first = build_candidates(_apartments(), _station(), _gate(access="상시 통행"), include_restricted=False)
    raw2 = pd.DataFrame([{"kapt_code": "K2", "complex_name": "다른 단지", "latitude": 35.1362,
                          "longitude": 129.0932, "households": 700}])
    homes2 = validate_apartments(raw2, pd.DataFrame([_station()])).apartments
    second = build_candidates(homes2, _station(), empty_entrances(), include_restricted=False)
    candidates = pd.concat([first, second], ignore_index=True)
    calls = []
    monkeypatch.setattr("src.metro.walking_routes._graphhopper_route", lambda *args: (
        calls.append(args) or (250.0, 180.0, [[129.092161, 35.135153], [129.093, 35.136]])
    ))
    assert calculate_pending(db, candidates, config)["완료"] == 2
    assert mark_apartments_stale(db, {"K1"}, reason="correction") == 1
    statuses = read_results(db, candidates, config).set_index("kapt_code").result_status.to_dict()
    assert statuses == {"K1": "갱신 필요", "K2": "완료"}
    only_changed = candidates[candidates.kapt_code.eq("K1")]
    assert calculate_pending(db, only_changed, config)["완료"] == 1
    assert calculate_pending(db, only_changed, config)["완료"] == 0
    assert len(calls) == 3
