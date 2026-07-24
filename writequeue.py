"""
併發寫入控制（見規格書 §10C）。

所有對 data/ 底下檔案（members.json、upload_log.csv、待清理清單.csv、
file_index.csv）的寫入，一律經由這裡的單一背景執行緒依序執行，
並以「寫暫存檔 + os.replace()」達成原子寫入。只用標準庫，不加外部依賴。
"""

from __future__ import annotations

import os
import queue
import threading
import uuid
from pathlib import Path
from typing import Callable


class WriteQueue:
    """單一背景執行緒依序執行寫入任務，天然序列化，避免併發衝突。"""

    def __init__(self) -> None:
        self._queue: "queue.Queue[tuple[Callable[[], None], threading.Event, list]]" = queue.Queue()
        self._thread = threading.Thread(target=self._worker, daemon=True, name="write-queue")
        self._stopped = False
        self._thread.start()

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                break
            func, done_event, errors = item
            try:
                func()
            except Exception as exc:  # noqa: BLE001 - 記錄後續由呼叫端決定如何處理
                errors.append(exc)
            finally:
                done_event.set()

    def submit(self, func: Callable[[], None], wait: bool = True, timeout: float | None = 30) -> None:
        """把寫入任務丟進佇列。預設等待完成，讓呼叫端能得知是否成功。"""
        done_event = threading.Event()
        errors: list[Exception] = []
        self._queue.put((func, done_event, errors))
        if wait:
            done_event.wait(timeout=timeout)
            if errors:
                raise errors[0]

    def shutdown(self) -> None:
        if not self._stopped:
            self._stopped = True
            self._queue.put(None)
            self._thread.join(timeout=5)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """先寫暫存檔，完成後以 os.replace() 原子性地取代目標檔。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    with open(tmp_path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8-sig") -> None:
    atomic_write_bytes(path, text.encode(encoding))


def append_bytes(path: Path, data: bytes) -> None:
    """
    以 append 模式在檔尾追加資料（給 append-only 的 CSV 紀錄檔用）。

    為什麼不用 atomic_write_bytes：紀錄檔（file_index.csv 等）只會不斷新增列，
    若每次都「整份讀進來 + 整份寫回」，檔案長到數萬列時每寫一列都要全檔讀寫，
    成本會隨列數線性上升（整體 O(n²)）。改用單次 append 為 O(1)。
    代價是崩潰時最壞情況只會在檔尾留下一行殘缺（CSV 讀取器可容忍/略過），
    遠比「整份重寫到一半損毀」輕微。所有 append 仍經由單一寫入佇列序列化，
    併發安全不受影響（§10C）。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


# 全域單例：整個程式共用同一條寫入佇列（同一支程式內的所有資料檔）。
default_write_queue = WriteQueue()
