from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pydeck as pdk
import streamlit as st
import yaml

from .apartments import (
    ApartmentDataError, build_station_links, bytes_sha256, file_sha256,
    read_apartment_parquet, resolve_apartment_path, validate_apartments,
)


@st.cache_data(max_entries=8, show_spinner=False)
def _load_local(path_text: str, signature: str, stations: pd.DataFrame):
    del signature
    return validate_apartments(read_apartment_parquet(Path(path_text)), stations)


@st.cache_data(max_entries=8, show_spinner=False)
def _load_upload(content: bytes, signature: str, stations: pd.DataFrame):
    del signature
    return validate_apartments(read_apartment_parquet(content), stations)


@st.cache_data(max_entries=8, show_spinner="역–아파트 직선거리를 계산하는 중입니다.")
def _links(apartments: pd.DataFrame, stations: pd.DataFrame, signature: str) -> pd.DataFrame:
    del signature
    return build_station_links(apartments, stations)


def _circle_polygon(latitude: float, longitude: float, radius_m: float, points: int = 96) -> dict:
    earth = 6_371_008.8
    lat1, lon1 = np.radians([latitude, longitude])
    angular = radius_m / earth
    bearings = np.linspace(0, 2 * np.pi, points, endpoint=False)
    lat2 = np.arcsin(np.sin(lat1) * np.cos(angular) + np.cos(lat1) * np.sin(angular) * np.cos(bearings))
    lon2 = lon1 + np.arctan2(
        np.sin(bearings) * np.sin(angular) * np.cos(lat1),
        np.cos(angular) - np.sin(lat1) * np.sin(lat2),
    )
    coords = [[float(np.degrees(lon)), float(np.degrees(lat))] for lat, lon in zip(lat2, lon2)]
    coords.append(coords[0])
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [coords]}}


def _segment_ids(config: dict, stations: pd.DataFrame, label: str) -> tuple[list[str], str]:
    spec = config.get("segments", {}).get(label, {})
    if not spec:
        return stations.canonical_station_id.astype(str).tolist(), "전체 분석 대상 역"
    line = stations[stations.line_id.astype(str).eq(str(spec.get("line_id", "")))].copy()
    if spec.get("type") == "ordered_range":
        line["order"] = pd.to_numeric(line.canonical_station_id, errors="coerce")
        line = line.sort_values("order")
        names = line.station_name.tolist()
        try:
            a, b = names.index(spec["start"]), names.index(spec["end"])
            ids = line.iloc[min(a, b):max(a, b) + 1].canonical_station_id.astype(str).tolist()
        except ValueError:
            ids = []
    else:
        wanted = set(spec.get("stations", []))
        ids = stations[stations.station_name.isin(wanted)].canonical_station_id.astype(str).tolist()
    return ids, str(spec.get("note", "설정 파일에서 관리하는 관심 역 목록입니다."))


