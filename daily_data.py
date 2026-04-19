#!/usr/bin/env python3
"""
INVICTUS Daily Data Collector v1.0.1
=====================================
v1.0.1 PATCH (2026-04-19):
- Yahoo Cloudflare 차단 대응 (curl_cffi 세션)
- Yahoo 수집 3회 재시도 (5s 간격)
- CRITICAL 결측 시 WARN 후 exit 0 (FRED 부분데이터라도 commit)
- 재시도/실패 카운트 로깅
"""

import os
import sys
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf
import requests

# ────────────────────────────────────────────────────────
# 상수
# ────────────────────────────────────────────────────────
VERSION = "1.0.1"
SCHEMA_VERSION = "1.0"
DATA_DIR = Path("data")
CSV_PATH = DATA_DIR / "daily_2026.csv"
META_PATH = DATA_DIR / "metadata.json"
AUDIT_PATH = DATA_DIR / "audit_log.jsonl"

YAHOO_RETRIES = 3
YAHOO_BACKOFF_SEC = 5

TICKERS_20 = [
    "GLD", "SMH", "EWZ", "XLE", "SLV", "PAVE", "COPX", "XLU",
    "VEA", "QQQM", "IWM", "XLF", "XLV", "INDA", "ITA", "CIBR",
    "NLR", "CQQQ", "VNM", "TLT",
]

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
CRITICAL_FIELDS = ["GLD_close", "SMH_close", "XLE_close", "VIX", "OAS_HY"]


# ────────────────────────────────────────────────────────
# Yahoo 세션 (Cloudflare 우회)
# ────────────────────────────────────────────────────────
_yahoo_session = None


def get_yahoo_session():
    global _yahoo_session
    if _yahoo_session is not None:
        return _yahoo_session
    try:
        from curl_cffi import requests as cc_requests
        _yahoo_session = cc_requests.Session(impersonate="chrome")
        print("✅ curl_cffi 세션 활성 (chrome impersonate)")
    except ImportError:
        print("⚠️  curl_cffi 미설치 — 기본 세션 사용 (차단 위험)", file=sys.stderr)
        _yahoo_session = None
    return _yahoo_session


def fetch_yahoo(symbol: str) -> dict:
    session = get_yahoo_session()
    last_err = None
    for attempt in range(1, YAHOO_RETRIES + 1):
        try:
            kwargs = {"session": session} if session else {}
            ticker = yf.Ticker(symbol, **kwargs)
            hist = ticker.history(period="5d", interval="1d")
            if hist.empty:
                last_err = "empty history"
                time.sleep(YAHOO_BACKOFF_SEC)
                continue
            last = hist.iloc[-1]
            return {
                "close": float(last["Close"]),
                "volume": float(last["Volume"]) if "Volume" in last else None,
                "date": hist.index[-1].strftime("%Y-%m-%d"),
                "attempts": attempt,
            }
        except Exception as e:
            last_err = str(e)[:120]
            if attempt < YAHOO_RETRIES:
                time.sleep(YAHOO_BACKOFF_SEC)
    print(f"⚠️  Yahoo FAIL {symbol} after {YAHOO_RETRIES} attempts: {last_err}",
          file=sys.stderr)
    return {"close": None, "volume": None, "date": None, "attempts": YAHOO_RETRIES}


def fetch_fred(series_id: str) -> dict:
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
def build_row():
    now_utc = datetime.now(timezone.utc)
    row = {
        "date": None,
        "as_of": now_utc.isoformat(timespec="seconds"),
        "source_version": VERSION,
    }
    nyse_dates = []
    yahoo_success = 0
    yahoo_fail = 0

    for sym in TICKERS_20:
        d = fetch_yahoo(sym)
        row[f"{sym}_close"] = d["close"]
        row[f"{sym}_volume"] = d["volume"]
        if d["close"] is not None:
            yahoo_success += 1
        else:
            yahoo_fail += 1
        if d["date"]:
            nyse_dates.append(d["date"])

    for name, sym in YAHOO_MACRO.items():
        d = fetch_yahoo(sym)
        row[name] = d["close"]
        if d["close"] is not None:
            yahoo_success += 1
        else:
            yahoo_fail += 1
        if d["date"]:
            nyse_dates.append(d["date"])

    fred_success = 0
    fred_fail = 0
    for name, sid in FRED_SERIES.items():
        d = fetch_fred(sid)
        row[name] = d["value"]
        row[f"{name}_asof"] = d["date"]
        if d["value"] is not None:
            fred_success += 1
        else:
            fred_fail += 1

    if nyse_dates:
        row["date"] = Counter(nyse_dates).most_common(1)[0][0]
    else:
        row["date"] = now_utc.date().isoformat()

    stats = {
        "yahoo_success": yahoo_success,
        "yahoo_fail": yahoo_fail,
        "fred_success": fred_success,
        "fred_fail": fred_fail,
    }
    return row, stats


# ────────────────────────────────────────────────────────
# Upsert + Audit
# ────────────────────────────────────────────────────────
def upsert_row(row: dict) -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    if CSV_PATH.exists():
        df = pd.read_csv(CSV_PATH)
    else:
        df = pd.DataFrame(columns=list(row.keys()))

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

    if df.empty:
        df_new = pd.DataFrame([row])
    else:
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


def update_metadata(result: dict, row: dict, stats: dict) -> None:
    meta = {
        "schema_version": SCHEMA_VERSION,
        "script_version": VERSION,
        "last_run_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_data_date": row["date"],
        "row_count": result["row_count"],
        "last_action": result["action"],
        "columns": list(row.keys()),
        "critical_fields": CRITICAL_FIELDS,
        "last_stats": stats,
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
        print("❌ FRED_API_KEY 미설정.", file=sys.stderr)
        return 1

    row, stats = build_row()
    print(f"\n📊 수집 결과:")
    print(f"   Yahoo: {stats['yahoo_success']}/{stats['yahoo_success']+stats['yahoo_fail']} 성공")
    print(f"   FRED:  {stats['fred_success']}/{stats['fred_success']+stats['fred_fail']} 성공")

    result = upsert_row(row)
    update_metadata(result, row, stats)

    print(f"\n✅ {result['action'].upper()} row for {row['date']}")
    print(f"   Total rows: {result['row_count']}")
    print(f"   CSV: {CSV_PATH}")

    # v1.0.1: WARN만 남기고 commit 진행
    missing = [f for f in CRITICAL_FIELDS if row.get(f) is None]
    if missing:
        print(f"\n⚠️  WARN: Missing critical: {missing}", file=sys.stderr)
        print(f"⚠️  부분 데이터로 commit 진행 (FRED={stats['fred_success']}개 정상)",
              file=sys.stderr)

    # Yahoo+FRED 모두 0건일 때만 fail
    if stats['yahoo_success'] == 0 and stats['fred_success'] == 0:
        print("❌ Yahoo+FRED 모두 0건 — 수집 실패", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
