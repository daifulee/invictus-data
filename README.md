# INVICTUS Data — T1 SSOT

INVICTUS 본체 운영용 일별 데이터 저장소. Triangulation 체계의 **T1 레이어** (자동수집·공식 API 기반).

## 🎯 역할

- **T1 SSOT**: Yahoo Finance + FRED API에서 매일 08:05 KST 자동 수집
- 본체(INVICTUS)는 이 저장소의 `daily_2026.csv`를 **web_fetch**로 조회
- T2 (스크린샷) + T3 (웹서치)와 3중 Triangulation 구성

## 📁 구조

```
data/
 ├─ daily_2026.csv      append-only 누적 (날짜 PK)
 ├─ metadata.json       스키마 + 최종 갱신시각
 └─ audit_log.jsonl     덮어쓰기 이력

.github/workflows/
 └─ daily_data.yml      GitHub Actions (cron: 5 23 * * 0-4)

daily_data.py            수집 스크립트 v1.0.0
```

## 🔗 본체 조회 URL

```
https://raw.githubusercontent.com/daifulee/invictus-data/main/data/daily_2026.csv
https://raw.githubusercontent.com/daifulee/invictus-data/main/data/metadata.json
```

## 📊 스키마

| 컬럼군 | 내용 |
|---|---|
| `date` (PK) | NYSE 거래일 (YYYY-MM-DD) |
| `as_of` | 수집 시각 UTC ISO8601 |
| `source_version` | `daily_data.py` 버전 |
| `<TICKER>_close`, `<TICKER>_volume` | 20티커 OHLCV |
| `VIX`, `MOVE`, `DXY`, `WTI`, `BTC`, `KRW`, `ES_F`, `NQ_F` | Yahoo 파생 매크로 8종 |
| `OAS_HY`, `T5YIE`, `SAHM`, `DFII10`, `T10Y2Y`, `ICSA`, `RRP`, `GS2`, `GS10` | FRED 센서 9종 |
| `<FRED>_asof` | FRED 시리즈별 갱신일 (시리즈마다 상이) |

## 🎯 20티커 유니버스

```
GLD, SMH, EWZ, XLE, SLV, PAVE, COPX, XLU, VEA, QQQM,
IWM, XLF, XLV, INDA, ITA, CIBR, NLR, CQQQ, VNM, TLT
```

## ⚙️ 원칙

- **append-only**: 날짜 PK 기준 중복 시 audit log 보존 후 갱신
- **판정 결과 저장 금지**: regime, 목표비중, RP% 등은 본체 세션에서만
- **REG-007 T1 준수**: 원재료만 저장, Oracle 엔진은 본체에서 live call
- **시간 인식**: `as_of` UTC → 본체가 KST·NYSE 상태 기반 Triangulation 수행

## 🔐 Secrets (GitHub 저장소 Settings → Secrets)

| 이름 | 값 |
|---|---|
| `FRED_API_KEY` | FRED Economic Data API key |

## 🔄 수동 실행

GitHub → Actions → INVICTUS Daily Data → Run workflow

## ⚠️ 장애 시

- Yahoo 일부 실패 → 해당 컬럼만 NaN, 다른 데이터 수집 계속
- FRED 실패 → 해당 시리즈만 NaN
- 3개 이상 CRITICAL 결측 → Actions 실패 (수집 롤백)

## 📝 버전

- `v1.0.0` (2026-04-19): 초기 배포. 20티커 + Yahoo 8 + FRED 9 = 총 57컬럼
