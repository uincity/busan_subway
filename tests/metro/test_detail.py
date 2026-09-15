from io import BytesIO

import pandas as pd

from src.metro.detail import positive_streaks, time_pattern, validate_events


def test_positive_streak_ignores_incomplete_month():
    frame = pd.DataFrame({
        "month": pd.period_range("2025-01", periods=4, freq="M"),
        "complete": [True, True, False, True], "yoy_pct": [1, 2, 10, 3],
    })
    assert positive_streaks(frame, 3) == []


def test_time_pattern_uses_each_day_type_valid_days():
    rows = []
    for date, value in [("2024-01-01", 10), ("2024-01-02", 20), ("2025-01-01", 30)]:
        for direction in ["board", "alight"]:
            rows.append({"canonical_station_id":"101", "service_date":pd.Timestamp(date),
                         "quality_flag":"ok", "direction":direction,
                         "time_bin_start":7, "count":value})
    change, summary = time_pattern(pd.DataFrame(rows), "101", 2024, 2025)
    weekday = summary.set_index("day_type").loc["평일"]
    assert weekday.baseline_avg == 30  # 두 방향 각각 (10+20)/2
    assert weekday.target_avg == 60
    assert weekday.absolute_change == 30


def test_event_validation_keeps_announcement_separate_from_actual_date():
    frame = pd.DataFrame([{"event_id":"e1","station_id":"101","event_type":"아파트 입주",
        "title":"계획", "announcement_date":"2025-01-01", "actual_start_date":"",
        "event_status":"계획"}])
    for col in set(__import__("src.metro.detail", fromlist=["EVENT_COLUMNS"]).EVENT_COLUMNS)-set(frame): frame[col]=""
    parsed, errors = validate_events(BytesIO(frame.to_csv(index=False).encode()), {"101"})
    assert not errors
    assert parsed.loc[0, "announcement_date"] == "2025-01-01"
    assert pd.isna(parsed.loc[0, "actual_start_date"]) or parsed.loc[0, "actual_start_date"] == ""
