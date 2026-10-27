# 63일 Rolling Quantile 기반 미국장 방향성 예측 분석 설계

## 1. 분석 목표

본 분석의 1차 목표는 **한국장 마감 시점까지 확인 가능한 미국 선물 ETF의 가격 움직임을 이용하여, 같은 날 미국 정규장(Open→Close)이 상승(Up)·횡보(Flat)·하락(Down) 중 어느 방향으로 움직일 가능성이 높은지 판단하는 것**이다.

현재 사용할 핵심 한국장 proxy는 다음과 같다.

- **304940 KODEX 미국나스닥100선물(H)**
- **219480 KODEX 미국S&P500선물(H)**

미국장 결과 변수는 다음을 사용한다.

- **QQQ**: NASDAQ-100 정규장 방향성 proxy
- **SPY**: S&P 500 정규장 방향성 proxy

분석의 목적은 미국장 수익률을 정밀하게 point forecast하는 것이 아니다.  
초기 단계에서는 다음 질문에 답하는 것이 핵심이다.

> “한국장 전체와 마지막 30분의 선물 ETF 움직임이 상대적으로 강하거나 약할 때, 미국 정규장이 Up / Flat / Down일 확률이 체계적으로 달라지는가?”

즉, 본 분석은 **정확한 수익률 예측보다 방향성 분류와 조건부 확률 구조 발견**을 우선한다.

---

# 2. 분석 철학

## 2.1 복잡한 모델보다 단순한 조건부 빈도에서 시작

초기 알파 연구에서 가장 중요한 것은 모델의 설명력이나 복잡도가 아니라 **신호 자체가 원천 데이터에서 직접 관찰되는가**이다.

예를 들어 다음과 같은 결과가 나온다고 하자.

- 304940의 당일 수익률이 최근 3개월 대비 상위 1/3
- 304940의 15:00~15:30 수익률도 최근 3개월 대비 상위 1/3
- 이 조건에서 QQQ가 상승한 비율이 63%

이 경우에는 별도의 머신러닝이나 복잡한 회귀모형 없이도 다음과 같은 경제적 해석이 가능하다.

> “한국장 동안 나스닥 선물 ETF가 강했고, 장 마감 직전까지 그 강세가 유지되었을 때 미국 정규장에서도 상승이 이어지는 경향이 있었다.”

이런 형태의 알파가 확인되면 이후 feature engineering이나 통계적 보정이 추가되더라도 **기저 원인이 무엇인지 명확하게 추적할 수 있다.**

반대로 처음부터 복잡한 모델을 사용하면 성과가 나와도 다음을 구분하기 어렵다.

- 실제 경제적 관계가 존재하는지
- 표본 내 과적합인지
- 특정 regime을 우연히 학습한 것인지
- 여러 약한 변수의 조합이 우연히 성과를 만든 것인지

따라서 본 연구의 초기 원칙은 다음과 같다.

1. **Feature는 최소한으로 사용**
2. **Feature scaling은 rolling quantile만 사용**
3. **모델은 사용하지 않고 empirical frequency를 직접 계산**
4. **Bayesian smoothing, 회귀, tree model, boosting은 1차 단계에서 배제**
5. **알파가 발견된 이후에만 모델과 feature를 확장**

---

# 3. 왜 63거래일 Rolling Quantile인가

## 3.1 절대 수익률 기준의 문제

예를 들어 304940의 당일 수익률이 +0.6%라고 하자.

이 +0.6%는 시장 환경에 따라 의미가 다를 수 있다.

- 저변동성 regime: 매우 강한 상승
- 고변동성 regime: 평범한 움직임
- 이벤트 직후 regime: 오히려 약한 움직임

따라서 단순히

\[
R_{day} > 0.5\%
\]

와 같은 고정 threshold를 사용할 경우 시장 변동성 수준이 바뀔 때 신호 정의가 흔들릴 수 있다.

---

