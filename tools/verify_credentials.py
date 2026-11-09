#!/usr/bin/env python3
"""3단계 결과 검증: Gmail refresh token 2개 + 네이버 IMAP 앱 비밀번호 (표준 라이브러리만 사용).

값은 화면에 표시되지 않는 입력(getpass)으로 받거나 환경변수에서 읽는다. 비밀 값은 출력하지 않는다.
  GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL1_REFRESH_TOKEN, GMAIL2_REFRESH_TOKEN,
  NAVER_IMAP_USER, NAVER_IMAP_PASSWORD

실행
  python tools/verify_credentials.py            # 전체 검사 (빈 값은 건너뜀)
  python tools/verify_credentials.py --only g1  # g1 | g2 | nv

검사 항목
  Gmail : 토큰 갱신 → gmail.modify 범위 → 계정 주소 → 메일 목록 조회 → 토큰 만료 기한 유무
  Naver : IMAP 로그인 → INBOX 선택 → 휴지통 폴더 탐지
"""
from __future__ import annotations

import argparse
import getpass
import imaplib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPE = "https://www.googleapis.com/auth/gmail.modify"
OK, NG, WARN = "[ 통과 ]", "[ 실패 ]", "[ 주의 ]"


def ask(env: str, label: str, secret: bool = True) -> str:
    v = os.getenv(env, "").strip()
    if v:
        print(f"  {label}: 환경변수 {env} 사용")
        return v
    try:
        v = (getpass.getpass if secret else input)(f"  {label} (비우면 건너뜀): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    return v


def http_json(req: urllib.request.Request) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except ValueError:
            return e.code, {}


def check_gmail(name: str, cid: str, csec: str, rt: str) -> bool:
    print(f"\n■ Gmail {name}")
    if not rt:
        print(f"  {WARN} refresh token 미입력 → 건너뜀")
        return True
    if re.search(r"\s", rt):
        print(f"  {NG} 토큰에 공백/줄바꿈 포함 → 복사 시 앞뒤 공백 제거")
        return False
    data = urllib.parse.urlencode({"client_id": cid, "client_secret": csec, "refresh_token": rt,
                                   "grant_type": "refresh_token"}).encode()
    try:
        code, tok = http_json(urllib.request.Request(TOKEN_URL, data=data))
    except urllib.error.URLError as e:
        print(f"  {NG} 네트워크 오류: {e.reason}")
        return False
    if code != 200:
        err = tok.get("error", code)
        hint = {
            "invalid_grant": "토큰 만료/폐기 또는 다른 클라이언트로 발급됨 → gmail_oauth.py 재실행",
            "invalid_client": "클라이언트 ID/보안 비밀 불일치",
            "unauthorized_client": "토큰을 발급한 클라이언트와 지금 입력한 클라이언트가 다름",
        }.get(err, tok.get("error_description", ""))
        print(f"  {NG} 토큰 갱신 실패 ({err}) — {hint}")
        return False
    print(f"  {OK} 토큰 갱신")
    ok = True
    if SCOPE in tok.get("scope", ""):
        print(f"  {OK} 범위 gmail.modify")
    else:
        print(f"  {NG} gmail.modify 범위 없음 (받은 범위: {tok.get('scope')}) → 재발급 필요")
        ok = False
    if "refresh_token_expires_in" in tok:
        days = int(tok["refresh_token_expires_in"]) // 86400
        print(f"  {WARN} 토큰 만료 기한 있음(약 {days}일) → 동의 화면이 '테스트' 상태에서 발급된 토큰일 수 있음. "
              f"'프로덕션' 게시 확인 후 재발급 권장")
    auth = {"Authorization": f"Bearer {tok['access_token']}"}
    code, prof = http_json(urllib.request.Request(f"{GMAIL_API}/profile", headers=auth))
    if code == 200:
        print(f"  {OK} 계정 {prof.get('emailAddress')} · 전체 메일 {prof.get('messagesTotal')}통")
    else:
        msg = prof.get("error", {}).get("message", "") if isinstance(prof.get("error"), dict) else ""
        print(f"  {NG} 프로필 조회 실패 {code} — {msg or 'Gmail API 사용 설정 확인'}")
        return False
    code, lst = http_json(urllib.request.Request(f"{GMAIL_API}/messages?maxResults=1", headers=auth))
    if code == 200:
        print(f"  {OK} 메일 목록 조회")
    else:
        print(f"  {NG} 메일 목록 조회 실패 {code}")
        ok = False
    return ok


def check_naver(user: str, pw: str) -> bool:
    print("\n■ Naver IMAP")
    if not (user and pw):
        print(f"  {WARN} 아이디/비밀번호 미입력 → 건너뜀")
        return True
    if "@" in user:
        print(f"  {WARN} 아이디에 '@' 포함 — 보통 '@naver.com' 없이 아이디만 입력")
    try:
        m = imaplib.IMAP4_SSL("imap.naver.com", 993, timeout=30)
    except OSError as e:
        print(f"  {NG} 서버 연결 실패: {e}")
        return False
    try:
        m.login(user, pw)
        print(f"  {OK} 로그인")
    except imaplib.IMAP4.error as e:
        print(f"  {NG} 로그인 실패 — ① 메일 환경설정 IMAP/SMTP '사용함' ② 애플리케이션 비밀번호(일반 비밀번호 아님) "
              f"③ 공백 제거 확인 ({str(e)[:80]})")
        try:
            m.logout()
        except Exception:
            pass
        return False
    ok = True
    typ, data = m.select("INBOX", readonly=True)
    if typ == "OK":
        print(f"  {OK} 받은메일함 {data[0].decode()}통")
    else:
        print(f"  {NG} 받은메일함 선택 실패")
        ok = False
    typ, boxes = m.list()
    raw = " ".join(b.decode(errors="ignore") for b in (boxes or []) if isinstance(b, bytes))
    if "\\Trash" in raw or "&1zTJwNG1-" in raw or "Trash" in raw:
        print(f"  {OK} 휴지통 폴더 탐지 (차단 메일 이동 가능)")
    else:
        print(f"  {WARN} 휴지통 폴더 미탐지 → 차단 메일 휴지통 이동이 동작하지 않을 수 있음")
    try:
        m.logout()
    except Exception:
        pass
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="Gmail/Naver 자격 증명 검증")
    ap.add_argument("--only", choices=["g1", "g2", "nv"], help="한 계정만 검사")
    a = ap.parse_args()
    results = []
    if a.only in (None, "g1", "g2"):
        print("Gmail OAuth 클라이언트")
        cid = ask("GMAIL_CLIENT_ID", "클라이언트 ID", secret=False)
        csec = ask("GMAIL_CLIENT_SECRET", "클라이언트 보안 비밀")
        if not (cid and csec):
            print(f"  {WARN} 클라이언트 정보 없음 → Gmail 검사 건너뜀")
        else:
            for key, env in (("g1", "GMAIL1_REFRESH_TOKEN"), ("g2", "GMAIL2_REFRESH_TOKEN")):
                if a.only in (None, key):
                    rt = ask(env, f"{key} refresh token")
                    results.append((key, check_gmail(key, cid, csec, rt)))
    if a.only in (None, "nv"):
        print("\nNaver IMAP")
        user = ask("NAVER_IMAP_USER", "네이버 아이디", secret=False)
        pw = ask("NAVER_IMAP_PASSWORD", "애플리케이션 비밀번호")
        results.append(("nv", check_naver(user, pw)))

    print("\n=== 결과 ===")
    for k, ok in results:
        print(f"  {k}: {'통과' if ok else '실패'}")
    failed = [k for k, ok in results if not ok]
    if not failed:
        print("모두 통과 → 4단계(Firebase) / 5단계(GitHub Secrets 등록) 진행")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
