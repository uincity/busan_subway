from __future__ import annotations

import json
import sys
from pathlib import Path

import altair as alt
import pandas as pd
import pydeck as pdk
import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from src.metro.growth import ComparisonSpec, FORMULA_VERSION, add_streaks, compare_periods, completed_years, rank_changes
from src.metro.pipeline import INTERIM, PROCESSED, REPORTS, run_all
from src.metro.detail_ui import render_station_detail
from src.metro.apartment_ui import render_apartment_tab

st.set_page_config(page_title="부산 도시철도 역세권 수요", page_icon=":material/subway:", layout="wide")
st.title("부산 도시철도 역세권 수요 탐색")
st.caption("승하차 건수의 규모·방향성과 연도별 변화를 탐색합니다. 승하차 합계는 고유 이용자 수가 아닌 연인원입니다.")


@st.cache_data(show_spinner="분석 자료를 준비하는 중입니다.")
def load_data():
    needed = ["station_metrics.parquet", "station_monthly.parquet", "station_annual.parquet", "station_calendar_monthly.parquet"]
    if not all((PROCESSED / name).exists() for name in needed):
        run_all()
    quality = pd.read_csv(REPORTS / "quality_summary.csv").set_index("metric")["value"]
    coordinates = pd.read_csv(ROOT / "config" / "station_coordinates.csv", dtype={"canonical_station_id": str})
    return (pd.read_parquet(PROCESSED / "station_metrics.parquet"),
            pd.read_parquet(PROCESSED / "station_monthly.parquet"),
            pd.read_parquet(PROCESSED / "station_annual.parquet"),
            pd.read_parquet(PROCESSED / "station_calendar_monthly.parquet"), coordinates, quality)


def csv_bytes(frame: pd.DataFrame, metadata: dict) -> bytes:
    return ("# " + json.dumps(metadata, ensure_ascii=False) + "\n" + frame.to_csv(index=False)).encode("utf-8-sig")


def top_chart(frame: pd.DataFrame, value_col: str, color: str, key: str | None = None):
    if frame.empty:
        st.info("해당 부호의 후보 역이 없습니다.")
        return None
    labels = frame.assign(chart_label=frame[value_col].map(lambda x: f"{x:+,.1f}"))
    bars = alt.Chart(labels).mark_bar(color=color).encode(
        x=alt.X(f"{value_col}:Q", title="증감률(%)" if value_col == "change_pct" else "증감 건수"),
        y=alt.Y("station_name:N", sort=alt.SortField(value_col), title=None),
        tooltip=[alt.Tooltip("station_name:N", title="역"), alt.Tooltip("baseline_value:Q", title="기준 이용량", format=",.0f"),
                 alt.Tooltip("target_value:Q", title="대상 이용량", format=",.0f"), alt.Tooltip("absolute_change:Q", title="절대 증감", format="+,.0f"),
                 alt.Tooltip("change_pct:Q", title="증감률", format="+.1f")])
    text = alt.Chart(labels).mark_text(align="left", dx=4).encode(
        x=f"{value_col}:Q", y=alt.Y("station_name:N", sort=alt.SortField(value_col)), text="chart_label:N")
    chart = (bars + text).add_params(
        alt.selection_point(name="station_pick", fields=["canonical_station_id"], on="click")
    ).properties(height=max(230, len(frame) * 30))
    if key is None:
        st.altair_chart(chart)
        return None
    state = st.altair_chart(chart, key=key, on_select="rerun", selection_mode="station_pick")
    picked = state.selection.get("station_pick", []) if state else []
    return str(picked[-1]["canonical_station_id"]) if picked else None


STATION_METRIC_LABELS = {
    "canonical_station_id": "역 코드",
    "station_name": "역명",
    "line_id": "호선",
    "valid_weekdays": "유효 평일수",
    "daily_ridership": "평일 일평균 승하차",
    "AM_board": "출근시간 승차",
    "AM_alight": "출근시간 하차",
    "PM_board": "퇴근시간 승차",
    "PM_alight": "퇴근시간 하차",
    "am_direction": "출근 방향성 지수",
    "pm_direction": "퇴근 방향성 지수",
    "residential_score": "주거 출발형 점수",
    "employment_score": "업무·통학 도착형 점수",
    "station_type": "역 유형",
    "low_sample_flag": "표본 부족 여부",
}

