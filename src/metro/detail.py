from __future__ import annotations

import calendar
from io import BytesIO
from pathlib import Path

import numpy as np
import pandas as pd

EVENT_COLUMNS = [
    "event_id", "station_id", "event_type", "title", "announcement_date",
    "actual_start_date", "end_date", "date_precision", "event_status",
    "latitude", "longitude", "distance_m", "scale_value", "scale_unit",
    "source_url", "source_title", "publisher", "source_published_date",
    "checked_at", "verification_status", "expected_pattern", "notes",
]
EVENT_TYPES = ["아파트 입주", "기업·공공기관 이전", "학교·병원·대형 점포 개설 또는 확장",
               "관광시설·축제·행사", "버스 노선·배차 변경", "역 출입구·보행 접근성 개선",
               "공사·운휴·도로 통제 등 일시적 영향", "기타"]


def load_ridership(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path)


def complete_common_dates(data: pd.DataFrame, station_id: str, start: pd.Timestamp,
                          end: pd.Timestamp) -> pd.DatetimeIndex:
    """Dates with every direction/hour cell present for the selected station."""
    part = data[data.canonical_station_id.astype(str).eq(str(station_id)) &
                data.service_date.between(start, end) & data.quality_flag.eq("ok")]
    expected = part[["direction", "time_bin_start"]].drop_duplicates().shape[0]
    counts = part.groupby("service_date")[["direction", "time_bin_start"]].apply(
        lambda x: len(x.drop_duplicates()))
    return pd.DatetimeIndex(counts[counts.eq(expected)].index)


def period_daily(data: pd.DataFrame, station_id: str, year: int, metric: str,
                 through_month: int | None = None) -> tuple[float, int, pd.Timestamp | None]:
    part = data[data.canonical_station_id.astype(str).eq(str(station_id)) &
                data.service_date.dt.year.eq(year) & data.quality_flag.eq("ok")]
    if through_month is not None:
        part = part[part.service_date.dt.month.le(through_month)]
    if metric != "total":
        part = part[part.direction.eq(metric)]
    daily = part.groupby("service_date")["count"].sum()
    return (float(daily.mean()) if len(daily) else np.nan, int(len(daily)),
            daily.index.max() if len(daily) else None)


def monthly_yoy(data: pd.DataFrame, station_id: str, metric: str = "total") -> pd.DataFrame:
    valid = data[data.canonical_station_id.astype(str).eq(str(station_id)) & data.quality_flag.eq("ok")].copy()
    if metric != "total":
        valid = valid[valid.direction.eq(metric)]
    daily = valid.groupby("service_date", as_index=False)["count"].sum()
    daily["month"] = daily.service_date.dt.to_period("M")
    out = daily.groupby("month", as_index=False).agg(value=("count", "mean"), valid_days=("service_date", "nunique"))
    out["expected_days"] = out.month.map(lambda p: calendar.monthrange(p.year, p.month)[1])
    out["complete"] = out.valid_days.eq(out.expected_days)
    prior = out[["month", "value"]].copy(); prior["month"] = prior.month + 12
    out = out.merge(prior.rename(columns={"value": "prior_value"}), on="month", how="left")
    out["yoy_pct"] = np.where(out.prior_value.gt(0), (out.value / out.prior_value - 1) * 100, np.nan)
    out["month_date"] = out.month.dt.to_timestamp()
    return out


def network_monthly_yoy(data: pd.DataFrame, exclude_station_id: str, metric: str = "total") -> pd.DataFrame:
    """Network trend from the processed calendar-month station totals."""
    station_month = data[~data.canonical_station_id.astype(str).eq(str(exclude_station_id))].copy()
    station_month["month"] = pd.to_datetime(dict(year=station_month.year, month=station_month.month_num, day=1)).dt.to_period("M")
    station_month["value"] = station_month[metric] / station_month.observed_days.replace(0, np.nan)
    net = station_month.groupby("month", as_index=False).value.sum().rename(columns={"value": "network_value"})
    prior = net.copy(); prior["month"] = prior.month + 12
    net = net.merge(prior.rename(columns={"network_value": "network_prior"}), on="month", how="left")
    net["network_yoy_pct"] = np.where(net.network_prior.gt(0), (net.network_value/net.network_prior-1)*100, np.nan)
    return net[["month", "network_yoy_pct"]]