def _display_table(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["approval_date"] = pd.to_datetime(out.get("approval_date"), errors="coerce").dt.strftime("%Y-%m-%d")
    out["age_display"] = out.get("apartment_age", pd.Series(index=out.index, dtype="Float64")).map(
        lambda x: "정보 없음" if pd.isna(x) else f"{int(x)}년 (원본 기준시점)"
    )
    return out.rename(columns={
        "complex_name": "단지명", "distance_m": "역까지 직선거리(m)", "households": "세대수",
        "buildings": "동수", "approval_date": "사용승인일", "age_display": "연식 및 기준",
        "parking_per_household": "세대당 주차대수", "sigungu": "구", "dong": "동",
        "road_address": "도로명주소",
    })[["단지명", "역까지 직선거리(m)", "세대수", "동수", "사용승인일", "연식 및 기준",
         "세대당 주차대수", "구", "동", "도로명주소"]]


def _csv_bytes(frame: pd.DataFrame, metadata: dict) -> bytes:
    header = "# " + json.dumps(metadata, ensure_ascii=False) + "\n"
    return (header + frame.to_csv(index=False)).encode("utf-8-sig")


def render_apartment_tab(*, root: Path, metrics: pd.DataFrame, coordinates: pd.DataFrame,
                         monthly: pd.DataFrame, comparison: pd.DataFrame | None = None) -> None:
    st.subheader("역별 인근 아파트")
    st.caption("역과 단지의 대표 좌표 사이 직선거리입니다. 출입구 위치·횡단보도·경사 등을 반영한 실제 보행거리와 다를 수 있습니다.")
    stations = metrics[["canonical_station_id", "station_name", "line_id", "daily_ridership"]].merge(
        coordinates[["canonical_station_id", "latitude", "longitude"]], on="canonical_station_id", how="left", validate="one_to_one"
    )
    stations["canonical_station_id"] = stations.canonical_station_id.astype(str)
    config = yaml.safe_load((root / "config" / "apartment_data.yaml").read_text(encoding="utf-8"))
    local_path, source_label = resolve_apartment_path(root)
    try:
        if local_path:
            signature = file_sha256(local_path)
            bundle = _load_local(str(local_path), signature, stations)
            source_label = f"{source_label}: {local_path}"
        else:
            st.warning("아파트 파일을 찾지 못했습니다. BUSAN_METRO_APARTMENT_PARQUET 또는 config/apartment_data.yaml에 경로를 설정해 주세요.")
            upload = st.file_uploader("대체 아파트 Parquet 업로드", type=["parquet"], key="apartment_upload",
                                      help="로컬 파일이 없을 때만 사용합니다. 현재 브라우저 세션에만 적용됩니다.")
            if upload is None:
                st.info("현재 설정 경로: data/interim/kapt_clean.parquet")
                return
            content = upload.getvalue()
            signature = bytes_sha256(content)
            bundle = _load_upload(content, signature, stations)
            source_label = "대체 업로드 파일 · 현재 세션에만 적용"
            st.info("업로드 자료는 현재 세션에서만 사용되며 서버 파일로 저장되지 않습니다.")
    except (ApartmentDataError, OSError, ValueError) as exc:
        st.error(f"아파트 자료를 사용할 수 없습니다: {exc}")
        return
    st.success(f"아파트 자료 자동 로드 완료: {len(bundle.apartments):,}개 단지")
    st.caption(f"사용 중인 자료: {source_label}")
    links = _links(bundle.apartments, stations, signature)

    segment_labels = list(config.get("segments", {"전체 역": []}))
    f1, f2, f3 = st.columns(3)
    with f1:
        line_options = ["전체"] + sorted(stations.line_id.dropna().astype(str).unique())
        line = st.selectbox("호선", line_options, key="apt_line")
    with f2:
        segment = st.selectbox("관심 구간", segment_labels, key="apt_segment")
    segment_ids, segment_note = _segment_ids(config, stations, segment)
    candidates = stations[stations.canonical_station_id.isin(segment_ids)]
    if line != "전체":
        candidates = candidates[candidates.line_id.astype(str).eq(line)]
    candidates = candidates.sort_values(pd.to_numeric(candidates.canonical_station_id, errors="coerce").name)
    if candidates.empty:
        st.warning("선택한 호선과 관심 구간에 함께 속하는 역이 없습니다. 필터를 변경해 주세요.")
        return
    ids = candidates.canonical_station_id.tolist()
    preferred = st.session_state.get("apt_station_id") or st.session_state.get("rising_station_id")
    if preferred not in ids:
        st.session_state["apt_station_id"] = ids[0]
    labels = candidates.set_index("canonical_station_id").apply(lambda r: f"{r.station_name} · {r.line_id}호선", axis=1).to_dict()
    with f3:
        station_id = st.selectbox("역", ids, format_func=labels.get, key="apt_station_id")
    selected_station = stations[stations.canonical_station_id.eq(station_id)].iloc[0]
    included_names = candidates.station_name.tolist()
    st.caption(f"{segment_note} 포함 역({len(included_names)}개): " + ", ".join(included_names))

    g1, g2, g3 = st.columns(3)
    with g1:
        radius = st.segmented_control("반경", [300, 500, 800, 1000], default=300,
                                      format_func=lambda x: f"{x:,}m", key="apt_radius")
    with g2:
        household_mode = st.segmented_control("최소 세대수", ["500", "1,000", "직접 입력"], default="500", key="apt_household_mode")
    with g3:
        minimum_households = st.number_input("직접 입력 세대수", min_value=0, value=500, step=50,
                                             disabled=household_mode != "직접 입력", key="apt_household_custom")
    minimum_households = {"500": 500, "1,000": 1000}.get(household_mode, int(minimum_households))

    linked = links[links.canonical_station_id.eq(station_id)].merge(bundle.apartments, on="kapt_code", validate="many_to_one")
    districts = sorted(linked.sigungu.dropna().astype(str).unique()) if "sigungu" in linked else []
    dongs = sorted(linked.dong.dropna().astype(str).unique()) if "dong" in linked else []
    ages = sorted(linked.age_group.dropna().astype(str).unique()) if "age_group" in linked else []
    h1, h2, h3 = st.columns(3)
    with h1: district_filter = st.multiselect("구", districts, key="apt_district")
    with h2: dong_filter = st.multiselect("동", dongs, key="apt_dong")
    with h3: age_filter = st.multiselect("연식 구간", ages, key="apt_age")
    filtered = linked[linked.distance_m.le(float(radius)) & linked.households.ge(minimum_households)].copy()
    if district_filter: filtered = filtered[filtered.sigungu.isin(district_filter)]
    if dong_filter: filtered = filtered[filtered.dong.isin(dong_filter)]
    if age_filter: filtered = filtered[filtered.age_group.isin(age_filter)]

    growth = np.nan
    if comparison is not None and not comparison.empty:
        match = comparison[comparison.canonical_station_id.astype(str).eq(station_id)]
        if not match.empty: growth = match.iloc[0].change_pct
    with st.container(horizontal=True):
        st.metric("선택 역", labels[station_id], border=True)
        st.metric("조건 충족 단지", f"{filtered.kapt_code.nunique():,}개", border=True)
        st.metric("단지 전체 세대수 합계", f"{filtered.drop_duplicates('kapt_code').households.sum():,.0f}세대", border=True)
        st.metric("평일 일평균 승하차", f"{selected_station.daily_ridership:,.0f}건", border=True)
        st.metric("선택 비교기간 증가율", "산출 불가" if pd.isna(growth) else f"{growth:+.1f}%", border=True)
    st.caption("세대수 합계는 반경 안에 대표 좌표가 있는 단지 전체의 세대수이며, 실제 역 이용 가구 수가 아닙니다. 이용량 증가율은 TOP10 탭의 현재 비교기간 정의를 재사용합니다.")

    selected_code = None
    if not filtered.empty:
        detail_options = filtered.sort_values("distance_m").kapt_code.tolist()
        detail_labels = filtered.set_index("kapt_code").complex_name.to_dict()
        selected_code = st.selectbox("상세·지도 강조 단지", detail_options, format_func=detail_labels.get, key="apt_complex")
    map_homes = filtered.copy()
    if not map_homes.empty:
        scale_max = max(float(map_homes.households.max()), 1)
        map_homes["marker_radius"] = 5 + np.sqrt(map_homes.households / scale_max) * 11
        map_homes["marker_color"] = map_homes.kapt_code.map(lambda x: [239, 68, 68, 220] if x == selected_code else [37, 99, 235, 190])
        map_homes["line_color"] = map_homes.kapt_code.map(lambda x: [127, 29, 29, 255] if x == selected_code else [255, 255, 255, 230])
        map_homes["distance_display"] = map_homes.distance_m.map(lambda x: f"{x:,.1f}m")
        map_homes["households_display"] = map_homes.households.map(lambda x: "정보 없음" if pd.isna(x) else f"{x:,.0f}세대")
        map_homes["approval_display"] = pd.to_datetime(map_homes.get("approval_date"), errors="coerce").dt.strftime("%Y-%m-%d").fillna("정보 없음")
        map_homes["parking_display"] = map_homes.get("parking_per_household", pd.Series(index=map_homes.index)).map(lambda x: "정보 없음" if pd.isna(x) else f"{x:.2f}대")
        map_homes["address_display"] = map_homes.get("road_address", pd.Series(index=map_homes.index)).fillna(map_homes.get("legal_address", "정보 없음"))
    station_map = pd.DataFrame([{
        "latitude": float(selected_station.latitude),
        "longitude": float(selected_station.longitude),
        "label": f"{selected_station.station_name}역",
        "tooltip_title": f"{selected_station.station_name}역 · {selected_station.line_id}호선",
        "tooltip_detail": f"평일 일평균 승하차 {selected_station.daily_ridership:,.0f}건",
    }])
    regular_homes = map_homes[map_homes.kapt_code.ne(selected_code)].copy() if not map_homes.empty else map_homes
    selected_home = map_homes[map_homes.kapt_code.eq(selected_code)].copy() if selected_code else map_homes.iloc[0:0].copy()
    layers = [
        pdk.Layer("GeoJsonLayer", _circle_polygon(float(selected_station.latitude), float(selected_station.longitude), float(radius)),
                  id="apartment-radius", filled=True, stroked=True, get_fill_color=[37, 99, 235, 35], get_line_color=[37, 99, 235, 180]),
    ]
    if not regular_homes.empty:
        layers.append(pdk.Layer("ScatterplotLayer", regular_homes, id="nearby-apartments", pickable=True, stroked=True,
                                get_position="[longitude, latitude]", get_radius="marker_radius", radius_units="'pixels'",
                                get_fill_color="marker_color", get_line_color="line_color", line_width_min_pixels=2))
    if not selected_home.empty:
        layers.append(pdk.Layer("ScatterplotLayer", selected_home, id="selected-apartment", pickable=True, stroked=True,
                                get_position="[longitude, latitude]", get_radius=19, radius_units="'pixels'",
                                get_fill_color=[239, 68, 68, 235], get_line_color=[127, 29, 29, 255], line_width_min_pixels=4))
    # 마지막 레이어가 화면 최상단에 그려지므로 역은 아파트가 겹쳐도 가려지지 않는다.
    layers.extend([
        pdk.Layer("ScatterplotLayer", station_map, id="selected-station", pickable=True, stroked=True,
                  get_position="[longitude, latitude]", get_radius=13, radius_units="'pixels'",
                  get_fill_color=[16, 185, 129, 255], get_line_color=[255, 255, 255, 255], line_width_min_pixels=3),
        pdk.Layer("TextLayer", station_map, id="selected-station-label", get_position="[longitude, latitude]",
                  get_text="label", get_size=15, size_units="'pixels'", get_pixel_offset=[0, -24],
                  get_color=[6, 78, 59, 255], get_text_anchor="'middle'", get_alignment_baseline="'bottom'"),
    ])
    st.pydeck_chart(pdk.Deck(map_style=None, initial_view_state=pdk.ViewState(
        latitude=float(selected_station.latitude), longitude=float(selected_station.longitude), zoom={300: 15.5, 500: 15, 800: 14.4, 1000: 14.1}[radius]),
        layers=layers, tooltip={"html": "<b>{complex_name}{tooltip_title}</b><br/>{tooltip_detail}<br/>거리: {distance_display}<br/>세대수: {households_display}<br/>사용승인일: {approval_display}<br/>세대당 주차: {parking_display}<br/>주소: {address_display}"}), height=560)
    st.caption("초록색 마커·라벨은 선택 역, 파란색은 조건 충족 아파트, 빨간색 큰 마커는 선택 단지입니다. 반투명 영역은 WGS84 구면 거리로 생성한 실제 설정 반경입니다.")

    if filtered.empty:
        st.info("해당 조건을 충족하는 단지가 없습니다.")
    else:
        sort_label = st.selectbox("목록 정렬", ["거리 가까운 순", "세대수 많은 순", "연식 낮은 순", "주차 많은 순"], key="apt_sort")
        sort_spec = {"거리 가까운 순": ("distance_m", True), "세대수 많은 순": ("households", False),
                     "연식 낮은 순": ("apartment_age", True), "주차 많은 순": ("parking_per_household", False)}[sort_label]
        ordered = filtered.sort_values(sort_spec[0], ascending=sort_spec[1], na_position="last")
        display = _display_table(ordered)
        st.dataframe(display, hide_index=True, column_config={
            "역까지 직선거리(m)": st.column_config.NumberColumn(format="%.1f"),
            "세대수": st.column_config.NumberColumn(format="%,.0f"),
            "동수": st.column_config.NumberColumn(format="%,.0f"),
            "세대당 주차대수": st.column_config.NumberColumn(format="%.2f"),
        }, key="apartment_list")
        metadata = {"station_id": station_id, "station": labels[station_id], "distance_basis": "대표 좌표 간 WGS84 직선거리",
                    "radius_m": radius, "minimum_households": minimum_households, "districts": district_filter,
                    "dongs": dong_filter, "age_groups": age_filter}
        st.download_button("현재 목록 CSV", _csv_bytes(display, metadata), f"{station_id}_apartments.csv", "text/csv", key="apt_download")

        chosen = filtered[filtered.kapt_code.eq(selected_code)].iloc[0]
        st.subheader("단지 상세")
        nearest = links[links.kapt_code.eq(selected_code) & links.nearest_station_rank.le(3)].merge(
            stations[["canonical_station_id", "station_name", "line_id"]], on="canonical_station_id").sort_values("nearest_station_rank")
        complex_links = links[links.kapt_code.eq(selected_code)]
        access_count = int(complex_links[complex_links.distance_m.le(float(radius))].canonical_station_id.nunique())
        with st.container(border=True):
            st.write(f"**{chosen.complex_name}** · kapt_code `{chosen.kapt_code}`")
            st.write(f"{chosen.households:,.0f}세대 · {chosen.get('buildings', np.nan):,.0f}동 · 사용승인일 {pd.to_datetime(chosen.get('approval_date')).strftime('%Y-%m-%d') if pd.notna(chosen.get('approval_date')) else '정보 없음'}")
            st.write("가까운 역: " + ", ".join(f"{r.station_name}({r.line_id}호선) {r.distance_m:,.1f}m" for _, r in nearest.iterrows()))
            st.write(f"현재 {radius:,}m 반경 내 접근 가능한 분석 역: {access_count}개")
            st.caption("가격·학군 자료는 표시하지 않습니다. 이후 가격 자료는 kapt_code를 키로 연결할 수 있습니다. 사용승인일은 실제 입주일과 다를 수 있습니다.")

    with st.expander("선택 역 이용량 추이", expanded=False):
        trend = monthly[monthly.canonical_station_id.astype(str).eq(station_id)].copy()
        if trend.empty: st.info("이용량 추이 자료가 없습니다.")
        else: st.line_chart(trend, x="month", y="daily_ridership")
        st.caption("인근 아파트 존재만으로 이용객 변화 원인을 주거 수요로 단정할 수 없습니다. 상세 원인 탐색은 ‘뜨는 역 · 지는 역 TOP10’ 탭에서 비교기간과 함께 확인하세요.")

    with st.expander("아파트 데이터 품질", expanded=False):
        labels_quality = {
            "input_rows": "읽은 행 수", "input_unique_complexes": "입력 고유 단지 수", "valid_complexes": "연결 유효 단지 수",
            "complexes_500plus": "500세대 이상 유효 단지", "exact_duplicate_rows_removed": "제거한 동일 중복 행",
            "conflicting_kapt_codes": "충돌 kapt_code", "coordinate_numeric_missing": "숫자 변환 불가 좌표",
            "coordinate_range_invalid": "범위 오류 좌표", "coordinate_swapped_likely": "위경도 뒤바뀜 의심",
            "location_outliers_over_100km_from_station": "부산 역 100km 밖 위치 이상치", "households_missing": "세대수 결측",
            "is_500plus_mismatches": "is_500plus 불일치", "approval_date_missing_or_invalid": "사용승인일 결측/변환 불가",
        }
        report = pd.DataFrame([{"검증 항목": labels_quality.get(k, k), "값": v} for k, v in bundle.quality.items()])
        st.dataframe(report, hide_index=True)
        st.caption("apartment_age는 원본 기준시점이 별도로 확인되지 않아 재계산하지 않았습니다. kapt_code 충돌은 평균·합산하지 않고 공간 연결에서 제외합니다.")
