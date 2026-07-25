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
    # 「兩邊都存」直接用「房子＋雲」的組合，而不是另一個無關的圖示（原為 📦）：
    # 使用者在「近 3 次資料夾」清單看到 🏠☁️ 時，一眼就能意會「這個資料夾上次
    # 兩邊都存了」，不必另外記憶第三個符號代表什麼（規格書 §6.2）。
    DEST_BOTH_LABEL: "🏠☁️",
}

STAGE_AWAITING_FOLDER = "awaiting_folder"
STAGE_AWAITING_DESTINATION = "awaiting_destination"
STAGE_RECEIVING_PHOTOS = "receiving_photos"
STAGE_DEBOUNCE = "debounce"
STAGE_PROCESSING = "processing"
STAGE_AWAITING_RESTART_CONFIRM = "awaiting_restart_confirm"
# 註：「等待輸入更正資料夾」不是 session 階段——它發生在上一次上傳已結束、
# session 早已清除之後，狀態記在 context.user_data（見 handlers/upload.py 的
# AWAITING_CORRECTION_FLAG 與 §7 第 10 點的失效條件）。

# 唯一會累計「完全閒置」時間的階段：已選好資料夾/目的地、等待傳照片或按「我傳完了」。
# 處理中、等待選資料夾/目的地、結案緩衝，皆不計入閒置（見規格書 §6.4）。
IDLE_COUNTING_STAGES = {STAGE_RECEIVING_PHOTOS}


def recent_folder_icon(last_dest_label: str) -> str:
    return DEST_ICONS.get(last_dest_label, "")


@dataclass
class ReceivedFile:
    """
    一張照片在本次上傳中的狀態。

    v3 起「登記」與「落地」是兩件事（規格書 §6.3）：收到 Telegram update 的當下
    就先建立本物件並計數（純記憶體、毫秒級），`temp_path` 等欄位要等背景下載
    worker 真的把檔案抓下來之後才會填上，故一律為 Optional。
    """
    file_id: str
    file_unique_id: Optional[str] = None
    media_group_id: Optional[str] = None
    received_at: datetime = field(default_factory=datetime.now)
    is_original_quality: bool = False  # True＝以 document 傳送的原始檔
    temp_path: Optional[Path] = None   # 下載落地後才有
    filename: Optional[str] = None
    downloaded: bool = False           # 已成功落地暫存區
    download_failed: bool = False      # 重試後仍下載失敗（§6.3.2）
    download_error: Optional[str] = None
    copied: bool = False               # 已成功複製到所有目的地


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
    # 按下「🔄 重新開始」進入二次確認之前所處的階段。使用者按「取消」時要回到
    # 這裡，而不是一律假設他原本正在傳照片（可能是在選資料夾階段誤觸的）。
    stage_before_restart_confirm: Optional[str] = None
    compressed_warned: bool = False
    counter_message_id: Optional[int] = None  # 收件計數訊息 id，刪舊發新置底顯示（§6.3.1）
    confirm_message_id: Optional[int] = None  # 「確認中…」訊息 id，緩衝期間更新張數
    inactivity_prompted: bool = False  # 是否已發出過「看起來傳得差不多囉，請問傳完了嗎？」靜置提醒
    inactivity_prompt_message_id: Optional[int] = None  # 靜置提醒訊息 id
    destinations: dict = field(default_factory=dict)  # label -> DestinationOutcome
    auto_appended: bool = False  # 是否為「遲到照片自動併案」開出來的 session（§6.6）
    duplicate_count: int = 0  # 本次撞名另存的次數（跨所有目的地累計，回報前需除以目的地數）
    flushed_count: int = 0  # 已進入過複製流程（成功或失敗）的張數
    stored_count: int = 0   # 已成功複製到「所有」目的地的張數，即畫面上的「已存好 N 張」
    pending_retry_files: list = field(default_factory=list)  # 分批寫入失敗、留待批次後重試的檔案
    pipeline: object = None  # SessionPipeline（背景 worker），型別刻意不綁，維持本模組零 asyncio 依賴

    # ── 畫面更新節流：時間與張數雙門檻（§6.3.1）──
    counter_last_update: Optional[datetime] = None
    counter_last_count: int = 0
    confirm_last_update: Optional[datetime] = None
    confirm_last_count: int = 0
    confirm_last_reanchor: Optional[datetime] = None  # 上次用「刪舊發新」把確認中訊息拉回底部的時間

    def touch(self, now: Optional[datetime] = None) -> None:
        self.last_activity_at = now or datetime.now()

    def enter_stage(self, stage: str, now: Optional[datetime] = None) -> None:
        self.stage = stage
        self.touch(now)

    def mark_counter_updated(self, now: Optional[datetime] = None) -> None:
        self.counter_last_update = now or datetime.now()
        self.counter_last_count = self.received_count

    def mark_confirm_updated(self, now: Optional[datetime] = None) -> None:
        self.confirm_last_update = now or datetime.now()
        self.confirm_last_count = self.received_count

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

    def is_stage_stuck(self, max_stuck_min: int, now: Optional[datetime] = None) -> bool:
        """
        任何階段的兜底逾時（規格書 §6.4，v3 新增）。

        §6.4 的 10 分鐘閒置只在收件階段累計，但 session 也可能停在其他階段回不來：
        例如按了「🔄 重新開始」卻始終不回答二次確認、或緩衝計時因異常未被觸發。
        這些狀態下 session 既不逾時、也不符合「絕對存活上限」的條件（因為已收到過
        照片），會永久佔用記憶體且暫存區的照片無人收尾。

        `STAGE_PROCESSING` 例外不計：收尾流程正在跑（大批次可能耗時數十分鐘），
        它自己會清掉 session，不需要也不應該被這個安全網打斷。
        """
        if self.stage == STAGE_PROCESSING:
            return False
        now = now or datetime.now()
        return (now - self.last_activity_at) >= timedelta(minutes=max_stuck_min)

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


