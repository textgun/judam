# Judam · 주담통화 모아보기

네이버 블로그에 올라온 주담통화(주식담당자 통화) 후기를 워치리스트 기업별로 모아 GitHub Pages로 보여줍니다.
본문은 복사하지 않고 제목·요약·원문 링크만 표시합니다.

## 파일
- `judam.py` : `collect`(네이버 블로그 검색 → data/날짜.json), `build`(site/ HTML 생성, 기업 검색창 포함)
- `watchlist.csv` : 대상 기업 목록 (name,code,aliases,market,sector). 네이버 증권 코스피·코스닥 상장 보통주 전체(스팩 제외, 2,596곳)와 네이버 업종, 시총순. `python judam.py watchlist`로 다시 받으면 aliases는 유지
- `.github/workflows/daily.yml` : 매일 14:40 KST 자동 실행 후 Pages 배포
- `data/`, `site/` : 날짜별 수집 데이터와 생성된 페이지. `data/backfill/posts.json`은 종목별 지난 글(한 번 채워넣기)
- `docs/` : 화면 디자인 계획·체크리스트·맥락 노트 (흰 바탕 + 괘선 기록 줄, 강조는 검색어 형광펜 하나)
- `preview.png`, `preview-search.png` : 현재 화면 캡처 (1100px, 2배 해상도)

## 페이지에 나오는 것
- 기업마다 최근 5건(그날 글 + 지난 글), 글마다 날짜
- 최근 7일 공시(DART, 최대 5건) — 기업 칸 맨 위
- 최근 7일 언급 급증 — 7일 3건 이상이고 앞선 4주 주평균의 2배 이상인 기업
- 전체 기간 검색(`search.html`) — 상장사 이름·종목코드·별칭으로 그 회사 주담통화 후기를 전 기간 최신순으로. 기업이 안 맞으면 제목에서 찾음. 데이터는 build가 `site/search.json`으로 만든다

## 로컬 실행
`.env`에 네이버 검색 API 키를 적습니다. `.env`는 `.gitignore`에 들어 있어 커밋되지 않습니다.
처음이면 `cp .env.example .env` 후 값을 채우세요. 셸에서 `export`한 값이 있으면 그쪽이 우선합니다.
```
NAVER_CLIENT_ID=발급받은_Client_ID
NAVER_CLIENT_SECRET=발급받은_Client_Secret
DART_API_KEY=발급받은_DART_인증키     # 없으면 공시 표시만 빠짐
```
```bash
python judam.py collect
python judam.py backfill         # 지난 글 채우기. 이미 찾은 종목은 건너뛴다(새 종목 1곳당 API 5회)
python judam.py build            # 검색 페이지는 fetch를 써서 file://로는 안 열림
python -m http.server 8767 --directory site   # http://localhost:8767 에서 확인
```

## GitHub 설정
1. 빈 저장소 생성 후 이 폴더 푸시
2. Settings → Secrets and variables → Actions 에 `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`, `DART_API_KEY` 등록
3. Settings → Pages → Source: GitHub Actions
