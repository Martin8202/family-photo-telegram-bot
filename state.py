"""
使用者狀態、近 3 次資料夾顯示、10 分鐘逾時計時（規格書 §5、§6、§6.4、§6.5）。

這個模組刻意寫成不依賴 python-telegram-bot / asyncio 的「純狀態機」，
方便直接用 pytest 做單元測試；handlers/upload.py 負責把 Telegram 的
事件（收到照片、按鈕點擊）轉換成對這裡物件的呼叫。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ── 目的地標籤與圖示 ──────────────────────────────

DEST_NAS_LABEL = "家裡硬碟"
DEST_ONEDRIVE_LABEL = "OneDrive"
DEST_BOTH_LABEL = "兩邊都存"

DEST_ICONS = {
    DEST_NAS_LABEL: "🏠",
    DEST_ONEDRIVE_LABEL: "☁️",
    DEST_BOTH_LABEL: "📦",
}

STAGE_AWAITING_FOLDER = "awaiting_folder"
STAGE_AWAITING_DESTINATION = "awaiting_destination"
STAGE_RECEIVING_PHOTOS = "receiving_photos"
STAGE_DEBOUNCE = "debounce"
STAGE_PROCESSING = "processing"
STAGE_AWAITING_RESTART_CONFIRM = "awaiting_restart_confirm"
STAGE_AWAITING_CORRECTION_FOLDER = "awaiting_correction_folder"

# 唯一會累計「完全閒置」時間的階段：已選好資料夾/目的地、等待傳照片或按「我傳完了」。
# 處理中、等待選資料夾/目的地、結案緩衝，皆不計入閒置（見規格書 §6.4）。
IDLE_COUNTING_STAGES = {STAGE_RECEIVING_PHOTOS}


def recent_folder_icon(last_dest_label: str) -> str:
    return DEST_ICONS.get(last_dest_label, "")


@dataclass
class ReceivedFile:
    temp_path: Path
    filename: str
    file_id: str
    media_group_id: Optional[str]
    received_at: datetime
    is_original_quality: bool  # True＝以 document 傳送的原始檔


@dataclass
class DestinationOutcome:
    """單一目的地（NAS 或 OneDrive）在這次上傳中的狀態，用於「兩邊都存」部分失敗判斷。"""
    label: str
    done: bool = False
    failed: bool = False
    error: Optional[str] = None
    written_paths: list = field(default_factory=list)  # list[(file_id, Path)]，寫入當下就配對，避免事後靠順序反推


@dataclass
class CompletedBatch:
    """一次完整上傳完成後留存的紀錄，供「↩️ 這批傳錯了」使用（§7）。"""
    telegram_id: int
    folder: str
    destination_label: str
    files: list  # list[ReceivedFile]，含每張的目的地寫入路徑另存於 written_paths
    written_paths: dict  # dest_label -> list[Path]，該次寫入各目的地的實際檔案路徑
    completed_at: datetime
    corrected: bool = False  # 「這批傳錯了」只能生效一次（防重複點擊, §6.3）


@dataclass
class UploadSession:
    telegram_id: int
    name: str
    stage: str = STAGE_AWAITING_FOLDER
    folder: Optional[str] = None
    destination: Optional[str] = None
    temp_dir: Optional[Path] = None
    started_at: datetime = field(default_factory=datetime.now)
    last_activity_at: datetime = field(default_factory=datetime.now)
    files: list = field(default_factory=list)  # list[ReceivedFile]，本次上傳全部照片
    pending_media_group_ids: set = field(default_factory=set)
    finish_clicked: bool = False
    restart_clicked: bool = False
    compressed_warned: bool = False
    counter_last_update: Optional[datetime] = None
    counter_message_id: Optional[int] = None  # 收件計數訊息 id，靠編輯同一則訊息更新，不洗版（§6.3.1）
    confirm_message_id: Optional[int] = None  # 「確認中…」訊息 id，緩衝期間收到新照片時更新張數
    destinations: dict = field(default_factory=dict)  # label -> DestinationOutcome
    last_written_count: int = 0  # 已成功複製到目的地（含所有內部小批）的張數，供「傳完了」時計算剩餘
    flushed_count: int = 0  # session.files 中，前面已經被進度性分批處理過（複製或列入重試）的張數
    pending_retry_files: list = field(default_factory=list)  # 進度性分批時寫入失敗、留待批次後重試的檔案

    def touch(self, now: Optional[datetime] = None) -> None:
        self.last_activity_at = now or datetime.now()

    def enter_stage(self, stage: str, now: Optional[datetime] = None) -> None:
        self.stage = stage
        self.touch(now)

    def is_idle_timed_out(self, timeout_min: int, now: Optional[datetime] = None) -> bool:
        if self.stage not in IDLE_COUNTING_STAGES:
            return False
        now = now or datetime.now()
        return (now - self.last_activity_at) >= timedelta(minutes=timeout_min)

    def is_abandoned(self, max_lifetime_min: int, now: Optional[datetime] = None) -> bool:
        """
        絕對存活上限的記憶體安全網（與 §6.4 的 10 分鐘閒置逾時無關）。

        §6.4 明訂「選資料夾／選目的地」階段不計入閒置、session 不會逾時——這是為了
        不要在使用者正常操作中途誤殺 session。但若使用者點了「我要上傳」後就再也
        沒回來（停在選資料夾且一張都沒傳），這個 session 會永遠佔著記憶體。
        此方法用一個「遠比正常互動長」的上限（預設 60 分鐘）辨識這種真正被遺棄、
        且尚未收到任何照片的 session，交由排程靜默清除，不影響任何實體檔案。
        """
        if self.received_count > 0:
            return False  # 已收到照片者交給正常逾時流程處理，這裡不碰，避免誤刪暫存
        now = now or datetime.now()
        return (now - self.started_at) >= timedelta(minutes=max_lifetime_min)

    def add_file(self, rf: ReceivedFile) -> None:
        self.files.append(rf)

    @property
    def received_count(self) -> int:
        return len(self.files)


class SessionManager:
    """所有使用中 session 與最近一次完成批次（供「這批傳錯了」）的集中管理。"""

    def __init__(self) -> None:
        self._sessions: dict[int, UploadSession] = {}
        self._last_batches: dict[int, CompletedBatch] = {}

    # ── session ──
    def get(self, telegram_id: int) -> Optional[UploadSession]:
        return self._sessions.get(telegram_id)

    def has_active(self, telegram_id: int) -> bool:
        return telegram_id in self._sessions

    def start(self, telegram_id: int, name: str, now: Optional[datetime] = None) -> UploadSession:
        session = UploadSession(telegram_id=telegram_id, name=name,
                                 started_at=now or datetime.now(),
                                 last_activity_at=now or datetime.now())
        self._sessions[telegram_id] = session
        return session

    def clear(self, telegram_id: int) -> Optional[UploadSession]:
        return self._sessions.pop(telegram_id, None)

    def all_sessions(self) -> list[UploadSession]:
        return list(self._sessions.values())

    # ── 已完成批次（供傳錯復原） ──
    def set_last_batch(self, batch: CompletedBatch) -> None:
        self._last_batches[batch.telegram_id] = batch

    def get_last_batch(self, telegram_id: int) -> Optional[CompletedBatch]:
        return self._last_batches.get(telegram_id)


# ── 批次切分（內部小批，對使用者透明，§6.3）──────────

def chunk_files(files: list, batch_size: int) -> list[list]:
    """把整批檔案切成內部小批，用於分批複製到目的地。"""
    return [files[i:i + batch_size] for i in range(0, len(files), batch_size)]


# ── 相簿（media group）計數說明 ───────────────────────
#
# 手機以「相簿」一次傳多張時，Telegram 會把每張拆成獨立 update 送達。
# 本程式對每一則 update 各自 add_file、逐張計數，因此計數天生正確，
# 不需要再依 media_group_id 做聚合（規格書 §6.3 的目標「計數不重複、不漏算」
# 已由逐張計數達成）。media_group_id 仍記在 ReceivedFile 上，保留供除錯查閱。


# ── 進度顯示輔助 ─────────────────────────────────────

def progress_bar(done: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return "▓" * 0 + "░" * width + " 0%（0/0 張）"
    ratio = min(done / total, 1.0)
    filled = round(ratio * width)
    bar = "▓" * filled + "░" * (width - filled)
    pct = round(ratio * 100)
    return f"{bar} {pct}%（{done}/{total} 張）"


def should_update_counter(session: UploadSession, now: Optional[datetime], throttle_sec: float) -> bool:
    """收件計數節流：固定每 N 秒彙整更新一次，避免頻繁 editMessageText 遭限流（§6.3.1）。"""
    now = now or datetime.now()
    if session.counter_last_update is None:
        return True
    return (now - session.counter_last_update).total_seconds() >= throttle_sec
