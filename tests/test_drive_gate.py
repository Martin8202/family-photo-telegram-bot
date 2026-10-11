"""
Drive 柵欄（drive_gate.py）的測試：每一種刪除／搬移／修改都必須被擋下，
而且**完全沒有送出網路請求**；允許的動作照常運作。全部用 httpx.MockTransport，不連網。
"""

from __future__ import annotations

import json

import httpx
import pytest

import drive_gate
from drive_gate import DRIVE, FOLDER_MIME, TOKEN_URL, UPLOAD, DriveError, DriveGate, FenceViolation, check_request


class Recorder:
    """假的 Google：記錄收到的每個請求，依網址回應。"""

    def __init__(self, files_status: int = 200):
        self.requests: list[httpx.Request] = []
        self.files_status = files_status
        self.token_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == TOKEN_URL:
            self.token_calls += 1
            return httpx.Response(200, json={"access_token": f"tok{self.token_calls}", "expires_in": 3600})
        if request.url.path == "/drive/v3/about":
            return httpx.Response(200, json={"user": {"emailAddress": "mom@example.com"}})
        if request.url.path.startswith("/upload/"):
            return httpx.Response(200, json={"id": "new-file", "name": "a.png"})
        if request.method == "POST":
            return httpx.Response(200, json={"id": "new-folder", "name": json.loads(request.content)["name"]})
        if self.files_status != 200:
            return httpx.Response(self.files_status, json={
                "error": {"message": "nope", "errors": [{"reason": "insufficientPermissions"}]}})
        return httpx.Response(200, json={"files": []})


@pytest.fixture
def token_file(tmp_path):
    p = tmp_path / "drive_token.json"
    p.write_text(json.dumps({"client_id": "cid", "client_secret": "sec", "refresh_token": "rt"}), encoding="utf-8")
    return p


def make_gate(token_file, recorder, violations=None):
    hook = violations.append if violations is not None else None
    return DriveGate(token_file, transport=httpx.MockTransport(recorder), on_violation=hook)


# ── 允許清單：被禁止的請求 ──────────────────────────────

FORBIDDEN = [
    ("DELETE", f"{DRIVE}/files/abc123", {}, None, "刪除檔案"),
    ("DELETE", f"{DRIVE}/files/trash", {}, None, "清空垃圾桶"),
    ("PATCH", f"{DRIVE}/files/abc123", {}, {"trashed": True}, "丟垃圾桶"),
    ("PATCH", f"{DRIVE}/files/abc123", {"addParents": "x", "removeParents": "y"}, {}, "搬移"),
    ("PATCH", f"{DRIVE}/files/abc123", {}, {"name": "改名"}, "改名"),
    ("PUT", f"{DRIVE}/files/abc123", {}, None, "覆寫"),
    ("POST", f"{DRIVE}/files/abc123/copy", {}, {}, "複製"),
    ("POST", f"{DRIVE}/files/abc123/permissions", {}, {}, "改分享權限"),
    ("POST", f"{DRIVE}/files", {}, {"name": "x", "mimeType": FOLDER_MIME, "parents": ["p"], "trashed": True},
     "新建時夾帶 trashed"),
    ("POST", f"{DRIVE}/files", {}, {"name": "x", "mimeType": "image/png", "parents": ["p"]}, "用新建網址建非資料夾"),
    ("POST", f"{DRIVE}/files", {}, {"name": "x", "mimeType": FOLDER_MIME, "parents": ["p", "q"]}, "兩個上層"),
    ("POST", f"{DRIVE}/files", {"removeParents": "y"}, {"name": "x", "mimeType": FOLDER_MIME, "parents": ["p"]},
     "新建時夾帶 removeParents"),
    ("GET", f"{DRIVE}/files", {"removeParents": "y"}, None, "查詢夾帶不明參數"),
    ("GET", f"{DRIVE}/files/abc123/export", {}, None, "未列名的子路徑"),
    ("GET", f"{DRIVE}/files?addParents=x", {}, None, "參數藏在網址裡"),
    ("POST", f"{UPLOAD}/files", {"uploadType": "resumable"}, None, "尚未開放的上傳方式"),
    ("PATCH", f"{UPLOAD}/files/abc123", {"uploadType": "multipart"}, None, "用上傳網址覆寫既有檔案"),
    ("GET", "https://evil.example.com/drive/v3/files", {}, None, "其他網站"),
]


@pytest.mark.parametrize("method,url,params,body,why", FORBIDDEN, ids=[f[-1] for f in FORBIDDEN])
def test_forbidden_requests_are_blocked(method, url, params, body, why):
    with pytest.raises(FenceViolation):
        check_request(method, url, params, body)