## 3.2 252일보다 63일을 사용하는 이유

252거래일은 약 1년이다.

장점은 표본이 많다는 것이지만, 단점은 **급격한 regime shift에 느리게 적응한다는 점**이다.

예를 들어 최근 1~2개월 사이에 시장 변동성이 급격히 커졌다면 1년 기준 quantile은 과거 저변동성 구간의 영향을 오래 유지한다.

본 전략은 한국장 마감 시점의 단기적 움직임을 이용하여 당일 미국장을 예측하는 것이므로, 상대적 강약을 정의하는 기준도 너무 장기적일 필요가 없다.

따라서 초기 기준으로 약 3개월에 해당하는 **63거래일 rolling window**를 사용한다.

\[
Q_t
=
Rank\left(
R_t;
R_{t-63},\dots,R_{t-1}
\right)
\]

중요한 점은 **현재 날짜 \(t\)는 quantile 기준을 계산하는 과거 표본에 포함하지 않는 것**이다.

즉, look-ahead bias를 방지하기 위해 반드시 과거 63거래일만 사용한다.

---

# 4. 핵심 Feature

초기 연구에서는 feature를 두 개만 사용한다.

## 4.1 당일 한국장 전체 수익률

\[
R^{day}_t
=
\log\left(
\frac{C_t}{O_t}
\right)
\]

304940을 NASDAQ 계열 분석에, 219480을 S&P500 계열 분석에 사용한다.

이 값은 한국장 동안 미국 선물 시장이 어느 방향으로 움직였는지를 요약한다.

---

## 4.2 마지막 30분 수익률

\[
R^{late}_t
=
\log\left(
\frac{P_{15:30}}{P_{15:00}}
\right)
\]

이 값은 하루 전체 방향성보다 **마감 직전의 새로운 정보와 momentum persistence**를 반영하는 변수로 해석한다.

예를 들어 동일하게 당일 +1% 상승한 경우라도:

### Case A
- 오전부터 상승
- 15:00~15:30에도 상승 지속

### Case B
- 오전에 크게 상승
- 15:00 이후 하락 반전

두 경우는 미국장 개장 직전의 상태가 다르다.

따라서 당일 수익률과 마지막 30분 수익률을 함께 사용한다.

---

# 5. Rolling Quantile State 정의

각 feature는 직전 63거래일을 기준으로 percentile rank를 계산한다.

## 5.1 Day State

\[
Q^{day}_t
=
PercentileRank_{63}
\left(
R^{day}_t
\right)
\]

## 5.2 Late State

\[
Q^{late}_t
=
PercentileRank_{63}
\left(
R^{late}_t
\right)
\]

각 quantile을 세 구간으로 나눈다.

### Low

\[
Q < \frac{1}{3}
\]

### Mid

\[
\frac{1}{3}
\le Q <
\frac{2}{3}
\]

### High

\[
Q \ge \frac{2}{3}
\]

---

# 6. 9-State 구조

Day State와 Late State를 결합하면 총 9개의 상태가 생성된다.

| Day \ Late | Low | Mid | High |
|---|---|---|---|
| **Low** | LL | LM | LH |
| **Mid** | ML | MM | MH |
| **High** | HL | HM | HH |

예를 들어:

### HH
- 당일 수익률: 최근 63일 상위 1/3
- 마지막 30분 수익률: 최근 63일 상위 1/3

해석:

> 한국장 전체에서 강했고 장 마감 직전에도 강세가 유지된 상태

### HL
- 당일은 강했지만
- 마지막 30분은 하위 1/3

해석:

> 장중에는 강했지만 마감 직전 momentum이 반전된 상태

이처럼 각 state는 경제적 해석이 직접 가능하다.

---

# 7. 미국장 Target 정의

미국 정규장 Open→Close 수익률을 계산한다.

NASDAQ 계열:

\[
R^{US}_t
=
\log
\left(
\frac{C^{QQQ}_t}
{O^{QQQ}_t}
\right)
\]

