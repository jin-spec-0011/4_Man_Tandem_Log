"""Firebase Realtime Database REST 클라이언트 (서비스 계정 OAuth2) + DRY_RUN 저장소."""
from __future__ import annotations

import json
import time

import requests

ROOT = "mail"


class StoreError(RuntimeError):
    pass


def compact(updates: dict) -> dict:
    """같은 PATCH 안의 상위/하위 경로 충돌 제거 (RTDB 는 이를 400 오류로 거부).
    - 상위 값이 dict  → 하위 값을 상위 dict 안으로 병합
    - 상위 값이 None/스칼라 → 하위 경로는 무시 (상위가 우선)"""
    items = sorted(((k.strip("/"), v) for k, v in updates.items()), key=lambda kv: kv[0].count("/"))
    out: dict = {}
    for path, val in items:
        parts = path.split("/")
        anc = next(("/".join(parts[:i]) for i in range(1, len(parts)) if "/".join(parts[:i]) in out), None)
        if anc is None:
            out[path] = val
            continue
        base = out[anc]
        if not isinstance(base, dict):
            continue
        node = base
        rel = parts[len(anc.split("/")):]
        for p in rel[:-1]:
            if not isinstance(node.get(p), dict):
                node[p] = {}
            node = node[p]
        if val is None:
            node.pop(rel[-1], None)
        else:
            node[rel[-1]] = val
    return out


class Firebase:
    def __init__(self, db_url: str, sa_json: str):
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
        try:
            info = json.loads(sa_json)
        except json.JSONDecodeError as e:
            raise StoreError(f"FIREBASE_SA_JSON 파싱 실패: {e}") from e
        if info.get("type") != "service_account":
            raise StoreError("FIREBASE_SA_JSON 이 서비스 계정 키(JSON)가 아닙니다")
        if not db_url.startswith("https://"):
            raise StoreError("FIREBASE_DB_URL 은 https:// 로 시작해야 합니다")
        self.creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/firebase.database",
                          "https://www.googleapis.com/auth/userinfo.email"])
        self._Request = Request
        self.base = db_url.rstrip("/")
        self.s = requests.Session()

    def _params(self, extra: dict | None = None) -> dict:
        if not self.creds.valid:
            self.creds.refresh(self._Request())
        p = {"access_token": self.creds.token}
        if extra:
            p.update(extra)
        return p

    def _url(self, path: str) -> str:
        return f"{self.base}/{ROOT}/{path.strip('/')}.json" if path.strip("/") else f"{self.base}/{ROOT}.json"

    def _req(self, method: str, path: str, body=None, params=None, timeout=60):
        last = None
        for attempt in range(4):
            try:
                r = self.s.request(method, self._url(path), params=self._params(params),
                                   data=None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                   timeout=timeout)
                if r.status_code in (401, 403):
                    raise StoreError(f"Firebase 권한 오류 {r.status_code}: 서비스 계정/DB URL 확인")
                if r.status_code >= 500 or r.status_code == 429:
                    raise requests.HTTPError(f"{r.status_code}", response=r)
                r.raise_for_status()
                return r.json() if r.content else None
            except StoreError:
                raise
            except requests.RequestException as e:
                last = e
                time.sleep(2 ** attempt)
        raise StoreError(f"Firebase {method} {path} 실패: {last}")

    def get(self, path: str, default=None, shallow: bool = False):
        v = self._req("GET", path, params={"shallow": "true"} if shallow else None)
        return default if v is None else v

    def patch(self, updates: dict):
        """ROOT 기준 다중 경로 업데이트. 값 None = 삭제. 400개 단위 분할."""
        items = list(compact(updates).items())
        for i in range(0, len(items), 400):
            self._req("PATCH", "", body=dict(items[i:i + 400]))


class DryRunStore:
    """Firebase 대신 로컬 JSON 파일에 기록 (읽기는 파일 내용 기준)."""

    def __init__(self, path: str):
        self.path = path
        try:
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, json.JSONDecodeError):
            self.data = {}

    def _walk(self, path: str):
        node = self.data
        for p in [x for x in path.strip("/").split("/") if x]:
            if not isinstance(node, dict) or p not in node:
                return None
            node = node[p]
        return node

    def get(self, path: str, default=None, shallow: bool = False):
        v = self._walk(path)
        if v is None:
            return default
        if shallow and isinstance(v, dict):
            return {k: True for k in v}
        return v

    def patch(self, updates: dict):
        for k, v in compact(updates).items():
            parts = [x for x in k.strip("/").split("/") if x]
            node = self.data
            for p in parts[:-1]:
                if not isinstance(node.get(p), dict):
                    node[p] = {}
                node = node[p]
            if v is None:
                node.pop(parts[-1], None)
            else:
                node[parts[-1]] = v
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
