# ETF 연구 디렉토리

2026-10-07에 연구 목적별로 코드, 입력 데이터, 기존 결과와 문서를 정리했습니다.

```text
ETF_iNAV/
├─ intraday_research/
│  ├─ code/                       인트라데이 백테스트·수집·검증 코드
│  ├─ data/                       선물 및 Kiwoom ETF 입력
│  ├─ results/                    기존 백테스트·EDA 결과
│  ├─ docs/                       전략 명세·연구 보고서
│  └─ archive/pre_reorganization/ 이동 전 코드 원본
├─ overnight_research/
│  ├─ code/                       오버나잇 백테스트·수집 코드
│  ├─ data/                       오버나잇 입력과 공통 시장 데이터
│  ├─ results/                    기존 오버나잇 연구 결과
│  ├─ docs/                       기존 연구 설계 문서
│  └─ archive/pre_reorganization/ 이동 전 코드 원본
└─ _layout_operations/            이동 목록·해시·정리 검증 기록
```

**공통 데이터는 `overnight_research/data`에 있습니다.** 인트라데이 코드는 이 경로를 직접 참조합니다. 특히 공통 ETF 분봉 원본은 `overnight_research/data/market_1m_data/KODEX200_069500_1m_toss.csv`입니다. 인트라데이에 있는 Kiwoom ETF 데이터와 선물/ETF 정렬 패널은 별도의 공급자 자료와 파생 자료입니다.

실행 파일과 사용 데이터의 대응은 [인트라데이 안내](intraday_research/README.md)와 [오버나잇 안내](overnight_research/README.md)에 정리했습니다. 코드의 기본 경로는 스크립트 위치를 기준으로 계산하므로 실행 폴더에 의존하지 않습니다. `.env`와 `.venv`는 워크스페이스 루트에 있습니다.

아래 명령은 이 루트에서 실행합니다. 검증은 기존에 필요한 패키지가 설치된 Python 3.10에서 수행했습니다. `.venv`에는 `aiohttp`가 없어 전체 인트라데이 테스트에는 사용하지 않았습니다.

```powershell
py -3.10 -m unittest discover -s intraday_research/code -p 'test_intraday*.py'
py -3.10 intraday_research/code/verify_intraday_bracket_outputs.py
py -3.10 intraday_research/code/verify_intraday_v2_outputs.py
py -3.10 overnight_research/code/verify_overnight_inputs.py
py -3.10 _layout_operations/verify_layout.py
```

원본 데이터·결과·전략 명세는 이동 전후 SHA-256으로 검증했습니다. 기존 실행 manifest와 `verification.json`은 보존했고, 이동 후 결과 검증은 `verification_after_reorganization.json`에 저장합니다. 이전 코드 해시는 `archive/pre_reorganization` 원본으로 검증하고, 경로가 수정된 현재 코드는 별도 해시로 검증합니다. 전략 규칙·파라미터·수수료는 변경하지 않았으며, 정리 과정에서 백테스트를 재실행하지 않았습니다.

기존 문서와 실행 기록에 나오는 옛 경로는 `_layout_operations/layout_manifest.json`에서 새 경로를 찾을 수 있습니다. `code/research_paths.py`가 기존 입력 경로도 해석합니다. 정리 전에 이미 없던 `analysis_output` 및 `analysis_output_closebet`의 결과 파일은 재생성하지 않았습니다.
