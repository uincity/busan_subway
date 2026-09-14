from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from src.metro.growth import build_period_totals

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data"
INTERIM = ROOT / "data" / "interim" / "metro"
PROCESSED = ROOT / "data" / "processed" / "metro"
REPORTS = ROOT / "reports" / "metro"
CONFIG = ROOT / "config" / "metro_analysis.yaml"
OBS_KEY = ["service_date", "source_station_code", "direction", "time_bin_start"]


def read_config() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_csv_flexible(path: Path, nrows: int | None = None) -> tuple[pd.DataFrame, str]:
    errors = []
    for enc in ("utf-8-sig", "cp949", "utf-8"):
        try:
            return pd.read_csv(path, dtype=str, encoding=enc, nrows=nrows), enc
        except (UnicodeDecodeError, pd.errors.ParserError) as exc:
            errors.append(f"{enc}: {exc}")
    raise ValueError(f"읽을 수 없는 CSV: {path}\n" + "\n".join(errors))


def _hour_column(name: str) -> tuple[int, int] | None:
    nums = re.findall(r"\d{1,2}", str(name))
    if len(nums) != 2 or "시" not in str(name):
        return None
    start, end = map(int, nums)
    return start, end


def inspect_files() -> pd.DataFrame:
    rows = []
    for path in sorted(RAW.rglob("*.csv")):
        df, enc = read_csv_flexible(path)
        date_col = next((c for c in df.columns if c.strip() == "년월일"), None)
        dates = pd.to_datetime(df[date_col].astype(str).str.strip(), errors="coerce") if date_col else pd.Series(dtype="datetime64[ns]")
        rows.append({
            "source_file_id": sha256(path)[:16], "file_name": path.name,
            "relative_path": str(path.relative_to(RAW)),
            "sha256": sha256(path), "bytes": path.stat().st_size, "encoding": enc,
            "rows": len(df), "date_min": dates.min(), "date_max": dates.max(),
            "column_count": len(df.columns), "read_status": "성공",
        })
    out = pd.DataFrame(rows)
    if not out.empty:
        out["is_exact_duplicate"] = out.duplicated("sha256", keep=False)
    return out


def _normalize_one(path: Path, file_id: str) -> pd.DataFrame:
    wide, _ = read_csv_flexible(path)
    wide.columns = [str(c).strip() for c in wide.columns]
    required = {"역번호", "역명", "년월일", "구분"}
    if not required.issubset(wide.columns):
        raise ValueError(f"필수 열 누락: {path.name} ({required - set(wide.columns)})")
    hours = {c: _hour_column(c) for c in wide.columns}
    hours = {c: v for c, v in hours.items() if v is not None}
    base = wide[["역번호", "역명", "년월일", "구분", *hours]].copy()
    long = base.melt(["역번호", "역명", "년월일", "구분"], var_name="time_bin_label", value_name="count")
    long["service_date"] = pd.to_datetime(long.pop("년월일").astype(str).str.strip(), errors="coerce")
    long["source_station_code"] = long.pop("역번호").astype(str).str.strip().str.replace(r"\.0$", "", regex=True).str.zfill(3)
    long["station_name"] = long.pop("역명").astype(str).str.replace(r"\s+", "", regex=True)
    long["direction"] = long.pop("구분").astype(str).str.strip().map({"승차": "board", "하차": "alight"})
    long["count"] = pd.to_numeric(long["count"].astype(str).str.replace(",", "", regex=False).str.strip(), errors="coerce")
    long["time_bin_start"] = long["time_bin_label"].map(lambda x: hours[x][0] % 24)
    long["time_bin_end"] = long["time_bin_label"].map(lambda x: hours[x][1] % 24)
    station_number = pd.to_numeric(long["source_station_code"], errors="coerce")
    long["line_id"] = np.select(
        [station_number.lt(200), station_number.between(200, 299), station_number.between(300, 399), station_number.between(400, 499)],
        ["1", "2", "3", "4"], default=None)
    long["operator"] = "부산교통공사"
    long["counting_unit_id"] = long["source_station_code"]
    long["canonical_station_id"] = long["source_station_code"]
    long["source_file_id"] = file_id
    long["quality_flag"] = np.select(
        [long.service_date.isna(), long.direction.isna(), long["count"].isna(), long["count"].lt(0)],
        ["invalid_date", "invalid_direction", "missing_count", "negative_count"], default="ok")
    return long[["service_date", "operator", "source_station_code", "canonical_station_id", "station_name",
                 "line_id", "counting_unit_id", "direction", "time_bin_start", "time_bin_end",
                 "time_bin_label", "count", "source_file_id", "quality_flag"]]


