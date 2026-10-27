# 63일 Rolling Quantile 기반 미국장 방향성 예측 분석 — 구현 명세 (Spec)

> **요청**: research.md의 분석 설계 문서를 실행 가능한 코드로 구현한다.
> 본 문서는 5차례 인터뷰(ask_user)를 통해 확인한 결정사항과 데이터 실측 결과를 반영한 구현 명세이다.
> **작성 시점**: 2026-09-28 (데이터 수집일). 코드는 아직 작성되지 않았음.

---

## 1. 목적과 범위 (Scope)

### 1.1 목적
research.md(§1–§20)의 전체 로드맵을 구현한다. 핵심 질문:

> "한국장 전체와 마지막 30분의 선물 ETF 움직임이 상대적으로 강하거나 약할 때, 미국 정규장이 Up/Flat/Down일 확률이 체계적으로 달라지는가?"

### 1.2 이번 이터레이션 범위 — **Full Roadmap**
| 블록 | research.md 근거 | 포함 여부 |
|---|---|---|
| 코어 분석 (state 집계, baseline/uplift, monotonicity, n, 기간별 stability) | §9–13 | ✅ |
| 임계값 robustness (±0.1%, ±0.2%) | §8 | ✅ |
| Quantile window robustness (42/63/84) | §16 후보1 | ✅ |
| Late 구간 정의 robustness (15:00–15:30 / 15:10–15:30 / 15:20–15:30) | §16 후보2 | ✅ |
| 219480 confirmation | §14 후보2, §16 후보3 | ✅ |
| P(R^US > n%) 그리드 | §14 후보3 | ✅ |
| Late momentum 구조 분해 (3개 sub-interval) | §14 후보1 | ✅ |
| 익일 국장 gap 연구 | §15 | ✅ |
| **OLS/로짓 (§17 실패 대응)** | §17 | ✅ 단, **실패/애매 판정 시에만 실행** (인터뷰 결정) |

### 1.3 산출물 형태 (인터뷰 결정)
- **단일 스크립트** (예: `quantile_state_analysis.py`) — 모듈 분리 없음, CLI 플래그 없음
- 출력 디렉터리 `analysis_output/`:
  - `report.md` — 한국어 분석 보고서 (표 + 해석 + 자동 판정)
  - PNG 플롯 (matplotlib)
  - 주요 표 CSV
- 콘솔에 요약 진행 로그 출력

### 1.4 언어 (인터뷰 결정)
- 코드 식별자/컬럼명: 영어 (기존 코드 관례 유지)
- **주석, 콘솔 메시지, 보고서 본문, 표 라벨: 한국어**
- **플롯 내 텍스트: 영어만** — Linux 환경에 한글 폰트 부재 시 glyph 깨짐 방지. 보고서 본문이 한국어이므로 플롯은 state 코드(LL/MM/HH…)와 영어 라벨 사용

---

## 2. 데이터 (실측 확인 완료)

### 2.1 입력 파일 (모두 `data/` 아래, Toss API로 수집됨 — 오프라인 분석, API 재호출 없음)
| 파일 | 내용 | 실측 범위 |
|---|---|---|
| `304940_daily.csv` | KODEX 미국나스닥100선물(H) 일봉 + `ret_oc` | 2021-09-28 ~ 2026-09-28, 1,223행 |
| `219480_daily.csv` | KODEX 미국S&P500선물(H) 일봉 + `ret_oc` | 2021-09-28 ~ 2026-09-28, 1,223행 |
| `304940_1500_1530_1m.csv` | 304940 1분봉 (15:00–15:30만) | 2021-12-20 ~ 2026-09-28, 1,166일 |
| `219480_1500_1530_1m.csv` | 219480 1분봉 (15:00–15:30만) | 2021-12-20 ~ 2026-09-28, 1,166일 |
| `304940_late_features.csv` | `ret_1500_1530`, `rv_1500_1530`, `volume_1500_1530`, `n_1m` | 2021-12-20 ~, 1,166행 |
| `219480_late_features.csv` | 동일 | 2021-12-20 ~, 1,166행 |
| `QQQ_daily.csv` | QQQ 일봉 + `ret_oc` | 2021-09-28 ~ 2026-09-28, 1,255행 |
| `SPY_daily.csv` | SPY 일봉 + `ret_oc` | 2021-09-28 ~ 2026-09-28, 1,255행 |
| `KODEX200_069500_daily_20100101_20260928.csv` | KODEX 200 일봉 (2010-01-04 ~) | gap 분석용 |

