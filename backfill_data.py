#!/usr/bin/env python3
"""
INVICTUS Backfill Script v1.0.0
=====================================
지정 기간(기본 3개월)의 과거 데이터를 daily_2026.csv에 소급 적재.

사용법:
  python backfill_data.py                  # 기본 3개월 (90일)
  python backfill_data.py --months 6       # 6개월
  python backfill_data.py --days 120       # 일수 직접 지정

환경변수:
  FRED_API_KEY  (필수)

특징:
- daily_data.py의 상수를 재사용하여 스키마 일치 (Yahoo 30 + FRED 18)
- Yahoo: v8 chart API의 range 파라미터로 일괄 수집
- FRED: observation_start 파라미터로 일괄 + forward-fill
- 기존 CSV의 동일 date row는 새 값으로 덮어쓰기 (upsert)
"""

import os
import sys
import time
import argparse
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import numpy as np
import requests
from curl_cffi import requests as cc_requests

# daily_data.py의 상수·스키마 재사용 (단일 진실의 원천)
from daily_data import (
    VERSION,
    DATA_DIR, CSV_PATH,
    TICKERS_20, YAHOO_MACRO, FRED_SERIES,
    FRED_API_KEY,
    YAHOO_BASE, YAHOO_RETRIES, YAHOO_BACKOFF_SEC,
)

# ────────────────────────────────────────────────────────
# 세션
# ────────────────────────────────────────────────────────
_yahoo_session = None


def get_yahoo_session():
    global _yahoo_session
    if _yahoo_session is None:
        _yahoo_session = cc_requests.Session(impersonate="chrome")
        print("✅ curl_cffi 세션 활성 (chrome impersonate)")
    return _yahoo_session


# ────────────────────────────────────────────────────────
# Yahoo 다중 바 조회
# ────────────────────────────────────────────────────────
def _pick_yahoo_range(days: int) -> str:
    """일수에 따라 Yahoo v8 range 토큰 선택."""
    if days <= 30:
        return "1mo"
    if days <= 90:
        return "3mo"
    if days <= 180:
        return "6mo"
    if days <= 365:
        return "1y"
    if days <= 730:
        return "2y"
    return "5y"


def fetch_yahoo_range(symbol: str, days: int) -> list:
    """
    Yahoo v8 chart API에서 최근 N일 일별 바 반환.
    반환: [{"date": "2026-01-24", "close": ..., "volume": ...}, ...]
    """
    session = get_yahoo_session()
    url = f"{YAHOO_BASE}/{symbol}"
    params = {"interval": "1d", "range": _pick_yahoo_range(days)}

    for attempt in range(1, YAHOO_RETRIES + 1):
        try:
            r = session.get(url, params=params, timeout=20)
            if r.status_code != 200:
                time.sleep(YAHOO_BACKOFF_SEC)
                continue
            data = r.json()
            result = data.get("chart", {}).get("result")
            if not result:
                time.sleep(YAHOO_BACKOFF_SEC)
                continue
            r0 = result[0]
            ts = r0.get("timestamp", []) or []
            ind = r0.get("indicators", {}).get("quote", [{}])[0]
            closes = ind.get("close", []) or []
            volumes = ind.get("volume", []) or []
            out = []
            for i, t in enumerate(ts):
                c = closes[i] if i < len(closes) else None
                v = volumes[i] if i < len(volumes) else None
                if c is None:
                    continue
                d = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")
                out.append({
                    "date": d,
                    "close": float(c),
                    "volume": float(v) if v is not None else None,
                })
            return out
        except Exception as e:
            if attempt < YAHOO_RETRIES:
                time.sleep(YAHOO_BACKOFF_SEC)
            else:
                print(f"⚠️ Yahoo FAIL {symbol}: {str(e)[:100]}", file=sys.stderr)
    return []


# ────────────────────────────────────────────────────────
# FRED 다중 관측 조회 + forward-fill
# ────────────────────────────────────────────────────────
def fetch_fred_range(series_id: str, start_date: str) -> list:
    """start_date 이후 모든 관측치(asc). 빈 값은 제외."""
    if not FRED_API_KEY:
        return []
    try:
        r = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "series_id": series_id,
                "api_key": FRED_API_KEY,
                "file_type": "json",
                "observation_start": start_date,
                "sort_order": "asc",
            },
            timeout=15,
        )
        r.raise_for_status()
        obs = r.json().get("observations", [])
        out = []
        for o in obs:
            v = o.get("value")
            if v in (".", "", None):
                continue
            out.append({"date": o["date"], "value": float(v)})
        return out
    except Exception as e:
        print(f"⚠️ FRED fail {series_id}: {e}", file=sys.stderr)
        return []


def forward_fill_fred(fred_obs: list, trading_dates: list) -> dict:
    """
    FRED 관측값을 거래일에 맞춰 forward-fill.
    반환: {trading_date: (value, asof_date)}
    """
    # trading_dates는 asc 정렬되어 있다고 가정
    out = {}
    idx = 0
    last_val, last_asof = None, None
    for td in trading_dates:
        while idx < len(fred_obs) and fred_obs[idx]["date"] <= td:
            last_val = fred_obs[idx]["value"]
            last_asof = fred_obs[idx]["date"]
            idx += 1
        out[td] = (last_val, last_asof)
    return out


