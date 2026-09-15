from pathlib import Path
import json

from streamlit.testing.v1 import AppTest


def test_dashboard_loads_without_exception():
    path = Path(__file__).resolve().parents[2] / "app.py"
    app = AppTest.from_file(path, default_timeout=30).run()
    assert not app.exception
    assert app.title[0].value == "부산 도시철도 역세권 수요 탐색"
    assert len(app.tabs) == 6
    assert any(tab.label == "뜨는 역 · 지는 역 TOP10" for tab in app.tabs)
    assert len(app.file_uploader) == 2  # 사건 CSV와 출입구 CSV 가져오기; 아파트는 로컬 Parquet 자동 로드
    assert len(app.get("deck_gl_json_chart")) == 2
    map_spec = json.loads(app.get("deck_gl_json_chart")[0].proto.json)
    map_layer = map_spec["layers"][0]
    assert map_layer["@@type"] == "ScatterplotLayer"
    assert len(map_layer["data"]) == 112
    assert map_layer["getFillColor"] == "@@=marker_color"
    assert map_layer["getRadius"] == "@@=marker_radius"
    apartment_map = json.loads(app.get("deck_gl_json_chart")[1].proto.json)
    layer_ids = [layer["id"] for layer in apartment_map["layers"]]
    assert layer_ids[0] == "apartment-radius"
    assert "selected-apartment" in layer_ids
    # 기본 역은 조건 충족 단지가 1개라 선택 단지 레이어만 존재한다.
    assert any(layer_id in layer_ids for layer_id in ["nearby-apartments", "selected-apartment"])
    assert layer_ids[-2:] == ["selected-station", "selected-station-label"]
    assert apartment_map["layers"][0]["@@type"] == "GeoJsonLayer"
    assert apartment_map["layers"][-2]["@@type"] == "ScatterplotLayer"
    assert apartment_map["layers"][-1]["@@type"] == "TextLayer"
    assert apartment_map["layers"][-2]["radiusUnits"] == "pixels"
    assert apartment_map["layers"][-1]["sizeUnits"] == "pixels"

    station_table = app.dataframe[0].value
    assert list(station_table.columns) == [
        "역 코드", "역명", "호선", "유효 평일수", "평일 일평균 승하차",
        "출근시간 승차", "출근시간 하차", "퇴근시간 승차", "퇴근시간 하차",
        "출근 방향성 지수", "퇴근 방향성 지수", "주거 출발형 점수",
        "업무·통학 도착형 점수", "역 유형", "표본 부족 여부",
    ]
    chart_spec = json.loads(app.get("vega_lite_chart")[0].proto.spec)
    assert chart_spec["encoding"]["y"]["sort"] == {
        "field": "daily_ridership", "order": "descending"
    }

    visible_english_columns = {
        "canonical_station_id", "station_name", "line_id", "station_type",
        "baseline_value", "target_value", "absolute_change", "change_pct",
        "relative_growth_pct", "increase_streak", "decrease_streak",
        "am_direction", "pm_direction", "daily_ridership", "metric", "value",
    }
    for table in app.dataframe:
        assert visible_english_columns.isdisjoint(set(table.value.columns))
