#!/usr/bin/env python3
"""GHA 자동 target_weights 산출 (Legio L1 기반 beta).

[v1.1 2026-04-23] CSV 스키마 자동 탐지 (기존 v1.0은 'Date'/티커 직매치만 지원):
  - date 컬럼: 'Date' / 'date' / 'DATE' / 'timestamp' 순차 탐지
  - 티커 컬럼: 정확 매치 → 대소문자 무시 → `{TICKER}_close` → `{TICKER}_adjclose` → `{TICKER}_*` (volume 제외)

[2026-04-23 Commander 🅐 승인] daifulee/invictus-data/.github/workflows/compute_target_weights.yml 트리거.

실행 흐름:
  1. data/daily_2026.csv 로드 (invictus-bot이 12:50 UTC에 갱신한 최신본)
  2. 20 유니버스에 대해 Legio v2.11 mom_score (base × vol_penalty × momma_mp) 산출
  3. Top7 선정 + 양수 스코어 비례 배분 + RP 10% 고정
  4. data/target_weights_latest.json 생성

⚠️ 주의: 이 값은 Legio L1만 반영한 beta. Claude 정기 브리핑의 최종값과 다를 수 있음.
       (Scenarios Bayesian / Oracle L2 / Commander Override 미반영)
       Claude 브리핑 실행 시 Commander가 동일 파일을 수동 업로드해 덮어씀.
"""
import sys, json, math
import pandas as pd
from datetime import datetime, timezone
from pathlib import Path

# ── Legio v2.11 SSOT 상수 (Main과 동일) ──
VOL_PENALTY_DENOM = 0.80
VOL_PENALTY_FLOOR = 0.50
MOMMA_ALPHA = 0.30
MOMMA_SLOPE_NORM = 0.011
MOMMA_SLOPE_LB = 5

# ── 20 유니버스 (Main 하드코딩과 동일) ──
UNIVERSE = ['GLD','SLV','XLE','ITA','SMH','COPX','NLR','EWZ','QQQM','PAVE',
            'CIBR','IWM','XLF','XLU','XLV','TLT','VNM','INDA','CQQQ','VEA']

RP_PCT = 0.10  # beta 자동 산출에서는 RP 10% 고정 (Oracle 레짐 기반 동적 산출은 Claude 브리핑 전용)


# ── [v1.1] CSV 스키마 자동 탐지 ──
def find_date_col(df):
    """날짜 컬럼 탐지 — daily_2026.csv 은 'date' (소문자) 사용."""
    for c in ['Date', 'date', 'DATE', 'timestamp', 'Timestamp']:
        if c in df.columns:
            return c
    # fallback: 'date' 부분 문자열 포함
    for c in df.columns:
        if 'date' in c.lower():
            return c
    return None


def find_ticker_col(df, ticker):
    """티커 컬럼 탐지 — 'GLD' / 'GLD_close' / 'GLD_adjclose' 등 모두 지원.

    github_data_loader v1.0.1 주석 기준:
      컬럼명이 '<TICKER>' 또는 '<TICKER>_<suffix>' 형태
    """
    upper = ticker.upper()
    # 1순위: 정확 매치
    if ticker in df.columns:
        return ticker
    # 2순위: 대소문자 무시 정확 매치
    for c in df.columns:
        if c.upper() == upper:
            return c
    # 3순위: {TICKER}_* 후보들
    candidates = [c for c in df.columns if c.upper().startswith(f"{upper}_")]
    if not candidates:
        return None
    # 3a: close (가장 일반적)
    close_cols = [c for c in candidates
                  if 'close' in c.lower() and 'adj' not in c.lower()]
    if close_cols:
        return close_cols[0]
    # 3b: adjclose
    adjclose = [c for c in candidates
                if 'adjclose' in c.lower() or 'adj_close' in c.lower()]
    if adjclose:
        return adjclose[0]
    # 3c: volume 제외한 아무것
    non_vol = [c for c in candidates if 'vol' not in c.lower()]
    return non_vol[0] if non_vol else candidates[0]


def mom(h, days):
    """(h[-1] / h[-days-1]) - 1 을 % 로 반환 (Main Legio mom 함수와 동일)."""
    if len(h) < days + 1:
        return None
    return (h[-1] - h[-days-1]) / h[-days-1] * 100


