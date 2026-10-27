# 국내 상장 미국지수 ETF 분봉 수집 확인 — 2026-10-07

이 문서는 초기 미국지수 ETF 수집 버전의 조회·검증 기록이다. 이후 같은 Python 파일을 국내 주식·ETF 공용으로 변경했으며, 현재 실행에는 `--symbols`를 명시하고 일봉은 `--interval 1d`를 사용한다. 최신 실행 예시와 출력 구조는 [공용 수집 코드 사용법](candle_collector_usage.md)을 따른다. 아래의 기존 데이터와 초기 검증 결과는 보존했다.

2021년 전체 **분봉**을 현재 API에서 새로 받는 경로는 확인되지 않았다. 다만 기존 보관 파일에는 두 ETF 모두 **2021-12-20~2026-09-28** 자료가 있어, 이 파일을 보존하면서 신규 API 자료를 이어 붙일 수 있다. 이 구간의 가격이 있다고 해서 현재 API에서 같은 과거 구간을 다시 받을 수 있다는 뜻은 아니다. 일봉은 별도로 두 ETF의 2021-01-04~2021-01-08 시가·종가 수집에 성공했다.

## 확인한 API와 범위

| 서비스 | 문서·실제 조회 결과 | 2021년 분봉 판단 |
|---|---|---|
| 토스 Open API | `/api/v1/candles`, `interval=1m`, `before`를 통한 과거 조회 및 연속 조회 지원. 기본 두 ETF의 2022-11-23 조회 성공. 2021-01-04, 2021-09-01, 2021-12-21, 2021-12-30 및 일부 2022년 시험 날짜에는 빈 응답 | 시험한 2021년 날짜를 현재 API에서 확보하지 못함. 문서가 2021년 전체 보존을 보장하지 않음 |
| KIS | `FHKST03010230` 공식 예제에 분봉 최대 1년 보관 명시 | 현재 시점에서 2021년 백필 용도로 사용할 수 없음 |
| 키움 REST | `ka10080`, 1분봉, `base_dt` 및 연속 조회. 두 기본 ETF의 2021-12-21 요청은 유효 시각이 없는 빈 행, 최근 날짜는 실제 봉 반환 | 시험한 2021년 날짜를 확보하지 못함. 이번 결과는 REST API의 실제 관측이며 다른 서비스 전체의 보존기간에 대한 보장은 아님 |
| 기존 보관 CSV | `304940_1500_1530_1m.csv`, `219480_1500_1530_1m.csv` | 2021-12-20부터 사용 가능. 2021-01~2021-12-17은 해당 파일에 없음 |

추가로 133690, 360750, 143850의 2021년 시험 날짜를 토스에서 조회했으나 빈 응답이었다. 상품을 바꾸는 것만으로 2021년 전체 수집이 가능하다고 판단할 근거는 찾지 못했다. 2022-11-23은 **조회 성공을 확인한 날짜**이며 모든 종목의 공식 보존 시작일로 단정하지 않는다.

공식 근거:

