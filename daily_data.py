#!/usr/bin/env python3
"""
INVICTUS Daily Data Collector v1.0.0
=====================================
매일 08:05 KST GitHub Actions 자동 실행.
Yahoo Finance + FRED 공식 API → append-only CSV.

원칙:
- T1 SSOT (Triangulation T1 레이어)
- append-only (날짜 PK, 덮어쓰기 시 audit 보존)
- as_of 컬럼 = 수집 시각 UTC ISO8601
- 판정 결과 저장 금지 (REG-007 T1 준수)
"""

import os
import sys
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf
import requests

# ────────────────────────────────────────────────────────
# 상수
# ────────────────────────────────────────────────────────
VERSION = "1.0.0"
SCHEMA_VERSION = "1.0"
DATA_DIR = Path("data")
CSV_PATH = DATA_DIR / "daily_2026.csv"
META_PATH = DATA_DIR / "metadata.json"
AUDIT_PATH = DATA_DIR / "audit_log.jsonl"

# INVICTUS 20티커 유니버스 (SSOT §0 기준)
TICKERS_20 = [
    "GLD", "SMH", "EWZ", "XLE", "SLV", "PAVE", "COPX", "XLU",
    "VEA", "QQQM", "IWM", "XLF", "XLV", "INDA", "ITA", "CIBR",
    "NLR", "CQQQ", "VNM", "TLT",
]

# Yahoo 파생 매크로 8종
YAHOO_MACRO = {
    "VIX":   "^VIX",
    "MOVE":  "^MOVE",
    "DXY":   "DX-Y.NYB",
    "WTI":   "CL=F",
    "BTC":   "BTC-USD",
    "KRW":   "KRW=X",
    "ES_F":  "ES=F",
    "NQ_F":  "NQ=F",
}

# FRED 9종 (SSOT §26 센서)
FRED_SERIES = {
    "OAS_HY":  "BAMLH0A0HYM2",
    "T5YIE":   "T5YIE",
    "SAHM":    "SAHMCURRENT",
    "DFII10":  "DFII10",
    "T10Y2Y":  "T10Y2Y",
    "ICSA":    "ICSA",
    "RRP":     "RRPONTSYD",
    "GS2":     "GS2",
    "GS10":    "GS10",
}

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")

# 필수 필드 (하나라도 결측 시 WARN, 3개+ 결측 시 FAIL)
CRITICAL_FIELDS = ["GLD_close", "SMH_close", "XLE_close", "VIX", "OAS_HY"]


# ────────────────────────────────────────────────────────
# 수집 함수
# ────────────────────────────────────────────────────────
def fetch_yahoo(symbol: str) -> dict:
    """Yahoo 최신 종가·거래량 조회. 실패 시 None."""
    try:
        hist = yf.Ticker(symbol).history(period="5d", interval="1d")
        if hist.empty:
            return {"close": None, "volume": None, "date": None}
        last = hist.iloc[-1]
        return {
            "close": float(last["Close"]),
            "volume": float(last["Volume"]) if "Volume" in last else None,
            "date": hist.index[-1].strftime("%Y-%m-%d"),
        }
    except Exception as e:
        print(f"⚠️  Yahoo fail {symbol}: {e}", file=sys.stderr)
        return {"close": None, "volume": None, "date": None}


def fetch_fred(series_id: str) -> dict:
    """FRED 최신 관측값 조회. API 키 필수."""
    if not FRED_API_KEY:
        return {"value": None, "date": None}
    try:
        r = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "series_id": series_id,
                "api_key": FRED_API_KEY,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 1,
            },
            timeout=10,
        )
        r.raise_for_status()
        obs = r.json().get("observations", [])
        if not obs:
            return {"value": None, "date": None}
        val = obs[0]["value"]
        return {
            "value": float(val) if val not in (".", "") else None,
            "date": obs[0]["date"],
        }
    except Exception as e:
        print(f"⚠️  FRED fail {series_id}: {e}", file=sys.stderr)
        return {"value": None, "date": None}