def select_canonical_files(inventory: pd.DataFrame) -> pd.DataFrame:
    """연도별로 가장 넓은 정본 하나를 선택하며 번호 복사·누적본은 후순위로 둔다."""
    candidates = inventory.copy()
    candidates["is_numbered_copy"] = candidates["file_name"].str.contains(r"\s\(\d+\)\.csv$", regex=True)
    # 내용이 완전히 같은 복사본도 무번호 원본명을 먼저 보존한다.
    candidates = candidates.sort_values(["is_numbered_copy", "file_name"]).drop_duplicates("sha256").copy()
    candidates["year"] = pd.to_datetime(candidates["date_min"]).dt.year
    candidates["span_days"] = (pd.to_datetime(candidates["date_max"]) - pd.to_datetime(candidates["date_min"])).dt.days
    chosen = (candidates.sort_values(
        ["year", "span_days", "is_numbered_copy", "rows", "file_name"],
        ascending=[True, False, True, False, True])
        .drop_duplicates("year", keep="first").copy())
    chosen["selection_reason"] = "연도별 최장 날짜범위; 동일 범위에서는 무번호 파일 우선"
    return chosen


def normalize() -> tuple[pd.DataFrame, pd.DataFrame]:
    inventory = inspect_files()
    INTERIM.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)
    inventory.to_csv(REPORTS / "raw_file_manifest.csv", index=False, encoding="utf-8-sig")
    # 동일 SHA는 한 번만 읽고, 연도별 가장 넓은 파일을 정본으로 선택한다.
    # 공공데이터포털의 부분 재게시 파일을 모두 melt하면 메모리만 소모하고 결과는 같다.
    chosen = select_canonical_files(inventory)
    chosen[["year", "file_name", "relative_path", "date_min", "date_max", "rows", "sha256", "selection_reason"]].to_csv(
        REPORTS / "selected_source_files.csv", index=False, encoding="utf-8-sig")
    frames = []
    for row in chosen.itertuples():
        normalized = _normalize_one(RAW / row.relative_path, row.source_file_id)
        # 원본에 다른 연도 날짜가 잘못 섞여 있어도 해당 연도 정본으로 유입시키지 않는다.
        frames.append(normalized[normalized.service_date.dt.year.eq(row.year)])
    data = pd.concat(frames, ignore_index=True)
    invalid = data.quality_flag.ne("ok")
    duplicate_count = int(data.duplicated(OBS_KEY, keep=False).sum())
    data = data.sort_values(OBS_KEY + ["source_file_id"]).drop_duplicates(OBS_KEY, keep="last")
    data.to_parquet(INTERIM / "ridership_long.parquet", index=False)
    quality = pd.DataFrame({"metric": ["normalized_rows", "invalid_rows", "overlapping_rows_before_dedup", "date_min", "date_max"],
                            "value": [len(data), int(invalid.sum()), duplicate_count, str(data.service_date.min().date()), str(data.service_date.max().date())]})
    quality.to_csv(REPORTS / "quality_summary.csv", index=False, encoding="utf-8-sig")
    return data, quality


