"""
檔案操作，集中管理（見規格書 §3、§4、§10、§11）。

核心保證：
- 對「目的地」（區網硬碟／OneDrive）只用 `shutil.copy2` 與 `Path.mkdir`，
  完全不含刪除／移動指令。
- 唯一的刪除函式 `safe_delete_in_temp()` 會先驗證路徑位於 TEMP_DIR 底下，
  不是就拒絕執行（刪除圍籬）。
- 所有會碰觸磁碟 I/O 的函式皆為同步函式，呼叫端須以 `asyncio.to_thread()`
  執行，避免阻塞 asyncio 事件迴圈（規格書 §3 架構準則）。
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from PIL import Image
    from PIL.ExifTags import TAGS
except ImportError:  # Pillow 未安裝時仍可 import 本模組（EXIF 功能退化）
    Image = None
    TAGS = None


# ── 檔名 ────────────────────────────────────────────

def read_exif_datetime(file_path: Path) -> Optional[datetime]:
    """讀取照片的 EXIF DateTimeOriginal。讀不到、損毀或非圖片一律回傳 None。"""
    if Image is None:
        return None
    try:
        with Image.open(file_path) as img:
            exif = img.getexif()
            if not exif:
                return None
            for tag_id, value in exif.items():
                tag = TAGS.get(tag_id, tag_id)
                if tag == "DateTimeOriginal":
                    return datetime.strptime(value, "%Y:%m:%d %H:%M:%S")
    except Exception:
        return None
    return None


def build_filename(
    received_time: datetime,
    ext: str = ".jpg",
    source_path: Optional[Path] = None,
    use_exif: bool = True,
) -> str:
    """
    產生檔名：優先採 EXIF 拍攝時間，讀不到才回退為接收時間（見規格書 §10）。
    格式：YYYYMMDD_HHMMSS_微秒.jpg
    """
    ts = None
    if use_exif and source_path is not None:
        ts = read_exif_datetime(source_path)
    if ts is None:
        ts = received_time
    micro = received_time.microsecond  # 微秒一律用接收時間，確保同批不同檔不撞名
    return f"{ts.strftime('%Y%m%d_%H%M%S')}_{micro:06d}{ext}"


def unique_destination(dest_dir: Path, filename: str) -> Path:
    """避免撞名：已存在就自動改名另存（加流水號），絕不覆蓋。"""
    dest_dir = Path(dest_dir)
    candidate = dest_dir / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    n = 2
    while True:
        candidate = dest_dir / f"{stem}_({n}){suffix}"
        if not candidate.exists():
            return candidate
        n += 1


# ── 資料夾 / 路徑 ────────────────────────────────────

INVALID_FOLDER_CHARS = '/\\:*?"<>|'


def sanitize_folder_name(name: str) -> str:
    """過濾資料夾名稱中的路徑非法字元，避免路徑錯誤。"""
    cleaned = "".join(c for c in name if c not in INVALID_FOLDER_CHARS).strip()
    return cleaned


def ensure_dir(path: Path) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


def user_temp_dir(temp_root: Path, telegram_id: int, name: str) -> Path:
    safe_name = sanitize_folder_name(name) or "使用者"
    return Path(temp_root) / f"{telegram_id}_{safe_name}"


def session_temp_dir(user_dir: Path, timestamp: datetime, folder_name: str) -> Path:
    safe_folder = sanitize_folder_name(folder_name) or "未命名資料夾"
    stamp = timestamp.strftime("%Y%m%d_%H%M")
    return Path(user_dir) / f"{stamp}_{safe_folder}"


# ── 複製（含重試）────────────────────────────────────

@dataclass
class CopyResult:
    success: bool
    dest_path: Optional[Path] = None
    error: Optional[str] = None
    attempts: int = 0


def copy_file(src: Path, dest_dir: Path, filename: str) -> Path:
    """單次複製：建目的地夾（如不存在）+ copy2，撞名自動改名。同步函式。"""
    dest_dir = Path(dest_dir)
    ensure_dir(dest_dir)
    dest_path = unique_destination(dest_dir, filename)
    shutil.copy2(src, dest_path)
    return dest_path


def copy_file_with_retry(
    src: Path,
    dest_dir: Path,
    filename: str,
    retry_times: int = 3,
    retry_delays: Optional[list[float]] = None,
    sleep_fn=time.sleep,
) -> CopyResult:
    """
    單檔重試（規格書 §6.3.2）：寫入失敗自動重試最多 retry_times 次，
    間隔遞增。同步函式，呼叫端需以 asyncio.to_thread() 執行。
    """
    delays = retry_delays if retry_delays is not None else [1, 3, 5]
    last_error: Optional[str] = None
    for attempt in range(1, retry_times + 1):
        try:
            dest_path = copy_file(src, dest_dir, filename)
            return CopyResult(success=True, dest_path=dest_path, attempts=attempt)
        except Exception as exc:  # noqa: BLE001 - 網芳暫時性錯誤種類不定
            last_error = str(exc)
            if attempt < retry_times:
                delay = delays[min(attempt - 1, len(delays) - 1)]
                sleep_fn(delay)
    return CopyResult(success=False, error=last_error, attempts=retry_times)


# ── 刪除圍籬（唯一可刪除的地方：暫存區）──────────────

class TempFenceViolation(PermissionError):
    """嘗試刪除 TEMP_DIR 以外的路徑時擲出。"""


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def safe_delete_in_temp(path: Path, temp_root: Path) -> None:
    """
    刪除暫存區內的檔案或（空/非空）資料夾。
    刪除前一律驗證路徑位於 temp_root 底下，不是就拒絕執行。
    這是全程式唯一具備刪除能力的函式，且僅能作用於暫存區。
    """
    path = Path(path)
    temp_root = Path(temp_root)
    if not _is_within(path, temp_root):
        raise TempFenceViolation(f"拒絕刪除：{path} 不在暫存區 {temp_root} 底下")
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


# ── 健檢 ────────────────────────────────────────────

def health_check(dest_dir: Path) -> tuple[bool, Optional[str]]:
    """於目的地寫入一個極小的暫存測試檔，確認成功後刪除。回傳 (成功與否, 錯誤訊息)。"""
    dest_dir = Path(dest_dir)
    test_file = dest_dir / ".photo-bot-healthcheck.tmp"
    try:
        ensure_dir(dest_dir)
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


# ── OneDrive 釋放本機空間 ─────────────────────────────

def free_onedrive_space(paths: list[Path]) -> None:
    """
    對指定檔案執行 `attrib +U -P`，標記為「僅線上」（規格書 §4.2）。
    若尚未同步完成，OneDrive 會等同步完成後才真正轉為僅線上，不會遺失資料。
    """
    for p in paths:
        p = Path(p)
        if not p.exists():
            continue
        subprocess.run(
            ["attrib", "+U", "-P", str(p)],
            shell=False,
            check=False,
            capture_output=True,
        )
