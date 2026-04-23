#!/usr/bin/env python3
"""GHA 자동 target_weights 산출 (Legio L1 기반 beta).

[v1.2 2026-04-23] Yahoo v8 chart API 직접 호출 — daily_2026.csv 의존 제거
  - 원인: daily_2026.csv는 5행 append 로그(04-19 시작) → Legio 252일 이력 불가
  - 해결: Yahoo v8 chart API 직접 호출 (query1.finance.yahoo.com/v8/finance/chart/{ticker})
  - 폴백: curl_cffi chrome impersonate → 실패 시 requests + UA 헤더
  - 메모리 교훈 #14 준수 (yfinance GHA 금지, Yahoo v8 chart + impersonate)

[v1.1] CSV 스키마 자동 탐지 (date 소문자, {TICKER}_close suffix)
[v1.0] 초안 (daily_2026.csv 'Date' 대문자 가정 → 실패)
"""
import sys, json, math, time
from datetime import datetime, timezone, date
from pathlib import Path

VOL_PENALTY_DENOM = 0.80
VOL_PENALTY_FLOOR = 0.50
MOMMA_ALPHA = 0.30
MOMMA_SLOPE_NORM = 0.011
MOMMA_SLOPE_LB = 5

UNIVERSE = ['GLD','SLV','XLE','ITA','SMH','COPX','NLR','EWZ','QQQM','PAVE',
            'CIBR','IWM','XLF','XLU','XLV','TLT','VNM','INDA','CQQQ','VEA']

RP_PCT = 0.10

YAHOO_V8_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"


def _fetch_curl_cffi(ticker, days=400):
    try:
        from curl_cffi import requests as cr
        end = int(time.time())
        start = end - days * 86400
        r = cr.get(
            YAHOO_V8_URL.format(ticker=ticker),
            params={"period1": start, "period2": end, "interval": "1d"},
            impersonate="chrome", timeout=15,
        )
        return (r.json(), "OK") if r.status_code == 200 else (None, f"HTTP {r.status_code}")
    except ImportError:
        return None, "curl_cffi_unavailable"
    except Exception as e:
        return None, f"curl_cffi error: {type(e).__name__}"


def _fetch_requests(ticker, days=400):
    try:
        import requests
        end = int(time.time())
        start = end - days * 86400
        r = requests.get(
            YAHOO_V8_URL.format(ticker=ticker),
            params={"period1": start, "period2": end, "interval": "1d"},
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"},
            timeout=15,
        )
        return (r.json(), "OK") if r.status_code == 200 else (None, f"HTTP {r.status_code}")
    except Exception as e:
        return None, f"requests error: {type(e).__name__}"


def fetch_yahoo_history(ticker, days=400, max_retries=2):
    last_reason = "no_attempt"
    for attempt in range(max_retries):
        data, reason = _fetch_curl_cffi(ticker, days)
        if data is None:
            data, reason = _fetch_requests(ticker, days)
        if data is None:
            last_reason = reason
            time.sleep(0.5 * (attempt + 1))
            continue
        try:
            result = data['chart']['result'][0]
            closes = result['indicators']['quote'][0]['close']
            clean = [c for c in closes if c is not None]
            if len(clean) >= 22:
                return clean, f"OK ({len(clean)}일)"
            last_reason = f"insufficient ({len(clean)}일)"
        except Exception as e:
            last_reason = f"parse error: {e}"
        time.sleep(0.3)
    return None, last_reason


def mom(h, days):
    if len(h) < days + 1:
        return None
    return (h[-1] - h[-days-1]) / h[-days-1] * 100