- [토스 REST 스펙](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json)
- [KIS 분봉 공식 예제](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_time_dailychartprice/inquire_time_dailychartprice.py)
- [키움 ka10080 명세](https://openapi.kiwoom.com/guide/apiGuideContents/07/ka10080)
- [키움 REST 안내](https://openapi.kiwoom.com/guide/apiguide)
- [KODEX 상품 자료](https://www.samsungfund.com/upload/kodex/newsroom/20251020144727705.pdf)
- [KRX ETF 거래·종가 단일가 안내](https://global.krx.co.kr/contents/GLB/06/0605/0605010102/GLB0605010102.jsp)

원시 조회 근거는 `overnight_research/data/us_etf_close_snapshots/availability_probes*.json`과 `toss_checked/raw`, `kiwoom_checked/raw`에 있다. 인증정보를 저장하지 않는다.

## 단일 수집 파일

`overnight_research/code/collect_us_etf_close_snapshots.py`

Python 3.10 이상. 의존성은 `requests`, `pandas`, `python-dotenv`다. 다른 연구용 Python 파일이나 VibeTrading 모듈을 import하지 않는다. 다른 Git 저장소에 이 파일 하나를 옮겨 실행할 수 있다. 인증정보와 기존 과거 CSV는 별도로 제공해야 한다.

기본 종목은 기존 연구의 두 종목이다.

- `304940`: KODEX 미국나스닥100선물(H), QQQ 방향성의 후보 proxy
- `219480`: KODEX 미국S&P500선물(H), SPY 방향성의 후보 proxy

임의의 국내 종목은 `--symbols 133690,360750`처럼 변경할 수 있다. 서로 다른 ETF의 가격 이력을 한 종목의 가격처럼 이어 붙이지 않는다.

작업공간 루트에서 실행하는 예:

```powershell
py -3.10 -m pip install requests pandas python-dotenv
py -3.10 overnight_research/code/collect_us_etf_close_snapshots.py --provider toss --start 2021-01-01 --end 2026-10-06
```

이 명령은 2021년 전체 확보를 보장하지 않는다. 기존 보관 자료가 없는 날은 API를 조회하고, 유효한 가격이 없으면 누락 상태로 남긴다. 기간과 종목은 수익률 최적화와 무관한 수집 설정이다.

이미 확보된 시작일 이후를 결합해 수집하는 예:

```powershell
py -3.10 overnight_research/code/collect_us_etf_close_snapshots.py --provider toss --start 2021-12-20 --end 2026-10-06
```

키움 조회 또는 API 없이 기존 자료만 가공하는 예:

```powershell
py -3.10 overnight_research/code/collect_us_etf_close_snapshots.py --provider kiwoom --start 2021-12-20 --end 2026-10-06 --out overnight_research/data/us_etf_close_snapshots/kiwoom_collected
py -3.10 overnight_research/code/collect_us_etf_close_snapshots.py --provider offline --start 2021-01-01 --end 2026-09-28 --out overnight_research/data/us_etf_close_snapshots/offline_checked
py -3.10 overnight_research/code/collect_us_etf_close_snapshots.py --provider kiwoom --probe-date 2021-12-21 --probe-date 2026-10-02
py -3.10 overnight_research/code/collect_us_etf_close_snapshots.py --self-test
```

`.env` 변수명:

```text
TOSS_CLIENT_ID
TOSS_CLIENT_SECRET
KIWOOM_APP_KEY
KIWOOM_APP_SECRET
KIWOOM_USE_MOCK=false
```

`--env-file`로 위치를 지정할 수 있고, 운영체제 환경변수가 파일보다 우선한다. 토스 토큰은 클라이언트당 하나만 유효하므로 다른 토스 수집 작업과 동시에 새 토큰을 발급하지 않는 편이 좋다. 토큰을 디스크에 저장하지 않는다. 계좌·주문 API는 호출하지 않는다.

다른 저장소에서 실행할 때 `--local-dir`는 기존 자료의 위치를, `--out`은 새 결과 위치를 지정한다. 로컬 입력은 종목별 `{symbol}_1500_1530_1m.csv`, offline 모드에서는 추가로 `{symbol}_daily.csv`다. 원본 입력을 덮어쓰지 않는다. 기존 자료는 provider 선택과 관계없이 먼저 재사용되며, 실제 가격의 출처는 각 행의 `source`, `origin`, manifest의 입력 해시에 기록된다. 따라서 provider를 키움으로 설정해도 가져온 2021년 토스 보관 자료가 신규 키움 수집 자료로 표시되지는 않는다.

거래일은 ETF 일봉으로 확인한다. 작업공간의 장기 KODEX200 일봉이 있으면 **날짜만** 보완하여 ETF 일봉보다 오래된 요청 구간의 누락도 드러낸다. `--calendar`로 `date` 열이 있는 다른 거래일 CSV를 지정할 수도 있다. ETF의 가격을 다른 종목 가격으로 채우지 않는다. 별도의 거래일 자료가 없는 이동 환경에서는 manifest의 실제 거래일 시작과 끝도 확인해야 한다.

## 봉 시각과 체결 가정

2026-10-02의 양 API 원시 봉을 대조했다. 토스 표시 시각을 1분 빼서 키움에 맞추면 양쪽에 존재하는 양수 거래량 OHLCV가 304940은 30/30개, 219480은 49/49개 일치했다. 이 관측에 따라 토스는 종료 시각, 키움은 시작 시각으로 처리한다. 기존 보관 파일의 시각도 같은 토스 관례로 해석한다. 2021년 봉을 현재 키움 API로 재검증한 것은 아니다.

| 확보하고 싶은 정보 | 키움 시작 시각 | 토스 종료 시각 | 봉 전체를 확인할 수 있는 시각 |
|---|---|---|---|
| 15:20 직전에 끝난 1분봉 | 15:19 | 15:20 | 15:20 |
| 1분 더 일찍 확정된 정보 | 15:18 | 15:19 | 15:19 |

KRX의 일반적인 종가 단일가 호가 접수는 15:20~15:30이고 종가 체결은 15:30이다. **15:20의 마지막 가격과 15:30의 공식 종가는 같다는 보장이 없다.** 15:20에 주문을 제출해 종가 단일가에 참여하는 경우와 15:20 가격에 체결하는 경우를 구분해야 한다. 수집 파일은 체결을 시뮬레이션하지 않는다.

## 출력과 연구에 사용할 열

기본 결과 위치는 `overnight_research/data/us_etf_close_snapshots/collected/`다.

- `proxy_panel.csv`: 날짜별 두 ETF 결합 자료. 재백테스트에 넘길 주 파일.
- `304940_snapshots.csv`, `219480_snapshots.csv`: 종목별 가격·시각·상태.
- `snapshots.csv`: 두 종목을 행으로 쌓은 자료.
- `coverage.csv`: 실제 관측, 해당 분의 거래 존재, 누락 일수.
- `manifest.json`: 요청 구간, 실제 거래일 구간, 원본·출력·코드 해시와 시각 해석.
- `raw/{provider}/{symbol}/{date}.json`: API 원시 봉. 다시 실행하면 날짜별 캐시를 재사용한다.

`price_1519`, `price_1520`은 정확한 종료 시각에 대응하는 봉의 제공 가격이다. `usable_*`은 해당 분에 양수 거래량이 있을 때만 참이다. 거래량 0인 봉은 `NO_TRADE_IN_TARGET_MINUTE`, 없는 봉은 `MISSING_EXACT_MINUTE`로 남는다. exact 가격을 임의로 채우지 않는다.

신호용 마지막 체결 가격은 `asof_price_*`에 별도로 기록한다. 같은 날, cutoff 이전에 확정된 양수 거래량 봉 중 마지막 것을 찾으며 표준 60분 이내에서만 확인한다. `asof_last_trade_available_at_*`, `asof_staleness_minutes_*`가 해당 가격의 시각과 오래된 정도를 나타낸다. 이전 날짜나 15:30 단일가 체결 가격을 가져오지 않는다. 로컬 원본은 15:00부터이므로 그 파일만 사용하는 날에는 실제 검색 가능한 과거 범위가 더 짧다. `asof_usable_*`은 이 조건을 만족하는 관측의 존재를 뜻하며 그 가격에 주문을 체결할 수 있다는 뜻은 아니다.

`return_from_krx_open_*`, `return_from_previous_krx_close_*`와 `asof_return_*`은 각각 국내 ETF 당일 시가·직전 KRX 거래일 종가를 기준으로 한 단순 수익률이다. 일봉이 누락된 경우 기준 가격을 채우지 않고 수익률도 비워 둔다. `daily_close`는 cutoff 이후 확정되는 정보이므로 당일 15:20 신호에 사용할 수 없다.

환헤지 선물형 ETF에는 선물 가격 변화 외에도 베이시스와 ETF 괴리가 반영된다. 국내 ETF의 전일 국내 종가 대비 수익률은 원래 QQQ/SPY의 직전 미국 정규장 종가 대비 PREUS 수익률과 기준 시각이 다르다. **FOREIGN_POS__BOTH_POS의 동일 전략·동일 성과를 기간만 늘려 검증한 것으로 처리하면 안 된다.** 국내 ETF 기반 proxy 전략으로 정의를 고정하고 겹치는 기간에서 먼저 비교해야 한다.

## 수행한 검증

1. 14개 자체 검증 통과: 봉 종료 시각, 단일가 미래 정보 배제, 전일 가격 대체 금지, 거래량 0 표시, 동일 시각 데이터 충돌, 빈 키움 응답, 캐시된 휴장 응답, 마지막 실제 거래 시각 처리, 무효화된 토큰의 1회 재인증 및 반복 인증 오류 시 중단.
2. 토스·키움으로 2026-09-29~2026-10-06의 실제 5거래일 수집 완료. 두 ETF 모두 마지막 체결 가격과 확인 가능 시각이 **5/5일** 일치했다. 그 5일에는 정확한 15:20 대상 분에 체결이 없어서 토스는 거래량 0 봉, 키움은 해당 봉이 없는 형태였다.
3. 기존 파일의 2026-09-23을 토스에서 다시 조회했다. 각 ETF의 중복 시각 22개에 대해 OHLCV가 모두 일치했다.
4. 기존 자료만 가공한 `offline_checked/proxy_panel.csv`에는 2021-01-04~2026-09-28 실제 거래일 1,405개가 남는다. 두 ETF 모두 2021-12-20부터 1,166일의 마지막 체결 가격 관측이 있고, 이전 239일은 누락이다. 두 ETF 모두 대상 15:20 분에 실제 거래가 있는 날은 348일이다. 데이터 보존과 정확한 분의 거래 존재를 별도로 확인한 결과다.
5. Python 구문 컴파일 통과. 원본 CSV는 수정하지 않았다. 이번 작업에서 전략 백테스트나 Calmar 성과 재계산은 수행하지 않았다.
6. `collected/proxy_panel.csv`는 실제 수집과 기존 자료 결합을 완료한 **2021-12-20~2026-10-06, 1,171거래일**의 자료다. 두 ETF 모두 15:20까지 확인 가능한 마지막 거래 가격이 존재한다. 정확한 대상 분에 두 ETF 모두 거래가 있는 날은 348일이다. 시각·날짜의 미래 정보 사용 여부와 원본 5개 CSV의 해시 보존을 확인했으며 검증 기록은 `verification_20261007.json`에 있다.
7. `early_2021_checked/snapshots.csv`에서 두 ETF 모두 **2021-01-04~2021-01-08 일봉 5일**의 시가·종가를 토스로 받았다. 같은 기간의 분봉 요청은 모두 빈 응답이었다. 2021년 초의 일봉 접근과 해당 시각의 분봉 접근을 별도로 검증했다.

2021년 초부터 정확한 시각의 분봉을 반드시 포함하려면 해당 구간을 보유한 다른 제공처나 기존 아카이브가 추가로 필요하다. 일봉만으로 특정 날의 15:19/15:20 가격을 복원할 수는 없다.
