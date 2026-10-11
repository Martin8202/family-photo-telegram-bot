"""
一次性授權：讓一位家人的 Google 帳號同意機器人存取 Google Drive，產生授權檔。

用法（在 photo-bot 資料夾執行）：
    python tools/authorize_drive.py --client-secret <下載的 client_secret.json> --out <授權檔路徑>
    （預設只申請「全部唯讀 ＋ 只能改機器人自己建立的檔案」；S1 實測不通過時才加 --full）

流程：開瀏覽器 → 家人登入並按同意 → Google 把授權碼送回本機 → 換成長期有效的
refresh token → 存成授權檔。

⚠️ 授權檔等同那個帳號雲端的鑰匙：不可上傳 GitHub、不可放進會同步的資料夾。
只用 Python 內建模組 ＋ httpx，不另外安裝套件。
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
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from drive_gate import DriveGate  # noqa: E402

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES_SAFE = "https://www.googleapis.com/auth/drive.readonly https://www.googleapis.com/auth/drive.file"
SCOPES_FULL = "https://www.googleapis.com/auth/drive"


def _wait_for_code(expected_state: str):
    """
    在本機隨機埠開一個一次性的小伺服器，等 Google 把授權碼送回來。
    回傳 (result, done, server)：result 先放 `_port`，收到回呼後填入 code/state/error。
    （這裡換授權碼不經過 drive_gate：那是 Google 登入的網址，不是 Drive。）
    """
    result: dict = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            qs = parse_qs(urlsplit(self.path).query)
            if "code" in qs or "error" in qs:
                result.update({k: v[0] for k, v in qs.items()})
                ok = "code" in qs and qs.get("state", [""])[0] == expected_state
                msg = "授權完成，可以關閉這個分頁，回到程式視窗。" if ok else "授權沒有完成，請回到程式視窗看說明。"
                body = f"<html><meta charset='utf-8'><body style='font-size:22px'>{msg}</body></html>".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
                done.set()
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    result["_port"] = port
    return result, done, server


def main() -> int:
    ap = argparse.ArgumentParser(description="一次性 Google Drive 授權")
    ap.add_argument("--client-secret", required=True, help="從 Google Cloud 下載的 OAuth 用戶端 JSON")
    ap.add_argument("--out", required=True, help="授權檔要存到哪裡（不要放在 OneDrive、不要放進 git）")
    ap.add_argument("--full", action="store_true", help="申請完整 Drive 權限（只有 S1 實測不通過時才用）")
    args = ap.parse_args()

    client = json.loads(Path(args.client_secret).read_text(encoding="utf-8"))
    client = client.get("installed") or client.get("web") or client
    client_id, client_secret = client["client_id"], client["client_secret"]
    scopes = SCOPES_FULL if args.full else SCOPES_SAFE

    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()

    result, done, server = _wait_for_code(state)
    redirect_uri = f"http://127.0.0.1:{result.pop('_port')}"
    url = AUTH_URL + "?" + urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scopes,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent select_account",  # 一定讓使用者挑帳號，避免直接用到電腦上已登入的別的帳號
    })

    print("\n即將打開瀏覽器。請**選擇要授權的那位家人的 Google 帳號**，看到「Google 尚未驗證這個應用程式」")
    print("時按「進階」→「前往（不安全）」，再按「繼續」同意。")
    print(f"\n瀏覽器沒有自動打開的話，請手動複製這個網址：\n{url}\n")
    webbrowser.open(url)

    if not done.wait(timeout=600):
        server.shutdown()
        print("10 分鐘內沒有完成授權，請重新執行。")
        return 1
    server.shutdown()

    if "error" in result:
        print(f"授權被拒絕或失敗：{result['error']}")
        return 1
    if result.get("state") != state:
        print("安全檢查失敗（state 不符），請重新執行。")
        return 1

    resp = httpx.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "code": result["code"],
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }, timeout=60)
    if resp.status_code != 200:
        print(f"換取權杖失敗：{resp.status_code} {resp.text[:300]}")
        return 1
    tok = resp.json()
    if "refresh_token" not in tok:
        print("Google 沒有給長期權杖（refresh token）。請到 Google 帳戶的「第三方應用程式」移除這個應用程式後重跑。")
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": tok["refresh_token"],
        "scope": tok.get("scope", scopes),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    gate = DriveGate(out)
    user = gate.about().get("user", {})
    gate.close()
    print(f"\n✅ 授權完成，授權檔已存到：{out}")
    print(f"   授權的帳號：{user.get('displayName', '?')}（{user.get('emailAddress', '?')}）")
    print(f"   取得的權限：{tok.get('scope', scopes)}")
    print("   請確認上面的帳號是那位家人的帳號；不是的話刪掉授權檔重跑。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
