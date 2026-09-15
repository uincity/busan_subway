from __future__ import annotations

from pathlib import Path

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

from .detail import (EVENT_COLUMNS, event_template_bytes, hypotheses, monthly_yoy,
                     network_monthly_yoy, positive_streaks, recommend_peers,
                     time_pattern, validate_events)


def _fmt(value, suffix=""):
    return "산출 불가" if pd.isna(value) else f"{value:,.1f}{suffix}"


@st.cache_data(max_entries=30, show_spinner=False)
def _load_station_raw(path_text: str, station_id: str, modified_ns: int) -> pd.DataFrame:
    return pd.read_parquet(path_text, filters=[("canonical_station_id", "==", station_id)])


def render_station_detail(*, rising: pd.DataFrame, comparison: pd.DataFrame,
                          raw_path: Path, calendar_monthly: pd.DataFrame, station_monthly: pd.DataFrame,
                          metrics: pd.DataFrame,
                          baseline_year: int, target_year: int, metric_col: str,
                          ytd_month: int | None, date_max: str, event_path: Path) -> None:
    st.divider()
    st.header("뜨는 역 원인 탐색")
    st.caption("증가 시점 → 증가 시간대 → 주변 사건 → 비교역 → 원인 후보와 근거. 이 화면은 원인을 자동 확정하지 않습니다.")
    if rising.empty:
        st.info("현재 필터의 뜨는 역이 없어 상세 분석을 표시할 수 없습니다."); return
    options = rising.canonical_station_id.astype(str).tolist()
    previous = st.session_state.get("rising_station_id")
    if previous not in options:
        if previous is not None: st.info("이전에 선택한 역이 새 TOP 목록에서 제외되어 1위 역으로 전환했습니다.")
        st.session_state["rising_station_id"] = options[0]
    labels = rising.set_index(rising.canonical_station_id.astype(str)).apply(
        lambda r: f"{r.station_name} · {r.line_id}호선", axis=1).to_dict()
    sid = st.selectbox("상세 분석 역", options, format_func=labels.get, key="rising_station_id")
    raw = _load_station_raw(str(raw_path), sid, raw_path.stat().st_mtime_ns)
    row = comparison[comparison.canonical_station_id.astype(str).eq(sid)].iloc[0]
    station_type = metrics.set_index(metrics.canonical_station_id.astype(str)).station_type.get(sid, "유형 미확인")
    scope = f"1~{ytd_month}월" if ytd_month else "전체 연도"
    with st.container(horizontal=True):
        st.metric("역", f"{row.station_name} · {row.line_id}호선", border=True)
        st.metric("기존 역 유형", station_type, border=True)
        st.metric(f"{baseline_year} 일평균", _fmt(row.baseline_value, "건/일"), border=True)
        st.metric(f"{target_year} 일평균", _fmt(row.target_value, "건/일"), border=True)
        st.metric("전년 대비", _fmt(row.change_pct, "%"), border=True)
        st.metric("일평균 순증가", _fmt(row.absolute_change, "건/일"), border=True)
    st.caption(f"비교기간: {baseline_year}년 {scope} ↔ {target_year}년 {scope} · 유효일 {int(row.baseline_days)}일/{int(row.target_days)}일 · 네트워크 대비 상대 성장 차이 {row.change_pct-row.network_change_pct:+.1f}%p. 승차+하차는 고유 방문자 수가 아닌 이용 건수입니다.")
    st.info("TOP10은 각 기간의 유효 관측일 일평균을 비교합니다. 결측일은 0건으로 간주하지 않으며, 기준값이 0 또는 없으면 증가율을 산출하지 않습니다.")

    monthly = monthly_yoy(raw, sid, metric_col).merge(network_monthly_yoy(calendar_monthly, sid, metric_col), on="month", how="left")
    if ytd_month: monthly = monthly[~((monthly.month.dt.year == target_year) & (monthly.month.dt.month > ytd_month))]
    st.subheader("A. 증가 시점")
    streak_len = st.slider("지속 증가 기준(연속 개월)", 2, 6, 3, key="streak_months")
    trend = monthly.melt(id_vars=["month_date","complete"], value_vars=["value"], var_name="series", value_name="이용 건수")
    st.altair_chart(alt.Chart(trend).mark_line(point=True).encode(
        x=alt.X("month_date:T", title="월"), y=alt.Y("이용 건수:Q", title="월별 일평균 이용 건수"),
        strokeDash=alt.StrokeDash("complete:N", title="완결 월"), tooltip=["month_date:T", alt.Tooltip("이용 건수:Q", format=",.0f"), "complete:N"]).properties(height=280))
    yoy = monthly.melt(id_vars=["month_date","complete"], value_vars=["yoy_pct","network_yoy_pct"], var_name="구분", value_name="증가율")
    yoy["구분"] = yoy["구분"].map({"yoy_pct":"선택 역", "network_yoy_pct":"선택 역 제외 데이터 네트워크"})
    st.altair_chart(alt.Chart(yoy).mark_line(point=True).encode(x=alt.X("month_date:T", title="월"), y=alt.Y("증가율:Q", title="전년 동월 대비(%)"), color="구분:N", strokeDash="complete:N", tooltip=["month_date:T","구분:N",alt.Tooltip("증가율:Q",format="+.1f")]).properties(height=280))
    streaks = positive_streaks(monthly, streak_len)
    if streaks:
        starts = ", ".join(f"{a.year}년 {a.month}월~{b.year}년 {b.month}월" for a,b in streaks)
        st.success(f"지속 증가 구간: {starts}. 최근 구간은 {streaks[-1][0].year}년 {streaks[-1][0].month}월부터 관찰됨. 주변 사건 확인이 필요함.")
    else: st.info("완결 월 자료에서 설정한 지속 증가 구간이 확인되지 않았거나 판정 자료가 부족합니다.")
    st.caption("미완결 월은 선 모양으로 구분하며 지속 증가 판정에서 제외합니다. 상대 성장은 인과효과가 아닌 공통 추세 대비 지표입니다.")

    st.subheader("B. 증가 시간대와 방향")
    change, day_summary = time_pattern(raw, sid, baseline_year, target_year, ytd_month)
    if change.empty: st.info("분석에 필요한 시간대 데이터 없음")
    else:
        st.dataframe(day_summary.rename(columns={"day_type":"요일 유형","baseline_avg":"기준 일평균","target_avg":"대상 일평균","absolute_change":"순증가량"}), hide_index=True)
        heat = change.copy(); heat["시간대"] = heat.time_bin_start.map(lambda h:f"{h:02d}:00~{(h+1)%24:02d}:00"); heat["방향"] = heat.direction.map({"board":"승차","alight":"하차"})
        st.altair_chart(alt.Chart(heat).mark_rect().encode(x=alt.X("시간대:N", sort=None), y=alt.Y("방향:N"), row=alt.Row("day_type:N", title="요일"), color=alt.Color("absolute_change:Q", title="건/해당 요일 일", scale=alt.Scale(scheme="redblue", reverse=True, domainMid=0)), tooltip=["day_type:N","시간대:N","방향:N",alt.Tooltip("absolute_change:Q",format="+,.0f"),"low_base:N"]).properties(height=90))
        top = heat.reindex(heat.absolute_change.abs().sort_values(ascending=False).index).head(12)[["day_type","시간대","방향","baseline_avg","target_avg","absolute_change","low_base"]]
        st.dataframe(top.rename(columns={"day_type":"요일 유형","baseline_avg":"기준 일평균","target_avg":"대상 일평균","absolute_change":"순증가량","low_base":"낮은 기저 경고"}), hide_index=True)
        st.caption("시간 구간은 시작 시각 포함·종료 시각 미포함입니다. 출근 07:00~09:00, 퇴근 17:00~20:00을 별도 가설에 사용합니다. 공식 공휴일 자료가 없어 평일·토요일·일요일까지만 구분합니다.")

    st.subheader("C. 주변 사건과 출처")
    stored = pd.read_csv(event_path, dtype=str) if event_path.exists() else pd.DataFrame(columns=EVENT_COLUMNS)
    uploaded = st.file_uploader("사건 CSV 업로드", type="csv", key="event_upload")
    events = stored
    if uploaded:
        candidate, errors = validate_events(uploaded, set(metrics.canonical_station_id.astype(str)))
        if errors:
            for error in errors: st.error(error)
        else: events = candidate; st.success("업로드 사건 자료를 현재 세션에 적용했습니다.")
    radius = st.segmented_control("직선거리 반경", ["500m","1km","영향 범위 사건 포함"], default="1km")
    station_events = events[events.station_id.astype(str).eq(sid)].copy() if not events.empty else events
    if radius != "영향 범위 사건 포함" and not station_events.empty:
        limit = 500 if radius == "500m" else 1000
        station_events = station_events[pd.to_numeric(station_events.distance_m, errors="coerce").le(limit)]
    if station_events.empty: st.info("등록·확인된 주변 사건 자료 없음. 실제 사건이 없다는 뜻은 아닙니다.")
    else:
        st.dataframe(station_events, hide_index=True, column_config={"source_url":st.column_config.LinkColumn("공식 출처")})
    with st.container(horizontal=True):
        st.download_button("사건 입력 템플릿", event_template_bytes(), "station_events_template.csv", "text/csv")
        st.download_button("현재 사건 자료", events.to_csv(index=False).encode("utf-8-sig"), "station_events.csv", "text/csv")
    st.caption("발표일과 실제 시작일은 별도 열입니다. 실제 시작일이 없으면 발생 시점으로 대체하지 않으며, 계획 상태·무출처 사건은 발생 원인으로 확정하지 않습니다. 배포 환경에서는 업로드가 현재 세션에만 적용될 수 있습니다.")

    st.subheader("D. 비교역")
    cutoff = streaks[-1][0].to_timestamp() if streaks else pd.Timestamp(target_year, 1, 1)
    peers = recommend_peers(station_monthly, metrics, sid, cutoff)
    if peers.empty: st.info("비교역 추천에 필요한 증가 이전 자료가 부족합니다.")
    else:
        peer_rows = metrics[metrics.canonical_station_id.astype(str).isin(peers.canonical_station_id.astype(str))].copy()
        peer_rows["station_id_text"] = peer_rows.canonical_station_id.astype(str)
        peer_options = peer_rows.set_index("station_id_text").station_name.to_dict()
        chosen = st.multiselect("비교역 수정(3~5개 권장)", list(peer_options), default=list(peer_options), format_func=peer_options.get)
        st.dataframe(peers[peers.canonical_station_id.astype(str).isin(chosen)].rename(columns={
            "canonical_station_id":"역 ID", "scale":"사전 평균 이용량", "variability":"사전 변동성",
            "trend":"사전 추세", "station_type":"역 유형", "line_id":"호선",
            "similarity_distance":"유사도 거리", "selection_reason":"선정 이유", "cutoff_date":"선정 기준일"}), hide_index=True)
        st.caption("비교역은 탐색된 증가 시작 전 12개월만 사용해 선정합니다. 거리가 작을수록 유사하며, 사후 결과는 선정에 사용하지 않습니다. 주말 비중·상세 시간대는 현재 월별 산출물만으로 추천에 반영하지 못했습니다. 인접역·동일 사건 영향 여부는 사건 자료가 확보돼야 별도 점검할 수 있습니다.")

    st.subheader("E. 원인 후보와 근거")
    evidence = hypotheses(change, station_events, row.change_pct-row.network_change_pct) if not change.empty else pd.DataFrame()
    if evidence.empty: st.info("원인 후보를 만들 시간대 자료가 없습니다.")
    else: st.dataframe(evidence, hide_index=True)
    st.warning("근거 수준은 인과관계 확률이 아니라 증거 조건 충족 수준입니다. 시간대 패턴만으로 직장인·학생·관광객을 확정하지 않습니다.")
    detail_export = change.assign(station_id=sid, baseline_year=baseline_year, target_year=target_year) if not change.empty else pd.DataFrame()
    st.download_button("상세 분석 결과 CSV", detail_export.to_csv(index=False).encode("utf-8-sig"), f"{sid}_detail.csv", "text/csv")
