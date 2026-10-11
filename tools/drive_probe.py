"""
S1 實測（V2 實作計畫 §1.2）：用家人的授權檔，實際測一次 Drive，回答三個問題：

1. 「全部唯讀 ＋ 只能改自己建立的檔案」這組權限，能不能把檔案放進**既有的**資料夾、
   能不能在既有資料夾底下**新建**資料夾？（決定 Q2／D12：兩層保護能不能都有）
2. `!家裡硬碟` 底下有多少資料夾？「一次抓完所有資料夾、在記憶體裡排樹」可不可行？
3. Drive 會不會給檔案的 md5Checksum？（重複照片比對要用）

所有請求都經過 drive_gate 柵欄，**這支腳本不可能刪除或搬移任何東西**。

用法（在 photo-bot 資料夾執行）：
    python tools/drive_probe.py --token <授權檔> --root-id <!家裡硬碟 ID> --photos-id <照片 ID> --existing-id <既有資料夾 ID>
    先不加 --write 跑一次（只讀）；確認沒問題再加 --write（會新增 1 個資料夾、2 張小圖，之後要自己刪）
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import time
import zlib
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from drive_gate import FOLDER_MIME, DriveError, DriveGate  # noqa: E402

QUARANTINE_NAME = "_重複檔_隔離區"
TEST_FOLDER_NAME = "_機器人測試_可刪除"
TEST_FILE_NAME = "_機器人測試_可刪除.png"


def tiny_png() -> bytes:
    """1×1 的 PNG，只用標準庫產生。"""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\x99\x00")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def summarize_folders(folders: list[dict], root_id: str, quarantine_name: str = QUARANTINE_NAME) -> dict:
    """
    把「帳號看得到的所有資料夾」排成樹，算出 root 底下有幾個、其中幾個在隔離區。
    folders: [{"id", "name", "parents": [...]}]
    """
    parent_of = {f["id"]: (f.get("parents") or [None])[0] for f in folders}
    name_of = {f["id"]: f["name"] for f in folders}
    quarantine_ids = {fid for fid, n in name_of.items() if n == quarantine_name and parent_of.get(fid) == root_id}

    under_root = 0
    in_quarantine = 0
    for fid in parent_of:
        seen = set()
        cur = parent_of[fid]
        hit_quarantine = fid in quarantine_ids
        while cur and cur not in seen:
            if cur in quarantine_ids:
                hit_quarantine = True
            if cur == root_id:
                under_root += 1
                in_quarantine += hit_quarantine
                break
            seen.add(cur)
            cur = parent_of.get(cur)
    return {
        "visible_total": len(folders),
        "under_root": under_root,
        "in_quarantine": in_quarantine,
        "usable_under_root": under_root - in_quarantine,
    }


def verdict(report: dict) -> list[str]:
    """依實測結果給白話結論。"""
    lines = []
    root = report.get("root", {})
    if root.get("error"):
        return [f"❌ 讀不到 !家裡硬碟（{root['error']}）：確認 ID 正確，且這個帳號有被共用到。"]
    if not root.get("canAddChildren"):
        lines.append("❌ 這個帳號對 !家裡硬碟 只有「檢視」權限 → 請把 !家裡硬碟 共用給他時改成「編輯者」，否則不管授權什麼都上傳不了。")

    tree = report.get("tree", {})
    if tree.get("error"):
        lines.append(f"⚠️ 一次抓全部資料夾失敗（{tree['error']}），資料夾快取要改做法。")
    elif tree.get("direct_children_missing"):
        lines.append(f"⚠️ 一次抓全部資料夾的做法漏了 {tree['direct_children_missing']} 個直接子資料夾，快取要改成逐層抓。")
    elif tree:
        lines.append(f"✅ 資料夾快取可行：!家裡硬碟 底下 {tree['under_root']} 個資料夾"
                     f"（隔離區 {tree['in_quarantine']} 個、可用 {tree['usable_under_root']} 個），"
                     f"抓一次 {tree['pages']} 頁、{tree['seconds']} 秒。")

    md5 = report.get("md5", {})
    if md5.get("ok"):
        lines.append("✅ Drive 有提供 md5Checksum，重複照片比對可以不用下載。")
    elif md5:
        lines.append(f"⚠️ 沒拿到 md5Checksum（{md5.get('error', '找不到檔案可測')}）。")

    writes = report.get("writes")
    if writes is None:
        lines.append("ℹ️ 這次沒有加 --write，還沒測新增；確認上面沒問題後加 --write 再跑一次。")
    else:
        ok_new = writes["create_folder"].get("ok")
        ok_existing = writes["upload_into_existing"].get("ok")
        if ok_new and ok_existing:
            lines.append("✅ S1 通過：在既有資料夾底下新建、放進既有資料夾都可以 → 採用兩層保護（唯讀＋只改自己建立的）。")
        else:
            failed = [k for k, v in writes.items() if not v.get("ok")]
            lines.append(f"❌ S1 未通過（失敗：{', '.join(failed)}）→ 依 D12 改申請完整 drive 權限，家人要重新授權一次。")
        lines.append(f"🧹 請自己到 Drive 刪掉測試留下的「{TEST_FOLDER_NAME}」資料夾與「{TEST_FILE_NAME}」（機器人不能刪）。")
    return lines


def _try(fn):
    try:
        return {"ok": True, **fn()}
    except DriveError as exc:
        return {"ok": False, "error": f"{exc.status} {exc.reason}: {exc.message}"}


def run(gate: DriveGate, root_id: str, photos_id: str, existing_id: str, write: bool) -> dict:
    report: dict = {"time": datetime.now().isoformat(timespec="seconds")}
    report["account"] = gate.about().get("user", {})

    fields = "id,name,mimeType,ownedByMe,capabilities(canAddChildren)"
    for key, fid in (("root", root_id), ("photos", photos_id), ("existing", existing_id)):
        r = _try(lambda fid=fid: gate.get_file(fid, fields))
        if r["ok"]:
            r["canAddChildren"] = r.pop("capabilities", {}).get("canAddChildren")
        report[key] = r
    if not report["root"]["ok"]:
        return report

    # 資料夾快取做法：一次抓「看得到的所有資料夾」再排樹
    try:
        t0 = time.time()
        folders, token, pages = [], None, 0
        while True:
            page = gate.list_files(f"mimeType = '{FOLDER_MIME}' and trashed = false",
                                   fields="nextPageToken,files(id,name,parents)", page_token=token)
            folders += page.get("files", [])
            pages += 1
            token = page.get("nextPageToken")
            if not token:
                break
        tree = summarize_folders(folders, root_id)
        tree.update(pages=pages, seconds=round(time.time() - t0, 1))
        # 對照組：逐層抓 root 與「照片」的直接子資料夾，確認上面的做法沒有漏
        known = {f["id"] for f in folders}
        missing = 0
        for pid in (root_id, photos_id):
            direct = gate.list_files(f"'{pid}' in parents and mimeType = '{FOLDER_MIME}' and trashed = false",
                                     fields="files(id)").get("files", [])
            missing += sum(1 for f in direct if f["id"] not in known)
        tree["direct_children_missing"] = missing
        report["tree"] = tree
        first_album = next((f for f in folders if photos_id in (f.get("parents") or [])), None)
    except DriveError as exc:
        report["tree"] = {"error": f"{exc.status} {exc.reason}: {exc.message}"}
        first_album = None

    # md5Checksum：在「照片」底下第一個相簿裡找幾個檔案
    if first_album:
        r = _try(lambda: gate.list_files(f"'{first_album['id']}' in parents and mimeType != '{FOLDER_MIME}' and trashed = false",
                                         fields="files(name,md5Checksum,size)", page_size=3))
        if r["ok"]:
            sample = r.get("files", [])
            r = {"ok": any(f.get("md5Checksum") for f in sample), "album": first_album["name"], "sample": sample}
        report["md5"] = r
    else:
        report["md5"] = {"ok": False, "error": "照片底下找不到相簿"}

    if write:
        png = tiny_png()
        writes = {"create_folder": _try(lambda: gate.create_folder(TEST_FOLDER_NAME, photos_id))}
        if writes["create_folder"]["ok"]:
            new_id = writes["create_folder"]["id"]
            writes["upload_into_new"] = _try(lambda: gate.upload_small(TEST_FILE_NAME, new_id, png, "image/png"))
        else:
            writes["upload_into_new"] = {"ok": False, "error": "資料夾沒建成，略過"}
        writes["upload_into_existing"] = _try(lambda: gate.upload_small(TEST_FILE_NAME, existing_id, png, "image/png"))
        report["writes"] = writes
    else:
        report["writes"] = None
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="S1 實測：Drive 權限與資料夾數量")
    ap.add_argument("--token", required=True)
    ap.add_argument("--root-id", required=True, help="!家裡硬碟 的資料夾 ID")
    ap.add_argument("--photos-id", required=True, help="!家裡硬碟\\照片 的資料夾 ID")
    ap.add_argument("--existing-id", required=True, help="一個不重要的既有資料夾 ID（測試放進既有資料夾）")
    ap.add_argument("--write", action="store_true", help="實際新增測試資料夾與小圖（之後要自己刪）")
    ap.add_argument("--report", help="報告 JSON 存放位置（預設與授權檔同一個資料夾）")
    args = ap.parse_args()

    gate = DriveGate(Path(args.token))
    try:
        report = run(gate, args.root_id, args.photos_id, args.existing_id, args.write)
    finally:
        gate.close()
    report["verdict"] = verdict(report)

    out = Path(args.report) if args.report else Path(args.token).with_name("s1_report.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    acct = report.get("account", {})
    print(f"帳號：{acct.get('displayName')}（{acct.get('emailAddress')}）")
    for line in report["verdict"]:
        print(line)
    print(f"\n完整報告：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
