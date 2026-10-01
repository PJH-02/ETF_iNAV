# 구현 계획 (Plan) — 63일 Rolling Quantile 미국장 방향성 분석

> **근거 문서**: `research.md`(연구 설계) + `interview.md`(구현 명세, 인터뷰 결정 20건 반영) + **`review.md`(핵심 수정 2건 반영)**
> **산출물**: `quantile_state_analysis.py` 단일 스크립트 → `analysis_output/` (report.md, PNG, 표 CSV)
> **본 문서**: 위 문서들을 실행 가능한 단계로 분해한 구현 계획. 작성 시점 2026-09-28, 코드 미작성.

---

## 0. 확정사항 요약 (interview.md + review.md 반영)

### 0.1 interview.md에서 인계 (변경 없음)
- 범위: Full roadmap (core §9–13, robustness §8/§16, extensions §14–15, failure branch §17)
- 데이터: `data/` 기존 CSV 오프라인만 사용 (Toss API 없음, `.env` 불필요)
- 단일 스크립트 `quantile_state_analysis.py`, CLI 플래그 없음
- 언어: 코드/컬럼 영어, 주석·콘솔·보고서 한국어, **플롯 라벨 영어만**
- Percentile: **midpoint tie**, strict warmup (63 유효 과거 관측치)
- Stability: 반기(H1/H2), 9 state 전체
- Gap: KODEX200(069500), Confirmation: Both-High, P(>n%): 0.25/0.50/0.75/1.00%
- Late variants: 15:00–15:30 / 15:10–15:30 / 15:20–15:30, sub-interval 3개 분해
- 1분봉 무결성: endpoint(15:00, 15:30 봉) 존재 필수
- 제외: 0% threshold, smoothing, ML, 81-cell cross, API 재수집, 백테스트

### 0.2 review.md로 변경된 사항 ⚠️
1. **[핵심] Quantile 계산 순서**: rolling percentile/state는 **KR feature 시계열 각각에서 먼저** 계산하고, 그 후 US target과 same-date inner join한다. (기존 계획의 "join 후 rolling"은 폐기 — 이는 63 KRX 거래일이 아니라 63 KR∩US 공통거래일 기준이 되어 state 정의를 바꾸는 버그였음)
2. **Quantile reference 정의**: 직전 63개 관측치 = **해당 KR ETF의 직전 유효 거래일** (US 거래일과의 교집합 아님). 한국장 열리고 미국장 휴장인 날도 정상 과거 관측치로 포함.
3. **Hard Verdict 폐기**: `+1%p / n≥30 / 최근 5반기 중 3 positive → 성공/애매/실패` 규칙 삭제. 대신 **Up-side / Down-side diagnostic summary**를 독립 출력 (Strong/Mixed/Weak 정성 라벨 참고용).
4. **HH Up-side와 LL Down-side는 독립 평가** (하나의 boolean으로 합치지 않음 — 최종 전략의 1차 목적이 상승 방향 signal이므로 대칭일 필요 없음).
5. **반기 stability는 전체 유효 반기를 전부 descriptive하게** 출력 (`HH positive halves = 7/9` 형식 요약). "최근 5개 반기" 규칙 제거.
6. **Monotonicity는 핵심 diagnostic**로 유지하되 hard cutoff로 pass/fail하지 않음.
7. **보고서에서 Core와 Extension을 구분** (review.md §7 추천 구조 채택, §8.2 참조).

---

## 1. Phase 0 — 환경 준비

1. `pip install matplotlib scipy statsmodels` (사용자 승인: interview.md §8).
   - 설치 확인: `python3 -c "import matplotlib, scipy, statsmodels"`.
   - 실패 시 `python3 -m pip install --user` 재시도 → 그래도 실패하면 §11 리스크 대응(numpy 폴백).
2. `analysis_output/`은 스크립트 실행 시 생성 (`mkdir(exist_ok=True)`).

**완료 기준**: 세 라이브러리 import 성공.

---

## 2. Phase 1 — 스크립트 골격 + CONFIG + 데이터 로딩

