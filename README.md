# Mail Desk — 개인 메일 대시보드 PWA

Gmail 2계정 + 네이버 메일을 매일 1회 자동 수집해 **일별 정리 · 월별 지출 · 예약 일자별 · 저관심 광고 차단 요청 · 보관함(숨김·차단)** 을 한 화면에서 관리합니다.
레거시 프로젝트는 `archive/legacy` 브랜치에 보관되어 있습니다.

```
GitHub Actions (매일 06:47 KST)                Firebase RTDB  mail/            GitHub Pages (PWA)
 collector/main.py                              daily/  spend/YYYY-MM/          index.html / app.js
   ├ Gmail API (OAuth refresh token) ×2  ─쓰기▶ reservations/  adSenders/  ◀─읽기/쓰기─ 일별 · 지출 · 예약
   ├ Naver IMAP (앱 비밀번호)                    retention/  meta/  accounts/        차단 요청 · 보관함
   └ 보관 정책: 180일 만료 자동 삭제 / D-7 경고 메일
```

- 메일 **본문은 저장하지 않음** (제목·발신자·추출값만). 읽음 상태 변경 없음 (Gmail `format=metadata`, IMAP `BODY.PEEK`).
- 모든 자격 증명은 GitHub Secrets 참조. 레포에는 개인 데이터 없음.

## 구성 파일

| 경로 | 역할 |
|---|---|
| `collector/main.py` | 수집 오케스트레이션 (보관 정책 → 계정별 처리 → 경고 → 기록) |
| `collector/sources.py` | `GmailSource`(Gmail REST API) · `ImapSource`(네이버) |
| `collector/parse.py` | 디코딩·광고 판정·지출/예약 추출 |
| `collector/retention.py` | 180일 보관 · D-7 경고 판정 (순수 함수) |
| `collector/store.py` | Firebase REST(서비스 계정) · DRY_RUN 저장소 |
| `collector/rules.json` | 계정 정의, 분류 키워드, 지출 카테고리, 차단 기준 |
| `collector/tests/` | pytest (파서·보관 정책·E2E) |
| `tools/gmail_oauth.py` | Gmail refresh token 발급 (로컬 1회) |
| `.github/workflows/daily.yml` | 매일 수집 + 수동 실행(백필·계정 선택) |
| `.github/workflows/ci.yml` | PR 테스트 (pytest, JS 문법, JSON 검증) |
| `index.html` `app.js` `style.css` `config.js` | 대시보드 (Vanilla JS, Firebase compat SDK) |
| `database.rules.json` | RTDB 보안 규칙 (본인 UID만 read/write) |
| `sw.js` `manifest.webmanifest` `icons/` | PWA |

## 세팅 순서

1. **Gmail API (Google Cloud Console)**
   1. 프로젝트 선택 → *API 및 서비스 → 라이브러리* → **Gmail API 사용**
   2. *OAuth 동의 화면*: User type **외부**, 테스트 사용자에 Gmail 2계정 추가
   3. **앱 게시 → 프로덕션으로 전환** (테스트 상태면 refresh token 이 **7일 후 만료**). 미인증 앱 경고 화면은 *고급 → 이동* 으로 통과
   4. *사용자 인증 정보 → OAuth 클라이언트 ID* → 유형 **데스크톱 앱** → 클라이언트 ID / 보안 비밀 확보
   5. 로컬 PC에서 계정별 실행 → 출력된 refresh token 저장
      ```bash
      python3 tools/gmail_oauth.py --client-id <ID> --client-secret <SECRET> --login-hint aaa@gmail.com
      python3 tools/gmail_oauth.py --client-id <ID> --client-secret <SECRET> --login-hint bbb@gmail.com
      ```
2. **네이버 메일**: 메일 → 환경설정 → POP3/IMAP 설정 → **IMAP/SMTP 사용함** → 내정보 → 보안설정 → 2단계 인증 → **애플리케이션 비밀번호** 발급
3. **Firebase**
   1. Realtime Database (기존 `qcells-tandem-ops` 사용, 잠금 모드)
   2. Authentication → 로그인 방법: **Google**, **이메일/비밀번호** 사용
   3. Authentication → 설정 → 승인된 도메인: `jin-spec-0011.github.io`
   4. 프로젝트 설정 → 서비스 계정 → **새 비공개 키 생성** (JSON, 커밋 금지)
