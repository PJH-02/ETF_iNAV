# 고정 TP·SL 전략 v1 백테스트 결과

**판정: v1을 거래가 잦고 매 거래의 수익성이 유사한 주력 전략으로 채택하기에는 근거가 부족하다.** OOS 평균 gross는 A 6.55bp, B 10.40bp로 4bp를 넘었지만 각각 17거래·6거래에 불과하다. 통합 전략은 51일에 23거래, 하루 0.45회, 거래 발생일 비율 29.4%다. 통합 OOS gross의 개별 95% block-bootstrap 구간은 −5.64~24.53bp로 넓어 4bp 기대수익이 확인되지 않았다.

통합 OOS 평균 net은 +1.55bp지만 중앙값은 −20.95bp이고 상위 수익 5일을 제외하면 평균 net은 −19.08bp다. 이번 분포는 소수 큰 수익 거래에 의존하는 형태다. 두 후보 모두 사용자가 선호하는 반복 빈도와 유사한 거래당 수익성을 충족하지 못했다. IS 통합 net −6.46bp와 OOS 통합 net +1.55bp의 차이도 일관된 수익성의 증거로 해석하지 않는다.

왕복 추가 체결비용을 2bp 높인 stress에서 통합 OOS 평균 net은 −0.45bp다. 경계 진입 참조의 통합 OOS net +7.44bp에 비해 한 봉 추가 지연 primary는 +1.55bp로, 체결 시점에도 민감하다. 비용 gate 제거 시 통합 OOS는 44거래로 늘지만 net −8.96bp다. 빈도를 늘리기 위해 같은 데이터에서 gate를 사후 완화하지 않았다.

구현 테스트 23개를 성과 계산 전에 통과했다. 계산 후 30개 사전 등록 실행의 거래 시각·고정 TP/SL·현금·수수료·거래량·명세/코드/입력 SHA256 일치를 확인했다. 결과 검증 과정에서 OOS simulation을 다시 실행하지 않았다. 자세한 기록은 `verification.json`에 있다.

전략과 진단을 먼저 고정하고 가격 기반 TP·SL로 평가했다. 20분 보유 time cut은 사용하지 않았다. 15:15 장 종료 관리 청산은 남아 있다. 수수료 왕복 4bp와 기본 체결비용 왕복 약 2bp를 적용했다.

IS: 첫 60일 보정 이후의 평가 기간. OOS: 2026-07-22~2026-10-06. 기존 잠금 OOS는 이번 v1에서 평가되었으며 앞으로 v1 결과를 보고 변경한 규칙의 미열람 검증 구간으로 재사용할 수 없다.

| 구간 | 전략 | 거래 | 거래/일 | 평균 gross bp | 평균 net bp | gross 95% 구간 | net 95% 구간 | median net bp | net PnL 원 |
|---|---|---:|---:|---:|---:|---|---|---:|---:|
| IS | A_continuation | 27 | 0.19 | -2.54 | -8.54 | [-16.62, 13.41] | [-22.61, 7.41] | -21.00 | -21,751 |
| IS | B_shock_recovery | 2 | 0.01 | 27.59 | 21.58 | [-14.58, 69.76] | [-20.57, 63.72] | 21.58 | 3,561 |
| IS | AB_one_position | 29 | 0.21 | -0.46 | -6.46 | [-14.68, 15.22] | [-20.67, 9.21] | -20.99 | -18,190 |
| OOS | A_continuation | 17 | 0.33 | 6.55 | 0.54 | [-10.90, 26.02] | [-16.89, 20.00] | -25.13 | 19 |
| OOS | B_shock_recovery | 6 | 0.12 | 10.40 | 4.40 | [-16.78, 45.44] | [-22.77, 39.42] | -15.24 | 2,916 |
| OOS | AB_one_position | 23 | 0.45 | 7.55 | 1.55 | [-5.64, 24.53] | [-11.63, 18.51] | -20.95 | 2,935 |

표본과 빈도 판정은 별도다. point gross 4bp 통과는 기대수익 4bp가 통계적으로 확인됐다는 뜻이 아니다. 100거래·20활성일 미만은 추론 표본 부족으로 표시했다. CI는 family 다중검정 보정 없는 개별 구간이며 같은 가격 경로를 사용한 단독/통합 결과를 독립 증거로 합산하지 않는다.

| 구간 | 전략 | 충분한 표본 | gross 4bp point | gross 4bp CI 지지 | net 양수 CI 지지 | 빈도 기준 | 활성일 비율 |
|---|---|---|---|---|---|---|---:|
| IS | A_continuation | False | False | False | False | False | 16.4% |
| IS | B_shock_recovery | False | True | False | False | False | 1.4% |
| IS | AB_one_position | False | False | False | False | False | 17.9% |
| OOS | A_continuation | False | True | False | False | False | 23.5% |
| OOS | B_shock_recovery | False | True | False | False | False | 11.8% |
| OOS | AB_one_position | False | True | False | False | False | 29.4% |

**사전 고정한 진단 결과**

