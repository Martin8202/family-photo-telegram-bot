"""
資料夾清單與相近比對（V2 規格 §3.3、§3.4）。

- 只掃 `照片` 底下第 1、2 層、只列資料夾，結果存成 `data/folders.json`。
- 重掃時機：啟動時沒有 JSON，或有上傳完成後、全家 10 分鐘沒有任何動作。
- 掃描中讀到的一律是舊清單：新清單掃完才一次換掉（記憶體與檔案都是）。
- 比對：名稱包含 > 日期相符 > 名稱相似（difflib）；同級新的優先；最多 5 筆。

除了 `FolderIndex.rescan()` 會碰磁碟，其餘都是純函式，方便測試。
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from writequeue import atomic_write_text

logger = logging.getLogger("photo-bot.folders")

SEP = "\\"  # 相對路徑一律用反斜線，跟 members.json 的最近資料夾一致

# ── 日期正規化 ────────────────────────────────────────
# 7/2、0702、7月2日、2026-07-02、2026/7/2、20260702、2007-7-2~8 都要認得

_DATE_PATTERNS = [
    re.compile(r"(?P<y>(?:19|20)\d{2})[-/.](?P<m>\d{1,2})[-/.](?P<d>\d{1,2})(?:~\d{1,2})?"),
    re.compile(r"(?P<y>(?:19|20)\d{2})(?P<m>\d{2})(?P<d>\d{2})"),
    re.compile(r"(?:(?P<y>(?:19|20)\d{2})年)?(?P<m>\d{1,2})月(?P<d>\d{1,2})[日號]?"),
    re.compile(r"(?<!\d)(?P<m>\d{1,2})/(?P<d>\d{1,2})(?!\d)"),
    re.compile(r"(?<!\d)(?P<m>\d{2})(?P<d>\d{2})(?!\d)"),
]


def _valid(m: int, d: int) -> bool:
    return 1 <= m <= 12 and 1 <= d <= 31


def split_dates(text: str) -> tuple[list[tuple[Optional[int], int, int]], str]:
    """把文字拆成 (日期清單, 去掉日期後的名稱)。日期是 (年或 None, 月, 日)。"""
    dates = []
    rest = text
    for pat in _DATE_PATTERNS:
        def _take(mt):
            y = mt.groupdict().get("y")
            m, d = int(mt.group("m")), int(mt.group("d"))
            if not _valid(m, d):
                return mt.group(0)  # 不像日期（例如 2026 當成月日），原樣留著
            dates.append((int(y) if y else None, m, d))
            return " "
        rest = pat.sub(_take, rest)
    return dates, _norm(rest)


def _norm(text: str) -> str:
    return re.sub(r"[\s\-_~、，,.]+", "", text).lower()


def has_date(text: str) -> bool:
    return bool(split_dates(text)[0])


def _date_hit(q_dates, f_dates) -> bool:
    for qy, qm, qd in q_dates:
        for fy, fm, fd in f_dates:
            if qm == fm and qd == fd and (qy is None or fy is None or qy == fy):
                return True
    return False


# ── 比對 ───────────────────────────────────────────────

SIMILAR_THRESHOLD = 0.5


def match(query: str, folders: list[dict], limit: int = 5) -> list[dict]:
    """
    找相近的資料夾。folders 每筆至少有 name、path、mtime。
    排序：名稱包含 > 日期相符 > 名稱相似，同一級新的（mtime 大）排前面。
    """
    q_dates, q_text = split_dates(query or "")
    if not q_dates and not q_text:
        return []
    scored = []
    for f in folders:
        f_dates, f_text = split_dates(f.get("name", ""))
        contains = bool(q_text) and q_text in f_text
        date_hit = _date_hit(q_dates, f_dates)
        similar = bool(q_text) and difflib.SequenceMatcher(None, q_text, f_text).ratio() >= SIMILAR_THRESHOLD
        if contains or date_hit or similar:
            scored.append(((contains, date_hit, similar, f.get("mtime", 0)), f))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [f for _, f in scored[:limit]]


def location_text(path: str) -> str:
    """白話位置：第 1 層「（在『照片』裡）」，第 2 層「（在『照片』裡的『上層』裡）」。"""
    parts = path.split(SEP)
    if len(parts) <= 1:
        return "（在「照片」裡）"
    return f"（在「照片」裡的「{SEP.join(parts[:-1])}」裡）"


# ── 掃描與清單檔 ───────────────────────────────────────

def scan_folders(root: Path, depth: int = 2, exclude=()) -> list[dict]:
    """只列資料夾、不讀檔案，掃到第 depth 層。"""
    exclude = set(exclude)
    out: list[dict] = []

    def _walk(dir_path: Path, rel: list[str], level: int):
        try:
            entries = list(os.scandir(dir_path))
        except OSError as exc:
            logger.warning("掃描資料夾失敗：%s（%s）", dir_path, exc)
            return
        for e in entries:
            try:
                if not e.is_dir() or e.name in exclude or e.name.startswith("."):
                    continue
                mtime = e.stat().st_mtime
            except OSError:
                continue
            path = rel + [e.name]
            out.append({"name": e.name, "path": SEP.join(path), "mtime": mtime})
            if level < depth:
                _walk(Path(e.path), path, level + 1)

    _walk(Path(root), [], 1)
    return out


class FolderIndex:
    """資料夾清單：記憶體一份、`data/folders.json` 一份，掃完才一起換掉。"""

    def __init__(self, json_path: Path, root: Path, depth: int = 2, exclude=(), idle_min: int = 10):
        self.json_path = Path(json_path)
        self.root = Path(root)
        self.depth = depth
        self.exclude = tuple(exclude)
        self.idle = timedelta(minutes=idle_min)
        self.folders: list[dict] = []
        self.dirty = False
        self.scanning = False
        self.last_activity = datetime.min
        self._added: dict[str, dict] = {}  # 掃描期間新建的，掃完要補回去
        self._lock = threading.Lock()

    def load(self) -> bool:
        """讀 JSON；沒有或壞掉就標記要重掃（啟動時那一次）。"""
        try:
            data = json.loads(self.json_path.read_text(encoding="utf-8-sig"))
            self.folders = list(data["folders"])
            return True
        except FileNotFoundError:
            self.dirty = True
            return False
        except Exception as exc:  # noqa: BLE001 - 壞檔一律當作沒有，重掃一次
            logger.warning("folders.json 讀不到，會重掃：%s", exc)
            self.dirty = True
            return False

    def note_activity(self, now: Optional[datetime] = None) -> None:
        self.last_activity = now or datetime.now()

    def mark_upload_done(self) -> None:
        self.dirty = True

    def due(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now()
        return self.dirty and not self.scanning and (now - self.last_activity) >= self.idle

    def search(self, query: str, limit: int = 5) -> list[dict]:
        return match(query, self.folders, limit)

    def add_folder(self, path: str) -> None:
        """機器人新建的資料夾立刻進清單（不等重掃）。"""
        entry = {"name": path.split(SEP)[-1], "path": path, "mtime": datetime.now().timestamp()}
        with self._lock:
            if any(f["path"] == path for f in self.folders):
                return
            self.folders = self.folders + [entry]
            if self.scanning:
                self._added[path] = entry
            self._save(self.folders)

    def rescan(self) -> None:
        """同步執行（呼叫端放進背景執行緒）。失敗就保留舊清單，等下一次上傳再試。"""
        with self._lock:
            self.scanning = True
            self.dirty = False
            self._added = {}
        try:
            if not self.root.is_dir():
                logger.warning("找不到照片資料夾，這次不重掃：%s", self.root)
                return
            fresh = scan_folders(self.root, self.depth, self.exclude)
            with self._lock:
                seen = {f["path"] for f in fresh}
                fresh += [e for p, e in self._added.items() if p not in seen]
                self._save(fresh)
                self.folders = fresh
            logger.info("資料夾清單已更新：%s 個", len(fresh))
        except Exception:
            logger.exception("重掃資料夾清單失敗，沿用舊清單")
        finally:
            self.scanning = False

    def _save(self, folders: list[dict]) -> None:
        payload = {"scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "folders": folders}
        atomic_write_text(self.json_path, json.dumps(payload, ensure_ascii=False))
