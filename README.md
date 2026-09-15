# busan_subway

부산교통공사 시간대별 승하차 원자료를 정규화하고 역별 수요, 출퇴근 방향성, 장기 변화와 ‘뜨는 역 · 지는 역 TOP10’을 탐색하는 Streamlit 프로젝트입니다.

승하차 합계는 고유 이용자 수가 아닌 승하차 건수(연인원)이며, 실제 OD·거주 인구·부동산 가격 전망을 의미하지 않습니다.

## 설치와 실행

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m src.metro.pipeline all
.venv\Scripts\python -m streamlit run app.py
```

개별 작업은 다음 명령으로 실행할 수 있습니다.

```powershell
.venv\Scripts\python -m src.metro.pipeline inventory
.venv\Scripts\python -m src.metro.pipeline normalize
.venv\Scripts\python -m src.metro.pipeline metrics
```

## 주요 기능

- 역별 평일 승하차 및 출퇴근 방향성 분류
- 부산교통공사 공식 역사 위경도에 기반한 역 유형·수요 지도
- 역명 변경을 역 코드 기준으로 연결
- 연간 승차·하차·합계와 관측 완전성 검증
- 전년·3년·5년·직접 선택 및 동일월 누적(YTD) 비교
- 증감률·절대 증감 기준 뜨는 역/지는 역 순위
- 상대 성장률, 저기저, 연속 증가·감소 판정
- 분석 결과 CSV 다운로드
- TOP 차트 클릭 또는 선택 상자로 여는 `뜨는 역 원인 탐색` 상세 화면
- 증가 시점·요일/시간대/방향·비교역·근거 수준을 분리한 탐색 분석
- 주변 사건 CSV 검증·세션 적용·템플릿 및 결과 다운로드
- `kapt_code` 기반 아파트 품질 검증, 전 역 측지 직선거리 연결, 최근접 역 보존
- 호선·관심 구간·반경·세대수·구·동·연식 필터와 아파트 지도·목록·상세·CSV

상세 화면은 `뜨는 역 · 지는 역 TOP10` 탭의 뜨는 역 차트 아래에 있습니다. 막대를 클릭하거나
`상세 분석 역` 선택 상자를 사용하세요. 순위는 결측일을 0으로 채우지 않고 각 기간의 유효
관측일로 나눈 일평균 이용 건수를 비교합니다. 승차+하차는 고유 방문자 수가 아닌 이용 건수입니다.

사건 자료는 `data/external/station_events.csv`에 별도 관리합니다. 운영 화면에는 검증된 실제 자료만
추가하고, 발표일과 실제 시작일을 구분하세요. 파일 쓰기가 지속되지 않는 배포 환경에서는 업로드가
현재 세션에만 적용되므로 `현재 사건 자료` 버튼으로 수정본을 내려받아 보관해야 합니다.

현재 완전한 최신 연도는 2025년입니다. 2026년 자료는 7월까지이므로 연간 순위에서는 제외하고 동일월 누적 비교에서만 사용합니다.

## 산출물

- `reports/metro/source_inventory.csv`: 공식 출처와 로컬 확보 상태
- `reports/metro/coverage_report.md`: 자료 범위 보고서
- `reports/metro/raw_file_manifest.csv`: 파일별 SHA-256과 날짜 범위
- `reports/metro/selected_source_files.csv`: 연도별로 선택한 정본 파일
- `reports/metro/quality_summary.csv`: 중복·오류·관측 범위 요약
- `data/interim/metro/ridership_long.parquet`: 정규화 long-format 자료
- `data/processed/metro/station_metrics.parquet`: 역별 지표와 유형
- `data/processed/metro/station_monthly.parquet`: 월별 평일 추세
- `data/processed/metro/station_annual.parquet`: 연간 승차·하차·합계와 완전성
- `data/processed/metro/station_calendar_monthly.parquet`: YTD 비교용 월별 합계
- `data/processed/metro/station_name_master.csv`: 표준 역명과 원본 명칭 이력
- `data/processed/apartments/kapt_master.parquet`: 검증된 아파트 마스터 스냅샷
- `data/processed/apartments/station_apartment_links.parquet`: 전체 역–단지 직선거리 연결표
- `data/processed/apartments/apartment_nearest_stations.parquet`: 단지별 가까운 역 3개
- `reports/apartments/metadata.json`: 원본 경로·추출일·SHA-256·품질·기본 연결 통계

원본 CSV와 생성된 Parquet는 용량과 중복 관리를 위해 Git에서 제외됩니다. 데이터 파일을 로컬 `data/`에 준비한 후 파이프라인을 실행하세요.

### 아파트 자료

아파트 원본은 환경변수 `BUSAN_METRO_APARTMENT_PARQUET`, `config/apartment_data.yaml`의
`source_path`, 같은 설정의 배포용 `snapshot_path`, 화면 Parquet 업로드 순서로 찾습니다.
업로드 파일은 현재 브라우저 세션에만 적용됩니다.

원본을 수정하지 않고 검증 스냅샷과 전 역 거리 연결표를 만들려면 실행하세요.

```powershell
.venv\Scripts\python -c "from pathlib import Path; from src.metro.apartments import build_artifacts; print(build_artifacts(Path.cwd()))"
```

생성물은 `data/processed/apartments/`와 `reports/apartments/`에 저장됩니다. 거리 포함 판정은
반올림 전 WGS84 대권거리로 하며, 500세대 기준은 `households >= 500`으로 다시 계산합니다.
`apartment_age`의 원본 기준일은 확인되지 않았으므로 화면에 주의를 표시합니다.

역 좌표는 공공데이터포털의 [부산교통공사 도시철도역사정보](https://www.data.go.kr/data/15043686/fileData.do)(기준일 2021-02-26)에서 수집했으며, `config/station_coordinates.csv`에 역 코드 기준으로 보관합니다.

## 테스트

```powershell
.venv\Scripts\python -m pytest -q -p no:cacheprovider tests\metro
```
