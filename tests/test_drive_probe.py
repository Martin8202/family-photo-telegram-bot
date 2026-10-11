"""S1 實測腳本（tools/drive_probe.py）的測試：樹狀統計、白話結論、整段流程（假的 Google，不連網）。"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import httpx
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import drive_probe  # noqa: E402
from drive_gate import DriveGate  # noqa: E402

ROOT, PHOTOS, QUAR = "root1", "photos1", "quar1"
FOLDERS = [
    {"id": ROOT, "name": "!家裡硬碟", "parents": ["mydrive"]},
    {"id": PHOTOS, "name": "照片", "parents": [ROOT]},
    {"id": "a1", "name": "2026-07-02金門泉州之旅", "parents": [PHOTOS]},
    {"id": "a2", "name": "2007-7-2~8金門旅遊", "parents": [PHOTOS]},
    {"id": QUAR, "name": "_重複檔_隔離區", "parents": [ROOT]},
    {"id": "q1", "name": "照片", "parents": [QUAR]},
    {"id": "q2", "name": "2007-7-2~8金門旅遊", "parents": ["q1"]},
    {"id": "other", "name": "家人自己的資料夾", "parents": ["mydrive"]},
]


def test_summarize_folders_counts_and_excludes_quarantine():
    s = drive_probe.summarize_folders(FOLDERS, ROOT)
    assert s == {"visible_total": 8, "under_root": 6, "in_quarantine": 3, "usable_under_root": 3}


def test_summarize_folders_survives_parent_cycle():
    looped = [{"id": "x", "name": "x", "parents": ["y"]}, {"id": "y", "name": "y", "parents": ["x"]}]
    assert drive_probe.summarize_folders(looped, ROOT)["under_root"] == 0


def test_tiny_png_is_valid_image():
    img = Image.open(io.BytesIO(drive_probe.tiny_png()))
    img.load()
    assert img.size == (1, 1)


def _report(**over):
    base = {
        "root": {"ok": True, "canAddChildren": True},
        "tree": {"under_root": 6, "in_quarantine": 3, "usable_under_root": 3, "pages": 1, "seconds": 0.1,
                 "direct_children_missing": 0},
        "md5": {"ok": True},
        "writes": None,
    }
    base.update(over)
    return base


def test_verdict_read_only_run_asks_for_write():
    text = "\n".join(drive_probe.verdict(_report()))
    assert "沒有加 --write" in text and "資料夾快取可行" in text


def test_verdict_viewer_permission_is_called_out():
    text = "\n".join(drive_probe.verdict(_report(root={"ok": True, "canAddChildren": False})))
    assert "編輯者" in text


def test_verdict_pass_and_fail():
    ok = {"create_folder": {"ok": True}, "upload_into_new": {"ok": True}, "upload_into_existing": {"ok": True}}
    assert "S1 通過" in "\n".join(drive_probe.verdict(_report(writes=ok)))
    bad = dict(ok, upload_into_existing={"ok": False, "error": "403"})
    text = "\n".join(drive_probe.verdict(_report(writes=bad)))
    assert "S1 未通過" in text and "upload_into_existing" in text and "自己到 Drive 刪掉" in text


def test_run_end_to_end_with_fake_google(tmp_path):
    token = tmp_path / "t.json"
    token.write_text(json.dumps({"client_id": "c", "client_secret": "s", "refresh_token": "r"}), encoding="utf-8")
    sent = []

    def handler(req: httpx.Request):
        sent.append((req.method, req.url.path))
        if req.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        path, q = req.url.path, req.url.params.get("q", "")
        if path.endswith("/about"):
            return httpx.Response(200, json={"user": {"displayName": "媽媽", "emailAddress": "mom@example.com"}})
        if path.startswith("/upload/"):
            return httpx.Response(200, json={"id": "f1", "name": "x.png"})
        if req.method == "POST":
            return httpx.Response(200, json={"id": "newfolder", "name": "x"})
        if path.startswith("/drive/v3/files/"):
            return httpx.Response(200, json={"id": "x", "name": "x", "capabilities": {"canAddChildren": True}})
        if "in parents and mimeType != " in q:
            return httpx.Response(200, json={"files": [{"name": "a.jpg", "md5Checksum": "abc", "size": "10"}]})
        if "in parents" in q:
            parent = q.split("'")[1]
            return httpx.Response(200, json={"files": [{"id": f["id"]} for f in FOLDERS if parent in f["parents"]]})
        return httpx.Response(200, json={"files": FOLDERS})

    gate = DriveGate(token, transport=httpx.MockTransport(handler))
    report = drive_probe.run(gate, ROOT, PHOTOS, "a2", write=True)

    assert report["account"]["emailAddress"] == "mom@example.com"
    assert report["tree"]["usable_under_root"] == 3 and report["tree"]["direct_children_missing"] == 0
    assert report["md5"]["ok"]
    assert all(v["ok"] for v in report["writes"].values())
    # 整段流程只用了允許的方法：沒有 DELETE / PATCH / PUT
    assert {m for m, _ in sent} <= {"GET", "POST"}
    assert "S1 通過" in "\n".join(drive_probe.verdict(report))
