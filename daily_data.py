#!/usr/bin/env python3
"""
INVICTUS Daily Data Collector v1.0.5
=====================================
v1.0.5 PATCH (2026-04-25):
  ARGUS 발견 기반 데이터 보강 (§11.8 매트릭스 + §12 외부 학습)

  YAHOO_MACRO 확장: 10종 → 16종
  추가 6종 (ARGUS Tier 1):
    * NG=F     - Henry Hub 천연가스 (XLF/CIBR/SMH 영향, 매트릭스 -0.36)
    * ^OVX     - Oil Volatility (COPX +0.39 B등급, PAVE +0.35)
    * JPY=X    - USD/JPY (§11.8.10 신흥국 통화 비교)
    * CNY=X    - USD/CNY (§11.8.10 신흥국 통화 비교)
    * ^VIX9D   - 9-Day VIX (단기 변동성)
    * ^SKEW    - CBOE SKEW

  FRED_SERIES 확장: 18종 → 25종
  추가 7종 (ARGUS Tier 1+2):
    * T10YIE       - 10Y BEI 인플레 기대 (T5YIE 보완)
    * T10Y3M       - 10Y-3M 스프레드 (T10Y2Y 보완, 더 강력)
    * DGS5         - 5Y Treasury (GLD A등급 ρ=+0.481)
    * DGS30        - 30Y Treasury (GLD A등급 ρ=+0.443)
    * STLFSI3      - St. Louis 금융 스트레스
    * BAMLC0A0CM   - IG OAS (HY 보완)
    * NAPM         - ISM 제조업 PMI

  ARGUS_EXTERNAL 신규 그룹 (5종, optional):
    * URA, URNM    - 우라늄 ETF (NLR proxy)
    * XOP, VDE     - 에너지 ETF
    * UNG          - 천연가스 ETF

v1.0.4 PATCH (2026-04-24):
  YAHOO_MACRO 확장: 8종 → 10종 (VIX3M, VVIX)

v1.0.3 PATCH (2026-04-24):
  FRED_SERIES 확장: 9종 → 18종

v1.0.2 PATCH (2026-04-19):
  yfinance 라이브러리 완전 제거, Yahoo v8 chart API 직접 호출
"""

import os
import sys
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
from curl_cffi import requests as cc_requests

# ────────────────────────────────────────────────────────
# 상수
# ────────────────────────────────────────────────────────
VERSION = "1.0.5"
SCHEMA_VERSION = "1.1"  # ⭐ v1.0.5: 신규 컬럼 추가로 minor 버전 업
DATA_DIR = Path("data")
CSV_PATH = DATA_DIR / "daily_2026.csv"
META_PATH = DATA_DIR / "metadata.json"
AUDIT_PATH = DATA_DIR / "audit_log.jsonl"

YAHOO_RETRIES = 3
YAHOO_BACKOFF_SEC = 3
YAHOO_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"

TICKERS_20 = [
    "GLD", "SMH", "EWZ", "XLE", "SLV", "PAVE", "COPX", "XLU",
    "VEA", "QQQM", "IWM", "XLF", "XLV", "INDA", "ITA", "CIBR",
    "NLR", "CQQQ", "VNM", "TLT",
]

# Yahoo 매크로 16종 (v1.0.5: ARGUS 6종 추가)
YAHOO_MACRO = {
    "VIX":   "^VIX",
    "MOVE":  "^MOVE",
    "DXY":   "DX-Y.NYB",
    "WTI":   "CL=F",
    "BTC":   "BTC-USD",
    "KRW":   "KRW=X",
    "ES_F":  "ES=F",
    "NQ_F":  "NQ=F",
    # v1.0.4 추가
    "VIX3M": "^VIX3M",
    "VVIX":  "^VVIX",
    # v1.0.5 신규 (ARGUS Tier 1)
    "NG":    "NG=F",       # 천연가스 (XLF/CIBR -0.36)
    "OVX":   "^OVX",       # Oil Volatility (COPX +0.39 B등급)
    "JPY":   "JPY=X",      # USD/JPY (§11.8.10 통화 비교)
    "CNY":   "CNY=X",      # USD/CNY (§11.8.10 통화 비교)
    "VIX9D": "^VIX9D",     # 9-Day VIX
    "SKEW":  "^SKEW",      # CBOE SKEW
}

