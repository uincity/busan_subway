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
- `kapt_code`별 복수 보행 출입구 수기 보정, 지도 클릭·직접 좌표 입력, 접근조건별 거리 비교

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

### 아파트 출입구 보정

`아파트연결` 탭의 `출입구 보정`에서 전체 유효 단지를 단지명 또는 `kapt_code`로 검색합니다.
편집 지도에서 지점을 클릭하거나 위도·경도를 직접 입력한 뒤 속성을 채우고 `변경 미리보기`와
`저장`을 차례로 누릅니다. 지도 클릭은 임시 좌표만 바꾸며 저장 전에는 파일이나 거리 결과에
반영되지 않습니다. 원본 아파트 Parquet와 중심 좌표는 수정하지 않습니다.

기본 계산에는 활성화되고 사용자가 확인했으며, `보행 전용` 또는 `보행·차량 겸용`이고
`상시 통행`인 출입구만 사용합니다. 화면 옵션으로 시간제한·입주민 전용 출입구를 포함할 수
있습니다. 차량 전용·폐쇄·미확인 출입구는 제외되며 유효 출입구가 없는 단지는 중심 좌표로
대체합니다. WGS84 직선거리는 계산 후보 탐색에만 사용하며, 지도와 반경 판정에는 영속 저장된
실제 보행 경로 거리만 사용합니다.

보정 자료는 `data/corrections/apartment_entrances.csv`에 원자적으로 저장되고 저장 직전 버전은
`apartment_entrances.csv.bak`에 보관됩니다. 파일 해시가 바뀌면 저장을 거부해 다른 세션의 변경을
덮어쓰지 않습니다. 두 파일은 개인 운영 자료이므로 Git에서 제외됩니다. 복원하려면 앱을 종료한
뒤 `.bak` 파일을 본 파일명으로 복사하거나, 화면의 CSV 가져오기에서 미리보기 후 병합합니다.
쓰기 가능한 영속 디스크가 없는 배포 환경에서는 저장이 실패하므로 CSV 다운로드본을 보관해야 합니다.

역 좌표는 공공데이터포털의 [부산교통공사 도시철도역사정보](https://www.data.go.kr/data/15043686/fileData.do)(기준일 2021-02-26)에서 수집했으며, `config/station_coordinates.csv`에 역 코드 기준으로 보관합니다.

## 테스트

```powershell
.venv\Scripts\python -m pytest -q -p no:cacheprovider tests\metro
```

## 실제 보행 경로 사전 계산

`아파트연결` 조회는 외부 API를 자동 호출하지 않고 SQLite에 저장된 실제 보행 경로만 읽습니다.
GraphHopper 키를 환경변수로 설정한 뒤 기본 역인 대연역(역 ID `213`, 2호선)을 먼저 계산할 수 있습니다.

```powershell
$env:GRAPHHOPPER_API_KEY="발급받은 키"
.venv\Scripts\python -m src.metro.precompute_routes --station-id 213 --include-restricted
```

화면 자동 계산은 같은 환경변수 또는 Git에 커밋하지 않는 `.streamlit/secrets.toml`의
`GRAPHHOPPER_API_KEY="발급받은 키"`를 사용할 수 있습니다.

실패 항목을 다시 시도하려면 `--retry-failed`, 시험 실행 범위를 제한하려면 `--limit 10`을 추가합니다.
특정 단지만 우선 재시도할 수도 있습니다.

```powershell
.venv\Scripts\python -m src.metro.precompute_routes --station-id 213 --include-restricted --retry-failed --kapt-code A10026094
```

기본 저장 위치는 `data/persistent/walking_routes.sqlite3`이며 프로세스 재시작 후에도 재사용됩니다.
컨테이너나 임시 파일시스템에 배포할 때는 영속 볼륨을 연결한 뒤
`BUSAN_METRO_ROUTE_DB`를 그 볼륨의 SQLite 경로로 반드시 지정해야 합니다. 로컬 프로젝트 디스크만으로
배포 환경의 영속성을 보장하지 않습니다.

GraphHopper의 요청 제한을 피하기 위해 기본 호출 간격은 1.1초이며 HTTP 429 응답은 대기 시간을
늘리면서 최대 4회 재시도합니다. 필요하면 `GRAPHHOPPER_REQUEST_INTERVAL_S`와
`GRAPHHOPPER_MAX_RETRIES` 환경변수로 조정할 수 있습니다.