### 2.1 파일 구조 (단일 파일, 논리 섹션 순서)
```
quantile_state_analysis.py
├─ CONFIG 상수 섹션
├─ 데이터 로딩/정제 유틸
├─ feature/quantile/state 계산 유틸        ← review.md 수정 B: KR 히스토리 기반
├─ 집계 유틸 (state 빈도, uplift, monotonicity, half-year)
├─ diagnostic summary 유틸                ← review.md 수정 D
├─ 분석 모듈 함수들 (core → extensions 순)
├─ 플롯 함수들
├─ report 작성 함수 (Core/Extension 구분)
└─ main() — Phase 2~7 순차 호출
```

### 2.2 CONFIG (모든 파라미터 상수)
```python
DATA_DIR = Path("data")
OUT_DIR = Path("analysis_output")
PARTIAL_DATE = pd.Timestamp("2026-09-28")  # 수집일 부분 행 — 모든 파일에서 drop
WINDOWS = [42, 63, 84]        # 63 = 기본
TERCILE_EDGES = (1/3, 2/3)
FLAT_BANDS = [0.001, 0.002]   # ±0.1% (기본), ±0.2% (robustness) — 0% 제외
LATE_INTERVALS = [(15,0,15,30), (15,10,15,30), (15,20,15,30)]  # (h,m,h,m)
SUB_INTERVALS = [(15,0,15,10), (15,10,15,20), (15,20,15,30)]
PCT_GRID = [0.0025, 0.0050, 0.0075, 0.0100]
PAIRS = {"304940": "QQQ", "219480": "SPY"}
SMALL_N_WARN = 30             # review.md §3.3: verdict 조건 → 소표본 warning flag로 전환
# (삭제됨: VERDICT_MIN_UPLIFT_PP, VERDICT_MIN_POSITIVE_HALVES — hard rule 폐기)
EARLY_CLOSE_DATES = [...]     # NYSE 반일 하드코딩 리스트 (부록 참고용, 분석에서 제외하지 않음)
```

### 2.3 데이터 로딩 및 정제 (`load_daily`, `load_late_features`, `load_1m`)
공통 규칙:
- `date` 컬럼을 `pd.Timestamp`로 파싱해 키로 사용. `ret_oc`는 재계산하지 않고 사용하되 `log(close/open)`과 최대불일치 > 1e-9 행 수를 로그(부록용).
- **PARTIAL_DATE 행 전 파일 drop** (KR daily, late, 1m, QQQ, SPY, 069500).
- 069500은 전체 CSV를 읽되 분석 구간만 슬라이스.
- 각 파일의 (시작, 끝, 행 수)를 로그 → 부록 표로 축적.

### 2.4 1분봉 무결성 (interview.md §4.4 — Require endpoints)
- 재계산에 쓰는 날은 **15:00 봉 존재 + 15:30 봉 존재** 필수. 중간 결손 허용.
- 재계산 `ret_1500_1530` vs `late_features.ret_1500_1530` 교차검증: |diff| > 1e-9 날짜 수 로그 (부록).

---

## 3. Phase 2 — 코어 계산 유틸 ⚠️ review.md 수정 1 적용

### 3.1 Rolling percentile (midpoint tie) — KR 히스토리 기반
```python
def rolling_pct_rank_midpoint(values: np.ndarray, window: int) -> np.ndarray:
    # Q_t = ((#past < R_t) + 0.5*(#past == R_t)) / window,  past = t-window .. t-1
    # t 미포함(look-ahead 방지). 유효 과거 관측치 < window면 NaN.
```
- 구현: for-loop sliding (N≈1,200, W≤84 → 충분히 빠름). 순수 numpy.
- 검증 self-check: [1,1,2,3] w=3, t=3 → past=[1,1,2], R=3 → 3/3=1.0.