# ────────────────────────────────────────────────────────
# 레코드 빌드
# ────────────────────────────────────────────────────────
def build_row() -> dict:
    """오늘자 1행 생성. NYSE 거래일 기준."""
    now_utc = datetime.now(timezone.utc)
    row = {
        "date": None,
        "as_of": now_utc.isoformat(timespec="seconds"),
        "source_version": VERSION,
    }
    nyse_dates = []

    # 20티커 OHLCV
    for sym in TICKERS_20:
        d = fetch_yahoo(sym)
        row[f"{sym}_close"] = d["close"]
        row[f"{sym}_volume"] = d["volume"]
        if d["date"]:
            nyse_dates.append(d["date"])

    # Yahoo 파생 매크로
    for name, sym in YAHOO_MACRO.items():
        d = fetch_yahoo(sym)
        row[name] = d["close"]
        if d["date"]:
            nyse_dates.append(d["date"])

    # FRED 센서 9종
    for name, sid in FRED_SERIES.items():
        d = fetch_fred(sid)
        row[name] = d["value"]
        row[f"{name}_asof"] = d["date"]  # FRED는 시리즈별 갱신일 상이

    # NYSE 거래일 = 다수결
    if nyse_dates:
        row["date"] = Counter(nyse_dates).most_common(1)[0][0]
    else:
        row["date"] = now_utc.date().isoformat()

    return row


# ────────────────────────────────────────────────────────
# Upsert + Audit
# ────────────────────────────────────────────────────────
def upsert_row(row: dict) -> dict:
    """CSV에 행 추가·갱신. 덮어쓰기 시 audit log 보존."""
    DATA_DIR.mkdir(exist_ok=True)

    if CSV_PATH.exists():
        df = pd.read_csv(CSV_PATH)
    else:
        df = pd.DataFrame(columns=list(row.keys()))

    # 새 컬럼 추가
    for c in set(row.keys()) - set(df.columns):
        df[c] = None

    date_key = row["date"]
    mask = df["date"] == date_key
    audit_entry = None

    if mask.any():
        old_row = df.loc[mask].iloc[0].to_dict()
        audit_entry = {
            "overwritten_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "date": date_key,
            "old": old_row,
            "new": row,
        }
        df = df.loc[~mask].copy()

    df_new = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    df_new = df_new.sort_values("date").reset_index(drop=True)
    df_new.to_csv(CSV_PATH, index=False)

    if audit_entry:
        with open(AUDIT_PATH, "a") as f:
            f.write(json.dumps(audit_entry, default=str) + "\n")

    return {
        "row_count": len(df_new),
        "action": "updated" if audit_entry else "inserted",
        "date": date_key,
    }


def update_metadata(result: dict, row: dict) -> None:
    meta = {
        "schema_version": SCHEMA_VERSION,
        "script_version": VERSION,
        "last_run_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_data_date": row["date"],
        "row_count": result["row_count"],
        "last_action": result["action"],
        "columns": list(row.keys()),
        "critical_fields": CRITICAL_FIELDS,
    }
    with open(META_PATH, "w") as f:
        json.dump(meta, f, indent=2)


# ────────────────────────────────────────────────────────
# 메인
# ────────────────────────────────────────────────────────
def main() -> int:
    print(f"INVICTUS Daily Data Collector v{VERSION}")
    print(f"Start: {datetime.now(timezone.utc).isoformat()}")

    if not FRED_API_KEY:
        print("❌ FRED_API_KEY 미설정. Secret 등록 필요.", file=sys.stderr)
        return 1

    row = build_row()
    result = upsert_row(row)
    update_metadata(result, row)

    print(f"✅ {result['action'].upper()} row for {row['date']}")
    print(f"   Total rows: {result['row_count']}")
    print(f"   CSV: {CSV_PATH}")

    # 결측 체크
    missing = [f for f in CRITICAL_FIELDS if row.get(f) is None]
    if missing:
        print(f"⚠️  Missing critical: {missing}", file=sys.stderr)
        if len(missing) >= 3:
            print("❌ 3개 이상 결측 — 수집 실패로 간주", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
