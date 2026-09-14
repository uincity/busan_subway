from pathlib import Path

from streamlit.testing.v1 import AppTest


def test_dashboard_loads_without_exception():
    path = Path(__file__).resolve().parents[2] / "app.py"
    app = AppTest.from_file(path, default_timeout=30).run()
    assert not app.exception
    assert app.title[0].value == "부산 도시철도 역세권 수요 탐색"
    assert len(app.tabs) == 6
    assert any(tab.label == "뜨는 역 · 지는 역 TOP10" for tab in app.tabs)
