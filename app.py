from __future__ import annotations

import json
import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from src.metro.growth import ComparisonSpec, FORMULA_VERSION, add_streaks, compare_periods, completed_years, rank_changes
from src.metro.pipeline import PROCESSED, REPORTS, run_all

st.set_page_config(page_title="부산 도시철도 역세권 수요", page_icon=":material/subway:", layout="wide")
st.title("부산 도시철도 역세권 수요 탐색")
st.caption("승하차 건수의 규모·방향성과 연도별 변화를 탐색합니다. 승하차 합계는 고유 이용자 수가 아닌 연인원입니다.")


@st.cache_data(show_spinner="분석 자료를 준비하는 중입니다.")
def load_data():
    needed = ["station_metrics.parquet", "station_monthly.parquet", "station_annual.parquet", "station_calendar_monthly.parquet"]
    if not all((PROCESSED / name).exists() for name in needed):
        run_all()
    quality = pd.read_csv(REPORTS / "quality_summary.csv").set_index("metric")["value"]
    return (pd.read_parquet(PROCESSED / "station_metrics.parquet"),
            pd.read_parquet(PROCESSED / "station_monthly.parquet"),
            pd.read_parquet(PROCESSED / "station_annual.parquet"),
            pd.read_parquet(PROCESSED / "station_calendar_monthly.parquet"), quality)


def csv_bytes(frame: pd.DataFrame, metadata: dict) -> bytes:
    return ("# " + json.dumps(metadata, ensure_ascii=False) + "\n" + frame.to_csv(index=False)).encode("utf-8-sig")


def top_chart(frame: pd.DataFrame, value_col: str, color: str):
    if frame.empty:
        st.info("해당 부호의 후보 역이 없습니다.")
        return
    labels = frame.assign(chart_label=frame[value_col].map(lambda x: f"{x:+,.1f}"))
    bars = alt.Chart(labels).mark_bar(color=color).encode(
        x=alt.X(f"{value_col}:Q", title="증감률(%)" if value_col == "change_pct" else "증감 건수"),
        y=alt.Y("station_name:N", sort=alt.SortField(value_col), title=None),
        tooltip=[alt.Tooltip("station_name:N", title="역"), alt.Tooltip("baseline_value:Q", title="기준 이용량", format=",.0f"),
                 alt.Tooltip("target_value:Q", title="대상 이용량", format=",.0f"), alt.Tooltip("absolute_change:Q", title="절대 증감", format="+,.0f"),
                 alt.Tooltip("change_pct:Q", title="증감률", format="+.1f")])
    text = alt.Chart(labels).mark_text(align="left", dx=4).encode(
        x=f"{value_col}:Q", y=alt.Y("station_name:N", sort=alt.SortField(value_col)), text="chart_label:N")
    st.altair_chart((bars + text).properties(height=max(230, len(frame) * 30)))


metrics, monthly, annual, calendar_monthly, quality_summary = load_data()
date_min, date_max = quality_summary["date_min"], quality_summary["date_max"]
tabs = st.tabs(["역세권 지표", "출퇴근 성격", "장기 변화", "뜨는 역 · 지는 역 TOP10", "아파트 연결", "데이터 상태"])

with tabs[0]:
    with st.container(horizontal=True):
        st.metric("분석 역", f"{len(metrics):,}개", border=True)
        st.metric("최대 유효 평일", f"{metrics.valid_weekdays.max():,.0f}일", border=True)
        st.metric("최근 관측일", str(date_max), border=True)
    rank_col = st.selectbox("순위 기준", ["daily_ridership", "AM_board", "AM_alight", "PM_board", "PM_alight"],
                            format_func={"daily_ridership": "평일 일평균 승하차", "AM_board": "출근 승차", "AM_alight": "출근 하차", "PM_board": "퇴근 승차", "PM_alight": "퇴근 하차"}.get)
    st.bar_chart(metrics.nlargest(20, rank_col).sort_values(rank_col), x="station_name", y=rank_col, horizontal=True)
    st.dataframe(metrics.sort_values(rank_col, ascending=False), hide_index=True)

with tabs[1]:
    st.scatter_chart(metrics, x="am_direction", y="pm_direction", size="daily_ridership", color="station_type")
    st.caption("방향성 지수와 기존 유형 분류는 최신 12개월 평일 자료 기준입니다.")
    st.dataframe(metrics.sort_values("residential_score", ascending=False), hide_index=True)

with tabs[2]:
    defaults = metrics.nlargest(3, "daily_ridership").station_name.tolist()
    names = st.multiselect("비교 역(2~5개 권장)", sorted(monthly.station_name.unique()), default=defaults)
    st.line_chart(monthly[monthly.station_name.isin(names)], x="month", y="daily_ridership", color="station_name")

