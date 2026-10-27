# 인트라데이 연구

## 코드와 입력

| 코드 | 역할 | 주요 입력 |
|---|---|---|
| `code/intraday_v2_backtest.py` | ETF T2/T3·시장 수요·분봉 대체 전략 | 공통 Toss ETF 분봉, `data/kiwoom_research_data` |
| `code/intraday_bracket_backtest.py` | 동결된 v1 TP/SL 전략 | `data/market_1m_data`의 정렬 패널·선물 분봉 |
| `code/f1_f2_backtest.py` | 기존 F1/F2 IS 백테스트 | 선물/ETF 정렬 패널 |
| `code/prep.py` | ETF·선물 분봉 정렬 패널 생성 | 공통 ETF 분봉, 선물 분봉 |
| `code/prepare_intraday_v2_data.py` | 과거 일별 top3 유니버스·멤버 분봉·수급 입력 준비 | 제공되는 과거 시총·수급 자료, Kiwoom 분봉 |
| `code/audit_intraday_v2_ticks.py` | 틱 커버리지 검사 | Kiwoom 틱 원시 페이지·분봉 |
| `code/collect_kiwoom_research.py` | Kiwoom 틱·분봉·일봉 수집 | 루트 환경 설정, 기존 VibeTrading 인증 코드 |
| `code/collect_multiasset_1m.py` | 기존 KIS 선물 분봉 수집 | 루트 환경 설정 |
| `code/test_intraday_*.py` | 기존 단위 테스트 | 합성 테스트 입력·동결 명세 |
| `code/verify_intraday_*_outputs.py` | 저장된 거래 기록 검증 | 기존 결과와 실행 manifest |

공통 입력은 **`../overnight_research/data/market_1m_data/`**에 있습니다. `KODEX200_069500_1m_toss.csv`, KOSPI·QQQ·SPY 분봉, 외국인 순매수 자료를 인트라데이 데이터 폴더에 복제하지 않았습니다.

`data/market_1m_data`에는 `KOSPI200_FUT_FRONT_1m_kis.csv`, `KOSPI200_FUT_FRONT_rolls.csv`, `KOSPI200_INDEX_1m_kis.csv`, `KOSPI200_FUT_ETF_1m_aligned.csv`가 있습니다. `data/kiwoom_research_data`에는 ETF 일봉·분봉, 수집별 틱 원시 페이지, manifest와 checkpoint가 있습니다. 원시 페이지의 과거 상대 경로는 `research_paths.py`가 새 위치로 해석하므로 수집 기록을 수정하지 않았습니다.

## 결과와 문서

- `results/backtest_f1_f2_minute_is`: 기존 F1/F2 IS 결과.
- `results/backtest_intraday_bracket_v1`: 동결된 v1 IS/OOS 결과.
- `results/backtest_intraday_v2`: v2 ETF 대체 전략 및 비용 시나리오 결과.
- `results/eda_f1_f2_f3_minute_v3_causal`: 기존 분봉 EDA 산출물.
- `docs/intraday_bracket_spec_v1.*`, `docs/intraday_v2_spec.*`: 원본 전략 명세.
- `docs/intraday_v2_development_report_20261007.md`: ETF 검증 보고서.
- `docs/OOS_usage_20261007.md`: 기존 데이터·OOS 사용 이력.

파일명이 `f1_f2_f3_minute_eda_aligned_causal.py`인 코드는 실제 내용이 종가→익일 시가 전략이어서 오버나잇 코드 폴더에 있습니다. 위 EDA 폴더는 기존 산출물을 그대로 보존한 것입니다.

## 실행

워크스페이스 루트에서 실행합니다. 새 실행은 해당 `results`의 산출물을 갱신합니다. 기존에 평가한 기간은 연구 이력이 유지되며 새 OOS가 되지 않습니다.

```powershell
# 저장된 결과 검증: 전략 재실행 없음
py -3.10 intraday_research/code/verify_intraday_bracket_outputs.py
py -3.10 intraday_research/code/verify_intraday_v2_outputs.py

# 기존 코드 재실행
py -3.10 intraday_research/code/f1_f2_backtest.py
py -3.10 intraday_research/code/intraday_bracket_backtest.py --partition is
py -3.10 intraday_research/code/intraday_v2_backtest.py
```

Kiwoom 수집은 `code/collect_kiwoom_research.py --help`에서 옵션을 확인할 수 있습니다. 수집 인증 경로는 워크스페이스 루트 및 기존 형제 프로젝트 `VibeTrading`을 기준으로 유지했습니다. 이번 정리에서는 신규 수집을 수행하지 않았습니다.
