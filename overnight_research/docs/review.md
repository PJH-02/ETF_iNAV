# 수정사항 정리 — 63일 Rolling Quantile 미국장 방향성 분석

## 1. 수정 목적

현재 `research.md`의 핵심 철학과 대부분의 구현 계획은 유지한다.

유지 사항:
- 304940 ↔ QQQ, 219480 ↔ SPY
- Day return + 15:00~15:30 Late return
- 직전 63거래일 rolling percentile
- Low / Mid / High tercile → 9-state
- 미국장 Up / Flat / Down
- state별 raw empirical frequency
- unconditional baseline / uplift / monotonicity / sample size / 반기 stability
- smoothing / Bayesian / ML / boosting 미사용
- Full roadmap 자체는 실행 가능

반드시 수정할 핵심은 두 가지다.

1. **Rolling quantile 계산 순서**
2. **Auto Verdict의 판정 로직**

Full roadmap의 robustness / confirmation / P(R>n%) / gap / OLS·Logit 등을 한 번에 계산하는 것은 허용한다. 다만 Core와 Extension 결과를 구분해서 해석한다.

---

# 2. 핵심 수정 1 — Rolling Quantile 계산 구조

## 2.1 현재 계획의 문제

현재 plan은 대체로 다음 순서다.

```text
KR daily
+ KR late
+ US daily
    ↓
same-date inner join
    ↓
rolling 63d percentile
    ↓
state 생성
```

이 방식은 원래 연구 정의와 다르다.

## 2.2 왜 문제가 되는가

원래 quantile은

\[
Q_t = Rank(R_t; R_{t-63}, \dots, R_{t-1})
\]

이고, 여기서 직전 63일은 **해당 한국 ETF의 직전 63개 KRX 거래일**이어야 한다.

한국장은 열렸지만 미국장이 휴장인 날도:
- 304940 / 219480이 실제 거래되었고
- ETF 가격에 당시 선물 움직임이 반영되었으며
- 이후 상대적 강약 판단에 사용되어야 하는 정상 과거 관측치다.

US target과 먼저 inner join하면 이런 날짜가 빠진다.

결과적으로 기준이

\[
최근\ 63\ KRX\ 거래일
\]

이 아니라

\[
최근\ 63\ KR-US\ 공통거래일
\]

이 된다.

이는 state 정의 자체를 바꾸는 문제다.

## 2.3 올바른 계산 순서

### 304940 → QQQ

```text
304940 전체 KR 거래일
        ↓
Day return
        ↓
직전 63개 KR 거래일 기준 percentile
        ↓
Day State

304940 late 전체 KR 거래일
        ↓
Late return
        ↓
직전 63개 KR late 관측일 기준 percentile
        ↓
Late State

Day State + Late State
        ↓
9-State 완성
        ↓
그 후 QQQ와 same-date inner join
        ↓
US Up / Flat / Down 부여
```

219480 → SPY도 동일하다.

## 2.4 구현 원칙

### Day quantile

`304940_daily.csv`, `219480_daily.csv` 각각의 전체 KR 거래일 시계열 위에서 계산한다.

\[
Q^{day}_t =
rac{
N(R_s<R_t)+0.5N(R_s=R_t)
}{63}
\]

단,

\[
s \in \{t-63,\dots,t-1\}
\]

이며 현재 \(t\)는 포함하지 않는다.

### Late quantile

`late_features.csv` 또는 검증된 1분봉 기반 late return 시계열에서 계산한다.

이 역시 **US 거래일과 join하기 전에** 계산한다.

## 2.5 Warmup 정의

`strict 63d warmup`은 유지한다.

> 현재 날짜 \(t\)의 state 계산에는 해당 KR feature 시계열에서 \(t\) 이전 유효 관측치가 63개 이상 필요하다.

Day와 Late는 각각 독립적으로 63개 과거 관측치가 필요하다.

그 후 Day/Late state가 모두 존재하는 날짜만 합치고, 마지막에 US target과 inner join한다.

## 2.6 코드 구조 수정

기존:

```python
pair = (
    kr_daily
    .merge(late, on="date")
    .merge(us_daily, on="date")
)

pair["q_day"] = rolling_pct(...)
pair["q_late"] = rolling_pct(...)
```

수정:

```python
day = kr_daily.copy()
day["q_day"] = rolling_pct_on_kr_history(day["kr_ret"], window=63)

late = late_features.copy()
late["q_late"] = rolling_pct_on_kr_history(late["late_ret"], window=63)

kr_state = (
    day[["date", "kr_ret", "q_day"]]
    .merge(late[["date", "late_ret", "q_late"]], on="date", how="inner")
)

kr_state["day_state"] = ...
kr_state["late_state"] = ...
kr_state["state"] = ...

pair = kr_state.merge(
    us_daily[["date", "us_ret"]],
    on="date",
    how="inner"
)
```

---

# 3. 핵심 수정 2 — Auto Verdict

## 3.1 현재 문제

현재 명세는 다음 조건을 사용한다.

1. HH Up uplift ≥ +1%p AND LL Down uplift ≥ +1%p
2. HH n ≥ 30 AND LL n ≥ 30
3. 최근 5개 반기 중 HH uplift positive ≥ 3개

그리고:
- 3/3 → 성공
- 2/3 → 애매
- 0~1/3 → 실패

로 판정한다.

이 구조는 원래 연구 목적에 비해 지나치게 임의적이다.

## 3.2 +1%p uplift 기준 문제

예:

\[
P(Up)=52.0\%, \quad P(Up|HH)=53.1\%
\]

이면 +1.1%p라서 조건을 통과하지만, 이를 경제적으로 의미 있는 alpha라고 보기 어렵다.

반대로 +1%p 미만이어도:
- 전체 state 구조가 monotonic하고
- 여러 반기에서 같은 방향이면

더 흥미로운 구조일 수 있다.

따라서 +1%p를 hard pass/fail threshold로 쓰면 안 된다.

## 3.3 n ≥ 30의 역할

n ≥ 30은 최소 표본 확인용 quality flag로는 유용하다.

하지만 5년 / 9-state 구조에서 **성공 여부의 핵심 조건**으로 쓰는 것은 부적절하다.

즉:
- report에 표시
- 작은 state에 warning
- verdict 직접 결정에는 사용하지 않음

이 적절하다.

## 3.4 HH와 LL 동시 성공 조건 문제

최종 전략의 1차 목적은 특히:

> 미국장 상승 가능성이 높은 날을 찾아 KR close 매수 후보로 발전시키는 것

이다.

따라서 Up alpha와 Down alpha는 대칭일 필요가 없다.

예:
- HH에서 Up probability가 강하게 증가
- LL에서는 Down probability 변화 없음

이어도 상승 방향 signal은 유효할 수 있다.

따라서 **HH Up-side와 LL Down-side는 독립적으로 평가**해야 한다.

---

# 4. Auto Verdict 수정안

## 4.1 역할 변경

Auto Verdict는 절대적 성공/실패 판정기가 아니라:

> 사람이 결과를 빠르게 읽기 위한 diagnostic summary

로 바꾼다.

가능하면 `PASS/FAIL`보다:
- Strong
- Mixed
- Weak

정도의 정성적 label을 사용한다.

## 4.2 Up-side / Down-side 독립 평가

### Up-side diagnostic
- `P(Up|HH) - P(Up)`
- HH n
- Late Low→Mid→High에서 P(Up) 증가 여부
- Day Low→Mid→High에서 P(Up) 증가 여부
- HH positive half-years / total

### Down-side diagnostic
- `P(Down|LL) - P(Down)`
- LL n
- Late High→Mid→Low에서 P(Down) 증가 여부
- Day High→Mid→Low에서 P(Down) 증가 여부
- LL positive half-years / total

두 방향을 하나의 boolean verdict로 합치지 않는다.

## 4.3 Hard threshold 제거

다음 값을 수치 그대로 출력한다.

```text
HH P(Up)
Unconditional P(Up)
HH uplift (%p)
HH n

LL P(Down)
Unconditional P(Down)
LL uplift (%p)
LL n

Late monotonicity
Day monotonicity

HH positive half-years / total
LL positive half-years / total
```

이 수치들을 보고 사람이 판단한다.

---

# 5. 반기 Stability 수정

최근 5개 반기 중 3개 positive 규칙은 제거한다.

원래 목적은:

> 특정 한두 regime이 전체 효과를 만드는지, 아니면 여러 기간에서 방향이 유지되는지 확인

하는 것이다.

따라서 전체 유효 반기를 모두 보여준다.

예:

| Half | HH n | P(Up|HH) | Baseline Up | Uplift |
|---|---:|---:|---:|---:|
| 2022H1 | | | | |
| 2022H2 | | | | |
| ... | | | | |
| 2026H1 | | | | |