컬럼: `timestamp, date, open, high, low, close, volume, symbol` (+daily에는 `ret_oc`).
KR 데이터 타임존 `Asia/Seoul`, US 데이터 타임존 `America/New_York` — **날짜 매칭은 `date` 컬럼(로컬 날짜) 기준**.

### 2.2 데이터 품질 사실 (스크립트가 반드시 처리해야 함)
1. **부분 행(partial row)**: **모든 파일**에 수집일 2026-09-28 행이 존재하며 장중 수집으로 불완전
   (예: QQQ volume 100,062 vs 평시 ~5,000만; KR ETF도 장중 수집 행 존재).
   → **인터뷰 결정: 모든 파일에서 2026-09-28(수집일) 행을 drop한다.** 일반화 규칙: 각 CSV의 최종 날짜가 "오늘(수집일)"이면 제거. 단순화를 위해 하드컷 `2026-09-28` 제거도 허용하되, 스크립트 상단 상수로 명시.
2. **KR 휴장 구간**: 2026-09-24~25(추석) → 마지막 완전 페어링일은 **2026-09-23**.
3. **late feature 시리즈 시작일이 늦음**: daily는 2021-09-28부터, late는 **2021-12-20**부터.
   → 약 57개 KR 영업일은 daily만 존재. 이 날들은 state 분석에서 제외(§2.4 warmup 규칙과 결합).
4. **1분봉 결손 가능성**: `n_1m`은 대부분 31이나 결손일 존재 가능 → §4.4 무결성 규칙 적용.
5. **US 조기폐장(반일) 존재**: 추수감사절 다음날, 7/3, 크리스마스 이브 등.
   → **인터뷰 결정: 그대로 사용하되, 보고서 데이터 품질 부록에 반일 날짜 목록과 함께 명시** (제외하지 않음; 하드코딩된 NYSE early-close 날짜 리스트를 데이터 품질 참고용으로만 출력).
6. US 일봉은 미수정(adjusted=false 수집)이지만 분석 기간 ~5년은 배당 왜곡이 O→C 방향성에 미미하므로 그대로 사용 (보고서 부록에 명시).

### 2.3 페어링 규칙
- 304940 ↔ QQQ, 219480 ↔ SPY를 **같은 `date` 기준 inner join**.
- KR 영업일이지만 US가 휴장인 날(또는 그 반대)은 해당 일 관측에서 제외하며, 제외 건수를 로그/부록에 기록.
- KR은 한국영업일 캘린더를 따르므로 미국 휴장일(예: US 공휴일)에 KR 단독 영업일이 존재 → 이 날 KR feature는 계산하되 target이 없어 분석 표본에서 제외.

### 2.4 Warmup / 결측 처리 (인터뷰 결정: **Strict 63d warmup**)
- Quantile 산출에는 **직전 63거래일 전체**가 필요. 63일 미만이면 state = NaN (분석에서 제외).
- late feature가 없는 날(2021-12-20 이전)도 state 분석에서 제외.
- 결과: 유효 분석 시작은 **대략 2022-04 초반** (2021-12-20 기준 63거래일 후). 보고서에 실제 시작일 명시.
- 같은 이유로 42일/84일 window robustness의 유효 시작일은 각각 다름 — variant 표에 시작일 명기.

---

## 3. 핵심 방법론 정의 (research.md + 인터뷰 확정)

### 3.1 Feature
- **Day return**: `R^day_t = log(C_t / O_t)` — 304940은 QQQ 분석에, 219480은 SPY 분석에 사용 (daily CSV의 `ret_oc` 컬럼과 동일 정의; 재계산하지 않고 컬럼 사용 가능하되 부록에서 동일성 확인).
- **Late return**: `R^late_t = log(P_15:30 / P_15:00)` — `late_features.csv`의 `ret_1500_1530` 사용.
  - §16 후보2 variant: 15:10–15:30, 15:20–15:30은 **1분봉 CSV에서 재계산** (late_features에는 없음).
    - 15:10–15:30: log(15:30 close / 15:10 open) — 15:10 봉의 open을 시작점으로 사용
    - 15:20–15:30: log(15:30 close / 15:20 open)
    - sub-interval 분해(§5.5)도 동일 방식: 15:00–15:10, 15:10–15:20, 15:20–15:30