def _throttle_passed(
    last_update: Optional[datetime],
    last_count: int,
    current_count: int,
    now: datetime,
    throttle_sec: float,
    throttle_count: Optional[int],
) -> bool:
    """
    雙門檻節流（規格書 §6.3.1）：「距上次更新已達 N 秒」**或**「已新增 M 張」，
    滿足其一即放行。

    為何不能只看時間：若一批照片在節流秒數內就全部抵達（張數不多、手機網路快時
    很常見），畫面會停在第 1 張的數字完全不動，直到使用者按「我傳完了」才第一次
    看到正確總數，造成「感覺卡住」的錯覺——這是 v2 實測回報的問題之一。
    """
    if last_update is None:
        return True
    if (now - last_update).total_seconds() >= throttle_sec:
        return True
    if throttle_count is not None and (current_count - last_count) >= throttle_count:
        return True
    return False


def should_update_counter(
    session: UploadSession,
    now: Optional[datetime],
    throttle_sec: float,
    throttle_count: Optional[int] = None,
) -> bool:
    """收件階段「📥 收到照片中…」的畫面更新節流（§6.3.1）。"""
    now = now or datetime.now()
    return _throttle_passed(
        session.counter_last_update, session.counter_last_count,
        session.received_count, now, throttle_sec, throttle_count,
    )


def should_update_confirm(
    session: UploadSession,
    now: Optional[datetime],
    throttle_sec: float,
    throttle_count: Optional[int] = None,
) -> bool:
    """
    緩衝期間「⏳ 確認中…」的張數更新節流（§6.3.1）。

    刻意與收件階段分開計時、且門檻更靈敏：v2 讓兩者共用 `COUNTER_UPDATE_SEC`（5 秒），
    而結案緩衝也是 5 秒，兩個窗一樣長，結果「確認中」的張數幾乎不可能更新到哪怕
    一次就被刪掉換成進度條——這正是使用者回報「數字不會跳」的直接原因。
    """
    now = now or datetime.now()
    return _throttle_passed(
        session.confirm_last_update, session.confirm_last_count,
        session.received_count, now, throttle_sec, throttle_count,
    )
