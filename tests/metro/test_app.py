from pathlib import Path
import json
import uuid

from streamlit.testing.v1 import AppTest


def test_dashboard_loads_without_exception(monkeypatch):
    monkeypatch.setenv("BUSAN_METRO_ROUTE_DB", str(Path(".tmp") / f"app-routes-{uuid.uuid4().hex}.sqlite3"))
    path = Path(__file__).resolve().parents[2] / "app.py"
    app = AppTest.from_file(path, default_timeout=30).run()
    assert not app.exception
    assert app.title[0].value == "부산 도시철도 역세권 수요 탐색"

    # 사이드바 제작자 링크 검증
    link_buttons = app.sidebar.get("link_button")
    assert len(link_buttons) == 1
    assert link_buttons[0].proto.label == "제작자: 열심남"
    assert link_buttons[0].proto.url == "https://uincity.github.io/"

    # 사이드바 메뉴 라디오 버튼 검증
    assert len(app.sidebar.radio) == 1
    menu_radio = app.sidebar.radio[0]
    expected_menus = [
        "역세권 지표",
        "출퇴근 성격",
        "장기 변화",
        "뜨는 역 · 지는 역 TOP10",
        "아파트 연결",
        "데이터 상태",
    ]
    assert list(menu_radio.options) == expected_menus
    assert menu_radio.value == "역세권 지표"

    # 1. 기본 메뉴: 역세권 지표 검증
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

    # 2. 메뉴 전환: 출퇴근 성격 검증
    app_commute = AppTest.from_file(path, default_timeout=30)
    app_commute.session_state["main_menu"] = "출퇴근 성격"
    app_commute.run()
    assert not app_commute.exception
    assert len(app_commute.get("deck_gl_json_chart")) >= 1
    map_spec = json.loads(app_commute.get("deck_gl_json_chart")[0].proto.json)
    map_layer = map_spec["layers"][0]
    assert map_layer["@@type"] == "ScatterplotLayer"
    assert len(map_layer["data"]) == 112
    assert map_layer["getFillColor"] == "@@=marker_color"
    assert map_layer["getRadius"] == "@@=marker_radius"

    # 3. 메뉴 전환: 아파트 연결 검증
    app_apt = AppTest.from_file(path, default_timeout=30)
    app_apt.session_state["main_menu"] = "아파트 연결"
    app_apt.run()
    assert not app_apt.exception
    assert len(app_apt.get("deck_gl_json_chart")) >= 1
    apartment_map = json.loads(app_apt.get("deck_gl_json_chart")[0].proto.json)
    layer_ids = [layer["id"] for layer in apartment_map["layers"]]
    assert layer_ids[0] == "apartment-radius"
    assert layer_ids[-2:] == ["selected-station", "selected-station-label"]
    assert apartment_map["layers"][0]["@@type"] == "GeoJsonLayer"
    assert apartment_map["layers"][-2]["@@type"] == "ScatterplotLayer"
    assert apartment_map["layers"][-1]["@@type"] == "TextLayer"

    station_widget = next(widget for widget in app_apt.selectbox if widget.label == "역")
    assert station_widget.value == "213"
    restricted_widget = next(widget for widget in app_apt.checkbox if widget.label == "시간제한·입주민 전용 포함")
    assert restricted_widget.value is True
    station_widget.set_value("214")
    restricted_widget.set_value(False)
    app_apt.run()
    assert next(widget for widget in app_apt.selectbox if widget.label == "역").value == "214"
    assert next(widget for widget in app_apt.checkbox if widget.label == "시간제한·입주민 전용 포함").value is False