### 3.2 Rolling Percentile Rank — **tie 처리: midpoint 방식 (인터뷰 결정)**
```
Q_t = (#{t-63 <= s < t : R_s < R_t} + 0.5 * #{t-63 <= s < t : R_s == R_t}) / 63
```
- 현재 시점 t는 과거 표본에 **미포함** (look-ahead 방지, §3.2).
- 이유: 이 ETF들의 수익률은 정확히 0인 날이 빈번. midpoint convention이 tie를 Low/High로 쏠리게 하지 않음.
- 구현: 63일이 짧으므로 for-loop 기반 sliding 계산 허용 (성능 문제 없음). pandas rolling rank의 다른 tie 옵션과 다름을 주석으로 명시.

### 3.3 Tercile 분할
- Low: `Q < 1/3`, Mid: `1/3 <= Q < 2/3`, High: `Q >= 2/3`
- 9-state: Day(Low/Mid/High) × Late(Low/Mid/High) → LL, LM, LH, ML, MM, MH, HL, HM, HH

### 3.4 Target (US 정규장 O→C)
- `R^US_t = log(C_t / O_t)` — QQQ 또는 SPY
- 분류 (기본값, ±0.1%):
  - Up: `R^US > +0.001`
  - Flat: `-0.001 <= R^US <= +0.001`
  - Down: `R^US < -0.001`
- **0% 기준(Up/Down 2분류) variant는 제외** (인터뷰 결정 — §8 robustness는 ±0.1%, ±0.2% 두 가지 thresholds만 실행)

### 3.5 집계 (§9, §11)
- raw empirical frequency만 사용: `P(Up|s) = N_Up,s / N_s` 등. **smoothing/shrinkage 없음** (§2.1 원칙).
- Unconditional baseline: `P(Up), P(Flat), P(Down)` 전체 표본 기준.
- Uplift: `Uplift_s = P(Up|s) - P(Up)` (Down도 동일).

### 3.6 기간별 Stability (인터뷰 결정: **반기(half-year)만**, 연도별 미포함)
- 기간 라벨: `2022H1, 2022H2, ..., 2026H2` (1–6월 = H1, 7–12월 = H2). 날짜 기준 단순 분할.
- **모든 9개 state** × 2개 페어를 반기별로 추적 (인터뷰 결정: 전체 state). 표 구성: 기간 × state별 `n, P(Up), uplift_vs_당시기간baseline`.
- 각 반기의 baseline은 해당 반기 전체 표본 기준으로 계산 (state별 uplift 비교를 위함).

---

## 4. 분석 모듈 상세 (스크립트 구조의 논리적 섹션)

### 4.1 Core (§9–13)
출력 표 (QQQ페어/SPY페어 각각):
1. **9-state 마스터 표**: `Day | Late | n | Up | Flat | Down | P(Up) | P(Flat) | P(Down) | P(Up)-baseline | P(Down)-baseline`
2. Baseline 요약 (전체 P(Up)/P(Flat)/P(Down), 표본 기간)
3. **Monotonicity 점검**: 각 Day 행(Low/Mid/High) 안에서 Late Low→Mid→High로 갈 때 `P(Up)` 단조증가 여부 / `P(Down)` 단조감소 여부 → 3행 중 몇 행 만족하는지 카운트. Day 기준의 대칭 검증도 수행.
4. 샘플 사이즈: state별 n 최소/평균, n < 최소기준 state 목록.

### 4.2 Robustness variants (§8, §16 후보1–2) — **인터뷰 결정: full variant tables**
각 variant마다 완전한 9-state 표(또는 8-state late-interval 표)를 보고서에 포함하고, 마지막에 **일관성 요약표** 제시:

| Variant | 유형 | P(Up|HH)−baseline 부호 | P(Down|LL)−baseline 부호 | Late-monotonic 행 수 | 최소 state n |
|---|---|---|---|---|---|

Variant 목록:
- Thresholds: ±0.1% (기본), ±0.2% → 총 2개 (63d window, 15:00–15:30)
- Windows: 42d, 63d(기준), 84d → 총 3개 (±0.1%, 15:00–15:30)
- Late intervals: 15:00–15:30(기준), 15:10–15:30, 15:20–15:30 → 총 3개 (±0.1%, 63d)
- **사후 최적 variant 선택 금지** — 부호/구조 일관성만 평가 (§8, §16 원칙)