### 3.2 ⚠️ 계산 순서 (review.md §2.2–2.3 — 기존 계획과 다름)
**반드시 아래 순서로 구현한다:**
```python
# 1) KR feature 시계열 각각에서 quantile 계산 (US와 join하기 전!)
day = kr_daily.copy()                              # 전체 KR 거래일
day["q_day"] = rolling_pct_rank_midpoint(day["kr_ret"].to_numpy(), window)

late = late_features.copy()                        # 전체 KR late 관측일 (2021-12-20~)
late["q_late"] = rolling_pct_rank_midpoint(late["late_ret"].to_numpy(), window)

# 2) KR 내부에서 Day/Late state 결합 (date 기준 inner)
kr_state = day[["date","kr_ret","q_day"]].merge(
    late[["date","late_ret","q_late"]], on="date", how="inner")
kr_state["state"] = tercile(kr_state["q_day"]) + tercile(kr_state["q_late"])  # LL..HH

# 3) 마지막에 US target과 same-date inner join
pair = kr_state.merge(us_daily[["date","us_ret"]], on="date", how="inner")
```
- **Day와 Late의 warmup은 각각 독립**: Day quantile은 KR daily 전체 히스토리(2021-09-28~)의 직전 63 KRX 거래일 기준, Late quantile은 late 시리즈(2021-12-20~)의 직전 63 KR 관측일 기준.
- 한국장은 열렸으나 미국장 휴장(US 공휴일 등)인 날은 quantile reference에 **정상 포함**됨 — state 정의가 "최근 63 KRX 거래일"을 유지함.
- Day quantile은 join 전이라 warmup이 2021-12 말에 끝나지만, Late 시리즈가 2021-12-20 시작이므로 KR state 결합(warmup 후)은 여전히 대략 2022-04 초반 시작. 유효 시작일은 실행 시 실측해 report에 명기.

### 3.3 Tercile / state
- Low: Q<1/3, Mid: 1/3≤Q<2/3, High: Q≥2/3.
- state 코드: LL/LM/LH/ML/MM/MH/HL/HM/HH.
- window별, feature별(day/late) quantile을 **KR 히스토리에서 한 번** 계산해 variants가 재사용.

### 3.4 Target 분류 (join 이후에 적용)
- Up: us_ret > +band / Flat: |us_ret| ≤ band / Down: us_ret < −band.
- 기본 band=0.001.

### 3.5 집계 유틸
- `state_table(df, band)`: 9 state × {n, Up, Flat, Down, P(Up), P(Flat), P(Down), P(Up)−baseline, P(Down)−baseline}.
- `baseline(df, band)`: 전체 P(Up)/P(Flat)/P(Down).
- `monotonicity(table)`: 
  - Late 기준: 각 Day 행에서 Late L→M→H로 갈 때 P(Up) 증가 행 수 / P(Down) 감소 행 수 (0–3)
  - Day 기준: 각 Late 행에서 Day L→M→H로 갈 때 동일 카운트 (review.md §6: 양방향 모두 핵심 diagnostic)
- `halfyear_stats(df, band)`: 반기 라벨 `f"{y}H{1 if m<=6 else 2}"`, 반기 baseline + 9-state별 n/P(Up)/uplift. **전체 유효 반기 모두** (제한 없음).

### 3.6 Diagnostic summary 유틸 (신규 — review.md §4)
```python
def up_side_diagnostic(state_tbl, mono, half_stats) -> dict:
    # P(Up|HH), baseline P(Up), HH uplift (%p), HH n,
    # Late L→M→H P(Up) 증가 여부, Day L→M→H P(Up) 증가 여부,
    # HH positive halves / total halves

def down_side_diagnostic(state_tbl, mono, half_stats) -> dict:
    # P(Down|LL), baseline P(Down), LL uplift (%p), LL n,
    # Late H→M→L P(Down) 증가 여부, Day H→M→L P(Down) 증가 여부,
    # LL positive halves / total halves
```
- 두 dict는 **독립 출력** (합치지 않음).
- 정성 라벨(Strong/Mixed/Weak)은 참고용으로만 계산 가능하나, 하드 규칙이 아님을 라벨 옆에 명기.
- `SMALL_N_WARN = 30` 미만 state는 report에 warning 표기 (판정 요소 아님).

---

## 4. Phase 3 — Core 분석 (research.md §9–13)

페어(304940→QQQ, 219480→SPY) 각각:

