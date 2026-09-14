import pandas as pd
from pathlib import Path
from unittest.mock import patch

from src.metro.pipeline import OBS_KEY, _hour_column, build_five_year_growth, build_metrics, build_station_name_master, read_config, select_canonical_files


def test_hour_header_variants():
    assert _hour_column("07시~08시") == (7, 8)
    assert _hour_column("24시-01시") == (24, 1)
    assert _hour_column("합계") is None


def test_classification_direction_and_no_duplicate_observation():
    days = pd.date_range("2024-01-01", periods=60, freq="B")
    rows = []
    values = {("AM", "board"): 200, ("AM", "alight"): 50, ("PM", "board"): 50, ("PM", "alight"): 200}
    for day in days:
        for period, hour in (("AM", 7), ("PM", 18)):
            for direction in ("board", "alight"):
                rows.append({"service_date": day, "operator":"부산교통공사", "source_station_code":"101", "canonical_station_id":"101", "station_name":"테스트", "line_id":"1", "counting_unit_id":"101", "direction":direction, "time_bin_start":hour, "time_bin_end":hour+1, "time_bin_label":f"{hour:02d}시", "count":values[(period, direction)], "source_file_id":"x", "quality_flag":"ok"})
    data = pd.DataFrame(rows)
    output = Path(".tmp/test_metrics")
    output.mkdir(parents=True, exist_ok=True)
    with patch("src.metro.pipeline.PROCESSED", output):
        result = build_metrics(data)
    assert result.loc[0, "station_type"] == "주거 출발형 추정"
    assert result.loc[0, "residential_score"] > 50
    assert not data.duplicated(OBS_KEY).any()


def test_thresholds_are_ordered():
    assert read_config()["sensitivity_thresholds"] == [0.10, 0.15, 0.20]


def test_five_year_growth_uses_matched_months():
    months = pd.period_range("2020-08", "2026-07", freq="M")
    frame = pd.DataFrame({"month": months.astype(str), "canonical_station_id": "101", "station_name": "테스트", "daily_ridership": [100 if p.year <= 2021 else 120 for p in months], "valid_days": 20})
    result = build_five_year_growth(frame).iloc[0]
    assert result.comparable_months == 12
    assert result.current_window == "2025-08~2026-07"
    assert result.baseline_window == "2020-08~2021-07"


def test_canonical_selection_prefers_full_unnumbered_file():
    inventory = pd.DataFrame([
        {"file_name":"자료(2020년) (1).csv", "relative_path":"자료(2020년) (1).csv", "sha256":"a", "rows":300, "date_min":"2020-01-01", "date_max":"2020-11-30"},
        {"file_name":"자료(2020년).csv", "relative_path":"자료(2020년).csv", "sha256":"b", "rows":365, "date_min":"2020-01-01", "date_max":"2020-12-31"},
        {"file_name":"자료복사 (1).csv", "relative_path":"자료복사 (1).csv", "sha256":"c", "rows":365, "date_min":"2021-01-01", "date_max":"2021-12-31"},
        {"file_name":"자료원본.csv", "relative_path":"자료원본.csv", "sha256":"c", "rows":365, "date_min":"2021-01-01", "date_max":"2021-12-31"},
    ])
    chosen = select_canonical_files(inventory).set_index("year")
    assert chosen.loc[2020, "file_name"] == "자료(2020년).csv"
    assert chosen.loc[2021, "file_name"] == "자료원본.csv"


def test_station_name_change_is_joined_by_code_and_transfer_line():
    rows = pd.DataFrame([
        {"canonical_station_id":"119", "line_id":"1", "station_name":"1서면", "service_date":pd.Timestamp("2023-08-31")},
        {"canonical_station_id":"119", "line_id":"1", "station_name":"서면", "service_date":pd.Timestamp("2023-09-01")},
        {"canonical_station_id":"219", "line_id":"2", "station_name":"서면", "service_date":pd.Timestamp("2023-09-01")},
        {"canonical_station_id":"213", "line_id":"2", "station_name":"대연", "service_date":pd.Timestamp("2023-09-01")},
    ])
    master = build_station_name_master(rows).set_index("canonical_station_id")
    assert master.loc["119", "canonical_station_name"] == "서면(1호선)"
    assert master.loc["219", "canonical_station_name"] == "서면(2호선)"
    assert master.loc["213", "canonical_station_name"] == "대연"
    assert master.loc["119", "source_name_aliases"] == "1서면 | 서면"