### 4.3 219480 Confirmation (§14 후보2, §16 후보3) — **인터뷰 결정: Both High 정의**
- 조건: 304940 state가 HH일 때, **219480의 Day state = High 이고 Late state = High** 인 날 vs 그 외 날 비교.
- 표:
  | 조건 | n | P(QQQ Up) | Avg QQQ ret |
  |---|---|---|---|
  | 304940 HH & 219480 Both High | | | |
  | 304940 HH & 219480 else | | | |
- SPY 페어 대칭 분석도 동일하게 수행 (219480 state가 HH일 때 3049480 confirmation) — 대칭성은 "NASDAQ 단독 vs 시장 전체 risk-on" 해석(§14)을 위해 SPY 방향에도 확인.
- 81-cell 전체 cross-tab은 하지 않음 (state 폭발 방지, §16 후보3 원칙).

### 4.4 1분봉 무결성 규칙 (인터뷰 결정: **Require endpoints**)
- late return 계산 시 해당 날짜의 1분봉에서 **15:00 봉 존재(시작 open)** 및 **15:30 봉 존재(마감 close)** 가 확인되는 날만 사용. 중간 결손은 허용.
- `late_features.csv`를 사용하는 기본 경로와 달리, 1분봉에서 재계산하는 경로(sub-interval, late-interval variants)는 이 규칙을 적용하고, 재계산 결과를 `ret_1500_1530`과 교차검증(불일치 날짜 수 로그).

### 4.5 Late Momentum 구조 분해 (§14 후보1) — **인터뷰 결정: 3개 sub-interval 모두**
- 1분봉에서 `ret_1500_1510`, `ret_1510_1520`, `ret_1520_1530` 계산 (63d midpoint percentile → tercile).
- 표: 각 sub-interval의 High/Low 상태별 `P(QQQ Up | HH_day & subHigh)`, `P(QQQ Up | HH_day & subLow)` 등 — 어느 구간이 정보력이 강한지 비교.
- continuation/reversal 조합(초반 상승+후반 상승 vs 초반 상승+후반 반전) 교차표도 제공.

### 4.6 P(R^US > n%) 그리드 (§14 후보3) — **인터뷰 결정: md 기본값**
- n ∈ {0.25%, 0.50%, 0.75%, 1.00%}
- 표: state(9개) × n → `P(R^US > n | state)`, 각 n의 unconditional baseline도 표기.

### 4.7 익일 국장 Gap (§15) — **인터뷰 결정: KODEX200(069500) 사용**
- `Gap_{t+1} = log(O^069500_{t+1} / C^069500_t)`
- 매칭 규칙: 분석일 t의 다음 **KODEX200 영업일** open 대비 t의 069500 close. (t가 KR 휴장 전 마지막 영업일이면 자연스럽게 다음 영업일로 연결; 휴장이 길어도 그대로 사용 — 단, gap 계산에 사용된 날짜쌍의 일수를 로그로 남김.)
- 표 1 (§15.1): 미국장 결과(Up/Flat/Down) × `익일 평균 gap`, `gap 상승확률`
- 표 2 (§15.2): KR state(9개) × `P(US Up)`, `Avg US Return`, `Avg Next KR Gap`
- 069500 CSV는 2010년부터이므로 분석 기간 커버 충분. **부분 수집 행(2026-09-28) 제거 대상에 포함.**

### 4.8 OLS/로짓 (§17) — **인터뷰 결정: 실패/애매 판정 시에만 실행**
- 자동 판정이 `성공`이면 이 섹션은 건너뛰고 "해당 없음"으로 표기.
- 판정이 `애매`/`실패`면 실행:
  1. OLS: `R^US = α + β1·R^day + β2·R^late + ε`, HAC(maxlags=5) — statsmodels
  2. Logit: `P(Up) = Logit(β0 + β1·Q^day + β2·Q^late)` — 연속 quantile 사용 (§17 실패 대응 1의 raw quantile 활용)
- 실행 여부와 관계없이 §17의 "discretization 완화" 진단(연속 Q^day × Q^late heatmap → P(Up) local average)은 플롯으로 제공 (애매/실패 진단에 유용).