S&P500 계열:

\[
R^{US}_t
=
\log
\left(
\frac{C^{SPY}_t}
{O^{SPY}_t}
\right)
\]

초기 분석에서는 이를 **Up / Flat / Down** 세 구간으로 분류한다.

---

# 8. Up / Flat / Down 정의

초기 연구에서는 threshold를 최적화하지 않는다.

예를 들어 기본값을 다음과 같이 고정한다.

\[
Up:
R^{US}>+0.1\%
\]

\[
Flat:
-0.1\%
\le
R^{US}
\le
+0.1\%
\]

\[
Down:
R^{US}<-0.1\%
\]

Flat을 따로 두는 이유는 0 부근의 매우 작은 움직임까지 방향성 예측 성공/실패로 분류하면 noise가 커지기 때문이다.

초기 기준은 ±0.1%로 고정하고, robustness check에서만 다음 정도를 확인한다.

- 0% 기준: Up / Down
- ±0.1%
- ±0.2%

가장 좋은 threshold를 사후 선택하는 것은 피한다.

---

# 9. 분석 방법

각 거래일에 대해 다음 순서를 따른다.

## Step 1. 과거 63일 기준 quantile 계산

현재 날짜 \(t\)의:

- 당일 수익률
- 마지막 30분 수익률

을 직전 63거래일과 비교한다.

---

## Step 2. Day / Late State 결정

예:

- Day quantile = 0.81 → High
- Late quantile = 0.74 → High

따라서:

\[
State_t=HH
\]

---

## Step 3. 같은 날짜 미국장 결과 부여

예:

\[
QQQ_{O\to C}=+0.52\%
\]

이면:

\[
Target_t=Up
\]

---

## Step 4. 전체 역사에서 State별 결과 집계

각 state마다:

- 관측 횟수
- Up 횟수
- Flat 횟수
- Down 횟수

를 센다.

그리고 단순 raw frequency를 계산한다.

\[
P(Up|s)
=
\frac{N_{Up,s}}{N_s}
\]

\[
P(Flat|s)
=
\frac{N_{Flat,s}}{N_s}
\]

\[
P(Down|s)
=
\frac{N_{Down,s}}{N_s}
\]

Bayesian smoothing이나 shrinkage는 사용하지 않는다.

---

# 10. 핵심 결과표

최소한 다음 표가 생성되어야 한다.

| Day | Late | n | Up | Flat | Down | P(Up) | P(Flat) | P(Down) |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| Low | Low | | | | | | | |
| Low | Mid | | | | | | | |
| Low | High | | | | | | | |
| Mid | Low | | | | | | | |
| Mid | Mid | | | | | | | |
| Mid | High | | | | | | | |
| High | Low | | | | | | | |
| High | Mid | | | | | | | |
| High | High | | | | | | | |

가장 중요한 것은 단순히 HH의 P(Up)가 높은지가 아니다.

**전체 state 구조가 경제적으로 일관적인가**를 본다.

예를 들어 다음과 같은 구조가 이상적이다.

\[
P(Up|LL)
<
P(Up|MM)
<
P(Up|HH)
\]

또는 Day=High 상태에서:

\[
P(Up|HL)
<
P(Up|HM)
<
P(Up|HH)
\]

처럼 마지막 30분 강도가 증가할수록 미국장 상승확률이 증가하면 해석력이 높다.

---

# 11. Unconditional Baseline과 비교

미국장은 원래 상승일 비율이 50%가 아닐 수 있다.

따라서 단순히:

\[
P(Up|HH)=58\%
\]

만 보고 알파라고 판단하면 안 된다.

먼저 전체 표본의 baseline을 계산한다.

\[
P(Up)
=
\frac{N_{Up}}{N}
\]

그리고 각 state의 uplift를 계산한다.

\[
Uplift_s
=
P(Up|s)-P(Up)
\]

예:

- 전체 P(Up) = 52%
- HH P(Up) = 64%

이면:

\[
Uplift_{HH}
=
+12\%p
\]

이다.

Down도 동일하게 확인한다.

---

# 12. Regime Robustness 검증

전체 5년 평균 하나만 보고 판단하지 않는다.

각 state의 성과가 시간에 따라 유지되는지 확인해야 한다.

초기에는 복잡한 regime classifier를 쓰지 않고 단순히 연도 또는 반기별로 나눈다.

예:

| 기간 | HH n | P(Up\|HH) |
|---|---:|---:|
| 2022 | | |
| 2023 | | |
| 2024 | | |
| 2025 | | |
| 2026 | | |

좋은 형태:

- 대부분 기간에서 baseline보다 높음
- 방향성이 일관됨
- 특정 한 해만 전체 효과를 설명하지 않음

나쁜 형태:

- 2022: 75%
- 2023: 42%
- 2024: 70%
- 2025: 39%

처럼 regime에 따라 부호가 뒤집힘

이 경우 단순 quantile signal은 안정적 알파라고 보기 어렵다.

---

# 13. 분석 성공 기준

본 연구에서 “성공”은 반드시 높은 accuracy를 의미하지 않는다.

다음 조건을 종합적으로 본다.

## 13.1 State별 확률 차이

예:

\[
P(Up|HH)
>
P(Up)
\]

그리고

\[
P(Down|LL)
>
P(Down)
\]

---

## 13.2 Monotonicity

Late State가 Low→Mid→High로 갈수록 Up probability가 증가하는지 본다.

예:

\[
P(Up|HL)
<
P(Up|HM)
<
P(Up|HH)
\]

---

## 13.3 Sample size

특정 state의 성과가 n=10~20에서 나온 경우 신뢰하지 않는다.

5년 표본에서 9 state를 사용하면 평균적으로 state당 100개 이상을 기대할 수 있기 때문에 충분한 관측 수 확보가 가능하다.

---

## 13.4 Time stability

연도별 또는 반기별로 방향이 유지되어야 한다.

---

# 14. 결과가 명확하게 성공할 경우

예를 들어 다음 결과가 나온다고 하자.

- HH: P(Up) = 64%
- 전체 P(Up) = 52%
- HL: P(Up) = 43%
- LL: P(Down) = 61%
- 연도별로 HH의 P(Up)가 지속적으로 baseline 상회

이 경우 가장 먼저 해야 할 것은 **모델을 복잡하게 만드는 것이 아니라 원인을 더 세분화하는 것**이다.

## 후보 1. 30분 momentum의 구조 확인

HH signal이 강한 이유가 마지막 30분 momentum이라면 다음을 추가 확인한다.

- 15:00~15:10
- 15:10~15:20
- 15:20~15:30

중 어느 구간이 가장 정보력이 강한가

또는:

\[
R_{15:00\to15:30}
\]

하나보다

- 후반부 continuation
- 초반 상승 후 후반 reversal

이 중요한지 확인한다.

---

## 후보 2. 219480 confirmation

304940 HH 상태에서 219480 역시 High인지 확인한다.

예:

| NQ State | S&P confirmation | P(QQQ Up) |
|---|---|---:|
| HH | High | |
| HH | Mid/Low | |

만약 219480도 강할 때 확률이 더 올라간다면:

> 단순 NASDAQ 단독 움직임이 아니라 미국 전체 equity risk-on 흐름일 때 continuation이 강하다

는 해석이 가능하다.

---

## 후보 3. 미국장 상승폭 threshold 확장

Up/Flat/Down이 성공적으로 예측되면 다음 연구는 자연스럽게:

\[
P(R^{US}>n\%|State)
\]

로 확장한다.

예:

- \(n=0.25\%\)
- \(n=0.50\%\)
- \(n=0.75\%\)
- \(n=1.00\%\)

각 state에 대해:

| State | P(US>0%) | P(US>0.25%) | P(US>0.5%) | P(US>0.75%) | P(US>1%) |
|---|---:|---:|---:|---:|---:|
| HH | | | | | |

를 계산한다.

이 결과가 실제 trading rule의 기반이 된다.

예:

> HH 상태이고 과거 동일 state에서 QQQ O→C > +0.5%였던 비율이 55% 이상

과 같은 규칙으로 발전할 수 있다.

---

# 15. 성공 이후 익일 국장 연구

미국장 방향성 예측이 의미 있게 성공한다면 다음 단계는 **실제 한국장 수익으로 전달되는가**를 확인하는 것이다.

## 15.1 실제 미국장 상승과 익일 국장 갭

\[
Gap_{t+1}
=
\log
\left(
\frac{O^{KR}_{t+1}}
{C^{KR}_t}
\right)
\]

미국장이 Up인 날과 그렇지 않은 날을 비교한다.

예:

| 미국장 결과 | 익일 평균 KR Gap | Gap 상승확률 |
|---|---:|---:|
| Up | | |
| Flat | | |
| Down | | |

---

## 15.2 State → 미국장 → 익일 갭 연결

예:

| KR State | P(US Up) | Avg US Return | Avg Next KR Gap |
|---|---:|---:|---:|
| HH | | | |
| MM | | | |
| LL | | | |

이 분석을 통해 다음 경로를 검증한다.

\[
KR\ Close\ State
\rightarrow
US\ Regular\ Session
\rightarrow
Next\ KR\ Open\ Gap
\]

이 경로가 명확하면 전략의 경제적 인과관계가 훨씬 강해진다.

---

# 16. 결과가 애매할 경우

예:

- HH P(Up) = 57%
- baseline = 53%
- 일부 연도에서는 효과 존재
- 일부 연도에서는 없음
- LL/HH 구조가 완전히 monotonic하지 않음

이 경우 즉시 모델을 복잡하게 만들기보다 **어느 부분에서 정보가 희석되는지 진단**한다.

## 후보 1. Quantile window 비교

63일만 고집하지 않고 robustness로:

- 42일
- 63일
- 84일

정도를 비교한다.

단, 가장 좋은 window를 선택하는 식으로 최적화하지 않는다.

핵심은 **효과의 부호와 구조가 여러 window에서 유지되는가**이다.

---

## 후보 2. 마지막 30분 정의 점검

15:00~15:30이 너무 길거나 짧을 수 있다.

다음 정도만 비교한다.

- 15:00~15:30
- 15:10~15:30
- 15:20~15:30

효과가 마지막 10분에 집중되는지 확인한다.

---

## 후보 3. 219480 confirmation 추가

304940만으로 약한 경우 219480을 단순 confirmation filter로 사용한다.

예:

- 304940 HH
- 219480 Late High

와 같은 조건.

여전히 state 수를 폭발적으로 늘리지 않는다.

---

## 후보 4. Regime dependence 확인

효과가 특정 기간에만 있다면 해당 기간의 공통점을 확인한다.

예:

- high-vol regime
- low-vol regime
- 상승장
- 하락장

이 단계에서도 복잡한 regime classifier보다는 단순 기준을 사용한다.

---

# 17. 결과가 명확하게 실패할 경우

다음과 같은 경우에는 현재 naive quantile 접근을 실패로 판단한다.

- 9 state의 Up/Down 확률 차이가 거의 없음
- HH와 LL 모두 baseline과 유사
- monotonicity 없음
- 기간별 부호가 반복적으로 뒤집힘
- 특정 1개 연도만 전체 효과를 설명

이 경우에는 “feature가 무의미한가”와 “state discretization이 정보를 버렸는가”를 구분해야 한다.

---

## 실패 대응 후보 1. Quantile discretization 완화

3분위로 나누면서 정보가 손실되었을 수 있다.

이 경우 raw quantile을 그대로 사용한다.

