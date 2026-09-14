from __future__ import annotations

import calendar
from dataclasses import dataclass

import numpy as np
import pandas as pd

FORMULA_VERSION = "annual-growth-v1"


@dataclass(frozen=True)
class ComparisonSpec:
    baseline_year: int
    target_year: int
    metric: str = "total"
    ytd_month: int | None = None


def build_period_totals(data: pd.DataFrame, station_master: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build calendar-year and calendar-month totals directly from valid observations."""
    valid = data.loc[data["quality_flag"].eq("ok"), [
        "service_date", "canonical_station_id", "line_id", "direction", "count"
    ]].copy()
    name_map = station_master.set_index("canonical_station_id")["canonical_station_name"]
    valid["station_name"] = valid["canonical_station_id"].map(name_map)
    valid["year"] = valid.service_date.dt.year
    valid["month_num"] = valid.service_date.dt.month

    keys = ["year", "canonical_station_id", "station_name", "line_id"]
    totals = (valid.groupby(keys + ["direction"], dropna=False)["count"].sum()
              .unstack("direction", fill_value=0).reset_index())
    for col in ("board", "alight"):
        if col not in totals:
            totals[col] = 0.0
    totals["total"] = totals["board"] + totals["alight"]
    days = valid.groupby(keys, dropna=False).service_date.nunique().rename("observed_days").reset_index()
    annual = totals.merge(days, on=keys, validate="one_to_one")
    annual["expected_days"] = annual.year.map(lambda y: 366 if calendar.isleap(int(y)) else 365)
    annual["coverage_pct"] = annual.observed_days / annual.expected_days * 100
    annual["is_complete"] = annual.observed_days.eq(annual.expected_days)

    month_keys = keys + ["month_num"]
    monthly = (valid.groupby(month_keys + ["direction"], dropna=False)["count"].sum()
               .unstack("direction", fill_value=0).reset_index())
    for col in ("board", "alight"):
        if col not in monthly:
            monthly[col] = 0.0
    monthly["total"] = monthly["board"] + monthly["alight"]
    month_days = valid.groupby(month_keys, dropna=False).service_date.nunique().rename("observed_days").reset_index()
    monthly = monthly.merge(month_days, on=month_keys, validate="one_to_one")
    monthly["expected_days"] = monthly.apply(
        lambda r: calendar.monthrange(int(r.year), int(r.month_num))[1], axis=1)
    monthly["is_complete"] = monthly.observed_days.eq(monthly.expected_days)
    return annual, monthly


def completed_years(annual: pd.DataFrame) -> list[int]:
    status = annual.groupby("year").is_complete.all()
    return sorted(status[status].index.astype(int).tolist())


def compare_periods(annual: pd.DataFrame, monthly: pd.DataFrame, spec: ComparisonSpec) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return normal comparison rows and exclusions; never replace missing totals with zero."""
    if spec.metric not in {"total", "board", "alight"}:
        raise ValueError("metric must be total, board, or alight")
    source = annual
    if spec.ytd_month is not None:
        source = (monthly[monthly.month_num.le(spec.ytd_month)]
                  .groupby(["year", "canonical_station_id", "station_name", "line_id"], as_index=False)
                  .agg(board=("board", "sum"), alight=("alight", "sum"), total=("total", "sum"),
                       observed_days=("observed_days", "sum"), expected_days=("expected_days", "sum"),
                       is_complete=("is_complete", "all")))

    cols = ["canonical_station_id", "station_name", "line_id", spec.metric,
            "observed_days", "expected_days", "is_complete"]
    base = source[source.year.eq(spec.baseline_year)][cols].rename(columns={
        spec.metric: "baseline_value", "observed_days": "baseline_days",
        "expected_days": "baseline_expected_days", "is_complete": "baseline_complete"})
    target = source[source.year.eq(spec.target_year)][cols].rename(columns={
        "station_name": "target_station_name", "line_id": "target_line_id", spec.metric: "target_value",
        "observed_days": "target_days", "expected_days": "target_expected_days", "is_complete": "target_complete"})
    joined = base.merge(target, on="canonical_station_id", how="outer", indicator=True)
    joined["station_name"] = joined.station_name.fillna(joined.target_station_name)
    joined["line_id"] = joined.line_id.fillna(joined.target_line_id)
    baseline_complete = joined["baseline_complete"].astype("boolean").fillna(False).astype(bool)
    target_complete = joined["target_complete"].astype("boolean").fillna(False).astype(bool)
    joined["exclusion_reason"] = np.select([
        joined._merge.eq("left_only"), joined._merge.eq("right_only"),
        ~baseline_complete, ~target_complete,
        joined.baseline_value.eq(0)], [
        "대상기간 결측", "신규역 또는 기준기간 결측", "기준기간 불완전", "대상기간 불완전", "기준값 0"], default="")
    excluded = joined[joined.exclusion_reason.ne("")].copy()
    result = joined[joined.exclusion_reason.eq("")].copy()
    result["absolute_change"] = result.target_value - result.baseline_value
    result["change_pct"] = (result.target_value / result.baseline_value - 1) * 100
    years = spec.target_year - spec.baseline_year
    result["cagr_pct"] = np.where(
        (years >= 2) & result.baseline_value.gt(0) & result.target_value.gt(0),
        ((result.target_value / result.baseline_value) ** (1 / years) - 1) * 100, np.nan)
    network_base, network_target = result.baseline_value.sum(), result.target_value.sum()
    network_ratio = network_target / network_base if network_base > 0 else np.nan
    result["relative_growth_pct"] = (result.target_value / result.baseline_value / network_ratio - 1) * 100
    result["network_change_pct"] = (network_ratio - 1) * 100
    q20 = result.baseline_value.quantile(.2) if len(result) else np.nan
    result["low_base"] = result.baseline_value.le(q20)
    keep = ["canonical_station_id", "station_name", "line_id", "baseline_value", "target_value",
            "absolute_change", "change_pct", "cagr_pct", "relative_growth_pct", "low_base",
            "baseline_days", "target_days", "network_change_pct"]
    return result[keep].reset_index(drop=True), excluded[
        ["canonical_station_id", "station_name", "line_id", "exclusion_reason"]].reset_index(drop=True)


def add_streaks(comparison: pd.DataFrame, annual: pd.DataFrame, metric: str, target_year: int) -> pd.DataFrame:
    out = comparison.copy()
    history = annual[annual.is_complete & annual.year.le(target_year)].copy()
    history = history.sort_values(["canonical_station_id", "year"])
    streaks: dict[str, tuple[int, int, str]] = {}
    for station_id, group in history.groupby("canonical_station_id"):
        years = group.year.astype(int).tolist()
        vals = group[metric].tolist()
        changes = []
        for i in range(1, len(group)):
            changes.append(np.sign(vals[i] - vals[i - 1]) if years[i] == years[i - 1] + 1 else np.nan)
        up = down = 0
        for sign in reversed(changes):
            if sign == 1 and down == 0: up += 1
            elif sign == -1 and up == 0: down += 1
            else: break
        recent = changes[-3:]
        label = "판정불가"
        if len(recent) == 3 and not any(pd.isna(recent)):
            if all(x == 1 for x in recent): label = "3회 연속 증가"
            elif all(x == -1 for x in recent): label = "3회 연속 감소"
            elif recent[-1] == 1: label = "최근 반등"
            elif recent[-1] == -1: label = "최근 감소"
            else: label = "중립"
        streaks[str(station_id)] = (up, down, label)
    mapped = out.canonical_station_id.astype(str).map(streaks)
    out["increase_streak"] = mapped.map(lambda x: x[0] if isinstance(x, tuple) else 0)
    out["decrease_streak"] = mapped.map(lambda x: x[1] if isinstance(x, tuple) else 0)
    out["trend_badge"] = mapped.map(lambda x: x[2] if isinstance(x, tuple) else "판정불가")
    return out


def rank_changes(data: pd.DataFrame, rank_by: str = "change_pct", top_n: int = 10) -> tuple[pd.DataFrame, pd.DataFrame]:
    if rank_by not in {"change_pct", "absolute_change"}:
        raise ValueError("rank_by must be change_pct or absolute_change")
    tie = [rank_by, "absolute_change", "target_value", "canonical_station_id"]
    rising = data[data[rank_by].gt(0)].sort_values(tie, ascending=[False, False, False, True]).head(top_n).copy()
    falling = data[data[rank_by].lt(0)].sort_values(tie, ascending=[True, True, False, True]).head(top_n).copy()
    rising.insert(0, "rank", range(1, len(rising) + 1))
    falling.insert(0, "rank", range(1, len(falling) + 1))
    return rising, falling
