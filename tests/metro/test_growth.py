import numpy as np
import pandas as pd

from src.metro.growth import ComparisonSpec, add_streaks, compare_periods, rank_changes


def annual_rows(values, station_id="101", name="역A", complete=True):
    return pd.DataFrame([{"year": year, "canonical_station_id": station_id, "station_name": name, "line_id": "1",
        "board": value / 2, "alight": value / 2, "total": value, "observed_days": 365,
        "expected_days": 365, "coverage_pct": 100, "is_complete": complete} for year, value in values.items()])


def empty_monthly():
    return pd.DataFrame(columns=["year", "month_num", "canonical_station_id", "station_name", "line_id",
        "board", "alight", "total", "observed_days", "expected_days", "is_complete"])


def test_change_math_and_sign_ranking():
    annual = pd.concat([annual_rows({2024: 100, 2025: 120}, "101", "증가역"),
                        annual_rows({2024: 200, 2025: 150}, "102", "감소역"),
                        annual_rows({2024: 50, 2025: 50}, "103", "동일역")])
    result, excluded = compare_periods(annual, empty_monthly(), ComparisonSpec(2024, 2025))
    rows = result.set_index("canonical_station_id")
    assert rows.loc["101", "absolute_change"] == 20
    assert np.isclose(rows.loc["101", "change_pct"], 20)
    assert rows.loc["102", "absolute_change"] == -50
    assert np.isclose(rows.loc["102", "change_pct"], -25)
    rising, falling = rank_changes(result, top_n=10)
    assert rising.canonical_station_id.tolist() == ["101"]
    assert falling.canonical_station_id.tolist() == ["102"]
    assert excluded.empty


def test_missing_zero_and_partial_are_excluded_not_zero_filled():
    annual = pd.concat([annual_rows({2024: 0, 2025: 10}, "100"), annual_rows({2025: 20}, "101"),
        annual_rows({2024: 30}, "102"), annual_rows({2024: 40}, "103").assign(is_complete=False), annual_rows({2025: 50}, "103")])
    result, excluded = compare_periods(annual, empty_monthly(), ComparisonSpec(2024, 2025))
    assert result.empty
    assert {"기준값 0", "신규역 또는 기준기간 결측", "대상기간 결측", "기준기간 불완전"}.issubset(set(excluded.exclusion_reason))


def test_ties_use_unrounded_values_then_stable_station_id():
    frame = pd.DataFrame({"canonical_station_id": ["102", "101", "103"], "change_pct": [10.004, 10.004, 10.0039],
                          "absolute_change": [10, 10, 99], "target_value": [110, 110, 109]})
    rising, falling = rank_changes(frame, top_n=5)
    assert rising.canonical_station_id.tolist() == ["101", "102", "103"]
    assert falling.empty


def test_streak_requires_consecutive_years_and_zero_breaks_it():
    comparison = pd.DataFrame({"canonical_station_id": ["101", "102", "103"]})
    annual = pd.concat([annual_rows({2022: 10, 2023: 20, 2024: 30, 2025: 40}, "101"),
        annual_rows({2021: 10, 2023: 20, 2024: 30, 2025: 40}, "102"), annual_rows({2022: 10, 2023: 20, 2024: 20, 2025: 30}, "103")])
    result = add_streaks(comparison, annual, "total", 2025).set_index("canonical_station_id")
    assert result.loc["101", "increase_streak"] == 3
    assert result.loc["101", "trend_badge"] == "3회 연속 증가"
    assert result.loc["102", "increase_streak"] == 2
    assert result.loc["103", "increase_streak"] == 1


def test_relative_growth_baseline_is_unchanged_by_later_filters():
    annual = pd.concat([annual_rows({2024: 100, 2025: 120}, "101"), annual_rows({2024: 100, 2025: 100}, "102")])
    result, _ = compare_periods(annual, empty_monthly(), ComparisonSpec(2024, 2025))
    before = result.set_index("canonical_station_id").loc["101", "relative_growth_pct"]
    assert before == result[result.canonical_station_id.eq("101")].iloc[0].relative_growth_pct
    assert np.isclose(before, (1.2 / 1.1 - 1) * 100)


def test_ytd_compares_exactly_same_month_range():
    monthly = pd.DataFrame([{"year": year, "month_num": month, "canonical_station_id": "101", "station_name": "역A", "line_id": "1",
        "board": year + month, "alight": year + month, "total": 2 * (year + month), "observed_days": 30,
        "expected_days": 30, "is_complete": True} for year in (2025, 2026) for month in (1, 2, 3)])
    result, _ = compare_periods(annual_rows({2025: 999999, 2026: 999999}), monthly, ComparisonSpec(2025, 2026, "total", 2))
    assert result.iloc[0].baseline_value == sum(2 * (2025 + m) for m in (1, 2))
    assert result.iloc[0].target_value == sum(2 * (2026 + m) for m in (1, 2))