with tabs[3]:
    st.subheader("뜨는 역 · 지는 역 TOP10")
    complete = completed_years(annual)
    default_target = max(complete)
    all_targets = complete + ([int(annual.year.max())] if int(annual.year.max()) not in complete else [])
    mode = st.segmented_control("비교 모드", ["전년 대비", "3년 전 대비", "5년 전 대비", "직접 선택", "동일월 누적(YTD)"], default="전년 대비")
    cols = st.columns(5)
    with cols[0]: target_year = st.selectbox("대상연도", all_targets, index=all_targets.index(default_target), key="growth_target")
    gap = {"전년 대비": 1, "3년 전 대비": 3, "5년 전 대비": 5}.get(mode)
    with cols[1]:
        if mode == "직접 선택":
            choices = [y for y in complete if y < target_year]
            baseline_year = st.selectbox("기준연도", choices, index=len(choices) - 1, key="growth_base")
        else:
            baseline_year = target_year - 1 if mode == "동일월 누적(YTD)" else target_year - gap
            st.text_input("기준연도", str(baseline_year), disabled=True)
    with cols[2]: metric_label = st.selectbox("승하차 지표", ["승하차 합계", "승차", "하차"])
    with cols[3]: rank_label = st.selectbox("순위 기준", ["증감률", "절대 증감"], key="growth_rank")
    with cols[4]: top_n = st.selectbox("TOP 개수", [5, 10, 20], index=1)
    metric_col = {"승하차 합계": "total", "승차": "board", "하차": "alight"}[metric_label]
    rank_col = {"증감률": "change_pct", "절대 증감": "absolute_change"}[rank_label]
    ytd_month = None
    if mode == "동일월 누적(YTD)":
        available = calendar_monthly[calendar_monthly.year.eq(target_year)].month_num.max()
        ytd_month = int(available) if pd.notna(available) else None
        st.info(f"동일월 누적 비교: {target_year}년과 {baseline_year}년의 1~{ytd_month}월. 단순 연율화하지 않습니다.")
    if baseline_year >= target_year or baseline_year not in annual.year.unique():
        st.error("비교 가능한 기준연도가 없습니다. 기준연도는 대상연도보다 앞서야 합니다.")
        st.stop()

    comparison, excluded = compare_periods(annual, calendar_monthly, ComparisonSpec(baseline_year, target_year, metric_col, ytd_month))
    comparison = add_streaks(comparison, annual, metric_col, target_year)
    type_map = metrics.set_index("canonical_station_id")["station_type"]
    comparison["station_type"] = comparison.canonical_station_id.map(type_map).fillna("유형 미확인")
    fcols = st.columns(4)
    with fcols[0]: selected_lines = st.multiselect("노선", sorted(comparison.line_id.dropna().astype(str).unique()), default=[])
    with fcols[1]: selected_types = st.multiselect("역 유형(최신 12개월 기준)", sorted(comparison.station_type.unique()), default=[])
    with fcols[2]: min_base = st.number_input("최소 기준 이용량", min_value=0, value=0, step=100_000)
    with fcols[3]: search = st.text_input("역명 검색")
    filtered = comparison.copy()
    if selected_lines: filtered = filtered[filtered.line_id.astype(str).isin(selected_lines)]
    if selected_types: filtered = filtered[filtered.station_type.isin(selected_types)]
    filtered = filtered[filtered.baseline_value.ge(min_base)]
    if search: filtered = filtered[filtered.station_name.str.contains(search, case=False, na=False)]
    rising, falling = rank_changes(filtered, rank_col, top_n)

    with st.container(horizontal=True):
        st.metric("비교 가능 역", f"{len(comparison):,}개", border=True)
        st.metric("증가", f"{comparison.absolute_change.gt(0).sum():,}개", border=True)
        st.metric("감소", f"{comparison.absolute_change.lt(0).sum():,}개", border=True)
        st.metric("변화 없음", f"{comparison.absolute_change.eq(0).sum():,}개", border=True)
        network = comparison.network_change_pct.iloc[0] if len(comparison) else float("nan")
        st.metric("동일 역 전체 증감률", f"{network:+.1f}%", border=True)
        st.metric("비교 제외", f"{len(excluded):,}개", border=True)
    st.caption(f"{baseline_year}년 → {target_year}년 · {metric_label} · 유형은 최신 12개월({date_max} 기준) 분류입니다. 저기저는 전체 비교 가능 역의 하위 20%입니다.")
    if 2019 in (baseline_year, target_year) or 2020 in (baseline_year, target_year):
        st.warning("코로나19 영향을 받은 연도 또는 2019년을 포함한 기저효과 비교입니다.")
    left, right = st.columns(2)
    with left:
        st.subheader(f"뜨는 역 TOP{top_n}"); top_chart(rising, rank_col, "#2563EB")
    with right:
        st.subheader(f"지는 역 TOP{top_n}"); top_chart(falling, rank_col, "#F97316")
    table_cols = ["rank", "station_name", "line_id", "station_type", "baseline_value", "target_value", "absolute_change", "change_pct",
                  "relative_growth_pct", "increase_streak", "decrease_streak", "trend_badge", "low_base"]
    table = pd.concat([rising.assign(direction="증가"), falling.assign(direction="감소")], ignore_index=True)[["direction", *table_cols]]
    st.dataframe(table, hide_index=True, column_config={
        "baseline_value": st.column_config.NumberColumn("기준 이용량(건)", format="%,.0f"),
        "target_value": st.column_config.NumberColumn("대상 이용량(건)", format="%,.0f"),
        "absolute_change": st.column_config.NumberColumn("증감(건)", format="%+,.0f"),
        "change_pct": st.column_config.NumberColumn("증감률", format="%+.1f%%"),
        "relative_growth_pct": st.column_config.NumberColumn("상대 성장률", format="%+.1f%%")})
    st.subheader("지도")
    st.info("역 좌표와 행정구역 마스터가 확보되지 않아 지도 및 부산시내/시외·구군 필터는 비활성화했습니다. 좌표가 없는 역도 순위표에는 유지됩니다.")
    with st.expander(f"비교 제외 {len(excluded)}개 역과 사유"): st.dataframe(excluded, hide_index=True)
    metadata = {"baseline_year": baseline_year, "target_year": target_year, "metric": metric_col, "rank_by": rank_col,
                "ytd_month": ytd_month, "formula_version": FORMULA_VERSION, "data_through": str(date_max),
                "filters": {"lines": selected_lines, "types": selected_types, "minimum_baseline": min_base, "search": search}}
    with st.container(horizontal=True):
        st.download_button("연간 역별 지표 CSV", csv_bytes(annual, metadata), "station_annual.csv", "text/csv")
        st.download_button("기간비교 전체표 CSV", csv_bytes(comparison, metadata), "station_comparison.csv", "text/csv")
        st.download_button("뜨는 역 CSV", csv_bytes(rising, metadata), "rising_stations.csv", "text/csv")
        st.download_button("지는 역 CSV", csv_bytes(falling, metadata), "falling_stations.csv", "text/csv")
        st.download_button("비교제외 CSV", csv_bytes(excluded, metadata), "excluded_stations.csv", "text/csv")

    st.subheader("선택역 연간 추세")
    selected = st.multiselect("추세 비교 역(2~5개)", sorted(annual.station_name.unique()), default=rising.station_name.head(2).tolist(), key="annual_trend_stations")
    annual_trend = annual[annual.station_name.isin(selected) & annual.is_complete]
    st.line_chart(annual_trend, x="year", y=metric_col, color="station_name")
    common = annual_trend.pivot(index="year", columns="station_name", values=metric_col).dropna()
    positive = common[(common > 0).all(axis=1)]
    if not positive.empty:
        index_year = int(positive.index.min())
        indexed = common.div(common.loc[index_year]).mul(100).reset_index().melt("year", var_name="station_name", value_name="index")
        st.caption(f"공통 양수 기준연도 {index_year}=100 지수")
        st.line_chart(indexed, x="year", y="index", color="station_name")
    st.subheader("연도별 TOP 기록")
    record_choices = [y for y in complete if y - 1 in complete]
    record_year = st.selectbox("전년 대비 기록 연도", record_choices, index=len(record_choices) - 1)
    record, _ = compare_periods(annual, calendar_monthly, ComparisonSpec(record_year - 1, record_year, metric_col))
    record_up, record_down = rank_changes(record, rank_col, top_n)
    st.dataframe(pd.concat([record_up.assign(direction="증가"), record_down.assign(direction="감소")]), hide_index=True)
    st.caption(f"{record_year - 1}~{record_year} 비교 가능 표본 {len(record)}개. 선택한 지표·순위 기준·TOP 개수를 적용했습니다.")

with tabs[4]:
    st.info("아파트 자료와 역 좌표가 확보되면 Haversine 직선거리 기준 연결을 제공할 수 있습니다. 현재는 임의 좌표를 생성하지 않습니다.")
    template = pd.DataFrame(columns=["apartment_id", "apartment_name", "latitude", "longitude"])
    st.download_button("입력 템플릿 다운로드", template.to_csv(index=False).encode("utf-8-sig"), "apartment_template.csv", "text/csv")

with tabs[5]:
    st.subheader("품질 요약"); st.dataframe(pd.read_csv(REPORTS / "quality_summary.csv"), hide_index=True)
    st.subheader("원본 파일 매니페스트"); st.dataframe(pd.read_csv(REPORTS / "raw_file_manifest.csv"), hide_index=True)
    st.caption(f"원자료 관측 범위: {date_min} ~ {date_max}")