GROWTH_LABELS = {
    "direction": "증감 구분", "rank": "순위", "canonical_station_id": "역 코드",
    "station_name": "역명", "line_id": "호선", "station_type": "역 유형",
    "baseline_value": "기준 이용량", "target_value": "대상 이용량",
    "absolute_change": "증감 건수", "change_pct": "증감률(%)",
    "cagr_pct": "연평균 증감률(%)", "relative_growth_pct": "상대 성장률(%)",
    "increase_streak": "연속 증가 횟수", "decrease_streak": "연속 감소 횟수",
    "trend_badge": "추세 상태", "low_base": "저기저 여부",
    "baseline_days": "기준 관측일수", "target_days": "대상 관측일수",
    "network_change_pct": "전체 증감률(%)", "exclusion_reason": "제외 사유",
}

ANNUAL_LABELS = {
    "year": "연도", "canonical_station_id": "역 코드", "station_name": "역명", "line_id": "호선",
    "board": "승차", "alight": "하차", "total": "승하차 합계", "observed_days": "관측일수",
    "expected_days": "기대일수", "coverage_pct": "자료 충족률(%)", "is_complete": "완전연도 여부",
}

STATION_TYPE_COLORS = {
    "주거 출발형 추정": [37, 99, 235, 190],
    "업무·통학 도착형 추정": [249, 115, 22, 190],
    "방향 균형형": [16, 185, 129, 190],
    "혼합형": [139, 92, 246, 190],
    "자료부족/판정보류": [107, 114, 128, 190],
}
STATION_TYPE_HEX = {name: f"#{rgba[0]:02x}{rgba[1]:02x}{rgba[2]:02x}" for name, rgba in STATION_TYPE_COLORS.items()}


metrics, monthly, annual, calendar_monthly, coordinates, quality_summary = load_data()
date_min, date_max = quality_summary["date_min"], quality_summary["date_max"]
tabs = st.tabs(["역세권 지표", "출퇴근 성격", "장기 변화", "뜨는 역 · 지는 역 TOP10", "아파트 연결", "데이터 상태"])

with tabs[0]:
    with st.container(horizontal=True):
        st.metric("분석 역", f"{len(metrics):,}개", border=True)
        st.metric("최대 유효 평일", f"{metrics.valid_weekdays.max():,.0f}일", border=True)
        st.metric("최근 관측일", str(date_max), border=True)
    rank_col = st.selectbox("순위 기준", ["daily_ridership", "AM_board", "AM_alight", "PM_board", "PM_alight"],
                            format_func={"daily_ridership": "평일 일평균 승하차", "AM_board": "출근 승차", "AM_alight": "출근 하차", "PM_board": "퇴근 승차", "PM_alight": "퇴근 하차"}.get)
    top_metrics = metrics.nlargest(20, rank_col).copy()
    station_chart = alt.Chart(top_metrics).mark_bar(color="#2563EB").encode(
        x=alt.X(f"{rank_col}:Q", title=f"{STATION_METRIC_LABELS[rank_col]}(건)", axis=alt.Axis(format=",")),
        y=alt.Y("station_name:N", title="역명", sort=alt.SortField(field=rank_col, order="descending")),
        tooltip=[
            alt.Tooltip("station_name:N", title="역명"),
            alt.Tooltip("line_id:N", title="호선"),
            alt.Tooltip(f"{rank_col}:Q", title=STATION_METRIC_LABELS[rank_col], format=",.0f"),
            alt.Tooltip("station_type:N", title="역 유형"),
            alt.Tooltip("valid_weekdays:Q", title="유효 평일수", format=",.0f"),
        ],
    ).properties(height=600)
    st.altair_chart(station_chart)
    detail = metrics.sort_values(rank_col, ascending=False).rename(columns=STATION_METRIC_LABELS)
    st.dataframe(
        detail,
        hide_index=True,
        column_config={
            "유효 평일수": st.column_config.NumberColumn(format="%,d일"),
            "평일 일평균 승하차": st.column_config.NumberColumn(format="%,.0f건"),
            "출근시간 승차": st.column_config.NumberColumn(format="%,.0f건"),
            "출근시간 하차": st.column_config.NumberColumn(format="%,.0f건"),
            "퇴근시간 승차": st.column_config.NumberColumn(format="%,.0f건"),
            "퇴근시간 하차": st.column_config.NumberColumn(format="%,.0f건"),
            "출근 방향성 지수": st.column_config.NumberColumn(format="%.3f"),
            "퇴근 방향성 지수": st.column_config.NumberColumn(format="%.3f"),
            "주거 출발형 점수": st.column_config.NumberColumn(format="%.1f"),
            "업무·통학 도착형 점수": st.column_config.NumberColumn(format="%.1f"),
        },
    )