def positive_streaks(monthly: pd.DataFrame, minimum: int = 3) -> list[tuple[pd.Period, pd.Period]]:
    rows = monthly[monthly.complete & monthly.yoy_pct.notna()].sort_values("month")
    periods, run = [], []
    for row in rows.itertuples():
        contiguous = not run or row.month == run[-1] + 1
        if row.yoy_pct > 0 and contiguous: run.append(row.month)
        elif row.yoy_pct > 0: run = [row.month]
        else:
            if len(run) >= minimum: periods.append((run[0], run[-1]))
            run = []
    if len(run) >= minimum: periods.append((run[0], run[-1]))
    return periods


def time_pattern(data: pd.DataFrame, station_id: str, baseline_year: int, target_year: int,
                 through_month: int | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    part = data[data.canonical_station_id.astype(str).eq(str(station_id)) &
                data.service_date.dt.year.isin([baseline_year, target_year]) & data.quality_flag.eq("ok")].copy()
    if through_month is not None: part = part[part.service_date.dt.month.le(through_month)]
    part["day_type"] = np.select([part.service_date.dt.dayofweek.lt(5), part.service_date.dt.dayofweek.eq(5)],
                                 ["평일", "토요일"], default="일요일")
    # 공휴일 달력이 없으므로 법정공휴일은 평일에 포함될 수 있다.
    part["year"] = part.service_date.dt.year
    daily = part.groupby(["year", "service_date", "day_type", "time_bin_start", "direction"], as_index=False)["count"].sum()
    cell = daily.groupby(["year", "day_type", "time_bin_start", "direction"], as_index=False).agg(avg=("count", "mean"), valid_days=("service_date", "nunique"))
    b = cell[cell.year.eq(baseline_year)].drop(columns="year").rename(columns={"avg":"baseline_avg", "valid_days":"baseline_days"})
    t = cell[cell.year.eq(target_year)].drop(columns="year").rename(columns={"avg":"target_avg", "valid_days":"target_days"})
    change = b.merge(t, on=["day_type","time_bin_start","direction"], how="outer")
    change["absolute_change"] = change.target_avg - change.baseline_avg
    change["low_base"] = change.baseline_avg.lt(30)
    days = part[["service_date", "day_type"]].drop_duplicates().groupby([part.service_date.dt.year.rename("year"), "day_type"]).size().unstack(0)
    day_summary = change.groupby("day_type", as_index=False).agg(baseline_avg=("baseline_avg","sum"), target_avg=("target_avg","sum"), absolute_change=("absolute_change","sum"))
    return change, day_summary


def recommend_peers(data: pd.DataFrame, metrics: pd.DataFrame, station_id: str,
                    cutoff: pd.Timestamp, count: int = 5) -> pd.DataFrame:
    """Select peers using pre-cutoff observations only."""
    pre = data.copy(); pre["month_date"] = pd.to_datetime(pre.month.astype(str) + "-01")
    pre = pre[pre.month_date.lt(cutoff) & pre.month_date.ge(cutoff-pd.DateOffset(months=12))]
    feats = pre.groupby("canonical_station_id").agg(scale=("daily_ridership","mean"),
                                                     variability=("daily_ridership","std"),
                                                     trend=("daily_ridership", lambda s: s.iloc[-1]-s.iloc[0] if len(s)>1 else np.nan))
    feats = feats.join(metrics.set_index("canonical_station_id")[["station_type","line_id"]], how="left")
    sid = str(station_id)
    if sid not in feats.index or len(feats) < 2: return pd.DataFrame()
    numeric = ["scale","variability","trend"]
    z = (feats[numeric] - feats[numeric].mean()) / feats[numeric].std().replace(0, 1)
    dist = ((z-z.loc[sid])**2).sum(axis=1).pow(.5)
    dist += (feats.station_type.ne(feats.loc[sid,"station_type"])*.75 + feats.line_id.ne(feats.loc[sid,"line_id"])*.25)
    out = feats.assign(similarity_distance=dist).drop(index=sid).nsmallest(count, "similarity_distance").reset_index()
    out["selection_reason"] = "증가 전 12개월 평일 규모·월별 추세·변동성·역 유형·호선 유사도"
    out["cutoff_date"] = cutoff.date().isoformat()
    return out


def event_template_bytes() -> bytes:
    return pd.DataFrame(columns=EVENT_COLUMNS).to_csv(index=False).encode("utf-8-sig")


def validate_events(source, station_ids: set[str]) -> tuple[pd.DataFrame, list[str]]:
    try: frame = pd.read_csv(source, dtype=str).reindex(columns=EVENT_COLUMNS)
    except Exception as exc: return pd.DataFrame(columns=EVENT_COLUMNS), [f"CSV를 읽을 수 없습니다: {exc}"]
    errors = []
    required = ["event_id","station_id","event_type","title"]
    for col in required:
        if frame[col].isna().any() or frame[col].astype(str).str.strip().eq("").any(): errors.append(f"필수 열 '{col}'에 빈 값이 있습니다.")
    if frame.event_id.duplicated().any(): errors.append("event_id가 중복되었습니다.")
    invalid_ids = sorted(set(frame.station_id.dropna().astype(str)) - station_ids)
    if invalid_ids: errors.append("알 수 없는 역 ID: " + ", ".join(invalid_ids[:10]))
    invalid_types = sorted(set(frame.event_type.dropna()) - set(EVENT_TYPES))
    if invalid_types: errors.append("허용되지 않은 사건 유형: " + ", ".join(invalid_types))
    for col in ["announcement_date","actual_start_date","end_date","source_published_date","checked_at"]:
        raw = frame[col].dropna().astype(str).str.strip(); bad = raw.ne("") & pd.to_datetime(raw, errors="coerce").isna()
        if bad.any(): errors.append(f"'{col}' 날짜 형식이 올바르지 않습니다(YYYY-MM-DD 권장).")
    return frame, errors


def hypotheses(change: pd.DataFrame, events: pd.DataFrame, peer_difference: float | None = None) -> pd.DataFrame:
    def total(day, hours, direction):
        q = change[change.day_type.eq(day) & change.time_bin_start.isin(hours) & change.direction.eq(direction)]
        return q.absolute_change.sum()
    observations = [
        ("주거 수요 증가", total("평일", [7,8], "board") + total("평일", [17,18,19], "alight"), "평일 아침 승차와 저녁 하차"),
        ("업무·통학 수요 증가", total("평일", [7,8], "alight") + total("평일", [17,18,19], "board"), "평일 아침 하차와 저녁 승차"),
        ("생활서비스 수요 증가", sum(total("평일", [h], d) for h in range(10,17) for d in ["board","alight"]), "평일 낮"),
        ("상권·관광·여가 수요 증가", sum(total(day, [h], d) for day in ["토요일","일요일"] for h in range(12,21) for d in ["board","alight"]), "주말 오후·저녁"),
    ]
    rows=[]
    for cause, value, label in observations:
        linked = events[events.expected_pattern.fillna("").str.contains(cause.split()[0], na=False)] if not events.empty else events
        confirmed = linked[linked.verification_status.eq("확인") & linked.actual_start_date.notna()] if not linked.empty else linked
        level = "근거 강함" if value > 0 and not confirmed.empty and peer_difference is not None and peer_difference > 0 else ("일부 부합" if value > 0 else "확인 불가")
        rows.append({"원인 후보":cause,"관측된 증가 패턴":f"{label} 순증가 {value:+,.0f}건/해당 요일 일",
                     "연결된 사건과 출처":"; ".join(linked.title.dropna()) if len(linked) else "확인 자료 없음",
                     "시간적 선후관계":"실제 시작일 확인 필요" if confirmed.empty else "확인된 실제 시작일 있음",
                     "예상 패턴과 실제 패턴의 일치":"부합" if value>0 else "불일치 또는 감소",
                     "비교역 대비 차이":f"{peer_difference:+.1f}%p" if peer_difference is not None else "자료 부족",
                     "반대 근거·대안 설명":"공통 추세·요일 구성·일시 행사 가능성 검토 필요",
                     "근거 수준":level,"추가 확인 사항":"공식 사건 자료와 장기 사전 추세 확인"})
    return pd.DataFrame(rows)
