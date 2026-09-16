from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from .apartments import read_apartment_parquet, resolve_apartment_path, validate_apartments
from .entrances import read_entrances
from .walking_routes import RouteConfig, build_candidates, calculate_pending, persistent_db_path


def main() -> None:
    parser = argparse.ArgumentParser(description="아파트 실제 보행 경로를 영속 저장소에 사전 계산합니다.")
    parser.add_argument("--station-id", default="213", help="기본값: 대연역(2호선) ID 213")
    parser.add_argument("--include-restricted", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--kapt-code", action="append", help="지정 단지만 계산합니다. 여러 번 지정할 수 있습니다.")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    root = Path.cwd()
    metrics = pd.read_parquet(root / "data/processed/metro/station_metrics.parquet")
    coords = pd.read_csv(root / "config/station_coordinates.csv", dtype={"canonical_station_id": str})
    stations = metrics[["canonical_station_id", "station_name", "line_id"]].merge(coords, on="canonical_station_id")
    stations["canonical_station_id"] = stations.canonical_station_id.astype(str).str.zfill(3)
    match = stations[stations.canonical_station_id.eq(str(args.station_id).zfill(3))]
    if match.empty:
        raise SystemExit(f"역 ID를 찾을 수 없습니다: {args.station_id}")
    source, _ = resolve_apartment_path(root)
    if source is None:
        raise SystemExit("아파트 원본 경로를 찾을 수 없습니다.")
    apartments = validate_apartments(read_apartment_parquet(source), stations).apartments
    entrances, _ = read_entrances(root / "data/corrections/apartment_entrances.csv")
    candidates = build_candidates(apartments, match.iloc[0], entrances, include_restricted=args.include_restricted)
    if args.kapt_code:
        candidates = candidates[candidates.kapt_code.isin(set(args.kapt_code))].copy()
        if candidates.empty:
            raise SystemExit("지정한 kapt_code가 현재 역의 계산 후보에 없습니다.")
    config = RouteConfig.from_env()
    if not config.available:
        raise SystemExit("GRAPHHOPPER_API_KEY를 설정해야 합니다. 직선거리는 도보거리로 저장하지 않습니다.")
    result = calculate_pending(persistent_db_path(root), candidates, config,
                               retry_failed=args.retry_failed, limit=args.limit,
                               progress=lambda n, total, counts: print(f"\r{n}/{total} {counts}", end="", flush=True))
    print("\n", result, persistent_db_path(root))


if __name__ == "__main__":
    main()