# ────────────────────────────────────────────────────────
# 백필 메인
# ────────────────────────────────────────────────────────
def run_backfill(days: int) -> int:
    print(f"INVICTUS Backfill v1.0.0 (collector v{VERSION})")
    print(f"Target: last {days} days")
    print(f"Start: {datetime.now(timezone.utc).isoformat()}\n")

    if not FRED_API_KEY:
        print("❌ FRED_API_KEY 미설정.", file=sys.stderr)
        return 1

    start_date = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()

    # 1. Yahoo 티커 수집
    print("📡 Yahoo 티커 수집 중...")
    yahoo_data = {}  # {symbol: {date: (close, volume)}}
    y_success, y_fail = 0, 0
    for sym in TICKERS_20:
        bars = fetch_yahoo_range(sym, days)
        yahoo_data[sym] = {b["date"]: (b["close"], b["volume"]) for b in bars}
        if bars:
            y_success += 1
        else:
            y_fail += 1
        print(f"  {sym}: {len(bars)} bars")

    # 2. Yahoo 매크로 수집
    print("\n📡 Yahoo 매크로 수집 중...")
    macro_data = {}  # {name: {date: close}}
    for name, sym in YAHOO_MACRO.items():
        bars = fetch_yahoo_range(sym, days)
        macro_data[name] = {b["date"]: b["close"] for b in bars}
        if bars:
            y_success += 1
        else:
            y_fail += 1
        print(f"  {name} ({sym}): {len(bars)} bars")

    # 3. 마스터 거래일 인덱스 (50% 이상 티커 커버 기준)
    date_counter = defaultdict(int)
    for sym_data in yahoo_data.values():
        for d in sym_data.keys():
            date_counter[d] += 1
    threshold = max(1, int(len(TICKERS_20) * 0.5))
    trading_dates = sorted([d for d, c in date_counter.items() if c >= threshold])

    if not trading_dates:
        print("❌ 거래일 인덱스 생성 실패 — Yahoo 수집 실패 가능성.", file=sys.stderr)
        return 1

    print(f"\n📅 Trading dates: {len(trading_dates)}일 ({trading_dates[0]} ~ {trading_dates[-1]})")

    # 4. FRED 수집 + forward-fill (월간 시리즈 대비 60일 패딩)
    print("\n📡 FRED 수집 중...")
    fred_start = (datetime.strptime(start_date, "%Y-%m-%d").date()
                  - timedelta(days=60)).isoformat()
    fred_data = {}  # {name: {trading_date: (value, asof)}}
    f_success, f_fail = 0, 0
    for name, sid in FRED_SERIES.items():
        obs = fetch_fred_range(sid, fred_start)
        fred_data[name] = forward_fill_fred(obs, trading_dates)
        matched = sum(1 for v, _ in fred_data[name].values() if v is not None)
        if obs:
            f_success += 1
        else:
            f_fail += 1
        print(f"  {name} ({sid}): {len(obs)} obs → {matched}/{len(trading_dates)} 일 매칭")

    # 5. 각 거래일에 대해 row 빌드
    print(f"\n🔨 {len(trading_dates)}일 row 생성...")
    as_of = datetime.now(timezone.utc).isoformat(timespec="seconds")
    src_ver = f"{VERSION}-backfill"
    rows = []
    for td in trading_dates:
        row = {"date": td, "as_of": as_of, "source_version": src_ver}
        for sym in TICKERS_20:
            c, v = yahoo_data.get(sym, {}).get(td, (None, None))
            row[f"{sym}_close"] = c
            row[f"{sym}_volume"] = v
        for name in YAHOO_MACRO:
            row[name] = macro_data.get(name, {}).get(td, None)
        for name in FRED_SERIES:
            v, asof = fred_data.get(name, {}).get(td, (None, None))
            row[name] = v
            row[f"{name}_asof"] = asof
        rows.append(row)

    # 6. CSV 업서트 (batch)
    print(f"\n💾 CSV 업서트 중...")
    DATA_DIR.mkdir(exist_ok=True)
    if CSV_PATH.exists():
        df = pd.read_csv(CSV_PATH)
    else:
        df = pd.DataFrame()

    new_df = pd.DataFrame(rows)

    # 겹치는 date 제거 (새 값으로 덮어쓰기)
    overlap_count = 0
    if not df.empty:
        overlap_count = int(df["date"].isin(new_df["date"]).sum())
        df = df[~df["date"].isin(new_df["date"])].copy()

    # 컬럼 통일 (dtype 경고 회피)
    all_cols = list(dict.fromkeys(list(df.columns) + list(new_df.columns)))
    df = df.reindex(columns=all_cols)
    new_df = new_df.reindex(columns=all_cols)

    merged = pd.concat([df, new_df], ignore_index=True, sort=False)
    merged = merged.sort_values("date").reset_index(drop=True)
    merged.to_csv(CSV_PATH, index=False)

    # 7. 요약
    print("\n" + "=" * 56)
    print(f"✅ Backfill 완료")
    print(f"  기간:          {trading_dates[0]} ~ {trading_dates[-1]}")
    print(f"  거래일:        {len(trading_dates)}일")
    print(f"  신규/갱신 row: {len(rows)} (그 중 덮어쓴 기존: {overlap_count})")
    print(f"  총 row:        {len(merged)}")
    print(f"  Yahoo 성공:    {y_success}/{y_success + y_fail}")
    print(f"  FRED 성공:     {f_success}/{f_success + f_fail}")
    print(f"  CSV:           {CSV_PATH}")
    print("=" * 56)

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="INVICTUS 과거 데이터 백필")
    parser.add_argument("--days", type=int, default=90, help="백필 일수 (기본 90 = 3개월)")
    parser.add_argument("--months", type=int, default=None, help="월수 지정 (days 덮어씀)")
    args = parser.parse_args()

    days = args.days
    if args.months is not None:
        days = args.months * 30

    sys.exit(run_backfill(days))