def legio_mom_score(h):
    if len(h) < 22:
        return None
    r1m = mom(h, 21); r3m = mom(h, 63); r6m = mom(h, 126); r12m = mom(h, 252)
    r1m = r1m if r1m is not None else 0
    r3m = r3m if r3m is not None else 0
    r6m = r6m if r6m is not None else 0
    if r12m is None:
        base = 0.25*(r1m/100) + 0.30*(r3m/100) + 0.45*(r6m/100)
    else:
        base = 0.25*(r1m/100) + 0.30*(r3m/100) + 0.30*(r6m/100) + 0.15*(r12m/100)
    vp = 1.0
    if len(h) >= 63:
        rets = [(h[j]-h[j-1])/h[j-1] for j in range(len(h)-63, len(h)) if h[j-1] > 0]
        if len(rets) >= 10:
            avg = sum(rets) / len(rets)
            var = sum((x - avg) ** 2 for x in rets) / (len(rets) - 1)
            ann_vol = math.sqrt(var) * math.sqrt(252)
            vp = max(VOL_PENALTY_FLOOR, min(1.0, 1.0 - ann_vol / VOL_PENALTY_DENOM))
    mp = 1.0
    if len(h) >= 25:
        ma20_now = sum(h[-20:]) / 20
        ma20_prev = sum(h[-20 - MOMMA_SLOPE_LB:-MOMMA_SLOPE_LB]) / 20
        if ma20_prev > 0:
            slope = (ma20_now - ma20_prev) / ma20_prev
            slope_neg = max(min(slope / MOMMA_SLOPE_NORM, 0.0), -1.0)
            mp = 1.0 + MOMMA_ALPHA * slope_neg
    return round(base * vp * mp, 4)


def main():
    out_path = Path("data/target_weights_latest.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"▶ Yahoo v8 chart API fetch 시작 — {len(UNIVERSE)}종")
    scores = {}
    for t in UNIVERSE:
        h, reason = fetch_yahoo_history(t, days=400)
        if h is None:
            print(f"  ❌ {t:<6} {reason}")
            continue
        s = legio_mom_score(h)
        if s is not None:
            scores[t] = s
            print(f"  ✅ {t:<6} {reason} L1={s:+.4f}")

    print(f"\n▶ L1 산출 {len(scores)}/20종")

    if not scores:
        print("❌ 전체 fetch 실패 — Yahoo API 차단 또는 네트워크 문제")
        sys.exit(2)

    sorted_scores = sorted(scores.items(), key=lambda x: -x[1])[:7]
    pos = [(t, s) for t, s in sorted_scores if s > 0]
    total = sum(s for _, s in pos)

    attack_pct = 1.0 - RP_PCT
    weights = {}
    if total > 0:
        for t, s in pos:
            weights[t] = round(s / total * attack_pct, 6)
    weights['달러RP(수시형)'] = RP_PCT
    sum_others = sum(v for k, v in weights.items() if k != '달러RP(수시형)')
    weights['달러RP(수시형)'] = round(1.0 - sum_others, 6)

    top7 = [t for t, _ in sorted_scores]
    based_on = date.today().isoformat()

    payload = {
        "updated": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        "based_on_date": based_on,
        "by": "GHA auto compute_target_weights.py v1.2 (Legio L1 beta, Yahoo v8)",
        "engine": "Legio v2.11 mom_score (base × vol_penalty × momma_mp)",
        "data_source": "Yahoo v8 chart API (query1.finance.yahoo.com)",
        "coverage": f"{len(scores)}/{len(UNIVERSE)}",
        "weights": weights,
        "top7": top7,
        "rp_pct": RP_PCT * 100,
        "override_status": "auto_beta_not_claude_final",
        "disclaimer": (
            "GHA 자동 산출 (Legio L1 only). Scenarios/Oracle L2/Commander Override 미반영. "
            "Claude 정기 브리핑 최종값과 다를 수 있음."
        ),
        "schema_version": "1.1",
    }

    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    print(f"\n✅ 저장: {out_path}")
    print(f"   Top7: {top7}")
    for k, v in sorted(weights.items(), key=lambda x: -x[1]):
        print(f"   {k:<20} {v*100:.2f}%")


if __name__ == '__main__':
    main()