with tabs[1]:
    direction_view = metrics.rename(columns=STATION_METRIC_LABELS)
    mapped = metrics.merge(coordinates, on="canonical_station_id", how="left", validate="one_to_one")
    mapped = mapped.dropna(subset=["latitude", "longitude"]).copy()
    mapped["marker_color"] = mapped.station_type.map(STATION_TYPE_COLORS).apply(
        lambda value: value if isinstance(value, list) else [107, 114, 128, 190])
    max_ridership = mapped.daily_ridership.max()
    mapped["marker_radius"] = (mapped.daily_ridership / max_ridership).pow(.5).mul(650)
    mapped["daily_ridership_display"] = mapped.daily_ridership.map(lambda value: f"{value:,.0f}건")
    mapped["am_direction_display"] = mapped.am_direction.map(lambda value: f"{value:.3f}")
    mapped["pm_direction_display"] = mapped.pm_direction.map(lambda value: f"{value:.3f}")
    st.subheader("역 유형과 평일 평균 승하차량 지도")
    st.pydeck_chart(pdk.Deck(
        map_style=None,
        initial_view_state=pdk.ViewState(
            latitude=float(mapped.latitude.mean()), longitude=float(mapped.longitude.mean()), zoom=10.1, pitch=0),
        layers=[pdk.Layer(
            "ScatterplotLayer", mapped, id="station-demand-map", pickable=True, stroked=True,
            get_position="[longitude, latitude]", get_fill_color="marker_color", get_line_color=[255, 255, 255, 210],
            get_radius="marker_radius", radius_min_pixels=4, radius_max_pixels=34, line_width_min_pixels=1)],
        tooltip={"html": "<b>{station_name}</b><br/>호선: {line_id}호선<br/>역 유형: {station_type}<br/>평일 평균 승하차: {daily_ridership_display}<br/>출근 방향성 지수: {am_direction_display}<br/>퇴근 방향성 지수: {pm_direction_display}",
                 "style": {"backgroundColor": "#111827", "color": "white"}},
    ), height=620)
    legend = pd.DataFrame({"역 유형": list(STATION_TYPE_HEX), "색상": list(STATION_TYPE_HEX.values())})
    st.caption("마커의 원 면적은 평일 평균 승하차량에 비례하며, 화면상 최소·최대 크기를 제한했습니다.")
    legend_chart = alt.Chart(legend).mark_circle(size=170).encode(
        y=alt.Y("역 유형:N", title=None, axis=alt.Axis(labelLimit=220)),
        color=alt.Color("색상:N", scale=None, legend=None),
    ).properties(width=260, height=150)
    st.altair_chart(legend_chart, width="content")

    color_domain = list(STATION_TYPE_COLORS)
    color_range = [STATION_TYPE_HEX[name] for name in color_domain]
    direction_chart = alt.Chart(direction_view).mark_circle(opacity=.72, stroke="white", strokeWidth=.6).encode(
        x=alt.X("출근 방향성 지수:Q", scale=alt.Scale(domain=[-1, 1])),
        y=alt.Y("퇴근 방향성 지수:Q", scale=alt.Scale(domain=[-1, 1])),
        size=alt.Size("평일 일평균 승하차:Q", scale=alt.Scale(range=[35, 1200]), legend=None),
        color=alt.Color("역 유형:N", scale=alt.Scale(domain=color_domain, range=color_range)),
        tooltip=[alt.Tooltip("역명:N"), alt.Tooltip("호선:N"), alt.Tooltip("역 유형:N"),
                 alt.Tooltip("평일 일평균 승하차:Q", format=",.0f"),
                 alt.Tooltip("출근 방향성 지수:Q", format=".3f"), alt.Tooltip("퇴근 방향성 지수:Q", format=".3f")],
    ).properties(height=520)
    st.altair_chart(direction_chart)
    st.caption("방향성 지수와 기존 유형 분류는 최신 12개월 평일 자료 기준입니다.")
    st.dataframe(direction_view.sort_values("주거 출발형 점수", ascending=False), hide_index=True)