4. **GitHub Secrets** (Settings → Secrets and variables → Actions)

   | 이름 | 값 | 필수 |
   |---|---|---|
   | `FIREBASE_DB_URL` | `https://qcells-tandem-ops-default-rtdb.firebaseio.com` | ● |
   | `FIREBASE_SA_JSON` | 서비스 계정 JSON 파일 내용 전체 | ● |
   | `GMAIL_CLIENT_ID` | OAuth 클라이언트 ID | Gmail |
   | `GMAIL_CLIENT_SECRET` | OAuth 클라이언트 보안 비밀 | Gmail |
   | `GMAIL1_REFRESH_TOKEN` | 메인 Gmail refresh token (`g1`) | Gmail |
   | `GMAIL2_REFRESH_TOKEN` | 서브 Gmail refresh token (`g2`) | Gmail |
   | `NAVER_IMAP_USER` | 네이버 아이디 (`@naver.com` 제외) | Naver |
   | `NAVER_IMAP_PASSWORD` | 네이버 애플리케이션 비밀번호 | Naver |

   계정 Secret 이 비어 있으면 해당 계정만 건너뜁니다(경고 표시). 계정 이름은 `collector/rules.json` → `accounts.*.label`.
5. **보안 규칙 게시**: `database.rules.json` 의 UID 확인(대시보드 '접근 권한 없음' 화면에 표시되는 값) → Firebase 콘솔 → Realtime Database → 규칙 탭에 붙여넣고 게시
6. **GitHub Pages**: Settings → Pages → `Deploy from a branch` → `main` / `(root)`
7. **최초 백필**: Actions → `daily-mail-collect` → Run workflow → `window_days = 60`
8. iPhone: Safari 접속 → Google 로그인 → 상단 🔑 로 비밀번호 연결 → 공유 → 홈 화면에 추가 → 이메일/비밀번호 로그인

## 화면 · 동작 규칙

| 화면 | 기준 |
|---|---|
| 일별 | 일자별 수신/미열람/광고/지출 KPI, 메일 목록(전체·미열람·지출·예약·광고 필터), 14일 추이. 계정·일당 일반 40건 / 광고 20건 저장 |
| 지출(월) | 제목에 승인/결제/영수증/출금 등 + 금액 추출 (KRW·USD·JPY). 취소·환불은 음수. 여러 계정 중복 수신은 같은 날·금액·10분 이내로 자동 병합. 탭하여 분류 수정·합계 제외·숨김 |
| 예약 | 제목에 예약/예매/booking/予約 등 + 본문 일자(수신일 이후 가장 이른 날짜)·시각·장소. 취소 메일은 동일 업체·일자 예약을 취소 처리 |
| 차단 요청 | 최근 30일 광고성 메일 **3통 이상 & 열람률 10% 이하** 발신자. 광고 판정: 제목 `(광고)` 표기, `List-Unsubscribe` 헤더, Gmail 프로모션 탭 (거래성 제목·`keep_senders` 제외) |
| 차단 처리 | [차단] → 다음 수집 때 ① RFC 8058 원클릭 수신거부(지원 발신자, 1회) ② 해당 발신자 메일 **휴지통 이동** ③ 이후 신규 메일도 매일 휴지통 이동. 원클릭 미지원은 수신거부 링크 표시 |
| 보관함 | 숨김·차단 항목 전체, D-day, 복원/차단 해제, 180일 연장 |

### 숨김·차단 보관 정책

| 항목 | 규칙 |
|---|---|
| 보관 기간 | 숨김/차단 **처리 시점부터 180일** (메일함 휴지통처럼 '이동한 날' 기준) |
| 삭제 승인 | **없음** — 만료 다음 수집 때 자동 삭제 (DB 레코드). 차단 항목은 삭제와 함께 차단 해제 → 기준 충족 시 다시 차단 요청에 등장 |
| 사전 경고 | 만료 **7일 전**부터 대시보드 상단 배너 + 보관함 배지, 경고 메일 **1회** (`notify_account` Gmail → 본인 주소), Actions 경고 주석 |
| 메일 원본 삭제 | 수집기는 메일을 **영구 삭제하지 않음**. 휴지통 이동만 하며, 영구 삭제는 **각 메일함 휴지통 규칙**(Gmail 30일 자동 비움, 네이버 환경설정의 휴지통 비우기)을 따름 |
| 연장 | 보관함 [180일 연장] → 처리 시점 재설정, 경고 이력 초기화 |

값 변경 시 `collector/retention.py`(`RETAIN_DAYS`, `WARN_DAYS`)와 `app.js` 상단 상수를 함께 수정합니다.

## 로컬 테스트

```bash
pip install -r collector/requirements-dev.txt
cd collector && python -m pytest -q tests

# 실제 메일함 읽기 + 로컬 파일 출력 (Firebase 미사용)
export GMAIL_CLIENT_ID=... GMAIL_CLIENT_SECRET=... GMAIL1_REFRESH_TOKEN=...
DRY_RUN=1 WINDOW_DAYS=3 ONLY_ACCOUNTS=g1 python collector/main.py   # → collector/out.json (커밋 제외)
```

> DRY_RUN 도 차단 발신자 휴지통 이동은 실제로 수행합니다. out.json 에 `adSenders` 가 없으면 이동 대상도 없습니다.

## 레거시 데이터

이전 버전 데이터(`pa/`)는 새 규칙에서 접근이 차단됩니다. 필요 없으면 Firebase 콘솔에서 `pa` 노드를 삭제하세요.