@pytest.mark.parametrize("method,url,params,body,why", FORBIDDEN, ids=[f[-1] for f in FORBIDDEN])
def test_forbidden_requests_never_reach_network(token_file, method, url, params, body, why):
    rec = Recorder()
    violations: list = []
    gate = make_gate(token_file, rec, violations)
    with pytest.raises(FenceViolation):
        gate._send(method, url, params=params, json_body=body)
    assert rec.requests == []          # 連換權杖都沒有，完全沒送出
    assert len(violations) == 1        # 有通知（階段 1 用來進維修模式）


def test_gate_has_no_destructive_methods():
    """對外的函式清單裡不可以出現刪除、搬移、修改類的動作。"""
    public = {n for n in dir(DriveGate) if not n.startswith("_")}
    assert public == {"about", "get_file", "list_files", "create_folder", "upload_small", "close"}


# ── 允許的動作照常運作 ─────────────────────────────────

ALLOWED = [
    ("GET", f"{DRIVE}/about", {"fields": "user"}, None),
    ("GET", f"{DRIVE}/files", {"q": "x", "fields": "files(id)", "pageSize": 10}, None),
    ("GET", f"{DRIVE}/files/AbC-12_3", {"fields": "id,md5Checksum"}, None),
    ("POST", f"{DRIVE}/files", {"fields": "id"}, {"name": "2026-10-11測試", "mimeType": FOLDER_MIME, "parents": ["p"]}),
    ("POST", f"{UPLOAD}/files", {"uploadType": "multipart", "fields": "id"}, None),
    ("POST", TOKEN_URL, {}, None),
]


@pytest.mark.parametrize("method,url,params,body", ALLOWED)
def test_allowed_requests_pass(method, url, params, body):
    check_request(method, url, params, body)


def test_list_files_sends_bearer_token(token_file):
    rec = Recorder()
    gate = make_gate(token_file, rec)
    gate.list_files("trashed = false")
    gate.list_files("trashed = false")
    assert rec.token_calls == 1  # 權杖快取，不是每次都換
    drive_reqs = [r for r in rec.requests if r.url.path == "/drive/v3/files"]
    assert len(drive_reqs) == 2
    assert drive_reqs[0].headers["Authorization"] == "Bearer tok1"


def test_401_refreshes_token_once_and_retries(token_file):
    calls = {"n": 0}

    def handler(request):
        if str(request.url) == TOKEN_URL:
            calls["n"] += 1
            return httpx.Response(200, json={"access_token": f"tok{calls['n']}", "expires_in": 3600})
        if request.headers["Authorization"] == "Bearer tok1":
            return httpx.Response(401, json={"error": {"message": "expired", "errors": [{"reason": "authError"}]}})
        return httpx.Response(200, json={"files": [{"id": "1"}]})

    gate = DriveGate(token_file, transport=httpx.MockTransport(handler))
    assert gate.list_files("x")["files"] == [{"id": "1"}]
    assert calls["n"] == 2


def test_drive_error_carries_reason(token_file):
    gate = make_gate(token_file, Recorder(files_status=403))
    with pytest.raises(DriveError) as ei:
        gate.list_files("x")
    assert ei.value.status == 403
    assert ei.value.reason == "insufficientPermissions"


def test_create_folder_body(token_file):
    rec = Recorder()
    gate = make_gate(token_file, rec)
    gate.create_folder("2026-10-11測試", "parent1")
    req = rec.requests[-1]
    assert req.method == "POST" and req.url.path == "/drive/v3/files"
    assert json.loads(req.content) == {"name": "2026-10-11測試", "mimeType": FOLDER_MIME, "parents": ["parent1"]}


def test_upload_small_multipart_body(token_file):
    rec = Recorder()
    gate = make_gate(token_file, rec)
    gate.upload_small("照片.png", "parent1", b"\x89PNGDATA", "image/png")
    req = rec.requests[-1]
    assert req.url.params["uploadType"] == "multipart"
    assert req.headers["Content-Type"].startswith("multipart/related; boundary=")
    assert '"parents": ["parent1"]'.encode() in req.content
    assert "照片.png".encode("utf-8") in req.content
    assert b"\x89PNGDATA" in req.content


def test_upload_small_rejects_large_files(token_file):
    gate = make_gate(token_file, Recorder())
    with pytest.raises(ValueError):
        gate.upload_small("big.bin", "p", b"0" * (drive_gate.SMALL_UPLOAD_MAX_BYTES + 1), "application/octet-stream")