요약은:

```text
HH positive halves = 7 / 9
LL positive halves = 6 / 9
```

처럼 표시한다.

최근 5개 반기를 별도 참고 지표로 보여주는 것은 가능하지만 hard verdict에는 사용하지 않는다.

---

# 6. Monotonicity의 역할

현재 Auto Verdict에서는 monotonicity가 참고 지표에 가깝지만, 원래 연구 철학에서는 매우 중요하다.

예:

\[
P(Up|HL)
<
P(Up|HM)
<
P(Up|HH)
\]

처럼 Late state가 강해질수록 Up probability가 증가한다면 단일 HH uplift보다 더 구조적인 관계를 의미한다.

따라서 monotonicity는 report의 핵심 diagnostic으로 유지한다.

단, 이 역시 단일 hard cutoff로 pass/fail하지 않는다.

---

# 7. Full Roadmap 처리

Full roadmap 자체는 유지해도 된다.

동일 실행에서 다음을 모두 계산 가능:
- 42 / 63 / 84일 robustness
- ±0.1 / ±0.2 target band
- 15:00~15:30 / 15:10~15:30 / 15:20~15:30
- 219480 confirmation
- P(US return > n%)
- late sub-interval
- KODEX200 gap
- continuous quantile heatmap
- 필요 시 OLS / Logit

단, report에서 반드시 **Core와 Extension을 분리**한다.

추천 구조:

```text
1. Core Result
   - 63d
   - 15:00~15:30
   - ±0.1%
   - 9-state
   - raw frequency

2. Core Robustness
   - ±0.2%

3. Extended Diagnostics
   - 42 / 84d
   - 10 / 20 / 30m
   - confirmation
   - sub-interval

4. Follow-up Economic Analysis
   - P(R>n%)
   - KR next-open gap

5. Alternative Models
   - continuous quantile
   - Logit
   - OLS
```

---

# 8. plan.md 직접 수정 항목

## 수정 A — Pairing

기존:
> 페어별 inner join 후 rolling quantile 계산

수정:
> KR feature 시계열에서 rolling quantile / state를 먼저 생성하고, 그 후 US target과 same-date inner join

## 수정 B — Rolling Percentile

다음 문장을 명시적으로 추가:

> Rolling percentile의 reference observations는 US 거래일과의 교집합이 아니라 해당 KR ETF의 직전 유효 거래일 관측치다.

## 수정 C — Core

기존 state table, baseline, monotonicity, 반기 stability는 유지.

단, hard verdict 계산은 diagnostic summary로 변경.

## 수정 D — Verdict

기존:

```text
+1%p uplift
n >= 30
최근 5개 반기 중 3개 positive
→ 성공/애매/실패
```

삭제.

대신 Up-side / Down-side diagnostic을 독립적으로 출력.

---

# 9. 최종 권장 구조

```text
KR ETF raw history
      │
      ├─ Day return
      │      ↓
      │  rolling 63 KR sessions
      │      ↓
      │   Day tercile
      │
      └─ Late return
             ↓
         rolling 63 KR sessions
             ↓
          Late tercile
             │
             ▼
          9-State
             │
             ▼
      US same-date join
             │
             ▼
       Up / Flat / Down
             │
             ▼
    Raw empirical frequency
             │
      ┌──────┼──────┐
      ▼      ▼      ▼
   Uplift  Mono.  Stability
      │      │      │
      └──────┴──────┘
             │
             ▼
      Diagnostic Summary
   (hard verdict 사용 안 함)
```

---

# 10. 최종 수정 요약

반드시 수정:
1. **Rolling quantile을 US target join 전에 계산**
2. Quantile reference는 **직전 63개의 KR ETF 거래일**
3. Day / Late quantile은 각 KR feature 시계열에서 계산
4. Auto Verdict의 `+1%p / n≥30 / 최근 5반기 중 3개` hard rule 제거
5. HH Up-side와 LL Down-side를 **독립적으로 평가**
6. 반기 stability는 전체 유효 반기를 descriptive하게 사용
7. monotonicity를 핵심 diagnostic으로 유지

유지 가능:
- Full roadmap
- robustness variants
- confirmation
- P(R>n%)
- late sub-interval
- KODEX200 gap
- continuous quantile / Logit / OLS

단, Core 결과와 Extension 결과는 명확히 구분해서 해석한다.
