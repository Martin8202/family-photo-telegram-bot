"""
成員清單讀寫邏輯（見規格書 §5）。

members.json 結構：
{
  "123456789": {
    "name": "秀琴",
    "telegram_id": 123456789,
    "joined_at": "2026-07-22 10:30",
    "status": "待審核",              # 待審核 / 已開通 / 已拒絕
    "recent_folders": [               # 這個人最近用過的資料夾；畫面上合併全家顯示（V2 D19）
      {"name": "阿嬤生日", "used_at": "2026-07-22 10:30"}   # name＝相對 `照片` 的路徑
    ],
    "warned_compressed_this_session": false
  },
  ...
}

所有寫入經由 writequeue 的單一背景執行緒 + 原子寫入，避免併發衝突與寫壞檔案。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from writequeue import WriteQueue, atomic_write_text, default_write_queue

STATUS_PENDING = "待審核"
STATUS_APPROVED = "已開通"
STATUS_REJECTED = "已拒絕"


@dataclass
class Member:
    name: str
    telegram_id: int
    joined_at: str
    status: str
    recent_folders: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class MembersStore:
    """成員清單的讀寫入口。讀取為記憶體快取 + 檔案；寫入一律走佇列序列化。"""

    def __init__(self, path: Path, write_queue: Optional[WriteQueue] = None):
        self.path = Path(path)
        self._write_queue = write_queue or default_write_queue
        self._lock = threading.RLock()  # 保護記憶體快取的讀寫一致性
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        with self._lock:
            if self.path.exists():
                try:
                    self._data = json.loads(self.path.read_text(encoding="utf-8-sig"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self._data = {}
            else:
                self._data = {}

    def _persist(self) -> None:
        # 在呼叫當下的執行緒序列化成字串（快照），再丟給寫入佇列落地磁碟，
        # 避免背景執行緒執行時，記憶體資料又被其他呼叫修改造成不一致。
        snapshot = json.dumps(self._data, ensure_ascii=False, indent=2)

        def _write():
            atomic_write_text(self.path, snapshot)

        self._write_queue.submit(_write)

    # ── 查詢 ──────────────────────────────────────
    def get(self, telegram_id: int) -> Optional[Member]:
        with self._lock:
            row = self._data.get(str(telegram_id))
            if row is None:
                return None
            return Member(
                name=row["name"],
                telegram_id=row["telegram_id"],
                joined_at=row["joined_at"],
                status=row["status"],
                recent_folders=row.get("recent_folders", []),
            )

    def is_approved(self, telegram_id: int) -> bool:
        m = self.get(telegram_id)
        return m is not None and m.status == STATUS_APPROVED

    def list_all(self) -> list[Member]:
        with self._lock:
            return [self.get(int(k)) for k in self._data.keys()]

    def list_pending(self) -> list[Member]:
        return [m for m in self.list_all() if m.status == STATUS_PENDING]

    # ── 變更 ──────────────────────────────────────
    def register(self, telegram_id: int, name: str, now: Optional[datetime] = None) -> Member:
        """
        新登記或重新登記（被拒絕者可重新登記，見 §5.1）。
        一律將狀態設回「待審核」。
        """
        now = now or datetime.now()
        with self._lock:
            existing = self._data.get(str(telegram_id))
            recent_folders = existing.get("recent_folders", []) if existing else []
            self._data[str(telegram_id)] = {
                "name": name,
                "telegram_id": telegram_id,
                "joined_at": now.strftime("%Y-%m-%d %H:%M"),
                "status": STATUS_PENDING,
                "recent_folders": recent_folders,
            }
            self._persist()
            return self.get(telegram_id)

    def set_status(self, telegram_id: int, status: str) -> Optional[Member]:
        with self._lock:
            row = self._data.get(str(telegram_id))
            if row is None:
                return None
            row["status"] = status
            self._persist()
            return self.get(telegram_id)

    def approve(self, telegram_id: int) -> Optional[Member]:
        return self.set_status(telegram_id, STATUS_APPROVED)

    def reject(self, telegram_id: int) -> Optional[Member]:
        return self.set_status(telegram_id, STATUS_REJECTED)

    def push_recent_folder(
        self, telegram_id: int, folder_name: str, used_at: Optional[datetime] = None, keep: int = 5,
    ) -> None:
        """
        把資料夾記進這個人的最近紀錄（V2 D19）。同名資料夾再次使用時移到最前面，不重複列出。
        """
        used_at = used_at or datetime.now()
        with self._lock:
            row = self._data.get(str(telegram_id))
            if row is None:
                return
            folders = [f for f in row.get("recent_folders", []) if f["name"] != folder_name]
            folders.insert(0, {"name": folder_name, "used_at": used_at.strftime("%Y-%m-%d %H:%M")})
            row["recent_folders"] = folders[:keep]
            self._persist()

    def family_recent_folders(self, n: int = 5) -> list[dict]:
        """
        全家最近用過的 n 個資料夾（V2 D19）：合併每個人的紀錄，同一個資料夾只留最新一筆。
        媽媽剛傳過的資料夾，爸爸打開就排在最上面。
        """
        latest: dict[str, str] = {}
        with self._lock:
            for row in self._data.values():
                for f in row.get("recent_folders", []):
                    name, used_at = f.get("name"), f.get("used_at", "")
                    if name and used_at > latest.get(name, ""):
                        latest[name] = used_at
        ordered = sorted(latest.items(), key=lambda kv: kv[1], reverse=True)[:n]
        return [{"name": name, "used_at": used_at} for name, used_at in ordered]

    def migrate_recent_folders(self, folder_exists) -> list[str]:
        """
        V1 舊紀錄轉換（V2 規格 §3.2）：帶 `last_dest` 的是 V1 的紀錄，名稱在 `照片`
        底下找得到才保留（拿掉 `last_dest`），找不到的移除。回傳被移除的名稱，供寫 log。

        只處理舊格式，新紀錄一律不動，所以重跑幾次都一樣。呼叫端必須先確認
        `照片` 資料夾可用——`G:` 沒掛上時每個都會「找不到」而被誤刪。
        """
        dropped: list[str] = []
        with self._lock:
            changed = False
            for row in self._data.values():
                kept = []
                for f in row.get("recent_folders", []):
                    if "last_dest" not in f:
                        kept.append(f)
                        continue
                    changed = True
                    if folder_exists(f["name"]):
                        kept.append({"name": f["name"], "used_at": f.get("used_at", "")})
                    else:
                        dropped.append(f["name"])
                row["recent_folders"] = kept
            if changed:
                self._persist()
        return dropped
