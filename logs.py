"""
三種 CSV 紀錄檔的寫入邏輯（規格書 §10A、§10B、§16.1、§10C、§10D）：

- data/upload_log.csv      上傳紀錄表（誰、何時、哪個資料夾、幾張、結果）
- data/待清理清單.csv        中止殘留 / 傳錯更正需人工清理的檔名
- data/file_index.csv      逐張照片索引（含 Telegram file_id），供 redownload.py 使用

三者職責獨立、各自成檔，程式只會新增列，不會清空或刪除既有內容。
寫入一律經由 writequeue 的單一背景執行緒序列化 + 原子寫入，
併發安全（見 §10C、測試案例 F7）。
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Optional

from writequeue import WriteQueue, atomic_write_text, default_write_queue

UPLOAD_LOG_HEADER = ["時間", "上傳者", "Telegram ID", "資料夾", "目的地", "張數", "結果"]
CLEANUP_LIST_HEADER = ["時間", "上傳者", "Telegram ID", "類型", "待刪位置", "檔名", "備註"]
FILE_INDEX_HEADER = ["時間", "上傳者", "Telegram ID", "目標資料夾", "目的地", "檔名", "file_id"]


class CsvLog:
    """單一 CSV 檔的 append-only 寫入器。使用 BOM 的 utf-8，Excel 開啟中文不亂碼。"""

    def __init__(self, path: Path, header: list[str], write_queue: Optional[WriteQueue] = None):
        self.path = Path(path)
        self.header = header
        self._write_queue = write_queue or default_write_queue
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self.path, self._render([header]))

    def _render(self, rows: list[list[str]]) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\r\n")
        for row in rows:
            writer.writerow(row)
        return buf.getvalue()

    def append_row(self, row: list) -> None:
        self.append_rows([row])

    def append_rows(self, rows: list[list]) -> None:
        if not rows:
            return

        def _write():
            existing = ""
            if self.path.exists():
                existing = self.path.read_text(encoding="utf-8-sig")
            new_text = existing + self._render(rows)
            atomic_write_text(self.path, new_text)

        self._write_queue.submit(_write)

    def read_all_rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        with open(self.path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            return list(reader)


class DataLogs:
    """三個資料檔的集中入口，bot.py / handlers 透過此物件寫入。"""

    def __init__(self, data_dir: Path, write_queue: Optional[WriteQueue] = None):
        data_dir = Path(data_dir)
        wq = write_queue or default_write_queue
        self.upload_log = CsvLog(data_dir / "upload_log.csv", UPLOAD_LOG_HEADER, wq)
        self.cleanup_list = CsvLog(data_dir / "待清理清單.csv", CLEANUP_LIST_HEADER, wq)
        self.file_index = CsvLog(data_dir / "file_index.csv", FILE_INDEX_HEADER, wq)

    def log_upload(self, time_str, uploader, telegram_id, folder, dest_label, count, result):
        self.upload_log.append_row([time_str, uploader, telegram_id, folder, dest_label, count, result])

    def log_cleanup(self, time_str, uploader, telegram_id, kind, location, filename, note=""):
        self.cleanup_list.append_row([time_str, uploader, telegram_id, kind, location, filename, note])

    def log_cleanup_batch(self, rows: list[tuple]):
        """rows: list of (time_str, uploader, telegram_id, kind, location, filename, note)"""
        self.cleanup_list.append_rows([list(r) for r in rows])

    def log_file_index(self, time_str, uploader, telegram_id, folder, dest_label, filename, file_id):
        self.file_index.append_row([time_str, uploader, telegram_id, folder, dest_label, filename, file_id])

    def log_file_index_batch(self, rows: list[tuple]):
        self.file_index.append_rows([list(r) for r in rows])