---

## 5. 자동 판정 (Auto Verdict) — 인터뷰 확정 임계값

**사용자 지정 규칙**: "≥ +1%p uplift, n ≥ 30, 5개 반기 중 3개 이상 positive"

각 페어(QQQ/SPY) 독립 판정. 판정은 보고서 최상단과 요약에 명시적으로 표기하고, **모든 임계값은 스크립트 상단 상수로 노출**:

```python
VERDICT_MIN_UPLIFT_PP = 1.0     # |uplift| 최소 기준 (%p)
VERDICT_MIN_STATE_N = 30        # state 최소 표본
VERDICT_MIN_POSITIVE_HALVES = 3 # 5개 반기 중 최소 positive 반기 수 (하한 기준 대비)
```

판정 로직 (QQQ 예):
1. `HH uplift = P(Up|HH) − P(Up) >= +1.0%p` 그리고 `LL uplift(Down) = P(Down|LL) − P(Down) >= +1.0%p`
2. HH와 LL의 n 모두 `>= 30`
3. HH의 반기별 `P(Up|HH) − 해당 반기 baseline`이 **5개 반기 중 3개 이상에서 양(+)**
4. Late-monotonicity(HL→HM→HH에서 P(Up) 증가 행 수)는 판정에 직접 사용하지 않고 **참고 지표**로만 표기 (§13.2는 성공 기준 중 하나지만 사용자 규칙이 명시하지 않음 → 보고서 해석란에서 논의)

- 1–3 모두 충족 → **성공**, 일부만 충족 → **애매**, 대부분 불충족 → **실패**
  - 구체적 조합: 충족 조건 수 3/3 → 성공, 2/3 → 애매(조건 명시), 0–1/3 → 실패
- 반기가 5개 미만이면(2026H2 등) 표본 있는 반기만 사용하고 그 사실을 명기
- 두 페어 모두 판정 후 종합 의견 한 문단 (§20의 8개 질문에 대한 답을 요약하는 형식)

**판정이 애매/실패면** §4.8의 OLS/로짓 + 연속 heatmap 진단이 자동 실행됨 (§17 흐름).

---

## 6. 플롯 목록 (PNG, 영어 라벨, `analysis_output/`)

1. `state_heatmap_qqq.png` / `state_heatmap_spy.png` — 3×3 heatmap: `P(Up)` (cell에 n 표기). colormap은 diverging (baseline 중심).
2. `uplift_bars_qqq.png` / `uplift_bars_spy.png` — 9 state uplift bar chart (baseline 대비 ±%p).
3. `halfyear_stability_qqq.png` / `halfyear_stability_spy.png` — 반기별 HH/LL uplift line chart (9 state 전체는 표로, 플롯은 HH/LL/HM/HL 정도 강조 가능하되 전체 state 라인 포함).
4. `robustness_summary.png` — variant별 HH uplift 부호 비교 (bar).
5. `threshold_grid_qqq.png` / `threshold_grid_spy.png` — state × n% grid heatmap.
6. `gap_by_us_outcome.png` — 미국장 Up/Flat/Down별 익일 069500 gap 분포 (box/violin).
7. `continuous_quantile_heatmap.png` — 연속 Q^day × Q^late bin heatmap (§17 진단용, 판정이 애매/실패일 때만).
8. `late_subinterval_compare.png` — 3개 sub-interval 정보력 비교 bar.

matplotlib 백엔드: `Agg` (headless). 스타일은 기본값 + 격자. 한글 폰트 시도하지 않음.

---

## 7. 보고서 구조 (`analysis_output/report.md`, 한국어)

1. **헤더**: 분석 제목, 실행일, 데이터 범위, 유효 표본 기간 (warmup 반영)
2. **자동 판정 요약** (QQQ 페어 / SPY 페어, 판정 근거 조건 체크리스트)
3. 데이터 품질 노트: 제거된 partial 행, 페어링 제외 건수, 1분봉 결손 처리 건수, US 반일 목록(참고), 미수정 가격 사용 명시
4. Core: baseline 표, 9-state 마스터 표 ×2 페어, monotonicity 결과, 샘플 사이즈
5. Robustness: variant별 전체 표 + 일관성 요약표
6. Confirmation: 219480 Both-High 분석 ×2 방향
7. P(US > n%) 그리드
8. Late 구조 분해
9. 익일 국장 gap
10. (조건부) OLS/로짓 + 연속 heatmap 진단
11. **§20의 8개 질문에 대한 답** — 각 질문별로 표/수치를 인용해 명시적으로 답변
12. 해석 및 다음 단계 제안 (md §14–17 흐름에 따라)