1. **9-state 마스터 표** (기본: 63d, ±0.1%, 15:00–15:30) → CSV + report 표.
2. Baseline 요약 + 유효 표본 기간 (실측 시작일 명기).
3. Monotonicity: Late 기준/Day 기준 각각 0–3 카운트와 상세 (핵심 diagnostic, review.md §6).
4. 샘플 사이즈: state별 n 최소/평균, n<30 state warning 목록.
5. **반기별 stability**: 전체 유효 반기 × 9 state (n, P(Up), uplift vs 당시 반기 baseline) → CSV.
   - 요약 라인: `HH positive halves = X / N`, `LL positive halves = X / N` (N = 유효 반기 수, ~9–10).
   - "최근 5개 반기" 별도 컷은 제거 (참고 정보가 필요하면 descriptive하게만 표기).

**완료 기준**: 페어당 9-state 표, 양방향 monotonicity, 전체 반기 표, Up/Down diagnostic dict가 콘솔/CSV에 생성.

---

## 5. Phase 4 — Robustness (research.md §8, §16 후보1–2)

Variant 매트릭스 (각각 9-state full 표 → report + CSV):
| Variant 그룹 | 조건 | 고정값 |
|---|---|---|
| Threshold | ±0.1% (기본), ±0.2% | 63d, 15:00–15:30 |
| Window | 42d, 63d(기준), 84d | ±0.1%, 15:00–15:30 |
| Late interval | 15:00–15:30(기준), 15:10–15:30, 15:20–15:30 | ±0.1%, 63d |

- Late interval variants는 1m에서 재계산(무결성 규칙 적용). **이 경우에도 quantile은 1m 기반 late 시리즈에서 join 전에 계산** (§3.2 순서 동일 적용).
- 일관성 요약표: `Variant | 유형 | P(Up|HH)−baseline 부호 | P(Down|LL)−baseline 부호 | Late-monotonic 행 수 | 최소 state n | 유효 시작일`.
- 사후 최적 선택 금지 — 부호/구조 일관성만 기술.

**완료 기준**: variant별 표 (기본 제외 6개: threshold 1 + window 2 + late 3... 즉 ±0.2%, 42d, 84d, 15:10–15:30, 15:20–15:30) + 요약표 생성.

---

## 6. Phase 5 — Extensions (Core와 구분되는 확장 진단)

### 6.1 219480 Confirmation (§14 후보2 / §16 후보3 — Both-High 정의)
- 양방향:
  - QQQ 방향: 304940 state=HH를 219480 Day=High & Late=High로 교차 → `n, P(QQQ Up), Avg QQQ ret`.
  - SPY 방향: 219480 state=HH를 304940 Day=High & Late=High로 교차 → `n, P(SPY Up), Avg SPY ret`.
- 날짜 inner join으로 두 페어 정렬. 81-cell은 만들지 않음.

### 6.2 P(R^US > n%) 그리드 (§14 후보3)
- n ∈ {0.25, 0.50, 0.75, 1.00}%.
- 9 state × n의 P(>) + 각 n의 unconditional baseline 행.

### 6.3 Late sub-interval 분해 (§14 후보1)
- 1m에서 `ret_1500_1510, ret_1510_1520, ret_1520_1530` 계산 (endpoint 무결성).
- 각 sub-interval 독립 63d midpoint quantile → tercile (**1m 시리즈 자체 히스토리에서 join 전 계산**).
- 표: sub-interval × {P(Up | DayHH & subHigh), P(Up | DayHH & subLow), n}.
- continuation/reversal 조합 교차표: (초반 부호, 후반 부호) 조합별 P(QQQ Up), n.

### 6.4 익일 국장 Gap (§15 — 069500)
- `Gap_{t+1} = log(O_{t+1}^{069500} / C_t^{069500})` — t+1은 069500의 **다음 영업일** (휴장 연결 그대로, 날짜쌍 간격>3일 건수만 로그).
- 표1: 미국장 Up/Flat/Down × 익일 평균 gap, gap>0 확률.
- 표2: KR state × P(US Up), Avg US Return, Avg Next KR Gap.

**완료 기준**: 6.1–6.4 표 전부 생성.

---

## 7. Phase 6 — Diagnostic Summary + 조건부 진단 ⚠️ review.md 수정 D