def build_metrics(data: pd.DataFrame | None = None) -> pd.DataFrame:
    cfg = read_config()
    if data is None:
        data = pd.read_parquet(INTERIM / "ridership_long.parquet")
    valid = data.query("quality_flag == 'ok'").copy()
    PROCESSED.mkdir(parents=True, exist_ok=True)
    station_master = build_station_name_master(valid)
    valid["station_name"] = valid["canonical_station_id"].map(
        station_master.set_index("canonical_station_id")["canonical_station_name"])
    station_master.to_csv(PROCESSED / "station_name_master.csv", index=False, encoding="utf-8-sig")
    annual, calendar_monthly = build_period_totals(valid, station_master)
    annual.to_parquet(PROCESSED / "station_annual.parquet", index=False)
    calendar_monthly.to_parquet(PROCESSED / "station_calendar_monthly.parquet", index=False)
    valid["is_weekday"] = valid.service_date.dt.dayofweek.lt(5)
    valid = valid[valid.is_weekday]
    valid["period"] = np.select([
        valid.time_bin_start.between(cfg["morning_start"], cfg["morning_end"] - 1),
        valid.time_bin_start.between(cfg["evening_start"], cfg["evening_end"] - 1)], ["AM", "PM"], default="other")
    peak = valid[valid.period.ne("other")].groupby(["service_date", "canonical_station_id", "station_name", "line_id", "period", "direction"], dropna=False)["count"].sum().unstack(["period", "direction"], fill_value=0)
    for col in [("AM", "board"), ("AM", "alight"), ("PM", "board"), ("PM", "alight")]:
        if col not in peak: peak[col] = 0
    peak.columns = [f"{a}_{b}" for a, b in peak.columns]
    peak = peak.reset_index()
    daily_total = valid.groupby(["service_date", "canonical_station_id"])["count"].sum().rename("daily_ridership")
    peak = peak.join(daily_total, on=["service_date", "canonical_station_id"])
    latest_month = peak.service_date.max().to_period("M")
    recent_start = (latest_month - (cfg["default_recent_months"] - 1)).start_time
    recent_peak = peak[peak.service_date.ge(recent_start)].copy()
    station = recent_peak.groupby(["canonical_station_id", "station_name", "line_id"], dropna=False).agg(
        valid_weekdays=("service_date", "nunique"), daily_ridership=("daily_ridership", "mean"),
        AM_board=("AM_board", "mean"), AM_alight=("AM_alight", "mean"),
        PM_board=("PM_board", "mean"), PM_alight=("PM_alight", "mean")).reset_index()
    station["am_direction"] = (station.AM_board - station.AM_alight) / (station.AM_board + station.AM_alight).replace(0, np.nan)
    station["pm_direction"] = (station.PM_alight - station.PM_board) / (station.PM_alight + station.PM_board).replace(0, np.nan)
    station["residential_score"] = 50 * (0.5 * (station.am_direction + station.pm_direction) + 1)
    station["employment_score"] = 100 - station.residential_score
    low = (station.valid_weekdays < cfg["minimum_valid_weekdays"]) | (station[["AM_board", "AM_alight", "PM_board", "PM_alight"]].sum(axis=1) < cfg["minimum_daily_peak_count"])
    t = cfg["classification_threshold"]
    station["station_type"] = np.select([
        low | station.am_direction.isna() | station.pm_direction.isna(),
        (station.am_direction >= t) & (station.pm_direction >= t),
        (station.am_direction <= -t) & (station.pm_direction <= -t),
        (station.am_direction.abs() < t) & (station.pm_direction.abs() < t)],
        ["자료부족/판정보류", "주거 출발형 추정", "업무·통학 도착형 추정", "방향 균형형"], default="혼합형")
    station["low_sample_flag"] = low
    station.to_parquet(PROCESSED / "station_metrics.parquet", index=False)
    monthly = peak.assign(month=peak.service_date.dt.to_period("M").astype(str)).groupby(["month", "canonical_station_id", "station_name"], as_index=False).agg(daily_ridership=("daily_ridership", "mean"), valid_days=("service_date", "nunique"))
    monthly.to_parquet(PROCESSED / "station_monthly.parquet", index=False)
    build_five_year_growth(monthly).to_parquet(PROCESSED / "station_growth_5y.parquet", index=False)
    return station


