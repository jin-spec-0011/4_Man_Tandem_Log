#!/usr/bin/env python3
"""Gmail refresh token 발급 도우미 (로컬 PC에서 계정당 1회 실행, 표준 라이브러리만 사용).

사전 준비
  Google Cloud Console → API 및 서비스
    1) Gmail API 사용 설정
    2) OAuth 동의 화면: 외부(External), 테스트 사용자에 Gmail 2계정 추가 후
       **'앱 게시' → 프로덕션** 으로 전환 (테스트 상태면 refresh token 이 7일 후 만료)
    3) 사용자 인증 정보 → OAuth 클라이언트 ID → 애플리케이션 유형 '데스크톱 앱'

실행
  python3 tools/gmail_oauth.py --client-id XXX.apps.googleusercontent.com --client-secret YYY
  → 브라우저에서 대상 Gmail 계정으로 로그인·허용 → 터미널에 refresh token 출력
  → GitHub Secrets GMAIL1_REFRESH_TOKEN / GMAIL2_REFRESH_TOKEN 에 저장

요청 범위: https://www.googleapis.com/auth/gmail.modify
  (읽기 + 휴지통 이동 + 본인에게 경고 메일 발송. 영구 삭제 권한 없음)
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/gmail.modify"


def main() -> int:
    ap = argparse.ArgumentParser(description="Gmail OAuth refresh token 발급")
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--client-secret", required=True)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--login-hint", default="", help="대상 Gmail 주소 (계정 선택 화면 자동 지정)")
    a = ap.parse_args()

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    redirect = f"http://127.0.0.1:{a.port}/"
    params = {"client_id": a.client_id, "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
              "access_type": "offline", "prompt": "consent", "state": state,
              "code_challenge": challenge, "code_challenge_method": "S256"}
    if a.login_hint:
        params["login_hint"] = a.login_hint
    url = AUTH_URL + "?" + urllib.parse.urlencode(params)

    result: dict = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" not in q and "error" not in q:
                self.send_response(404)
                self.end_headers()
                return
            if q.get("state", [""])[0] != state:
                result["error"] = "state 불일치 (CSRF 의심) — 다시 실행하세요"
            elif "error" in q:
                result["error"] = q["error"][0]
            else:
                result["code"] = q["code"][0]
            ok = "code" in result
            body = ("<h2>완료 — 터미널로 돌아가세요.</h2>" if ok else f"<h2>실패: {result.get('error')}</h2>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
            done.set()

        def log_message(self, *_):
            pass

    try:
        srv = http.server.HTTPServer(("127.0.0.1", a.port), Handler)
    except OSError as e:
        print(f"포트 {a.port} 사용 불가: {e} → --port 로 다른 포트 지정", file=sys.stderr)
        return 1
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("브라우저에서 아래 주소를 열고 대상 Gmail 계정으로 허용하세요:\n\n" + url + "\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    if not done.wait(timeout=300):
        srv.shutdown()
        print("5분 내 응답 없음 → 다시 실행하세요", file=sys.stderr)
        return 1
    srv.shutdown()
    if "code" not in result:
        print(f"인증 실패: {result.get('error')}", file=sys.stderr)
        return 1

    data = urllib.parse.urlencode({"code": result["code"], "client_id": a.client_id,
                                   "client_secret": a.client_secret, "redirect_uri": redirect,
                                   "grant_type": "authorization_code", "code_verifier": verifier}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=data), timeout=30) as r:
            tok = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"토큰 교환 실패 {e.code}: {e.read().decode(errors='replace')[:300]}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"네트워크 오류: {e.reason}", file=sys.stderr)
        return 1
    rt = tok.get("refresh_token")
    if not rt:
        print("refresh_token 없음 → https://myaccount.google.com/permissions 에서 앱 액세스 삭제 후 재실행",
              file=sys.stderr)
        return 1
    if SCOPE not in tok.get("scope", ""):
        print(f"경고: 허용 범위에 gmail.modify 없음 (받은 범위: {tok.get('scope')})", file=sys.stderr)
    if "refresh_token_expires_in" in tok:
        print(f"경고: 만료 기한이 있는 토큰(약 {int(tok['refresh_token_expires_in']) // 86400}일) — "
              "동의 화면이 '테스트' 상태일 수 있음. '프로덕션' 게시 후 재발급 권장", file=sys.stderr)
    print("\n=== refresh token (GitHub Secret 에 그대로 저장, 외부 노출 금지) ===\n" + rt + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