with tabs[2]:
    defaults = metrics.nlargest(3, "daily_ridership").station_name.tolist()
    names = st.multiselect("비교 역(2~5개 권장)", sorted(monthly.station_name.unique()), default=defaults)
    monthly_view = monthly[monthly.station_name.isin(names)].rename(columns={
        "month": "연월", "daily_ridership": "평일 일평균 승하차", "station_name": "역명"})
    st.line_chart(monthly_view, x="연월", y="평일 일평균 승하차", color="역명")

with tabs[3]:
    st.subheader("뜨는 역 · 지는 역 TOP10")
    complete = completed_years(annual)
    default_target = max(complete)
    all_targets = complete + ([int(annual.year.max())] if int(annual.year.max()) not in complete else [])
    mode = st.segmented_control("비교 모드", ["전년 대비", "3년 전 대비", "5년 전 대비", "직접 선택", "동일월 누적"], default="전년 대비")
    cols = st.columns(5)
    with cols[0]: target_year = st.selectbox("대상연도", all_targets, index=all_targets.index(default_target), key="growth_target")
    gap = {"전년 대비": 1, "3년 전 대비": 3, "5년 전 대비": 5}.get(mode)
    with cols[1]:
        if mode == "직접 선택":
            choices = [y for y in complete if y < target_year]
            baseline_year = st.selectbox("기준연도", choices, index=len(choices) - 1, key="growth_base")
        else:
            baseline_year = target_year - 1 if mode == "동일월 누적" else target_year - gap
            st.text_input("기준연도", str(baseline_year), disabled=True)
    with cols[2]: metric_label = st.selectbox("승하차 지표", ["승하차 합계", "승차", "하차"])
    with cols[3]: rank_label = st.selectbox("순위 기준", ["증감률", "절대 증감"], key="growth_rank")
    with cols[4]: top_n = st.selectbox("TOP 개수", [5, 10, 20], index=1)
    metric_col = {"승하차 합계": "total", "승차": "board", "하차": "alight"}[metric_label]
    rank_col = {"증감률": "change_pct", "절대 증감": "absolute_change"}[rank_label]
    ytd_month = None
    if mode == "동일월 누적":
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
    with fcols[2]: min_base = st.number_input("최소 기준 일평균 이용량", min_value=0, value=0, step=1_000)
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
        st.subheader(f"뜨는 역 TOP{top_n}"); clicked_station = top_chart(rising, rank_col, "#2563EB", "rising_top_chart")
        if clicked_station and clicked_station != st.session_state.get("rising_station_id"):
            st.session_state["rising_station_id"] = clicked_station
            st.rerun()
    with right:
        st.subheader(f"지는 역 TOP{top_n}"); top_chart(falling, rank_col, "#F97316")
    table_cols = ["rank", "station_name", "line_id", "station_type", "baseline_value", "target_value", "absolute_change", "change_pct",
                  "relative_growth_pct", "increase_streak", "decrease_streak", "trend_badge", "low_base"]
    table = pd.concat([rising.assign(direction="증가"), falling.assign(direction="감소")], ignore_index=True)[["direction", *table_cols]]
    table = table.rename(columns=GROWTH_LABELS)
    st.dataframe(table, hide_index=True, column_config={
        "baseline_value": st.column_config.NumberColumn("기준 이용량(건)", format="%,.0f"),
        "target_value": st.column_config.NumberColumn("대상 이용량(건)", format="%,.0f"),
        "absolute_change": st.column_config.NumberColumn("증감(건)", format="%+,.0f"),
        "change_pct": st.column_config.NumberColumn("증감률", format="%+.1f%%"),
        "relative_growth_pct": st.column_config.NumberColumn("상대 성장률", format="%+.1f%%")})
    st.subheader("지도")
    st.info("역 좌표와 행정구역 마스터가 확보되지 않아 지도 및 부산시내/시외·구군 필터는 비활성화했습니다. 좌표가 없는 역도 순위표에는 유지됩니다.")
    with st.expander(f"비교 제외 {len(excluded)}개 역과 사유"):
        st.dataframe(excluded.rename(columns=GROWTH_LABELS), hide_index=True)
    metadata = {"baseline_year": baseline_year, "target_year": target_year, "metric": metric_col, "rank_by": rank_col,
                "ytd_month": ytd_month, "formula_version": FORMULA_VERSION, "data_through": str(date_max),
                "filters": {"lines": selected_lines, "types": selected_types, "minimum_baseline": min_base, "search": search}}
    with st.container(horizontal=True):
        st.download_button("연간 역별 지표 CSV", csv_bytes(annual, metadata), "station_annual.csv", "text/csv")
        st.download_button("기간비교 전체표 CSV", csv_bytes(comparison, metadata), "station_comparison.csv", "text/csv")
        st.download_button("뜨는 역 CSV", csv_bytes(rising, metadata), "rising_stations.csv", "text/csv")
        st.download_button("지는 역 CSV", csv_bytes(falling, metadata), "falling_stations.csv", "text/csv")
        st.download_button("비교제외 CSV", csv_bytes(excluded, metadata), "excluded_stations.csv", "text/csv")

    render_station_detail(
        rising=rising, comparison=comparison, raw_path=INTERIM / "ridership_long.parquet",
        calendar_monthly=calendar_monthly, station_monthly=monthly, metrics=metrics,
        baseline_year=baseline_year, target_year=target_year, metric_col=metric_col,
        ytd_month=ytd_month, date_max=str(date_max),
        event_path=ROOT / "data" / "external" / "station_events.csv",
    )

    st.subheader("선택역 연간 추세")
    selected = st.multiselect("추세 비교 역(2~5개)", sorted(annual.station_name.unique()), default=rising.station_name.head(2).tolist(), key="annual_trend_stations")
    annual_trend = annual[annual.station_name.isin(selected) & annual.is_complete]
    metric_korean = ANNUAL_LABELS[metric_col]
    annual_trend_view = annual_trend.rename(columns={"year": "연도", metric_col: metric_korean, "station_name": "역명"})
    st.line_chart(annual_trend_view, x="연도", y=metric_korean, color="역명")
    common = annual_trend.pivot(index="year", columns="station_name", values=metric_col).dropna()
    positive = common[(common > 0).all(axis=1)]
    if not positive.empty:
        index_year = int(positive.index.min())
        indexed = common.div(common.loc[index_year]).mul(100).reset_index().melt("year", var_name="station_name", value_name="index").rename(
            columns={"year": "연도", "station_name": "역명", "index": "기준연도 대비 지수"})
        st.caption(f"공통 양수 기준연도 {index_year}=100 지수")
        st.line_chart(indexed, x="연도", y="기준연도 대비 지수", color="역명")
    st.subheader("연도별 TOP 기록")
    record_choices = [y for y in complete if y - 1 in complete]
    record_year = st.selectbox("전년 대비 기록 연도", record_choices, index=len(record_choices) - 1)
    record, _ = compare_periods(annual, calendar_monthly, ComparisonSpec(record_year - 1, record_year, metric_col))
    record_up, record_down = rank_changes(record, rank_col, top_n)
    record_table = pd.concat([record_up.assign(direction="증가"), record_down.assign(direction="감소")]).rename(columns=GROWTH_LABELS)
    st.dataframe(record_table, hide_index=True)
    st.caption(f"{record_year - 1}~{record_year} 비교 가능 표본 {len(record)}개. 선택한 지표·순위 기준·TOP 개수를 적용했습니다.")

with tabs[4]:
    render_apartment_tab(root=ROOT, metrics=metrics, coordinates=coordinates, monthly=monthly, comparison=comparison)

with tabs[5]:
    quality_labels = {"metric": "품질 항목", "value": "값"}
    manifest_labels = {"source_file_id": "원본 파일 ID", "file_name": "파일명", "relative_path": "상대 경로",
        "sha256": "SHA-256", "bytes": "파일 크기(바이트)", "encoding": "문자 인코딩", "rows": "행 수",
        "date_min": "최초 일자", "date_max": "최종 일자", "column_count": "열 수", "read_status": "읽기 상태",
        "is_exact_duplicate": "완전 중복 여부"}
    st.subheader("품질 요약"); st.dataframe(pd.read_csv(REPORTS / "quality_summary.csv").rename(columns=quality_labels), hide_index=True)
    st.subheader("원본 파일 목록"); st.dataframe(pd.read_csv(REPORTS / "raw_file_manifest.csv").rename(columns=manifest_labels), hide_index=True)
    st.caption(f"원자료 관측 범위: {date_min} ~ {date_max}")
