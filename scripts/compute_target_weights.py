#!/usr/bin/env python3
"""GHA 자동 target_weights 산출 (Legio L1 기반 beta).

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

    # 전종목 L1 산출
    scores = {}
    for t in UNIVERSE:
        if t not in df.columns:
            print(f"  ⚠️ {t} 컬럼 없음 — 스킵")
            continue
        h = df[t].dropna().tolist()
        if len(h) < 22:
            continue
        s = legio_mom_score(h)
        if s is not None:
            scores[t] = s

    print(f"\nL1 산출 {len(scores)}종:")
    for t, s in sorted(scores.items(), key=lambda x: -x[1]):
        print(f"  {t:<6} L1={s:+.4f}")

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

    top7 = [t for t, _ in sorted_scores]

    # based_on_date 추출
    based_on = None
    if 'Date' in df.columns:
        based_on = str(df['Date'].iloc[-1])

    payload = {
        "updated": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        "based_on_date": based_on,
        "by": "GHA auto compute_target_weights.py v1.0 (Legio L1 beta)",
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
