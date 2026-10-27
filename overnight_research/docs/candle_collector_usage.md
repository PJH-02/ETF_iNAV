# 국내 주식·ETF 공용 수집 코드

파일: `overnight_research/code/collect_us_etf_close_snapshots.py`

종목은 소스 코드를 수정하지 않고 `--symbols`로 지정한다. 환헤지 여부, 미국지수 여부, KODEX200 여부에 따른 분기가 없다. `--interval 1d`는 일봉만 수집하고 분봉이나 로컬 분봉 파일을 요구하지 않는다. `--interval 1m`은 기존 15:19·15:20 관측 자료를 만든다. 두 모드는 모두 같은 `normalize`와 티커 설정을 사용한다.

## 실행

Python 3.10 이상과 `requests`, `pandas>=2.0`, `python-dotenv`가 필요하다. 인증 변수와 `--env-file` 사용법은 이전과 같다. 키움 인증 변수가 여러 `.env` 파일에 나뉘어 있으면 `--env-file`을 반복 지정한다. 다른 저장소의 경로를 코드에 고정하지 않는다.

기존 미국지수 proxy 자료와 같은 기간의 KODEX200 일봉:

```powershell
py -3.10 overnight_research/code/collect_us_etf_close_snapshots.py --provider toss --interval 1d --symbols 069500 --start 2021-12-20 --end 2026-10-06
```

2021년 초부터 KODEX200과 다른 ETF를 함께 수집:

```powershell
py -3.10 overnight_research/code/collect_us_etf_close_snapshots.py --provider toss --interval 1d --symbols 069500,133690,360750 --start 2021-01-01 --end 2026-10-06
```

티커 목록만 바꿔도 해당 종목들의 일봉이 동일한 열로 저장된다. `069500`의 앞자리 0은 포함해야 한다. 공백이 들어간 목록은 따옴표로 감싼다. 영문이 들어간 6자리 국내 종목코드도 받을 수 있다. 특정 종목을 별도로 등록하는 사전은 없다.

분봉 관측 예:

```powershell
py -3.10 overnight_research/code/collect_us_etf_close_snapshots.py --provider toss --interval 1m --symbols 069500,133690,360750 --start 2026-10-02 --end 2026-10-02
```

기간이 길다고 과거 분봉이 자동으로 존재하는 것은 아니다. 공급자가 반환하지 않은 분봉은 누락으로 표시한다. 일봉의 종가를 해당 날의 15:20 가격으로 대체하지 않는다.

## 저장 구조

`--out`을 생략하면 공급자·봉 종류·티커 조합에 따라 자동으로 폴더를 구분한다.

```text
overnight_research/data/market_data/
  toss/1d/069500/
  toss/1d/069500_133690_360750/
  toss/1m/069500_133690_360750/
```

일봉 모드 출력:

- `{symbol}_daily.csv`: 요청 기간으로 자른 종목별 OHLCV.
- `daily.csv`: 모든 요청 종목을 행으로 쌓은 자료.
- `daily_panel.csv`: 같은 날짜의 종목들을 열로 연결한 자료.
- `coverage.csv`: 종목별 실제 행 수와 최초·최종 날짜.
- `manifest.json`: 요청 조건, 티커, 공급자, 원본·출력·코드 해시.
- `{symbol}_daily_{provider}.csv`: 직전 종가 확인용 이전 구간이 포함될 수 있는 수집 캐시. 백테스트용으로 기간을 맞춘 파일은 `{symbol}_daily.csv`다.
- `raw/{provider}/{symbol}/daily_{start}_{end}/`: 원시 API 페이지.

일봉 CSV의 공통 열은 `timestamp, open, high, low, close, volume, symbol, source, origin, date`다. 날짜는 한국 시각으로 처리하고 티커는 문자열로 저장한다. CSV를 읽을 때도 `pd.read_csv(path, dtype={"symbol": str})`를 사용하면 앞자리 0을 보존할 수 있다.

분봉 모드의 종목별 `snapshots.csv` 구조와 `proxy_panel.csv` 파일명은 유지한다. 종목을 두 개로 가정하던 집계 열은 `all_usable_1520`, `all_asof_usable_1520`으로 일반화했다. 이전 `proxy_for` 종목 설명 열은 제거하고 `symbol`을 종목 식별자로 사용한다. 기존에 저장해 둔 연구 CSV를 변경한 것은 아니다.

직접 `--out`을 지정한 폴더가 다른 종목 조합·봉 종류·공급자의 자료를 담고 있으면 코드가 섞어서 덮어쓰지 않고 경로를 변경하라는 오류를 낸다. 같은 조건의 재실행은 캐시를 재사용한다. 수집 기간은 같은 폴더에서도 변경할 수 있고 출력은 새 요청 기간으로 다시 잘린다.

## 기존 CSV 사용

`--local-dir` 아래에서 티커가 들어간 일반적인 파일명을 탐색한다. 일봉은 `{symbol}_daily*.csv` 및 `*_{symbol}_daily*.csv`, 분봉은 `{symbol}*_1m.csv` 및 `*_{symbol}_1m*.csv`를 지원한다. 기존 `304940_1500_1530_1m.csv` 같은 이름도 사용할 수 있다.

파일명이 다른 경우 선택한 봉 종류에 대해 `--local-file SYMBOL=PATH`를 지정한다.

```powershell
py -3.10 overnight_research/code/collect_us_etf_close_snapshots.py --provider offline --interval 1d --symbols 069500 --start 2021-01-01 --end 2026-09-28 --local-file "069500=overnight_research/data/KODEX200_069500_daily_20100101_20260928.csv"
```

정규화한 CSV에는 토스 원시 필드명이나 키움의 `dt`·`cntr_tm`을 다시 요구하지 않는다. 시간대 없는 로컬 날짜·시각은 한국 시각으로 처리한다. 기존 CSV의 숫자 `69500`은 종목 식별 시 `069500`으로 복원하지만, 다른 티커의 가격을 요청 티커로 이름만 바꾸지는 않는다. OHLC 범위와 가격·거래량의 유효성 검사는 유지한다. 오류에는 문제가 있는 티커·봉 종류·시각이 표시된다.

고정된 KODEX200 파일을 거래일 자료로 자동으로 사용하던 동작은 제거했다. 다른 날짜 자료로 누락까지 확인하려면 `--calendar PATH`를 명시한다. 이 파일에서는 날짜만 사용하고 가격을 채우지 않는다.

## 검증 결과

- 자체 검증 22개 통과. 임의 티커, 앞자리 0, 키움 일봉과 캐시, 시간대 없는 CSV, 일봉 모드의 분봉 요청 금지, 신규 티커의 로컬 분봉 파일 부재를 포함한다.
- 토스: `069500`, `133690`, `360750` 일봉을 2021-01-04~2026-10-06 각 1,410행 수집.
- 토스: 위 세 종목의 2026-10-02 분봉 관측 수집 성공.
- 키움: `069500`의 2026-09-29~2026-10-06 일봉 5행 수집 성공.
- 기존 두 미국지수 ETF의 로컬 분봉 처리를 최근 3거래일로 다시 확인.

수정 전 코드는 `_layout_operations/collector_before_generic_20261007.py`에 보관했다. 과거 연구 원본 데이터와 성과 결과는 이번 수정으로 덮어쓰지 않았다.