# FRED 25종 — T1 9 + T2 9 + ARGUS 7
FRED_SERIES = {
    # T1 기존 9종 (v1.0.0)
    "OAS_HY":  "BAMLH0A0HYM2",
    "T5YIE":   "T5YIE",
    "SAHM":    "SAHMCURRENT",
    "DFII10":  "DFII10",
    "T10Y2Y":  "T10Y2Y",
    "ICSA":    "ICSA",
    "RRP":     "RRPONTSYD",
    "GS2":     "GS2",          # 월간 (히스토리 보존)
    "GS10":    "GS10",         # 월간 (히스토리 보존)
    # T2 신규 9종 (v1.0.3, 2026-04-24)
    "WTREGEN": "WTREGEN",      # Treasury General Account (weekly)
    "DGS10":   "DGS10",        # 10Y Treasury (daily)
    "DGS2":    "DGS2",         # 2Y Treasury (daily)
    "DTB3":    "DTB3",         # 13-week T-Bill (daily)
    "EFFR":    "EFFR",         # Effective Fed Funds Rate (daily)
    "SOFR":    "SOFR",         # SOFR (daily)
    "NFCI":    "NFCI",         # Chicago Fed NFCI (weekly)
    "WALCL":   "WALCL",        # Fed Balance Sheet (weekly)
    "UMCSENT": "UMCSENT",      # U.Michigan Consumer Sentiment (monthly)
    # ARGUS 신규 7종 (v1.0.5, 2026-04-25)
    "T10YIE":  "T10YIE",          # 10Y BEI 인플레 기대 (§12.5)
    "T10Y3M":  "T10Y3M",          # 10Y-3M 스프레드 (T10Y2Y 보완)
    "DGS5":    "DGS5",            # 5Y Treasury (GLD A등급 ρ=+0.481)
    "DGS30":   "DGS30",           # 30Y Treasury (GLD A등급 ρ=+0.443)
    "STLFSI3": "STLFSI3",         # St. Louis 금융 스트레스
    "OAS_IG":  "BAMLC0A0CM",      # IG OAS (HY 보완)
    "NAPM":    "NAPM",            # ISM 제조업 PMI
}

# ⭐ v1.0.5 신규: ARGUS 외부 ETF (Optional, NLR/XLE/XLU proxy)
ARGUS_EXTERNAL = {
    "URA":   "URA",     # Global X Uranium (NLR proxy)
    "URNM":  "URNM",    # Sprott Uranium Miners (NLR proxy, 2019+)
    "XOP":   "XOP",     # S&P E&P (XLE 보조)
    "VDE":   "VDE",     # Vanguard Energy (XLE 보조)
    "UNG":   "UNG",     # US Natural Gas Fund
}

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
CRITICAL_FIELDS = ["GLD_close", "SMH_close", "XLE_close", "VIX", "OAS_HY"]

# ────────────────────────────────────────────────────────
# Yahoo v8 chart API 직접 호출
# ────────────────────────────────────────────────────────
_yahoo_session = None

def get_yahoo_session():
    global _yahoo_session
    if _yahoo_session is None:
        _yahoo_session = cc_requests.Session(impersonate="chrome")
        print("✅ curl_cffi 세션 활성 (chrome impersonate)")
    return _yahoo_session


