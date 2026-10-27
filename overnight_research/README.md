# 오버나잇 연구

## 코드와 입력

| 코드 | 역할 | 주요 입력 / 출력 |
|---|---|---|
| `code/bt.py` | 국내 상장 미국 ETF 강도로 KODEX200 종가→익일 시가 백테스트 | `data/*daily.csv`, 미국 ETF 14:30~15:30 분봉 / `results/analysis_output_closebet` |
| `code/test.py` | 외국인 수급·QQQ/SPY PREUS 오버나잇 전략 연구 | `results/analysis_output_us_oc/research_dataset.csv`, 외국인 자료 / `results/analysis_output_foreign_preus` |
| `code/f1_f2_f3_minute_eda_aligned_causal.py` | FOREIGN_POS__BOTH_POS 종가→익일 시가 전략 비교 | 같은 연구 패널·외국인 자료 / `results/analysis_output_foreign_preus_pnl` |
| `code/quantile_state_analysis.py` | 미국 ETF 강도·미국장 방향·KODEX 익일 갭 분석 | `data` 일봉·장후반 분봉·late_features / `results/analysis_output` |
| `code/collect_preus_sparse_history.py` | QQQ/SPY PREUS 희소 분봉 수집 | `data/1m_data` |
| `code/toss_proxy_research_compact.py` | 기존 ETF·미국시장 수집 연구 코드 | `data/toss_proxy_research`, `data`의 정리된 입력 |
| `code/kodex.py` | KODEX150 일봉 수집 | `data/toss_kodex150_daily` |

`test.py`는 단위 테스트가 아니라 연구 실행 파일입니다. `f1_f2_f3_minute_eda_aligned_causal.py`도 이름과 달리 실제 내용은 오버나잇 PnL 비교 코드입니다. 파일명은 기존 이력 추적을 위해 유지했습니다.

## 데이터

- `data/` 바로 아래: KODEX200·국내 상장 미국 ETF·QQQ/SPY 일봉, 미국 ETF 장후반 분봉·파생 특징.
- `data/1m_data`: QQQ/SPY PREUS 희소 분봉·enriched·병합 자료와 Toss 일봉.
- `data/market_1m_data`: 공통 KODEX200 ETF 분봉, KOSPI·QQQ·SPY 분봉, `KRX 외국인 순매수.xlsx`.
- `data/toss_proxy_research`, `data/toss_kodex150_daily`: 기존 수집 파일.
- `results/analysis_output_us_oc/research_dataset.csv`: 기존 연구에서 만들어진 중간 패널이며 외국인/PREUS 전략의 직접 입력입니다. 기존 결과와 함께 보존했습니다.

인트라데이와 공통으로 사용하는 원본은 이 데이터 폴더에만 둡니다. 각 연구의 `research_paths.py`가 같은 파일을 직접 참조합니다.

## 결과와 실행

`results/analysis_output_us_oc` 및 `results/analysis_output_foreign_preus_pnl`의 기존 결과를 이동했습니다. 정리 전 이미 없었던 `analysis_output`·`analysis_output_closebet`의 결과 파일은 재생성하지 않았습니다. `docs`에는 기존 `research.md`, `interview.md`, `plan.md`, `review.md`를 보존했습니다.

아래 명령은 워크스페이스 루트에서 실행하며, 재실행은 해당 결과 폴더를 갱신합니다.

```powershell
py -3.10 overnight_research/code/bt.py
py -3.10 overnight_research/code/test.py
py -3.10 overnight_research/code/f1_f2_f3_minute_eda_aligned_causal.py
py -3.10 overnight_research/code/quantile_state_analysis.py
```

입력 경로와 기존 로더만 검사하려면 `py -3.10 overnight_research/code/verify_overnight_inputs.py`를 실행합니다. 결과는 `results/input_verification_after_reorganization.json`에 저장합니다.

정리 과정에서는 입력 로딩과 코드 경로만 확인했고 전략·비용 가정은 변경하지 않았습니다. `bt.py`의 기존 명세에 있는 종가 정보/동일 종가 체결 가정도 그대로입니다. `.env`는 루트 파일을 읽습니다.