예:

\[
Q^{day}\in[0,1]
\]

\[
Q^{late}\in[0,1]
\]

그리고 단순한 heatmap이나 local average로 확인한다.

---

## 실패 대응 후보 2. 회귀분석

Quantile state가 실패했지만 연속적인 관계가 존재할 가능성이 있다.

이 경우 baseline으로:

\[
R^{US}
=
\alpha+
\beta_1R^{day}
+
\beta_2R^{late}
+\epsilon
\]

또는

\[
P(Up)
=
Logit(
\beta_0+
\beta_1Q^{day}
+
\beta_2Q^{late}
)
\]

를 사용할 수 있다.

다만 이 단계에서는 여전히 해석 가능한 선형/로짓 모델까지만 사용한다.

---

## 실패 대응 후보 3. Feature 자체 재검토

당일 수익률과 마지막 30분 수익률이 실제 정보의 핵심이 아닐 수 있다.

이 경우 다음 순서로 한 개씩 추가한다.

1. 30분 trend efficiency
2. 30분 realized volatility
3. 219480 confirmation
4. NASDAQ-S&P 상대강도

여러 feature를 동시에 추가하지 않는다.

---

# 18. 본 분석에서 의도적으로 사용하지 않는 것

초기 연구 단계에서는 다음을 사용하지 않는다.

- Bayesian smoothing
- Random Forest
- XGBoost / LightGBM
- Neural Network
- LSTM
- Hidden Markov Model
- 복잡한 regime classifier
- 대규모 feature selection
- 자동 hyperparameter search

이유는 단순하다.

> “가장 단순한 가격 상태만으로도 방향성이 예측되는가?”

를 먼저 확인해야 하기 때문이다.

이 단계에서 성과가 나오지 않는다면 복잡한 모델에서 나온 성과는 더 강하게 의심해야 한다.

---

# 19. 최종 연구 흐름

```text
304940 / 219480
Daily O→C return
        │
        ├── trailing 63d percentile
        │
15:00~15:30 return
        │
        └── trailing 63d percentile
                 │
                 ▼
           Low / Mid / High
                 │
                 ▼
             3 × 3 State
                 │
        ┌────────┼────────┐
        ▼        ▼        ▼
       Up       Flat     Down
        │
        ▼
State별 raw empirical frequency
        │
        ├── unconditional baseline 비교
        ├── monotonicity 확인
        ├── sample size 확인
        └── 연도/반기별 stability 확인
                 │
        ┌────────┼────────┐
        ▼        ▼        ▼
     성공       애매      실패
        │        │        │
        │        │        └─ 회귀/로짓, feature 재검토
        │        └─ window/late interval/confirmation 점검
        │
        ├─ P(US return > n%) 연구
        ├─ 219480 confirmation
        ├─ late momentum 구조 분해
        └─ US Up → 익일 KR gap 연구
```

---

# 20. 1차 연구에서 반드시 답해야 할 질문

최종적으로 다음 질문에 답할 수 있어야 한다.

1. **당일 한국장 선물 ETF의 상대적 강약이 미국 정규장 방향과 관계가 있는가?**
2. **마지막 30분의 상대적 강약이 추가 정보를 제공하는가?**
3. **Day와 Late가 같은 방향일 때 continuation probability가 높아지는가?**
4. **HH / LL 상태의 Up / Down 확률이 unconditional baseline과 얼마나 차이나는가?**
5. **해당 차이가 연도별로도 같은 방향인가?**
6. **효과가 NASDAQ과 S&P500에서 동시에 관측되는가, 아니면 특정 시장에만 존재하는가?**
7. **이 신호가 충분히 강하다면 미국장 수익률이 +n%를 초과할 확률까지 확장 가능한가?**
8. **미국장 방향성 예측이 익일 한국장 시가 gap으로 실제 전달되는가?**

이 8개 질문에 대한 답이 본 연구의 1차 산출물이 된다.