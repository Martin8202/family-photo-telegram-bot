"""
Google Drive 柵欄 —— 全程式**唯一**能連 Google Drive 的地方（V2 規格 §4）。

為什麼要有它：機器人用的是一位家人的 Google 授權，那把「鑰匙」在技術上能刪除、
搬移該帳號雲端裡的任何檔案。我們的程式本來就不做這些事，但萬一程式有 bug、
之後改錯、或電腦被動過，後果就是全家的照片不見。柵欄確保那種請求根本送不出去。

設計：
- 對外只提供 5 個動作：`about`、`list_files`、`get_file`、`create_folder`、
  `upload_small`。**沒有任何刪除、搬移、修改的函式**。
- 所有請求最後都經過 `_send()`，它先用 `check_request()` 比對**允許清單**
  （方法＋網址＋參數＋內容）。不在清單上的一律擋下、丟 `FenceViolation`，
  請求不會送出。用允許清單而不是禁止清單：Drive 的「移動」「丟垃圾桶」藏在
  修改請求的參數裡（`removeParents`、`trashed`），禁止清單容易漏。
- 觸發時呼叫 `on_violation`（階段 1 用它進入維修模式，規格 D18）。

只用 `httpx`（python-telegram-bot 本來就帶的套件），不加 Google 官方函式庫。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

import httpx

TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"

# 小檔上傳（multipart）的上限；Google 建議 5 MB 以下才用這種方式，更大的檔案
# 在階段 1 改用續傳式上傳（resumable）。
SMALL_UPLOAD_MAX_BYTES = 5 * 1024 * 1024

_READ_PARAMS = {
    "q", "fields", "pageSize", "pageToken", "orderBy", "spaces", "corpora",
    "includeItemsFromAllDrives", "supportsAllDrives",
}
_CREATE_PARAMS = {"fields", "supportsAllDrives"}
_UPLOAD_PARAMS = {"uploadType", "fields", "supportsAllDrives"}
_CREATE_BODY_KEYS = {"name", "mimeType", "parents"}


class FenceViolation(PermissionError):
    """請求不在允許清單上，已被擋下、沒有送出。"""


class DriveError(RuntimeError):
    """Drive 回應錯誤（請求本身是允許的，只是失敗了，例如沒有權限、找不到）。"""

    def __init__(self, status: int, reason: str, message: str):
        super().__init__(f"{status} {reason}: {message}")
        self.status = status
        self.reason = reason
        self.message = message


def _is_file_id(segment: str) -> bool:
    # Drive 的檔案 ID 只含英數、底線、連字號；這樣也順便擋掉 "files/x/copy" 之類的子路徑
    return bool(segment) and all(c.isalnum() or c in "-_" for c in segment)


def check_request(method: str, url: str, params: dict, json_body: Optional[dict] = None) -> None:
    """
    允許清單。通過就什麼都不做；不通過丟 `FenceViolation`。

    純函式、不連網，方便測試每一種被禁止的請求。
    """
    method = method.upper()
    parts = urlsplit(url)
    if parts.query:
        raise FenceViolation(f"網址不可夾帶參數，參數要分開傳：{url}")
    base = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/")
    keys = set(params)

    def deny(why: str):
        raise FenceViolation(f"{method} {url} 被擋下：{why}")

    if method == "POST" and url == TOKEN_URL:
        return  # 換取存取權杖（不是 Drive 操作）

    if base != "https://www.googleapis.com":
        deny("不是 Google Drive 的網址")

    if method == "GET":
        if path == "/drive/v3/about":
            if keys - {"fields"}:
                deny(f"不允許的參數 {sorted(keys - {'fields'})}")
            return
        if path == "/drive/v3/files" or (
            path.startswith("/drive/v3/files/") and _is_file_id(path[len("/drive/v3/files/"):])
        ):
            if keys - _READ_PARAMS:
                deny(f"不允許的參數 {sorted(keys - _READ_PARAMS)}")
            return
        deny("不在允許的查詢清單")

    if method == "POST" and path == "/drive/v3/files":
        # 只允許「新建資料夾」：內容只能有名稱、類型、上層，類型必須是資料夾
        if keys - _CREATE_PARAMS:
            deny(f"不允許的參數 {sorted(keys - _CREATE_PARAMS)}")
        body = json_body or {}
        if set(body) - _CREATE_BODY_KEYS:
            deny(f"新建內容含不允許的欄位 {sorted(set(body) - _CREATE_BODY_KEYS)}")
        if body.get("mimeType") != FOLDER_MIME:
            deny("這個網址只允許新建資料夾")
        if len(body.get("parents") or []) != 1:
            deny("新建資料夾必須指定剛好一個上層資料夾")
        return

    if method == "POST" and path == "/upload/drive/v3/files":
        if keys - _UPLOAD_PARAMS:
            deny(f"不允許的參數 {sorted(keys - _UPLOAD_PARAMS)}")
        if params.get("uploadType") != "multipart":
            deny("目前只允許 multipart 小檔上傳")
        return

    deny("不在允許清單（刪除、搬移、修改、複製一律不允許）")


class DriveGate:
    def __init__(
        self,
        token_path: Path,
        transport: Optional[httpx.BaseTransport] = None,
        on_violation: Optional[Callable[[FenceViolation], None]] = None,
        timeout: float = 60,
    ):
        self._token_path = Path(token_path)
        creds = json.loads(self._token_path.read_text(encoding="utf-8"))
        self._client_id = creds["client_id"]
        self._client_secret = creds.get("client_secret", "")
        self._refresh_token = creds["refresh_token"]
        self._access_token: Optional[str] = None
        self._expires_at = 0.0
        self._on_violation = on_violation
        self._http = httpx.Client(transport=transport, timeout=timeout)

    # ── 權杖 ──────────────────────────────────────

    def _refresh(self) -> None:
        resp = self._send("POST", TOKEN_URL, data={
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": self._refresh_token,
            "grant_type": "refresh_token",
        }, auth=False)
        payload = resp.json()
        self._access_token = payload["access_token"]
        # 提早 60 秒視為過期，避免剛好在請求途中失效
        self._expires_at = time.time() + int(payload.get("expires_in", 3600)) - 60

    def _auth_header(self) -> dict:
        if not self._access_token or time.time() >= self._expires_at:
            self._refresh()
        return {"Authorization": f"Bearer {self._access_token}"}

    # ── 唯一的出口 ────────────────────────────────

    def _send(self, method: str, url: str, params: Optional[dict] = None, json_body: Optional[dict] = None,
              data: Optional[dict] = None, content: Optional[bytes] = None, headers: Optional[dict] = None,
              auth: bool = True) -> httpx.Response:
        params = dict(params or {})
        try:
            check_request(method, url, params, json_body)
        except FenceViolation as exc:
            if self._on_violation:
                self._on_violation(exc)
            raise

        for attempt in (1, 2):
            hdrs = dict(headers or {})
            if auth:
                hdrs.update(self._auth_header())
            resp = self._http.request(method, url, params=params or None, json=json_body,
                                      data=data, content=content, headers=hdrs)
            if resp.status_code == 401 and auth and attempt == 1:
                self._access_token = None  # 權杖可能被提早作廢，重新換一次再試
                continue
            break

        if resp.status_code >= 400:
            reason, message = "unknown", resp.text[:300]
            try:
                err = resp.json().get("error", {})
                if isinstance(err, dict):
                    message = err.get("message", message)
                    reason = (err.get("errors") or [{}])[0].get("reason", err.get("status", reason))
                else:  # 權杖端點的錯誤格式：{"error": "invalid_grant", ...}
                    reason, message = err, resp.json().get("error_description", message)
            except ValueError:
                pass
            raise DriveError(resp.status_code, reason, message)
        return resp

    # ── 允許的 5 個動作 ────────────────────────────

    def about(self, fields: str = "user(displayName,emailAddress)") -> dict:
        return self._send("GET", f"{DRIVE}/about", params={"fields": fields}).json()

    def get_file(self, file_id: str, fields: str = "id,name,mimeType,parents") -> dict:
        return self._send("GET", f"{DRIVE}/files/{file_id}",
                          params={"fields": fields, "supportsAllDrives": "true"}).json()

    def list_files(self, q: str, fields: str = "nextPageToken,files(id,name,mimeType,parents)",
                   page_size: int = 1000, page_token: Optional[str] = None, order_by: Optional[str] = None) -> dict:
        params = {"q": q, "fields": fields, "pageSize": page_size,
                  "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
        if page_token:
            params["pageToken"] = page_token
        if order_by:
            params["orderBy"] = order_by
        return self._send("GET", f"{DRIVE}/files", params=params).json()

    def create_folder(self, name: str, parent_id: str) -> dict:
        body = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
        return self._send("POST", f"{DRIVE}/files", params={"fields": "id,name,parents"},
                          json_body=body).json()

    def upload_small(self, name: str, parent_id: str, data: bytes, mime_type: str) -> dict:
        """上傳 5 MB 以下的檔案（multipart）。更大的檔案在階段 1 改用續傳式上傳。"""
        if len(data) > SMALL_UPLOAD_MAX_BYTES:
            raise ValueError(f"檔案 {len(data)} bytes 超過 multipart 建議上限，請改用續傳式上傳")
        boundary = f"photo-bot-{uuid.uuid4().hex}"
        metadata = json.dumps({"name": name, "parents": [parent_id]}, ensure_ascii=False).encode("utf-8")
        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
            + metadata
            + f"\r\n--{boundary}\r\nContent-Type: {mime_type}\r\n\r\n".encode()
            + data
            + f"\r\n--{boundary}--\r\n".encode()
        )
        return self._send(
            "POST", f"{UPLOAD}/files",
            params={"uploadType": "multipart", "fields": "id,name,parents,md5Checksum,size"},
            content=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"},
        ).json()

    def close(self) -> None:
        self._http.close()
