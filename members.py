"""
成員清單讀寫邏輯（見規格書 §5）。

members.json 結構：
{
  "123456789": {
    "name": "秀琴",
    "telegram_id": 123456789,
    "joined_at": "2026-07-22 10:30",
    "status": "待審核",              # 待審核 / 已開通 / 已拒絕
    "recent_folders": [               # 見 §6.2「近 3 次資料夾」，各使用者獨立
      {"name": "阿嬤生日", "last_dest": "家裡硬碟", "used_at": "..."}
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
class RecentFolder:
    name: str
    last_dest: str
    used_at: str


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
        self, telegram_id: int, folder_name: str, dest_label: str, used_at: Optional[datetime] = None,
        keep: int = 3,
    ) -> None:
        """
        更新「近 N 次資料夾」（各使用者獨立，不綁定目的地，僅記錄名稱 + 上次目的地供顯示，見 §6.2）。
        同名資料夾再次使用時移到最前面，不重複列出。
        """
        used_at = used_at or datetime.now()
        with self._lock:
            row = self._data.get(str(telegram_id))
            if row is None:
                return
            folders = [f for f in row.get("recent_folders", []) if f["name"] != folder_name]
            folders.insert(0, {
                "name": folder_name,
                "last_dest": dest_label,
                "used_at": used_at.strftime("%Y-%m-%d %H:%M"),
            })
            row["recent_folders"] = folders[:keep]
            self._persist()

    def get_recent_folders(self, telegram_id: int) -> list[dict]:
        m = self.get(telegram_id)
        return m.recent_folders if m else []