def build_station_name_master(data: pd.DataFrame) -> pd.DataFrame:
    """역코드를 기준으로 명칭 변경 이력을 연결하고 환승역만 호선을 붙여 구분한다."""
    names = data[["canonical_station_id", "line_id", "station_name", "service_date"]].drop_duplicates().copy()
    latest = names.sort_values("service_date").groupby("canonical_station_id", as_index=False).tail(1)
    latest["base_name"] = latest.apply(
        lambda r: re.sub(rf"^{re.escape(str(r.line_id))}(?=\D)", "", str(r.station_name)), axis=1)
    duplicate_names = latest.groupby("base_name")["canonical_station_id"].nunique()
    duplicate_names = set(duplicate_names[duplicate_names.gt(1)].index)
    latest["canonical_station_name"] = latest.apply(
        lambda r: f"{r.base_name}({r.line_id}호선)" if r.base_name in duplicate_names else r.base_name, axis=1)
    aliases = (names.groupby("canonical_station_id")["station_name"]
               .agg(lambda s: " | ".join(sorted(set(map(str, s))))).rename("source_name_aliases"))
    bounds = names.groupby("canonical_station_id").service_date.agg(first_observed="min", last_observed="max")
    result = latest[["canonical_station_id", "line_id", "canonical_station_name"]].join(
        aliases, on="canonical_station_id").join(bounds, on="canonical_station_id")
    return result.sort_values(["line_id", "canonical_station_id"]).reset_index(drop=True)


def build_five_year_growth(monthly: pd.DataFrame) -> pd.DataFrame:
    """최신 12개월과 정확히 5년 전 같은 12개월을 유효 평일 가중평균으로 비교한다."""
    frame = monthly.copy()
    frame["period"] = pd.PeriodIndex(frame["month"], freq="M")
    latest_end = frame["period"].max()
    latest_start = latest_end - 11
    baseline_start, baseline_end = latest_start - 60, latest_end - 60
    current = frame[frame.period.between(latest_start, latest_end)].copy()
    baseline = frame[frame.period.between(baseline_start, baseline_end)].copy()
    current["match_period"] = current.period - 60
    comparable = current.merge(
        baseline[["canonical_station_id", "period", "daily_ridership", "valid_days"]],
        left_on=["canonical_station_id", "match_period"], right_on=["canonical_station_id", "period"],
        suffixes=("_current", "_baseline"))
    comparable["current_total"] = comparable.daily_ridership_current * comparable.valid_days_current
    comparable["baseline_total"] = comparable.daily_ridership_baseline * comparable.valid_days_baseline
    result = comparable.groupby("canonical_station_id", as_index=False).agg(
        station_name=("station_name", "last"), comparable_months=("match_period", "nunique"),
        current_total=("current_total", "sum"), baseline_total=("baseline_total", "sum"),
        current_days=("valid_days_current", "sum"), baseline_days=("valid_days_baseline", "sum"))
    result["current_daily"] = result.current_total / result.current_days
    result["baseline_daily"] = result.baseline_total / result.baseline_days
    result["absolute_change"] = result.current_daily - result.baseline_daily
    result["change_pct"] = np.where(result.baseline_daily.gt(0), (result.current_daily / result.baseline_daily - 1) * 100, np.nan)
    if result.empty:
        result["relative_growth_pct"] = pd.Series(dtype=float)
        result["current_window"] = pd.Series(dtype=str)
        result["baseline_window"] = pd.Series(dtype=str)
        return result
    network_current = result.current_total.sum() / result.current_days.sum()
    network_baseline = result.baseline_total.sum() / result.baseline_days.sum()
    result["relative_growth_pct"] = ((result.current_daily / result.baseline_daily) / (network_current / network_baseline) - 1) * 100
    result["current_window"] = f"{latest_start}~{latest_end}"
    result["baseline_window"] = f"{baseline_start}~{baseline_end}"
    return result