---

## 8. 기술 요구사항 / 환경

- Python 3.12.3 (확인됨). 기존 설치: pandas, numpy.
- **pip 설치 허용 (인터뷰 결정)**: `matplotlib`, `scipy`, `statsmodels` 설치 필요.
  - statsmodels: OLS HAC, Logit
  - scipy: 일부 카이제곱/이항 참고치가 필요하면 사용 (선택)
- 의존성 최소화: sklearn 미사용.
- 실행 커맨드: `python3 quantile_state_analysis.py` (플래그 없음 — 단일 스크립트, 전체 파이프라인 실행).
- `.env`/Toss API 의존 없음 (완전 오프라인).
- 데이터 경로는 스크립트 기준 상대경로 `data/...` 사용.
- 재현성: 모든 임계값/파라미터는 스크립트 상단 CONFIG 섹션에 상수로 명시 (window, thresholds, tercile 경계, verdict 상수, partial-day 컷오프 날짜).

## 9. 성능/규모 추정
- 분석 표본: 페어당 ~1,100일 중 warmup 제거 후 ~950–1,000일, 9-state 평균 n ≈ 100+ (§13.3 기대치와 일치).
- 반기 구간: 2022H1 ~ 2026H2 = 10개 (실측 데이터로는 2022H1~2026H1+).
- 계산량: sliding percentile은 O(N×W), N≈1,200, W=63 → 매우 빠름. 플롯 8종 포함 전체 실행 수 초 수준.

## 10. 명시적으로 하지 않는 것 (이번 이터레이션)
- Bayesian smoothing, tree/boosting, NN, HMM, regime classifier, hyperparameter search (§18)
- 자동 threshold/window 최적 선택 (§8, §16 원칙 — 일관성만 평가)
- 0% threshold variant (인터뷰 결정으로 제외)
- 81-cell cross confirmation (과도한 state 폭발 방지)
- Toss API 재수집 코드 작성 (기존 `toss_proxy_research_compact.py` 존재; 재수집이 필요하면 별도 실행)
- kodex.py/KODEX200 수집 코드 변경 — 069500 CSV는 읽기만 함
- 실거래/백테스트 실행 로직 (이번 단계는 연구 분석까지만; §14의 trading rule 언급은 다음 단계 과제)

## 11. 인터뷰 결정사항 요약표 (추적용)
| # | 질문 | 결정 |
|---|---|---|
| 1 | 코드 범위 | Full roadmap (§9–17 전부) |
| 2 | 산출물 | 스크립트 + 플롯 + Markdown 보고서 |
| 3 | 데이터 소스 | 기존 CSV 오프라인 사용 |
| 4 | 언어 | 한국어 (플롯 라벨만 영어) |
| 5 | partial day | 모든 파일에서 수집일(2026-09-28) 행 제거 |
| 6 | warmup | strict 63거래일 |
| 7 | percentile tie | midpoint convention |
| 8 | stability granularity | 반기만 |
| 9 | 의존성 | pip 설치 허용 (matplotlib, scipy, statsmodels) |
| 10 | gap 자산 | KODEX200(069500) |
| 11 | confirmation 정의 | 219480 Day High & Late High (Both High) |
| 12 | 1분봉 무결성 | endpoint 존재(15:00, 15:30) 필수 |
| 13 | 자동 판정 | 사용 — +1%p / n≥30 / 5반기 중 3 positive |
| 14 | 코드 구성 | 단일 스크립트 |
| 15 | OLS/로짓 타이밍 | 판정이 애매/실패일 때만 |
| 16 | 0% threshold | 제외 |
| 17 | P(US>n%) grid | 0.25/0.50/0.75/1.00% |
| 18 | US 반일 | 유지 + 부록 명시 |
| 19 | late sub-interval | 3개 모두 (15:00–15:10, 15:10–15:20, 15:20–15:30) |
| 20 | robustness 상세 | variant별 full tables + 일관성 요약 |
