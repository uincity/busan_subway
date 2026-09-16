from __future__ import annotations

import io
import json
import os
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pydeck as pdk
import streamlit as st
import yaml

from .apartments import (
    ApartmentDataError, bytes_sha256, file_sha256,
    read_apartment_parquet, resolve_apartment_path, validate_apartments,
)
from .entrances import (
    ACCESS_STATUSES, ENTRANCE_COLUMNS, ENTRANCE_TYPES, SOURCE_TYPES,
    VERIFICATION_STATUSES, EntranceConflictError, EntranceDataError,
    build_effective_station_links, empty_entrances, entrance_revision,
    merge_import, new_entrance_id, parse_entrance_csv, read_entrances,
    save_entrances_atomic, utc_now_text, validate_candidate,
)
from .walking_routes import (
    RouteConfig, best_results, build_candidates, calculate_pending,
    mark_apartments_stale, persistent_db_path, read_results, status_counts,
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
def _links(apartments: pd.DataFrame, stations: pd.DataFrame, entrances: pd.DataFrame,
           signature: str, entrance_signature: str, mode: str, include_restricted: bool) -> pd.DataFrame:
    del signature, entrance_signature
    return build_effective_station_links(apartments, stations, entrances, mode=mode,
                                         include_restricted=include_restricted)


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
    out["corrected"] = out.coordinate_basis.eq("보행 출입구")
    return out.rename(columns={
        "complex_name": "단지명", "effective_distance_m": "적용 거리(m)", "coordinate_basis": "좌표 기준",
        "center_distance_m": "중심점 거리(m)", "selected_entrance_name": "적용 출입구",
        "selected_access_status": "출입구 접근 조건", "selected_verified_at": "확인일", "corrected": "출입구 보정 여부",
        "households": "세대수",
        "buildings": "동수", "approval_date": "사용승인일", "age_display": "연식 및 기준",
        "parking_per_household": "세대당 주차대수", "sigungu": "구", "dong": "동",
        "road_address": "도로명주소",
    })[["단지명", "적용 거리(m)", "좌표 기준", "중심점 거리(m)", "적용 출입구", "출입구 접근 조건", "확인일",
         "출입구 보정 여부", "세대수", "동수", "사용승인일", "연식 및 기준", "세대당 주차대수", "구", "동", "도로명주소"]]


def _csv_bytes(frame: pd.DataFrame, metadata: dict) -> bytes:
    header = "# " + json.dumps(metadata, ensure_ascii=False) + "\n"
    return (header + frame.to_csv(index=False)).encode("utf-8-sig")


def _entrance_csv_bytes(frame: pd.DataFrame) -> bytes:
    return frame[ENTRANCE_COLUMNS].to_csv(index=False).encode("utf-8-sig")


def _persist_entrances(frame: pd.DataFrame, path: Path, route_db: Path,
                       affected_codes: set[str], affected_entrance_ids: set[str]) -> bool:
    try:
        revision = save_entrances_atomic(frame, path, st.session_state.get("entrance_revision"))
    except (EntranceDataError, EntranceConflictError, OSError) as exc:
        st.error(f"출입구를 저장하지 못했습니다: {exc}")
        return False
    st.session_state["entrance_revision"] = revision
    st.session_state["entrances"] = frame
    stale_count = mark_apartments_stale(
        route_db, affected_codes, reason=f"출입구 보정 변경 · revision {revision[:12]}",
        entrance_ids=affected_entrance_ids,
    )
    st.session_state["route_refresh_queue"] = {
        "revision": revision, "kapt_codes": sorted(affected_codes),
    }
    _links.clear()
    st.success(f"출입구 보정을 저장하고 관련 경로 {stale_count:,}건을 갱신 대상으로 표시했습니다.")
    return True


def _render_edit_map(home: pd.Series, gates: pd.DataFrame, station: pd.Series, radius: int) -> None:
    try:
        import folium
        from streamlit_folium import st_folium
    except ImportError:
        st.warning("편집 지도 클릭 구성요소가 없어 직접 좌표 입력만 사용할 수 있습니다.")
        return
    center = [float(home.latitude), float(home.longitude)]
    m = folium.Map(location=center, zoom_start=17, control_scale=True)
    folium.Marker(center, tooltip="원본 단지 중심(수정 불가)", icon=folium.Icon(color="blue", icon="home")).add_to(m)
    folium.Marker([float(station.latitude), float(station.longitude)], tooltip=f"{station.station_name}역",
                  icon=folium.Icon(color="green", icon="train", prefix="fa")).add_to(m)
    folium.Circle([float(station.latitude), float(station.longitude)], radius=radius, color="#10b981",
                  fill=True, fill_opacity=.06, tooltip=f"{radius:,}m 반경").add_to(m)
    for _, gate in gates.iterrows():
        color = "orange" if bool(gate.enabled) else "gray"
        folium.CircleMarker([gate.latitude, gate.longitude], radius=8, color=color, fill=True,
                            tooltip=f"{gate.entrance_name} · {gate.access_status}").add_to(m)
    clicked = st_folium(m, height=430, width="100%", key=f"entrance_map_{home.kapt_code}",
                        returned_objects=["last_clicked"])
    point = clicked.get("last_clicked") if clicked else None
    if point:
        st.session_state["entrance_draft_lat"] = float(point["lat"])
        st.session_state["entrance_draft_lon"] = float(point["lng"])
        st.caption(f"지도에서 임시 좌표를 지정했습니다: {point['lat']:.7f}, {point['lng']:.7f} — 저장 전에는 영구 반영되지 않습니다.")


def _render_entrance_editor(apartments: pd.DataFrame, entrances: pd.DataFrame, path: Path,
                            route_db: Path, station: pd.Series, radius: int,
                            far_warning_m: float = 1000) -> pd.DataFrame:
    st.divider()
    st.subheader("출입구 보정")
    st.caption("전체 유효 단지를 단지명 또는 kapt_code로 검색합니다. 원본 중심 좌표는 변경하지 않습니다.")
    searchable = apartments[apartments.is_valid_for_linkage].copy()
    query = st.text_input("단지 검색", placeholder="단지명 또는 kapt_code", key="entrance_search")
    if query:
        mask = searchable.complex_name.astype(str).str.contains(query, case=False, na=False, regex=False) | searchable.kapt_code.astype(str).str.contains(query, case=False, na=False, regex=False)
        searchable = searchable[mask]
    options = searchable.kapt_code.astype(str).tolist()
    if not options:
        st.info("검색 결과가 없습니다.")
        return entrances
    names = searchable.set_index(searchable.kapt_code.astype(str)).apply(lambda r: f"{r.complex_name} · {r.kapt_code}", axis=1).to_dict()
    code = st.selectbox("보정할 단지", options, format_func=names.get, key="entrance_complex")
    home = searchable[searchable.kapt_code.astype(str).eq(code)].iloc[0]
    gates = entrances[entrances.kapt_code.astype(str).eq(code)].copy()
    st.caption("파란 마커는 수정 불가 원본 중심, 주황/회색 원은 등록 출입구(활성/비활성), 초록 마커와 원은 선택 역 및 반경입니다.")
    _render_edit_map(home, gates, station, radius)
    if st.button("원본 중심 좌표로 입력값 이동", key="entrance_center", icon=":material/my_location:"):
        st.session_state["entrance_draft_lat"] = float(home.latitude)
        st.session_state["entrance_draft_lon"] = float(home.longitude)

    gate_ids = ["새 출입구"] + gates.entrance_id.astype(str).tolist()
    gate_names = {"새 출입구": "새 출입구"} | gates.set_index("entrance_id").entrance_name.to_dict()
    chosen_id = st.selectbox("출입구 선택", gate_ids, format_func=gate_names.get, key="entrance_id_choice")
    existing = None if chosen_id == "새 출입구" else gates[gates.entrance_id.eq(chosen_id)].iloc[0]
    draft_key = (code, chosen_id)
    if st.session_state.get("entrance_loaded_key") != draft_key:
        st.session_state["entrance_loaded_key"] = draft_key
        st.session_state["entrance_draft_lat"] = float(home.latitude if existing is None else existing.latitude)
        st.session_state["entrance_draft_lon"] = float(home.longitude if existing is None else existing.longitude)
    defaults = existing if existing is not None else {}
    with st.form("entrance_form"):
        name = st.text_input("출입구 이름", value=str(defaults.get("entrance_name", "")))
        c1, c2 = st.columns(2)
        with c1: lat = st.number_input("위도", format="%.7f", key="entrance_draft_lat")
        with c2: lon = st.number_input("경도", format="%.7f", key="entrance_draft_lon")
        c1, c2 = st.columns(2)
        with c1:
            entrance_type = st.selectbox("출입구 유형", ENTRANCE_TYPES, index=ENTRANCE_TYPES.index(defaults.get("entrance_type", "미확인")))
            verification = st.selectbox("확인 상태", VERIFICATION_STATUSES, index=VERIFICATION_STATUSES.index(defaults.get("verification_status", "미확인")))
            enabled = st.checkbox("계산 사용", value=bool(defaults.get("enabled", False)))
        with c2:
            access = st.selectbox("접근 상태", ACCESS_STATUSES, index=ACCESS_STATUSES.index(defaults.get("access_status", "미확인")))
            source = st.selectbox("확인 출처", SOURCE_TYPES, index=SOURCE_TYPES.index(defaults.get("source_type", "기타")))
            verified_at = st.date_input("확인일", value=pd.to_datetime(defaults.get("verified_at"), errors="coerce").date() if pd.notna(pd.to_datetime(defaults.get("verified_at"), errors="coerce")) else None)
        access_note = st.text_input("접근 조건 메모", value=str(defaults.get("access_note", "")))
        source_reference = st.text_input("참고 URL 또는 확인 내용", value=str(defaults.get("source_reference", "")))
        notes = st.text_area("메모", value=str(defaults.get("notes", "")))
        submitted = st.form_submit_button("변경 미리보기", type="primary")
    if submitted:
        now = utc_now_text()
        candidate = {
            "entrance_id": new_entrance_id() if existing is None else existing.entrance_id, "kapt_code": code,
            "entrance_name": name.strip(), "latitude": lat, "longitude": lon, "entrance_type": entrance_type,
            "access_status": access, "access_note": access_note, "verification_status": verification,
            "enabled": enabled, "source_type": source, "source_reference": source_reference,
            "verified_at": verified_at.isoformat() if verified_at else "",
            "created_at": now if existing is None else existing.created_at, "updated_at": now, "notes": notes,
        }
        check = validate_candidate(candidate, apartments, entrances, far_warning_m=far_warning_m)
        if not name.strip(): check["errors"].append("출입구 이름을 입력해 주세요.")
        st.session_state["entrance_preview"] = (candidate, check)
    preview = st.session_state.get("entrance_preview")
    if preview and preview[0]["kapt_code"] == code:
        candidate, check = preview
        for message in check["warnings"]: st.warning(message)
        for message in check["errors"]: st.error(message)
        center_to_gate = check["center_distance_m"]
        center_station = float(build_effective_station_links(pd.DataFrame([home]), pd.DataFrame([station]), empty_entrances()).iloc[0].center_distance_m)
        candidate_frame = pd.concat([entrances[~entrances.entrance_id.eq(candidate["entrance_id"])], pd.DataFrame([candidate])], ignore_index=True)
        preview_link = build_effective_station_links(pd.DataFrame([home]), pd.DataFrame([station]), candidate_frame).iloc[0]
        st.dataframe(pd.DataFrame([{
            "중심점→출입구(m)": center_to_gate, "중심점 기준 역거리(m)": center_station,
            "출입구 적용 후 역거리(m)": preview_link.effective_distance_m,
            "거리 차이(m)": preview_link.effective_distance_m - center_station,
            "현재 반경 포함 변화": f"{center_station <= radius} → {preview_link.effective_distance_m <= radius}",
            "적용 출입구": preview_link.selected_entrance_name or "단지 중심", "접근 조건": preview_link.selected_access_status or "-",
        }]), hide_index=True)
        st.caption("모든 거리는 역 대표 좌표까지의 WGS84 직선거리입니다. 단지 경계·실제 보행 경로를 검증한 결과가 아닙니다.")
        with st.container(horizontal=True):
            if st.button("저장", disabled=bool(check["errors"]), type="primary", key="entrance_save"):
                if _persist_entrances(candidate_frame, path, route_db, {str(code)}, {str(candidate["entrance_id"])}):
                    st.session_state.pop("entrance_preview", None); st.rerun()
            if st.button("취소", key="entrance_cancel"):
                st.session_state.pop("entrance_preview", None); st.rerun()
    if existing is not None:
        confirm = st.checkbox("삭제 확인: 이 출입구를 삭제합니다", key="entrance_delete_confirm")
        if st.button("출입구 삭제", disabled=not confirm, key="entrance_delete", icon=":material/delete:"):
            updated = entrances[~entrances.entrance_id.eq(existing.entrance_id)].copy()
            if _persist_entrances(updated, path, route_db, {str(code)}, {str(existing.entrance_id)}): st.rerun()

    st.markdown("#### 등록 출입구")
    if gates.empty: st.info("이 단지에 등록된 출입구가 없습니다.")
    else:
        shown = gates.copy(); shown["계산 포함(기본)"] = shown.enabled & shown.verification_status.eq("사용자 확인") & shown.entrance_type.isin(["보행 전용", "보행·차량 겸용"]) & shown.access_status.eq("상시 통행")
        st.dataframe(shown[["entrance_name", "latitude", "longitude", "entrance_type", "access_status", "verification_status", "enabled", "계산 포함(기본)"]], hide_index=True)

    st.markdown("#### CSV 가져오기·내보내기")
    st.download_button("출입구 전체 CSV", _entrance_csv_bytes(entrances), "apartment_entrances.csv", "text/csv")
    uploaded = st.file_uploader("출입구 CSV 가져오기", type=["csv"], key="entrance_csv")
    if uploaded is not None:
        try:
            incoming = parse_entrance_csv(uploaded.getvalue())
            missing_codes = sorted(set(incoming.kapt_code) - set(apartments.kapt_code.astype(str)))
            if missing_codes: st.warning(f"현재 마스터에 없는 kapt_code {len(missing_codes)}개는 미연결 상태로 보존됩니다.")
            action = st.segmented_control("중복 ID 처리", ["새 ID만 추가", "동일 ID 업데이트"], default="새 ID만 추가")
            merged, changes = merge_import(entrances, incoming, action)
            st.dataframe(changes[["change", "entrance_id", "kapt_code", "entrance_name", "latitude", "longitude"]], hide_index=True)
            if st.button("미리보기 내용 가져오기", key="entrance_import_apply"):
                affected = set(changes.kapt_code.astype(str))
                affected_ids = set(changes.entrance_id.astype(str))
                if _persist_entrances(merged, path, route_db, affected, affected_ids): st.rerun()
        except EntranceDataError as exc: st.error(f"가져오기 검증 실패: {exc}")
    return st.session_state.get("entrances", entrances)


def render_apartment_tab(*, root: Path, metrics: pd.DataFrame, coordinates: pd.DataFrame,
                         monthly: pd.DataFrame, comparison: pd.DataFrame | None = None) -> None:
    st.subheader("역별 인근 아파트")
    st.caption("저장된 실제 보행 경로를 조회합니다. 직선거리는 계산 후보 탐색에만 사용하며 도보거리로 표시하지 않습니다.")
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
    entrance_path = root / "data" / "corrections" / "apartment_entrances.csv"
    disk_revision = entrance_revision(entrance_path)
    if st.session_state.get("entrance_revision") != disk_revision or "entrances" not in st.session_state:
        try:
            entrance_data, disk_revision = read_entrances(entrance_path)
        except (EntranceDataError, OSError) as exc:
            st.error(f"출입구 보정 파일을 읽을 수 없습니다: {exc}")
            entrance_data, disk_revision = empty_entrances(), entrance_revision(entrance_path)
        st.session_state["entrances"] = entrance_data
        st.session_state["entrance_revision"] = disk_revision
    entrances = st.session_state["entrances"]

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
    # 최초 진입에만 데이터의 역 ID·노선을 검증해 대연역(2호선)을 적용한다.
    daeyeon = stations[
        stations.canonical_station_id.eq("213")
        & stations.line_id.astype(str).eq("2")
    ]
    default_station_id = None if daeyeon.empty else str(daeyeon.iloc[0].canonical_station_id)
    preferred = st.session_state.get("apt_station_id")
    if preferred is None:
        preferred = default_station_id or st.session_state.get("rising_station_id")
    if preferred not in ids:
        preferred = ids[0]
    st.session_state["apt_station_id"] = preferred
    labels = candidates.set_index("canonical_station_id").apply(lambda r: f"{r.station_name} · {r.line_id}호선", axis=1).to_dict()
    with f3:
        station_id = st.selectbox("역", ids, format_func=labels.get, key="apt_station_id")
    selected_station = stations[stations.canonical_station_id.eq(station_id)].iloc[0]
    included_names = candidates.station_name.tolist()
    st.caption(f"{segment_note} 포함 역({len(included_names)}개): " + ", ".join(included_names))

    g1, g2, g3, g4 = st.columns(4)
    with g1:
        radius = st.segmented_control("반경", [300, 500, 800, 1000], default=300,
                                      format_func=lambda x: f"{x:,}m", key="apt_radius")
    with g2:
        household_mode = st.segmented_control("최소 세대수", ["500", "1,000", "직접 입력"], default="500", key="apt_household_mode")
    with g3:
        minimum_households = st.number_input("직접 입력 세대수", min_value=0, value=500, step=50,
                                             disabled=household_mode != "직접 입력", key="apt_household_custom")
    minimum_households = {"500": 500, "1,000": 1000}.get(household_mode, int(minimum_households))
    with g4:
        basis_label = st.segmented_control("거리 기준", ["보정 좌표 우선", "원본 중심 좌표만"], default="보정 좌표 우선", key="apt_basis")
        st.session_state.setdefault("apt_restricted", True)
        include_restricted = st.checkbox("시간제한·입주민 전용 포함", key="apt_restricted")
    st.caption("포함 시 사용자 확인된 시간제한·입주민 전용 보행 출입구도 후보가 됩니다. 실제 통행 가능 시간과 입주민 자격을 확인해야 합니다.")

    try:
        secret_route_key = str(st.secrets.get("GRAPHHOPPER_API_KEY", ""))
    except (FileNotFoundError, KeyError):
        secret_route_key = ""
    route_config = RouteConfig.from_env(graphhopper_key=secret_route_key or None)
    route_db = persistent_db_path(root)
    st.caption(f"경로 저장 위치: {route_db}")
    if not os.getenv("BUSAN_METRO_ROUTE_DB", "").strip():
        st.warning("현재 경로 저장소는 프로젝트 로컬 디스크입니다. 컨테이너형 배포에서는 영속 볼륨을 연결하고 BUSAN_METRO_ROUTE_DB를 그 경로로 지정하세요.")
    candidates_for_route = build_candidates(
        bundle.apartments, selected_station, entrances,
        include_restricted=include_restricted,
        mode="entrance_preferred" if basis_label == "보정 좌표 우선" else "center_only",
        candidate_radius_m=1600,
    )
    household_map = bundle.apartments.set_index(bundle.apartments.kapt_code.astype(str)).households
    needed_candidates = candidates_for_route[
        candidates_for_route.candidate_distance_m.le(float(radius))
        & candidates_for_route.kapt_code.map(household_map).ge(minimum_households)
    ].copy()
    route_results = read_results(route_db, candidates_for_route, route_config)
    needed_results = read_results(route_db, needed_candidates, route_config)
    route_counts = status_counts(needed_results)

    # 일반 조회와 보정 저장 직후 모두 현재 화면에 필요한 누락/갱신 결과만 한 번 계산한다.
    auto_pending = needed_results[needed_results.result_status.isin(["미계산", "갱신 필요"])]
    refresh_job = st.session_state.get("route_refresh_queue")
    if refresh_job:
        changed = set(refresh_job.get("kapt_codes", []))
        current_changed = auto_pending[auto_pending.kapt_code.astype(str).isin(changed)]
        auto_pending = current_changed
        st.session_state.pop("route_refresh_queue", None)
    if route_config.available and not auto_pending.empty:
        st.info(f"현재 조회에 필요한 경로 {len(auto_pending):,}건을 자동 갱신합니다. 출입구 보정 내용은 이미 저장되었습니다.")
        bar = st.progress(0, text="보행 경로 자동 계산 준비 중")
        def update_auto_progress(done, total, counts):
            bar.progress(done / max(total, 1), text=f"자동 계산 {done}/{total} · 완료 {counts['완료']} · 실패 {counts['실패']}")
        auto_counts = calculate_pending(
            route_db, auto_pending[candidates_for_route.columns], route_config,
            progress=update_auto_progress,
        )
        st.session_state["route_last_calculation"] = auto_counts
        st.rerun()
    elif refresh_job and not route_config.available:
        st.warning("출입구 보정은 저장되었지만 API 키가 없어 경로 계산은 갱신 대기 상태입니다. 키 설정 후 화면 버튼으로 계산할 수 있습니다.")

    prepared = best_results(route_results)
    if prepared.empty:
        linked = bundle.apartments.iloc[0:0].copy()
        for col in ["distance_m", "effective_distance_m", "duration_s", "route_json", "selected_latitude",
                    "selected_longitude", "center_distance_m", "selected_entrance_id", "selected_entrance_name",
                    "selected_access_status", "coordinate_basis"]:
            linked[col] = pd.Series(dtype="object")
    else:
        route_view = prepared.rename(columns={
            "apartment_entrance_id": "selected_entrance_id", "apartment_lat": "selected_latitude",
            "apartment_lon": "selected_longitude", "access_status": "selected_access_status",
        }).copy()
        route_view["effective_distance_m"] = route_view.distance_m
        route_view["coordinate_basis"] = np.where(route_view.selected_entrance_id.eq("center"), "단지 중심", "보행 출입구")
        route_view["selected_entrance_name"] = route_view.selected_entrance_id.map(
            entrances.set_index("entrance_id").entrance_name.to_dict()).fillna("단지 중심")
        route_view["selected_verified_at"] = route_view.selected_entrance_id.map(
            entrances.set_index("entrance_id").verified_at.to_dict())
        center_map = candidates_for_route.drop_duplicates("kapt_code").set_index("kapt_code").center_distance_m
        route_view["center_distance_m"] = route_view.kapt_code.map(center_map)
        linked = route_view.merge(bundle.apartments, on="kapt_code", validate="one_to_one", suffixes=("", "_home"))

    status_text = " · ".join(f"{name} {count:,}건" for name, count in route_counts.items()) or "후보 없음"
    st.info(f"영속 경로 저장소: {status_text}")
    last_calculation = st.session_state.pop("route_last_calculation", None)
    if last_calculation:
        if last_calculation.get("실패"):
            st.warning(f"자동 계산 완료 {last_calculation['완료']:,}건 · 실패 {last_calculation['실패']:,}건. 보정 내용은 보존되며 실패 항목만 재시도할 수 있습니다.")
        else:
            st.success(f"자동 경로 갱신 완료: {last_calculation['완료']:,}건")
    action_cols = st.columns(2)
    with action_cols[0]:
        calculate_clicked = st.button("현재 역 미계산·갱신 필요 경로 계산", type="primary",
                                      disabled=not route_config.available, key="calculate_routes")
    with action_cols[1]:
        retry_clicked = st.button("실패 경로 재시도", disabled=not route_config.available,
                                  key="retry_routes")
    if not route_config.available:
        st.caption("경로 계산은 환경변수 또는 Streamlit Secrets에 GRAPHHOPPER_API_KEY 설정 후 사용할 수 있습니다. 저장 결과 조회에는 키가 필요하지 않습니다.")
    if calculate_clicked or retry_clicked:
        bar = st.progress(0, text="보행 경로 계산 준비 중")
        def update_progress(done, total, counts):
            bar.progress(done / max(total, 1), text=f"경로 계산 {done}/{total} · 완료 {counts['완료']} · 실패 {counts['실패']}")
        counts = calculate_pending(route_db, needed_candidates, route_config,
                                   retry_failed=retry_clicked, progress=update_progress)
        st.success(f"경로 계산 완료: {counts}")
        st.rerun()
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
        st.metric("보행 출입구 기준", f"{filtered.coordinate_basis.eq('보행 출입구').sum():,}개", border=True)
        st.metric("단지 중심 기준", f"{filtered.coordinate_basis.eq('단지 중심').sum():,}개", border=True)
    st.caption("세대수 합계는 저장된 실제 보행거리 기준이며 실제 역 이용 가구 수가 아닙니다. 보정 좌표는 사용자가 확인한 단지 보행 출입구입니다.")

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
        map_homes["distance_display"] = map_homes.effective_distance_m.map(lambda x: f"{x:,.1f}m")
        map_homes["center_latitude"] = map_homes.latitude
        map_homes["center_longitude"] = map_homes.longitude
        map_homes["latitude"] = map_homes.selected_latitude
        map_homes["longitude"] = map_homes.selected_longitude
        map_homes["households_display"] = map_homes.households.map(lambda x: "정보 없음" if pd.isna(x) else f"{x:,.0f}세대")
        map_homes["approval_display"] = pd.to_datetime(map_homes.get("approval_date"), errors="coerce").dt.strftime("%Y-%m-%d").fillna("정보 없음")
        map_homes["parking_display"] = map_homes.get("parking_per_household", pd.Series(index=map_homes.index)).map(lambda x: "정보 없음" if pd.isna(x) else f"{x:.2f}대")
        map_homes["address_display"] = map_homes.get("road_address", pd.Series(index=map_homes.index)).fillna(map_homes.get("legal_address", "정보 없음"))
        # pydeck로 전체 원본 행을 직렬화하지 않고 마커·팝업·선택 경로에 필요한 값만 보낸다.
        map_homes = map_homes[[
            "kapt_code", "complex_name", "latitude", "longitude", "center_latitude", "center_longitude",
            "marker_radius", "marker_color", "line_color", "distance_display", "households_display",
            "approval_display", "parking_display", "address_display", "route_json",
        ]].copy()
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
        route_coords = json.loads(selected_home.iloc[0].route_json)
        if len(route_coords) >= 2:
            layers.append(pdk.Layer("PathLayer", [{"path": route_coords}], id="selected-walking-route",
                                    get_path="path", get_color=[220, 38, 38, 220], get_width=5,
                                    width_min_pixels=3))
        layers.append(pdk.Layer("ScatterplotLayer", selected_home, id="selected-apartment", pickable=True, stroked=True,
                                get_position="[longitude, latitude]", get_radius=19, radius_units="'pixels'",
                                get_fill_color=[239, 68, 68, 235], get_line_color=[127, 29, 29, 255], line_width_min_pixels=4))
        selected_center = selected_home.assign(latitude=selected_home.center_latitude, longitude=selected_home.center_longitude,
                                               tooltip_title="원본 단지 중심", tooltip_detail="수정되지 않는 원본 중심 좌표")
        layers.append(pdk.Layer("ScatterplotLayer", selected_center, id="selected-apartment-center", pickable=True, stroked=True,
                                get_position="[longitude, latitude]", get_radius=9, radius_units="'pixels'",
                                get_fill_color=[250, 204, 21, 255], get_line_color=[113, 63, 18, 255], line_width_min_pixels=2))
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
    st.caption("초록색 마커·라벨은 선택 역, 파란색은 조건 충족 아파트의 적용 좌표, 빨간색 큰 마커는 선택 단지의 적용 좌표, 노란색은 선택 단지의 수정 불가 원본 중심입니다. 반투명 영역은 WGS84 구면 거리로 생성한 설정 반경입니다.")

    if filtered.empty:
        st.info("해당 조건을 충족하는 단지가 없습니다.")
    else:
        sort_label = st.selectbox("목록 정렬", ["거리 가까운 순", "세대수 많은 순", "연식 낮은 순", "주차 많은 순"], key="apt_sort")
        sort_spec = {"거리 가까운 순": ("effective_distance_m", True), "세대수 많은 순": ("households", False),
                     "연식 낮은 순": ("apartment_age", True), "주차 많은 순": ("parking_per_household", False)}[sort_label]
        ordered = filtered.sort_values(sort_spec[0], ascending=sort_spec[1], na_position="last")
        display = _display_table(ordered)
        st.dataframe(display, hide_index=True, column_config={
            "적용 거리(m)": st.column_config.NumberColumn(format="%.1f"),
            "중심점 거리(m)": st.column_config.NumberColumn(format="%.1f"),
            "세대수": st.column_config.NumberColumn(format="%,.0f"),
            "동수": st.column_config.NumberColumn(format="%,.0f"),
            "세대당 주차대수": st.column_config.NumberColumn(format="%.2f"),
        }, key="apartment_list")
        metadata = {"station_id": station_id, "station": labels[station_id], "distance_basis": basis_label + " · WGS84 직선거리",
                    "radius_m": radius, "minimum_households": minimum_households, "districts": district_filter,
                    "dongs": dong_filter, "age_groups": age_filter}
        st.download_button("현재 목록 CSV", _csv_bytes(display, metadata), f"{station_id}_apartments.csv", "text/csv", key="apt_download")

        chosen = filtered[filtered.kapt_code.eq(selected_code)].iloc[0]
        st.subheader("단지 상세")
        with st.container(border=True):
            st.write(f"**{chosen.complex_name}** · kapt_code `{chosen.kapt_code}`")
            st.write(f"{chosen.households:,.0f}세대 · {chosen.get('buildings', np.nan):,.0f}동 · 사용승인일 {pd.to_datetime(chosen.get('approval_date')).strftime('%Y-%m-%d') if pd.notna(chosen.get('approval_date')) else '정보 없음'}")
            st.write(f"{selected_station.station_name}({selected_station.line_id}호선) 실제 보행 {chosen.distance_m:,.0f}m · 약 {chosen.duration_s / 60:.0f}분")
            st.write(f"경로 제공자 {chosen.provider} · 계산 버전 {chosen.calculation_version} · 계산 시각 {chosen.calculated_at}")
            st.caption("가격·학군 자료는 표시하지 않습니다. 이후 가격 자료는 kapt_code를 키로 연결할 수 있습니다. 사용승인일은 실제 입주일과 다를 수 있습니다.")
            all_gates = entrances[entrances.kapt_code.astype(str).eq(str(selected_code))].copy()
            if all_gates.empty: st.info("등록된 출입구가 없습니다. 중심 좌표를 사용합니다.")
            else:
                all_gates["현재 계산 포함"] = all_gates.entrance_id.eq(chosen.selected_entrance_id)
                st.dataframe(all_gates[["entrance_name", "entrance_type", "access_status", "verification_status", "enabled", "verified_at", "현재 계산 포함"]], hide_index=True)

    st.caption("미계산·실패·갱신 필요 결과는 정상 결과와 분리되며 지도와 거리 조건 집계에 포함되지 않습니다.")

    entrances = _render_entrance_editor(bundle.apartments, entrances, entrance_path, route_db, selected_station, int(radius),
                                         float(config.get("entrance_far_warning_m", 1000)))

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