| 구간 | 진단 | 전략 | 거래 | gross bp | net bp | TP/SL 동시접촉 거래 |
|---|---|---|---:|---:|---:|---:|
| IS | boundary_reference | A_continuation | 29 | 1.97 | -4.03 | 0 |
| IS | boundary_reference | B_shock_recovery | 6 | -0.18 | -6.17 | 0 |
| IS | boundary_reference | AB_one_position | 35 | 1.60 | -4.40 | 0 |
| IS | slippage_stress | A_continuation | 27 | -2.54 | -10.53 | 0 |
| IS | slippage_stress | B_shock_recovery | 2 | 27.59 | 19.57 | 0 |
| IS | slippage_stress | AB_one_position | 29 | -0.46 | -8.46 | 0 |
| IS | no_cost_gate_diagnostic | A_continuation | 54 | -0.05 | -6.04 | 0 |
| IS | no_cost_gate_diagnostic | B_shock_recovery | 13 | -2.13 | -8.13 | 0 |
| IS | no_cost_gate_diagnostic | AB_one_position | 66 | -0.53 | -6.53 | 0 |
| IS | tp_first_diagnostic | A_continuation | 27 | -2.54 | -8.54 | 0 |
| IS | tp_first_diagnostic | B_shock_recovery | 2 | 27.59 | 21.58 | 0 |
| IS | tp_first_diagnostic | AB_one_position | 29 | -0.46 | -6.46 | 0 |
| OOS | boundary_reference | A_continuation | 17 | 14.78 | 8.78 | 0 |
| OOS | boundary_reference | B_shock_recovery | 6 | 9.67 | 3.67 | 0 |
| OOS | boundary_reference | AB_one_position | 23 | 13.45 | 7.44 | 0 |
| OOS | slippage_stress | A_continuation | 17 | 6.55 | -1.46 | 0 |
| OOS | slippage_stress | B_shock_recovery | 6 | 10.40 | 2.40 | 0 |
| OOS | slippage_stress | AB_one_position | 23 | 7.55 | -0.45 | 0 |
| OOS | no_cost_gate_diagnostic | A_continuation | 32 | -2.74 | -8.74 | 0 |
| OOS | no_cost_gate_diagnostic | B_shock_recovery | 12 | -3.56 | -9.56 | 0 |
| OOS | no_cost_gate_diagnostic | AB_one_position | 44 | -2.97 | -8.96 | 0 |
| OOS | tp_first_diagnostic | A_continuation | 17 | 6.55 | 0.54 | 0 |
| OOS | tp_first_diagnostic | B_shock_recovery | 6 | 10.40 | 4.40 | 0 |
| OOS | tp_first_diagnostic | AB_one_position | 23 | 7.55 | 1.55 | 0 |

경계 진입은 확정과 동일 시각의 첫 체결을 참조하는 진단이다. 주 결과로 채택하지 않는다. 체결비용 stress는 사전 주문 상한을 바꾸지 않는다. TP 우선은 OHLC 순서 가정의 민감도이며 재진입 경로가 달라지므로 전체 수익의 엄밀한 상한이라고 단정하지 않는다. 비용 gate 제거는 제한된 손익 구조와 빈도를 분리하는 진단이며 전략 개선으로 승격하지 않는다.

**청산·위험과 손익 구조**

| 구간 | 전략 | TP 계열 | SL 계열 | 장 종료 | median 보유 분 | 평균 목표폭 bp | 평균 손절폭 bp | MTM MDD 원 | 정지/0거래량 노출 거래 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| IS | A_continuation | 6 | 21 | 0 | 23.00 | 55.57 | 21.49 | -35,019 | 0 |
| IS | B_shock_recovery | 1 | 1 | 0 | 20.50 | 48.73 | 21.27 | -3,427 | 0 |
| IS | AB_one_position | 7 | 22 | 0 | 23.00 | 55.09 | 21.48 | -35,560 | 0 |
| OOS | A_continuation | 6 | 10 | 1 | 9.00 | 91.90 | 38.31 | -15,185 | 0 |
| OOS | B_shock_recovery | 2 | 4 | 0 | 6.50 | 45.68 | 19.53 | -8,153 | 0 |
| OOS | AB_one_position | 8 | 14 | 1 | 8.00 | 79.84 | 33.41 | -19,542 | 0 |

신호에는 연속 공동 자료만 사용했지만 보유 중 ETF 청산은 별도 전체 ETF tape에서 추적했다. 선물 공백 날짜를 삭제하지 않았다. 0거래량 봉에서 체결을 만들지 않았으며 execution tape 자체의 미관측 장중 공백 노출은 별도 표시했다. 실제 호가·깊이·초 단위 도착·시장정지 여부·부분체결은 1분봉으로 인증할 수 없다.

시장 폭 후보 C는 추가 구성종목/업종 데이터가 없어 미실행이다. 임의 대체 지표로 구현했다고 주장하지 않았다. 결과를 보고 v1 시간이나 TP·SL을 수정하지 않았다.

구체적 신호·가격·비용 공식은 `intraday_bracket_spec_v1.md`와 JSON, 구현은 `intraday_bracket_backtest.py`에 있다. 각 구간/진단/전략 디렉터리에 trades, daily, monthly, entry_hour, rejections를 저장했다. primary의 minute_equity는 장중 평가손익 drawdown을 확인하는 자료다.