def write_source_reports(inventory: pd.DataFrame) -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    source = pd.DataFrame([{
        "provider": "부산교통공사", "dataset": "시간대별 승하차인원", "official_url": "https://www.data.go.kr/data/3057229/fileData.do",
        "access_method": "CSV 직접 다운로드 또는 활용신청 후 API", "published_coverage": "2026-01~2026-07", "local_coverage": f"{inventory.date_min.min():%Y-%m-%d}~{inventory.date_max.max():%Y-%m-%d}",
        "time_resolution": "일×역×승하차×1시간", "line_scope": "부산교통공사 1~4호선", "aggregation": "개찰구 승하차 연인원", "encoding": ", ".join(sorted(inventory.encoding.unique())),
        "update_cycle": "월간", "license": "이용허락범위 제한 없음", "access_verified": "공식 페이지 확인; 최신 파일은 로컬 미확보"
    }, {
        "provider": "부산교통공사", "dataset": "도시철도역사정보", "official_url": "https://www.data.go.kr/data/15043686/fileData.do",
        "access_method": "CSV 직접 다운로드", "published_coverage": "기준일 2021-02-26", "local_coverage": "미확보", "time_resolution": "기준일 스냅샷",
        "line_scope": "역사", "aggregation": "역사 속성", "encoding": "미확인", "update_cycle": "비정기", "license": "공식 페이지 참조", "access_verified": "페이지 확인; 파일 미확보"
    }])
    source.to_csv(REPORTS / "source_inventory.csv", index=False, encoding="utf-8-sig")
    local_max = pd.to_datetime(inventory.date_max).max()
    official_max = pd.Timestamp("2026-07-31")
    gap_note = ("- 로컬 자료가 공식 최신 공개 범위까지 확보되었다." if local_max >= official_max
                else f"- {local_max + pd.Timedelta(days=1):%Y-%m-%d} 이후는 현재 로컬 분석에서 **미확보**이며 0으로 간주하지 않는다.")
    md = f"""# 데이터 커버리지 보고서

- 로컬 원본: {len(inventory)}개(내용 기준 {inventory.sha256.nunique()}개)
- 실제 관측 범위: {inventory.date_min.min():%Y-%m-%d} ~ {inventory.date_max.max():%Y-%m-%d}
- 공식 페이지 최신 공개 범위: 2026-01-01 ~ 2026-07-31
{gap_note}
- 동일 SHA 파일은 1회만 처리하고, 부분 재게시본의 겹치는 관측은 `일자×역코드×승하차×시간대` 키로 제거한다.
- 환승역은 게이트 집계 단위를 보존한다. 수영역처럼 통합 게이트인 경우 임의로 호선별 배분하지 않는다.
"""
    (REPORTS / "coverage_report.md").write_text(md, encoding="utf-8")


def run_all() -> None:
    data, _ = normalize()
    build_metrics(data)
    inventory = pd.read_csv(REPORTS / "raw_file_manifest.csv", parse_dates=["date_min", "date_max"])
    write_source_reports(inventory)
    print(json.dumps({"status": "ok", "rows": len(data), "date_max": str(data.service_date.max().date())}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["inventory", "normalize", "metrics", "all"])
    args = parser.parse_args()
    if args.command == "inventory":
        inv = inspect_files(); write_source_reports(inv); print(inv.to_string(index=False))
    elif args.command == "normalize": normalize()
    elif args.command == "metrics": build_metrics()
    else: run_all()
