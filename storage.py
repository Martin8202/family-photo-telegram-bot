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

import json
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

EXIF_SUB_IFD_TAG = 0x8769       # 指向 Exif 子標籤頁的指標標籤
TAG_DATETIME_ORIGINAL = 0x9003  # DateTimeOriginal（實際拍攝時間，存在子標籤頁）
TAG_DATETIME = 0x0132           # DateTime（根標籤頁的修改時間，退而求其次）


def _parse_exif_datetime(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), "%Y:%m:%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def read_exif_datetime(file_path: Path) -> Optional[datetime]:
    """
    讀取照片的 EXIF 拍攝時間（規格書 §10）。讀不到、損毀或非圖片一律回傳 None。

    重要：標準相機／手機（iOS/Android）把 `DateTimeOriginal`（0x9003）存在
    **Exif 子標籤頁**（ExifSubIFD，由根標籤頁的 0x8769 指向），而不是根標籤頁。
    因此必須先用 `getexif().get_ifd(0x8769)` 進到子頁去讀，只掃根標籤頁永遠讀不到。
    子頁讀不到時，才退而求其次用根標籤頁的 DateTime（0x0132）。
    """
    if Image is None:
        return None
    try:
        with Image.open(file_path) as img:
            exif = img.getexif()
            if not exif:
                return None

            # 1) 首選：Exif 子標籤頁的 DateTimeOriginal（實際拍攝時間）
            try:
                sub_ifd = exif.get_ifd(EXIF_SUB_IFD_TAG)
            except Exception:
                sub_ifd = {}
            if sub_ifd:
                dt = _parse_exif_datetime(sub_ifd.get(TAG_DATETIME_ORIGINAL))
                if dt is not None:
                    return dt

            # 2) 相容退回：少數檔案可能把 DateTimeOriginal 放在根標籤頁
            dt = _parse_exif_datetime(exif.get(TAG_DATETIME_ORIGINAL))
            if dt is not None:
                return dt

            # 3) 最後退回：根標籤頁的 DateTime（修改時間，聊勝於無）
            return _parse_exif_datetime(exif.get(TAG_DATETIME))
    except Exception:
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

MAX_FOLDER_NAME_LENGTH = 100

# Windows 保留的裝置名稱，不能拿來當資料夾名
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class FolderNameError(ValueError):
    """
    資料夾名稱不符合命名規則。

    例外訊息是**要直接回覆給使用者看的中文說明**，故一律寫成長輩看得懂的句子，
    不揭露技術細節（規格書 §2「對家人友善」）。
    """


def _has_control_char(text: str) -> bool:
    return any(ord(c) < 32 or ord(c) == 127 for c in text)


def validate_folder_name(name: str) -> str:
    """
    檢查使用者輸入的資料夾名稱能不能安全地當成 Windows 路徑的一段。
    合格則回傳去掉頭尾空白的名稱；不合格則丟 `FolderNameError`，訊息可直接回覆使用者。

    **刻意「檢查並退回」而不是「靜默修正」**：使用者打的名字就是他要的相簿名。
    實測踩過的坑是使用者輸入 `2026-07-025大量測試` 時中間夾了一個換行字元
    （手機輸入法斷行或複製貼上帶進來的），Windows 直接回 WinError 123
    「檔案名稱、目錄名稱或磁碟區標籤語法錯誤」而讓整個流程中斷。若改成自動把
    換行換成空白，會產生一個 `2026-07-0 25大量測試` 這種使用者沒預期的資料夾，
    日後也對不上——明確請他改名比較不會出錯。
    """
    if name is None:
        raise FolderNameError("資料夾名稱不能是空的，請重新輸入")

    if _has_control_char(name):
        raise FolderNameError(
            "資料夾名稱裡不能有換行，請把名稱打在同一行之後再傳一次\n"
            "（如果是複製貼上的，貼上時可能不小心帶進了換行）"
        )

    cleaned = name.strip()
    if not cleaned:
        raise FolderNameError("資料夾名稱不能是空的，請重新輸入")

    bad = sorted({c for c in cleaned if c in INVALID_FOLDER_CHARS})
    if bad:
        raise FolderNameError(
            f"資料夾名稱裡不能有這些符號：{' '.join(bad)}\n請換一個名字再傳一次"
        )

    if len(cleaned) > MAX_FOLDER_NAME_LENGTH:
        raise FolderNameError(f"資料夾名稱太長了（最多 {MAX_FOLDER_NAME_LENGTH} 個字），請取短一點")

    if cleaned.endswith("."):
        raise FolderNameError("資料夾名稱的結尾不能是「.」，請換一個名字再傳一次")

    if cleaned.upper() in _WINDOWS_RESERVED_NAMES:
        raise FolderNameError(f"「{cleaned}」是電腦的保留名稱，不能當資料夾名，請換一個")

    return cleaned


def sanitize_folder_name(name: str) -> str:
    """
    把字串強制整理成安全的路徑片段，**不會失敗**。

    這是給「不該因為名稱不漂亮就中斷」的內部用途使用的（例如用成員姓名組出
    暫存資料夾名）。使用者親手輸入的資料夾名稱請改用 `validate_folder_name()`，
    那條路要明確退回請他改名，而不是靜默改掉他取的名字。
    """
    if not name:
        return ""
    # 控制字元（換行、Tab…）一律視為空白：留著必定讓 Windows 拒絕整條路徑
    cleaned = "".join(" " if (ord(c) < 32 or ord(c) == 127) else c for c in name)
    cleaned = "".join(c for c in cleaned if c not in INVALID_FOLDER_CHARS)
    cleaned = " ".join(cleaned.split())          # 連續空白收斂，並去掉頭尾
    cleaned = cleaned[:MAX_FOLDER_NAME_LENGTH]
    cleaned = cleaned.rstrip(". ")               # Windows 不允許結尾是點或空白
    if cleaned.upper() in _WINDOWS_RESERVED_NAMES:
        cleaned += "_"
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


SESSION_INFO_FILENAME = "_session_info.json"


def write_session_info(session_dir: Path, info: dict) -> None:
    """
    在暫存子夾裡記一份側車檔，記錄這次上傳的目的地／資料夾／上傳者。
    暫存資料夾名稱本身只帶得動資料夾名，記不下「選了哪個目的地」；
    程式中斷後若沒有這份資訊，復原時就只能用預設值瞎猜，可能補送到錯的地方
    （見規格書 §4.3 中斷復原策略）。
    """
    ensure_dir(session_dir)
    (Path(session_dir) / SESSION_INFO_FILENAME).write_text(
        json.dumps(info, ensure_ascii=False), encoding="utf-8"
    )


def read_session_info(session_dir: Path) -> Optional[dict]:
    p = Path(session_dir) / SESSION_INFO_FILENAME
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


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