### 7.1 Diagnostic summary (hard verdict 대체, 페어별)
- **Up-side**와 **Down-side**를 독립 블록으로 출력:
  - Up-side: `P(Up|HH)`, baseline `P(Up)`, uplift(%p), HH n, Late/Day monotonicity, HH positive halves/total
  - Down-side: `P(Down|LL)`, baseline `P(Down)`, uplift(%p), LL n, Late/Day monotonicity(반대 방향), LL positive halves/total
- 정성 라벨 Strong/Mixed/Weak는 사람이 수치를 보고 판단하도록 **참고 가이드라인**만 report에 제시 (하드 규칙 아님을 명시).
- n<30 소표본 state는 warning 표기.
- verdict 상수(VERDICT_*)는 CONFIG에서 제거되었음을 주석으로 명시 (재현성 이력 추적).

### 7.2 조건부 진단 (§17)
- **실행 조건 변경**: hard verdict가 없어졌으므로, 진단 실행 조건도 review.md 철학에 맞게 조정:
  - 기본: 연속 quantile heatmap은 **항상** 생성 (diagnostic summary의 일부 — discretization 완화 확인용, review.md §7 Alternative Models 위치).
  - OLS/로짓: Up/Down diagnostic이 모두 약할 때(예: 두 방향 uplift 모두 ≈0이고 monotonicity 없음) 실행 — 실행 조건은 상수로 노출하고 기본값은 "약한 신호 시".
- 1. OLS: `us_ret ~ const + kr_ret + late_ret`, HAC(maxlags=5) — statsmodels.
- 2. Logit: `P(Up) ~ const + Q_day + Q_late` (연속 quantile, ±0.1% 기준 표본).

### 7.3 항상 수행
- §20의 8개 질문에 대한 답을 표/수치 인용 형태로 report에 작성.
- 해석 + 다음 단계 제안 문단 (research.md §14–17 흐름 참조).

---

## 8. Phase 7 — 플롯 + Report

### 8.1 플롯 (matplotlib Agg, 영어 라벨, OUT_DIR 저장)
1. `state_heatmap_{qqq,spy}.png` — 3×3 P(Up) heatmap, cell에 n, diverging colormap (baseline 중심).
2. `uplift_bars_{qqq,spy}.png` — 9-state uplift bar (±%p).
3. `halfyear_stability_{qqq,spy}.png` — 전체 유효 반기 uplift 라인 (9 state 전체, HH/LL 강조).
4. `robustness_summary.png` — variant별 HH uplift bar.
5. `threshold_grid_{qqq,spy}.png` — state × n% heatmap.
6. `gap_by_us_outcome.png` — Up/Flat/Down별 익일 gap box plot.
7. `continuous_quantile_heatmap_{qqq,spy}.png` — **항상 생성** (§7.2 변경).
8. `late_subinterval_compare.png` — sub-interval 정보력 bar.
- 한글 폰트 시도하지 않음(깨짐 방지). `plt.close(fig)` 누수 방지.

### 8.2 Report (`analysis_output/report.md`, 한국어) — review.md §7 구조 채택
```text
1. 헤더 (제목, 실행일, 데이터 범위, 유효 표본 기간, 계산 순서 명시: KR 히스토리 기반 quantile 후 US join)
2. Core Result
   - baseline, 9-state 마스터 표 ×2 페어, monotonicity(Late/Day), sample size(+warning)
   - Up-side / Down-side diagnostic summary (독립 블록, hard verdict 없음)
   - 반기 stability 전체 (HH/LL positive halves = X/N 요약 포함)
3. Core Robustness
   - ±0.2% band variant full 표 + 일관성 요약
4. Extended Diagnostics
   - 42/84d window variants, late interval variants (10/20/30m), confirmation, sub-interval 분해
5. Follow-up Economic Analysis
   - P(R>n%) 그리드, KODEX200 next-open gap
6. Alternative Models
   - 연속 quantile heatmap (항상), OLS/로짓 (약한 신호 시)
7. 데이터 품질 노트 (부록): partial drop, 페어링 제외, 1m 결손, 반일 목록, 미수정 가격, ret_oc 검증
8. §20 8개 질문 답변
9. 해석 및 다음 단계
```
- 표는 마크다운 테이블로 직접 렌더링. 수치는 소수 1–3자리 고정.
- **Core(2)와 Extension(4–5)의 해석 경계를 명시**: 확정 판단은 Core+Core Robustness 근거로만, Extension은 보조 단서로만 기술.