def fetch_yahoo(symbol: str) -> dict:
    """Yahoo v8 chart API 직접 호출. yfinance 라이브러리 미사용."""
    session = get_yahoo_session()
    url = f"{YAHOO_BASE}/{symbol}"
    params = {"interval": "1d", "range": "5d"}

    last_err = None
    for attempt in range(1, YAHOO_RETRIES + 1):
        try:
            r = session.get(url, params=params, timeout=15)
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}"
                time.sleep(YAHOO_BACKOFF_SEC)
                continue
            data = r.json()
            chart = data.get("chart", {})
            result = chart.get("result")
            err = chart.get("error")
            if err:
                last_err = str(err)[:100]
                time.sleep(YAHOO_BACKOFF_SEC)
                continue
            if not result:
                last_err = "empty result"
                time.sleep(YAHOO_BACKOFF_SEC)
                continue
            r0 = result[0]
            ts = r0.get("timestamp", [])
            ind = r0.get("indicators", {}).get("quote", [{}])[0]
            closes = ind.get("close", [])
            volumes = ind.get("volume", [])

            last_close, last_vol, last_ts = None, None, None
            for i in range(len(closes) - 1, -1, -1):
                if closes[i] is not None:
                    last_close = float(closes[i])
                    last_vol = float(volumes[i]) if i < len(volumes) and volumes[i] is not None else None
                    last_ts = ts[i] if i < len(ts) else None
                    break

            if last_close is None:
                last_err = "all null closes"
                time.sleep(YAHOO_BACKOFF_SEC)
                continue

            date_str = (datetime.fromtimestamp(last_ts, tz=timezone.utc).strftime("%Y-%m-%d")
                        if last_ts else None)
            return {
                "close": last_close,
                "volume": last_vol,
                "date": date_str,
                "attempts": attempt,
            }
        except Exception as e:
            last_err = str(e)[:120]
            if attempt < YAHOO_RETRIES:
                time.sleep(YAHOO_BACKOFF_SEC)

    print(f"⚠️ Yahoo FAIL {symbol} after {YAHOO_RETRIES} attempts: {last_err}",
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
        print(f"⚠️ FRED fail {series_id}: {e}", file=sys.stderr)
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

    # 20 ETF
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

    # Yahoo 매크로 16종
    for name, sym in YAHOO_MACRO.items():
        d = fetch_yahoo(sym)
        row[name] = d["close"]
        if d["close"] is not None:
            yahoo_success += 1
        else:
            yahoo_fail += 1
        if d["date"]:
            nyse_dates.append(d["date"])

    # ⭐ v1.0.5: ARGUS 외부 ETF 5종
    for name, sym in ARGUS_EXTERNAL.items():
        d = fetch_yahoo(sym)
        row[f"{name}_close"] = d["close"]
        row[f"{name}_volume"] = d["volume"]
        if d["close"] is not None:
            yahoo_success += 1
        else:
            yahoo_fail += 1
        if d["date"]:
            nyse_dates.append(d["date"])

    # FRED 25종
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
# Upsert + Audit (변경 없음)
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
    total_y = stats['yahoo_success'] + stats['yahoo_fail']
    total_f = stats['fred_success'] + stats['fred_fail']
    print(f"\n📊 수집 결과:")
    print(f"  Yahoo: {stats['yahoo_success']}/{total_y} 성공")
    print(f"    (20 ETF + 16 매크로 + 5 외부 = 41 항목)")
    print(f"  FRED: {stats['fred_success']}/{total_f} 성공 (25 시리즈)")

    result = upsert_row(row)
    update_metadata(result, row, stats)

    print(f"\n✅ {result['action'].upper()} row for {row['date']}")
    print(f"  Total rows: {result['row_count']}")
    print(f"  CSV: {CSV_PATH}")

    missing = [f for f in CRITICAL_FIELDS if row.get(f) is None]
    if missing:
        print(f"\n⚠️ WARN: Missing critical: {missing}", file=sys.stderr)
        print(f"⚠️ 부분 데이터로 commit 진행 (FRED={stats['fred_success']}개 정상)",
              file=sys.stderr)

    if stats['yahoo_success'] == 0 and stats['fred_success'] == 0:
        print("❌ Yahoo+FRED 모두 0건 — 수집 실패", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