def legio_mom_score(h):
    """Legio v2.11 L1 = base × vol_penalty × momma_mp (Main decide_target_weights 체인 1~2단계)."""
    if len(h) < 22:
        return None
    r1m = mom(h, 21); r3m = mom(h, 63); r6m = mom(h, 126); r12m = mom(h, 252)
    r1m = r1m if r1m is not None else 0
    r3m = r3m if r3m is not None else 0
    r6m = r6m if r6m is not None else 0
    if r12m is None:
        base = 0.25*(r1m/100) + 0.30*(r3m/100) + 0.45*(r6m/100)  # 가중치 재배분
    else:
        base = 0.25*(r1m/100) + 0.30*(r3m/100) + 0.30*(r6m/100) + 0.15*(r12m/100)
    # vol_penalty: 63일 연환산 변동성
    vp = 1.0
    if len(h) >= 63:
        rets = []
        for j in range(len(h) - 63, len(h)):
            if h[j-1] > 0:
                rets.append((h[j] - h[j-1]) / h[j-1])
        if len(rets) >= 10:
            avg = sum(rets) / len(rets)
            var = sum((x - avg) ** 2 for x in rets) / (len(rets) - 1)
            ann_vol = math.sqrt(var) * math.sqrt(252)
            vp = max(VOL_PENALTY_FLOOR, min(1.0, 1.0 - ann_vol / VOL_PENALTY_DENOM))
    # momma: MA20 5일 기울기 감쇠
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
    csv_path = Path("data/daily_2026.csv")
    out_path = Path("data/target_weights_latest.json")

    if not csv_path.exists():
        print(f"❌ {csv_path} 없음 — invictus-bot cron 선행 실행 필요")
        sys.exit(1)

    df = pd.read_csv(csv_path)
    print(f"✅ {csv_path} 로드: {len(df)}행, 컬럼 {len(df.columns)}개")
    print(f"   컬럼 샘플 (앞 15개): {list(df.columns[:15])}")

    # [v1.1] date 컬럼 탐지
    date_col = find_date_col(df)
    if date_col:
        # 최신 행 index (날짜 정렬 후)
        try:
            df_sorted = df.sort_values(date_col)
            based_on = str(df_sorted[date_col].iloc[-1])[:10]  # YYYY-MM-DD 부분만
        except Exception:
            based_on = str(df[date_col].iloc[-1])[:10]
        print(f"   date 컬럼: '{date_col}' → based_on_date = {based_on}")
    else:
        based_on = None
        print(f"   ⚠️ date 컬럼 미발견 → based_on_date = None")

    # 전종목 L1 산출
    scores = {}
    col_map = {}
    for t in UNIVERSE:
        col = find_ticker_col(df, t)
        if col is None:
            print(f"  ⚠️ {t} 컬럼 미발견 — 스킵")
            continue
        col_map[t] = col
        h = df[col].dropna().tolist()
        if len(h) < 22:
            print(f"  ⚠️ {t} (col={col}) 데이터 부족: {len(h)}개")
            continue
        s = legio_mom_score(h)
        if s is not None:
            scores[t] = s

    print(f"\nL1 산출 {len(scores)}/20종:")
    for t, s in sorted(scores.items(), key=lambda x: -x[1]):
        print(f"  {t:<6} (col={col_map[t]:<20}) L1={s:+.4f}")

    if not scores:
        print("\n❌ 산출된 L1 없음 — UNIVERSE 티커 컬럼 모두 미발견. CSV 스키마 확인 필요.")
        sys.exit(2)

    # Top7 선정
    sorted_scores = sorted(scores.items(), key=lambda x: -x[1])[:7]
    pos = [(t, s) for t, s in sorted_scores if s > 0]
    total = sum(s for _, s in pos)

    # 비중 배분 (양수 스코어 비례)
    attack_pct = 1.0 - RP_PCT
    weights = {}
    if total > 0:
        for t, s in pos:
            weights[t] = round(s / total * attack_pct, 6)
    weights['달러RP(수시형)'] = RP_PCT

    # [v1.0 반올림 보정] 6자리 round 누적 오차를 RP가 흡수 → 합 = 1.0000000 정확
    sum_others = sum(v for k, v in weights.items() if k != '달러RP(수시형)')
    weights['달러RP(수시형)'] = round(1.0 - sum_others, 6)

    top7 = [t for t, _ in sorted_scores]

    payload = {
        "updated": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        "based_on_date": based_on,
        "by": "GHA auto compute_target_weights.py v1.1 (Legio L1 beta)",
        "engine": "Legio v2.11 mom_score (base × vol_penalty × momma_mp)",
        "weights": weights,
        "top7": top7,
        "rp_pct": RP_PCT * 100,
        "override_status": "auto_beta_not_claude_final",
        "disclaimer": (
            "GHA 자동 산출 (Legio L1 only). Scenarios/Oracle L2/Commander Override 미반영. "
            "Claude 정기 브리핑 최종값과 다를 수 있음."
        ),
        "schema_version": "1.0",
    }

    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    print(f"\n✅ 저장: {out_path}")
    print(f"   Top7: {top7}")
    print(f"   비중: {weights}")


if __name__ == '__main__':
    main()