### 8.3 콘솔 로그
- 단계 진행 `[PHASE n] ...`, 각 표 요약(상위 몇 행) 출력. 로그는 한국어.

---

## 9. 검증 체크리스트 (구현 후 즉시 수행)

1. `python3 quantile_state_analysis.py` 단독 실행, 종료코드 0.
2. rolling_pct_rank self-check 통과.
3. **[신규 — review.md 핵심]** US-only 휴장일(KR은 영업, US는 휴장 — 예: US 공휴일)이 quantile reference에 포함되는지 확인: `q_day` 계산 시점의 KR 히스토리 길이가 pair join 후 히스토리 길이보다 크거나 같아야 하고, 조기 US 휴장 날짜가 state 계산에 반영되는지 샘플 검증 (예: 특정 US 공휴일 전후 날짜의 Q_day가 KR 63거래일 기준으로 계산됐는지 수동 확인).
4. 페어 표본 수 일치: QQQ 페어 ≈ SPY 페어 (차이는 페어링 제외 건수로 설명 가능해야 함).
5. 9-state 합계 n == 유효 표본 수 (NaN state 없음).
6. P(Up)+P(Flat)+P(Down) = 1 (각 state, 반올림 오차 내).
7. Baseline 재계산 일치 (state 표 합 = 전체).
8. Windows 42/84 variants의 유효 시작일이 63d보다 각각 빠른가/느린가 확인.
9. report.md의 반기 라벨이 실제 유효 반기(2022H1~2026H1/H2)와 일치.
10. PNG 생성 확인 (continuous heatmap 포함, OLS/로짓 조건부).
11. **[신규]** Diagnostic summary 출력에 VERDICT 계열 하드 규칙이 등장하지 않는지 확인 (grep).

---

## 10. 마일스톤 순서 (실행 순서)

| 순서 | 작업 | 의존 |
|---|---|---|
| M1 | 의존성 설치 (Phase 0) | — |
| M2 | 스크립트 골격 + CONFIG + 로딩/정제 (Phase 1) | M1 |
| M3 | **KR 히스토리 기반 quantile/state 파이프라인 (Phase 2, review.md 순서)** | M2 |
| M4 | core 분석 + 반기 stability + diagnostic summary (Phase 3) | M3 |
| M5 | robustness variants (Phase 4) | M4 |
| M6 | extensions 4종 (Phase 5) | M4 (1m 로딩은 M2) |
| M7 | 조건부 진단 OLS/로짓 (Phase 6) | M4, M5 |
| M8 | 플롯 + report (Core/Extension 구분) (Phase 7) | M4–M7 |
| M9 | 검증 체크리스트 + 수정 | M8 |

각 마일스톤 완료 시 스크립트를 실행해 중간 결과가 정상 생성되는지 확인한 뒤 다음 단계로 진행.

---

## 11. 리스크 / 대응

| 리스크 | 대응 |
|---|---|
| statsmodels 설치 실패/버전 충돌 | `pip install --user` 재시도 → 실패 시 OLS는 numpy 수동(HAC 제외, 주석 명시), Logit은 Newton 직접 구현 |
| Day와 Late warmup 시작이 달라 생기는 혼동 | 두 시리즈의 quantile 유효 시작일을 각각 로그/report에 명기 (Day: 2021-12 말~, Late: 2022-04 초반~) |
| late/1m 재계산과 late_features 불일치 | 교차검증 로그로 불일치 건수 노출, 1e-9 허용 오차 |
| 069500 파일명 하드코딩 실수 | CONFIG에 경로 상수화 |
| 플롯 폰트 경고 다수 | warnings 필터로 침묵 처리, 라벨은 영어만 사용 |
| matplotlib 설치 후에도 headless 오류 | `matplotlib.use("Agg")`를 import 최상단에서 강제 |
| Core/Extension 결과가 섞여 과대해석 | report 구조를 review.md §7 순서로 강제하고 해석 경계 문장 삽입 |
