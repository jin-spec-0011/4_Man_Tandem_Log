# Mail Ops — 개인 메일 대시보드

Gmail 2계정 + Naver 1계정을 매일 자동 수집해 **일별 요약 · 월별 지출 · 일자별 예약 · 저관심 광고 차단 요청**을 한 화면에서 관리하는 PWA.

```
GitHub Actions (매일 06:47 KST)          Firebase RTDB (pa/)            GitHub Pages (PWA)
 collector/collect.py ── IMAP ──▶ Gmail×2, Naver     daily/        ◀── 읽기 ──  index.html / app.js
   ├ 일별 요약                                    spend/YYYY-MM/            ├ 오늘
   ├ 지출 추출 (규칙 + 선택적 LLM)  ── 쓰기 ──▶    reservations/            ├ 지출(월)
   ├ 예약 추출                                    blockCandidates/  ◀── 승인 ─┤ 예약(일자)
   └ 차단 후보 산출 / 승인분 처리                  blocklist, allowlist      └ 차단 요청
```

- 메일 **본문은 저장하지 않음** (제목·발신자·추출값만 기록). 레포는 공개여도 개인 데이터는 Firebase에만 존재.
- 수집은 `BODY.PEEK` 사용 → **읽음 상태 변경 없음**.

## 구성 파일

| 경로 | 역할 |
|---|---|
| `collector/collect.py` | IMAP 수집·분류·추출·차단 처리 |
| `collector/rules.json` | 분류 키워드, 지출 카테고리, 차단 기준, 보호 발신자 |
| `.github/workflows/daily.yml` | 매일 실행 + 수동 실행(백필) |
| `index.html` `app.js` `style.css` | 대시보드 (Vanilla JS, Firebase compat SDK) |
| `config.js` | Firebase 웹 구성값 |
| `database.rules.json` | RTDB 보안 규칙 (본인 UID만 접근) |
| `sw.js` `manifest.webmanifest` `icons/` | PWA |

## 세팅 순서

1. **메일 앱 비밀번호 발급**
   - Gmail(2계정 각각): Google 계정 → 보안 → 2단계 인증 활성화 → *앱 비밀번호* 생성 (16자리, 공백 제거)
   - Naver: 메일 → 환경설정 → POP3/IMAP 설정 → **IMAP/SMTP 사용함** → (2단계 인증 사용 시) 내정보 → 보안설정 → *애플리케이션 비밀번호* 발급
2. **Firebase 프로젝트**
   1. Realtime Database 생성 (리전 `asia-southeast1` 권장, 잠금 모드)
   2. Authentication → 로그인 방법: **Google**, **이메일/비밀번호** 사용 설정
   3. Authentication → 설정 → 승인된 도메인: `jin-spec-0011.github.io` 추가
   4. 프로젝트 설정 → 일반 → 웹 앱 추가 → 구성값을 `config.js` 에 입력
   5. 프로젝트 설정 → 서비스 계정 → **새 비공개 키 생성** (JSON 다운로드, 커밋 금지)
3. **GitHub Secrets** (Settings → Secrets and variables → Actions)

   | 이름 | 값 |
   |---|---|
   | `ACCOUNTS_JSON` | 아래 형식 (한 줄로) |
   | `FIREBASE_DB_URL` | `https://qcells-tandem-ops-default-rtdb.firebaseio.com` |
   | `FIREBASE_SA_JSON` | 서비스 계정 JSON 파일 내용 전체 |
   | `ANTHROPIC_API_KEY` | (선택) 지출·예약 추출 정밀도 향상. 미설정 시 규칙 기반 |

   ```json
   [
     {"id":"g1","label":"Gmail 메인","host":"imap.gmail.com","user":"aaa@gmail.com","pass":"앱비밀번호16자리"},
     {"id":"g2","label":"Gmail 서브","host":"imap.gmail.com","user":"bbb@gmail.com","pass":"앱비밀번호16자리"},
     {"id":"nv","label":"Naver","host":"imap.naver.com","user":"아이디","pass":"애플리케이션비밀번호"}
   ]
   ```
4. **GitHub Pages**: Settings → Pages → Source `Deploy from a branch` → `main` / `(root)`
5. **최초 로그인 → UID 확보**: Pages 주소 접속 → 로그인 → "접근 권한 없음" 화면의 UID 복사
6. **보안 규칙 배포**: `database.rules.json` 의 `REPLACE_WITH_YOUR_UID` 를 UID로 교체 → Firebase 콘솔 Realtime Database → 규칙 탭에 붙여넣고 게시
7. **최초 백필**: Actions → `daily-mail-collect` → Run workflow → `window_days = 60` (지출·예약 2개월치 적재)
8. iPhone: Safari로 접속 → 공유 → 홈 화면에 추가

## 동작 규칙

| 기능 | 기준 |
|---|---|
| 일별 요약 | 최근 `window_days`(기본 3일) 일자별 수신/미열람/광고 수, 광고 제외 미열람 메일 상위 15건 |
| 광고 판정 | 제목 `(광고)` 표기, 또는 `List-Unsubscribe` 헤더 보유 대량메일 (거래성 제목·`keep_senders` 제외) |
| 지출 | 제목에 승인/결제/영수증/출금 등 + 금액 추출. 취소·환불은 음수. 같은 날 같은 금액은 대시보드에서 "중복 의심" 표시 → 탭하여 제외 |
| 예약 | 제목에 예약/예매/booking 등 + 본문 일자 추출(수신일 이후 가장 이른 날짜). 취소 메일은 동일 업체·일자 예약을 취소 처리 |
| 차단 요청 | 최근 30일 광고성 메일 **3통 이상** & 열람률 **10% 이하** 발신자 |
| 차단 처리 | 대시보드 승인 → 다음 수집 시 ① RFC 8058 원클릭 수신거부(지원 발신자만 자동) ② 기존 메일 휴지통 이동 ③ 이후 신규 메일 자동 휴지통 이동. 원클릭 미지원은 대시보드에 수신거부 링크 표시 |

기준값 변경: `collector/rules.json` (`block_rule`), 대시보드 표시용은 `app.js` 의 `BLOCK_RULE` 도 함께 수정.

## 로컬 테스트

```bash
pip install -r collector/requirements.txt
export ACCOUNTS_JSON='[...]'
DRY_RUN=1 WINDOW_DAYS=3 python collector/collect.py   # collector/out.json 으로 출력 (커밋 제외됨)
```

> ⚠️ DRY_RUN 도 승인된 차단 처리·휴지통 이동은 Firebase 상태가 없으므로 실행되지 않음. 실 메일함 변경은 Firebase 연결 상태에서만 발생.
