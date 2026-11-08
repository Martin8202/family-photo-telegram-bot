"""
上傳照片流程（規格書 §6、§7）。

v3 架構準則（規格書 §3.1，最重要）
────────────────────────────────
這支檔案裡的 `handle_*` 事件處理函式一律只做「登記 ＋ 回覆」等**毫秒級**動作，
實際的下載與複製全部交給 `SessionPipeline` 的背景 worker。

原因：python-telegram-bot 預設 `max_concurrent_updates=1`，**一則 update 沒處理完
就不會去取下一則**。家人傳的每張照片是一則 update，按下的每顆按鈕也是一則 update，
共用同一條佇列。v2 把下載與「每滿 20 張複製到目的地」同步寫在照片處理函式裡，
以「兩邊都存」為例光是寫入節流就是 20×0.3×2 = 12 秒／批——這段期間佇列完全停滯，
使用者按下的「✅ 我傳完了」只能排隊，超過 Telegram callback 的約 15 秒有效期還會
直接失效被丟棄。

⚠️ `asyncio.to_thread()` 解決不了這件事：它只保證「不凍結事件迴圈」，處理函式本身
仍在 await、仍未返回，佇列照樣被堵住。這是兩件不同的事。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional  # noqa: F401 - 型別註記用

from telegram import ReplyParameters, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

import notify
import storage
from handlers import folder_picker

from keyboards import (
    CB_CORRECTION,
    CB_DUP_COPY,
    CB_DUP_SKIP,
    CB_FINISH,
    CB_CONTINUE_RECEIVING,
    CB_RESTART,
    CB_RESTART_CANCEL,
    CB_RESTART_CONFIRM,
    correction_keyboard,
    duplicate_decision_keyboard,
    in_session_keyboard,
    pending_reminder_keyboard,
    restart_confirm_keyboard,
    clear_persistent_keyboard,
)
from state import (
    DEST_DRIVE_LABEL,
    STAGE_AWAITING_FOLDER,
    STAGE_AWAITING_RESTART_CONFIRM,
    STAGE_DEBOUNCE,
    STAGE_PROCESSING,
    STAGE_QUEUED,
    STAGE_RECEIVING_PHOTOS,
    CompletedBatch,
    DestinationOutcome,
    PendingDuplicates,
    ReceivedFile,
    UploadSession,
    UploadSession,
    chunk_files,
    progress_bar,
    should_update_confirm,
    should_update_counter,
)

logger = logging.getLogger("photo-bot.upload")

AWAITING_CORRECTION_FLAG = "awaiting_correction_folder"
CORRECTION_FLAG_AT = "awaiting_correction_folder_at"

NOT_STARTED_REMINDER_KEY = "last_not_started_reminder_at"
NOT_STARTED_REMINDER_COOLDOWN_SEC = 30

# v3 新增參數的預設值，容許尚未同步更新的 config.py 也能運作（見規格書 §12.1）
_DEFAULTS = {
    "DOWNLOAD_WORKERS": 3,
    "DOWNLOAD_RETRY_TIMES": 3,
    "COUNTER_UPDATE_COUNT": 8,
    "CONFIRM_UPDATE_SEC": 2,
    "CONFIRM_UPDATE_COUNT": 3,
    "COUNTER_REANCHOR_SEC": 5,
    "CORRECTION_PROMPT_MAX_MIN": 10,
    "STAGE_STUCK_MAX_MIN": 30,
    "DUPLICATE_PROMPT_MAX_MIN": 10,
    "LAST_BATCH_MAX_MIN": 720,
    "RECENT_FOLDERS_COUNT": 5,
    "PICKER_QUIET_SEC": 3,
    "PENDING_REMIND_MIN": 10,
    "PENDING_DISCARD_AFTER_REMIND_MIN": 10,
    "ALLOWED_PHOTO_EXT": [".jpg", ".jpeg", ".png", ".heic"],
    "ALLOWED_VIDEO_EXT": [".mp4", ".mov"],
    "ALLOWED_DOC_EXT": [".pdf", ".docx", ".xlsx", ".pptx", ".txt"],
    "MAX_DOWNLOAD_MB": 20,
}

# 沒有檔名時（例如一般方式傳的影片）靠 MIME 類型推副檔名
_MIME_EXT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/heic": ".heic",
    "video/mp4": ".mp4", "video/quicktime": ".mov",
}

PENDING_TEMP_LABEL = "待選資料夾"  # 還沒選資料夾時暫存子夾的名稱；真正的資料夾記在側車檔


def _cfg(config, name: str):
    return getattr(config, name, _DEFAULTS[name])


def _services(context: ContextTypes.DEFAULT_TYPE):
    bd = context.application.bot_data
    return bd["members"], bd["notifier"], bd["config"], bd["sessions"], bd["logs"]


# ── Telegram 呼叫的安全封裝 ───────────────────────────

async def _safe_send(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, reply_markup=None):
    """
    送出 Telegram 訊息，失敗只記 log、不拋例外。

    關鍵原則（照片不遺失）：實際搬運照片的流程，絕不可以因為「送一則狀態訊息
    失敗」（例如網路瞬斷造成的 NetworkError）而整個中斷——否則暫存區的照片
    會滯留、session 卡住無法收尾。所有收尾階段的 Telegram 呼叫一律走這裡。
    """
    try:
        return await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
    except Exception as exc:  # noqa: BLE001
        logger.warning("送出 Telegram 訊息失敗（chat_id=%s）：%s", chat_id, exc)
        return None


async def _safe_answer(query) -> None:
    """
    回應 callback query，失敗只記 log、不中斷後續動作（規格書 §8）。

    Telegram 的 callback query 約 15 秒後就會過期，逾期呼叫 `answer()` 會回
    BadRequest。v2 沒有保護，於是大批次照片造成佇列積壓時，「✅ 我傳完了」的整個
    處理函式在第一行就爆掉——使用者按了完全沒反應，session 一路卡到 10 分鐘逾時。
    """
    try:
        await query.answer()
    except Exception as exc:  # noqa: BLE001
        logger.warning("回應 callback query 失敗（可能已逾時）：%s", exc)


async def _reply_to_query(query, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None):
    """
    回覆一次按鈕點擊。

    ⚠️ **不可以直接用 `query.message.reply_text()`**：Telegram 對超過約 48 小時的
    訊息不會回傳完整內容，PTB 會給一個 `InaccessibleMessage`——它**沒有
    `reply_text`**，而 `Update.effective_message` 在這種情況下會是 `None`。
    完成訊息上的「📷 再傳一批」「↩️ 這批傳錯了」正是會被隔幾天才點的按鈕，
    直接呼叫 `reply_text` 會拋 AttributeError，使用者只會看到一則
    「出了點狀況」，完全不知道發生什麼事。

    這裡先試著回覆原訊息（有的話版面比較連貫），失敗或訊息不可存取時，
    改為直接對該使用者發新訊息——後者只需要 chat_id，永遠可用。
    """
    message = getattr(query, "message", None)
    if message is not None and hasattr(message, "reply_text"):
        try:
            return await message.reply_text(text, reply_markup=reply_markup)
        except Exception as exc:  # noqa: BLE001
            logger.warning("回覆按鈕點擊失敗，改為直接發送訊息：%s", exc)
    return await _safe_send(context, query.from_user.id, text, reply_markup=reply_markup)


async def _delete_message_safe(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: Optional[int]) -> None:
    if message_id is None:
        return
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass  # 刪不掉（例如使用者手動刪過）也無妨


async def _write_record_safe(context: ContextTypes.DEFAULT_TYPE, what: str, fn, *args) -> bool:
    """
    寫入 CSV 紀錄檔／成員清單，失敗只記 log ＋ 通知管理員，**絕不中斷照片處理流程**。

    規格書 §2 的「通知失敗不可中斷本體工作」同樣適用於記錄檔。實測踩到的坑：
    管理員用 Excel 開著 `待清理清單.csv`（那正是這個檔案存在的目的——照著它刪檔），
    Windows 鎖檔導致 `open(path,"ab")` 拋 PermissionError，例外一路往上炸掉整個
    「這批傳錯了」流程——照片其實**已經複製完成**了，卻沒回覆使用者、沒寫
    file_index、新資料夾沒進「最近使用」，而且 `batch.corrected` 已被設為 True
    導致連重試都不行。一個記錄檔寫不進去，絕不該有這種連鎖後果。
    """
    try:
        await asyncio.to_thread(fn, *args)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("寫入%s失敗：%s", what, exc)
        try:
            _, notifier, _, _, _ = _services(context)
            await notifier.notify_admin(notify.msg_log_write_failure(what, str(exc)))
        except Exception:
            pass  # 連通知都送不出去時也不能再往外拋
        return False


async def _log_step(context: ContextTypes.DEFAULT_TYPE, telegram_id: int, name: str, step: str) -> None:
    """步驟紀錄（V2 O14）：誰在何時走到哪一步，不記內容。寫不進去不影響照片。"""
    _, _, _, _, logs = _services(context)
    await _write_record_safe(
        context, "步驟紀錄", logs.log_step,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"), name, telegram_id, step,
    )


async def _safe_delete_temp(path, temp_root) -> None:
    """
    刪除暫存區內的檔案／資料夾，以 to_thread 執行避免阻塞事件迴圈（規格書 §3），
    並吞下刪除圍籬例外（非暫存區路徑一律拒絕，屬預期行為）。
    """
    if path is None:
        return
    try:
        await asyncio.to_thread(storage.safe_delete_in_temp, path, temp_root)
    except storage.TempFenceViolation:
        pass


async def remind_not_started(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    提醒「直接把照片傳給我就可以」（V2 D17），附冷卻時間避免洗版：
    沒有進行中的批次時連打好幾則字，不要每一則都回一次。
    """
    now = datetime.now()
    last = context.user_data.get(NOT_STARTED_REMINDER_KEY)
    if last is not None and (now - last).total_seconds() < NOT_STARTED_REMINDER_COOLDOWN_SEC:
        return
    context.user_data[NOT_STARTED_REMINDER_KEY] = now
    await update.effective_message.reply_text(notify.user_msg_not_started())


def drive_root(config) -> Path:
    """Google Drive 電腦版裡的 `照片` 資料夾（V2 規格 D6、D11）。所有資料夾都相對於它。"""
    return Path(config.DRIVE_PHOTO_ROOT)


def _destination_targets(config, folder: str) -> dict:
    """這批要寫入的 {標籤: 資料夾路徑}。V2 只有 Google Drive 一個目的地。"""
    return {DEST_DRIVE_LABEL: drive_root(config) / folder}


# ── 單一入口：直接傳照片就開一批（V2 D17）──────────────

async def _start_batch(context: ContextTypes.DEFAULT_TYPE, telegram_id: int, name: str, queued: bool = False):
    """
    開一個「待選資料夾」批次（queued=True 時是 D24 的排隊批次）。照片先收進暫存區、再問放哪。
    暫存子夾先用「待選資料夾」命名，選好的資料夾記在側車檔（復原以側車檔為準）。
    """
    _, notifier, config, sessions, _ = _services(context)
    now = datetime.now()
    session = sessions.start_queued(telegram_id, name, now) if queued else sessions.start(telegram_id, name, now)
    session.temp_dir = storage.session_temp_dir(
        storage.user_temp_dir(Path(config.TEMP_DIR), telegram_id, name), now, PENDING_TEMP_LABEL
    )
    # 同一分鐘內接連開兩批（例如排隊批次）會撞到同一個暫存子夾，加秒數區隔
    if queued:
        session.temp_dir = session.temp_dir.with_name(f"{session.temp_dir.name}_{now:%S}")
    await asyncio.to_thread(
        storage.write_session_info, session.temp_dir,
        {"destination": DEST_DRIVE_LABEL, "folder": None, "telegram_id": telegram_id, "name": name},
    )
    if not queued:
        _clear_correction_flag(context)
        await _log_step(context, telegram_id, name, "開始上傳")
    if config.HEALTH_CHECK_ON_SESSION:
        # 只看不寫；G: 不能用也照收照片（§3.6），寫入時失敗會留在暫存區、由復原補送
        ok, err = await asyncio.to_thread(storage.health_check, drive_root(config))
        if not ok:
            await notifier.notify_admin(
                notify.msg_health_check_failed(f"開 session 健檢／{DEST_DRIVE_LABEL}", err or "未知錯誤")
            )
    return session


def _picker_job_name(telegram_id: int) -> str:
    return f"picker:{telegram_id}"


def _schedule_picker(context: ContextTypes.DEFAULT_TYPE, telegram_id: int) -> None:
    """照片停了 PICKER_QUIET_SEC 秒才出現選資料夾畫面，避免按鈕被後面的照片推上去（§3.1）。"""
    job_queue = context.application.job_queue
    if job_queue is None:
        return
    name = _picker_job_name(telegram_id)
    for job in job_queue.get_jobs_by_name(name):
        job.schedule_removal()
    config = context.application.bot_data["config"]
    job_queue.run_once(
        _picker_fire, when=_cfg(config, "PICKER_QUIET_SEC"), name=name,
        data={"telegram_id": telegram_id}, user_id=telegram_id, chat_id=telegram_id,
    )


async def _picker_fire(context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_id = context.job.data["telegram_id"]
    _, _, _, sessions, _ = _services(context)
    session = sessions.get(telegram_id)
    if session is None or session.stage != STAGE_AWAITING_FOLDER:
        return
    await show_upload_picker(context, session)


async def show_upload_picker(context: ContextTypes.DEFAULT_TYPE, session) -> None:
    """（重新）發出「收到 N 張！要放進哪個資料夾？」；張數沒變就不重發。"""
    if session.picker_message_id is not None and session.picker_shown_count == session.received_count:
        return
    await _delete_message_safe(context, session.telegram_id, session.picker_message_id)
    sent = await folder_picker.show_picker(
        context, session.telegram_id, folder_picker.MODE_UPLOAD, notify.user_msg_picker(session.received_count)
    )
    session.picker_message_id = sent.message_id if sent is not None else None
    session.picker_shown_count = session.received_count


def picker_still_wanted(context: ContextTypes.DEFAULT_TYPE, telegram_id: int, mode: str) -> bool:
    """選資料夾畫面的按鈕還有沒有效：上傳要有待選批次，更正要在有效時間內。"""
    if mode == folder_picker.MODE_CORRECTION:
        return _correction_flag_active(context)
    _, _, _, sessions, _ = _services(context)
    session = sessions.get(telegram_id)
    if session is None or session.stage != STAGE_AWAITING_FOLDER:
        return False
    session.touch()
    return True


async def on_folder_chosen(context: ContextTypes.DEFAULT_TYPE, telegram_id: int, folder: str) -> None:
    """選好資料夾：開始寫入，照片停了就自動上傳（D24，不用按「傳完了」）。"""
    _, _, _, sessions, _ = _services(context)
    session = sessions.get(telegram_id)
    if session is None or session.stage != STAGE_AWAITING_FOLDER:
        return
    await _assign_folder(context, session, folder)
    await _log_step(context, telegram_id, session.name, "選好資料夾")

    _, _, config, _, _ = _services(context)
    ready_text = notify.user_msg_upload_ready(folder)
    # 讓使用者分得清「開新資料夾」還是「加進既有資料夾」。G: 可能卡住，以 to_thread 執行（§3）。
    try:
        if await asyncio.to_thread((drive_root(config) / folder).exists):
            ready_text += "\n" + notify.user_msg_folder_exists(folder, [DEST_DRIVE_LABEL])
    except Exception:
        pass  # 檢查失敗不影響上傳本身，頂多少一句提示
    await _safe_send(context, telegram_id, ready_text, reply_markup=in_session_keyboard())


async def _assign_folder(context: ContextTypes.DEFAULT_TYPE, session, folder: str) -> None:
    """把資料夾掛到批次上、更新側車檔、進入收件並排好自動上傳。"""
    session.folder = folder
    session.destination = DEST_DRIVE_LABEL
    _, _, config, _, _ = _services(context)
    for label in _destination_targets(config, folder):
        session.destinations[label] = DestinationOutcome(label=label)
    await asyncio.to_thread(
        storage.write_session_info, session.temp_dir,
        {"destination": DEST_DRIVE_LABEL, "folder": folder, "telegram_id": session.telegram_id, "name": session.name},
    )
    session.enter_stage(STAGE_RECEIVING_PHOTOS)
    _schedule_debounce(context, session.telegram_id)


async def _activate_queued(context: ContextTypes.DEFAULT_TYPE, ended) -> None:
    """上一批結束後，排隊的批次接上來，一樣放進同一個資料夾（D24）。"""
    _, _, _, sessions, _ = _services(context)
    session = sessions.get(ended.telegram_id)
    if session is None or session.stage != STAGE_QUEUED:
        return
    if not ended.folder:
        session.enter_stage(STAGE_AWAITING_FOLDER)
        _schedule_picker(context, session.telegram_id)
        return
    await _assign_folder(context, session, ended.folder)
    await _safe_send(context, session.telegram_id, notify.user_msg_queued_next(session.received_count, ended.folder))


async def handle_folder_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """處理打字。回傳 True 代表已處理。還沒選資料夾＝搜尋；選好之後回提示（D23）。"""
    # 傳錯復原（↩️ 這批傳錯了）必須最先檢查：它發生在上一次上傳已經完成、
    # session 早已被清除之後，放在 session 檢查之後會永遠進不來。
    if _correction_flag_active(context):
        await folder_picker.handle_text(update, context, folder_picker.MODE_CORRECTION)
        return True

    _, _, _, sessions, _ = _services(context)
    telegram_id = update.effective_user.id
    session = sessions.get(telegram_id)
    if session is None:
        return False
    if session.stage == STAGE_AWAITING_FOLDER:
        session.touch()
        await folder_picker.handle_text(update, context, folder_picker.MODE_UPLOAD)
        return True
    if session.folder:
        await update.effective_message.reply_text(notify.user_msg_already_chosen(session.folder))
        return True
    return False


# ── 背景工作管線：下載 worker ×N ＋ 複製 worker ×1（§3.1、§6.3.3）──

class SessionPipeline:
    """
    一個 upload session 的背景工作管線。

    並行上限固定、**不隨照片數量增加**（規格書 §6.3.3）：
    - 下載 worker：`DOWNLOAD_WORKERS` 個（預設 3），兼顧速度與對 Telegram API 的禮貌。
    - 複製 worker：**固定 1 個**。網芳（SMB）最怕並行寫入，維持與 v2 相同的
      「一批一批依序寫、每張間隔 WRITE_THROTTLE_SEC」行為。
    - 佇列裡放的只是 metadata（`ReceivedFile`，每筆數百 bytes），不是照片本體，
      所以一次傳幾千張也只佔一兩 MB 記憶體。

    磁碟 I/O 的**總量與 v2 完全相同**——每張照片仍是「下載寫入暫存 1 次 ＋ 每個
    目的地複製 1 次 ＋ 刪暫存 1 次」，v3 只改變由誰觸發、何時觸發。
    """

    def __init__(self, context: ContextTypes.DEFAULT_TYPE, session):
        self.context = context
        self.session = session
        self.download_queue: asyncio.Queue = asyncio.Queue()
        self.copy_queue: asyncio.Queue = asyncio.Queue()
        self.buffer: list = []  # 已落地暫存區、尚未複製到目的地的照片
        self._tasks: list = []
        self._download_worker_count = 0
        self._stopped = False

    @property
    def config(self):
        return self.context.application.bot_data["config"]

    def start(self) -> None:
        workers = max(1, int(_cfg(self.config, "DOWNLOAD_WORKERS")))
        self._download_worker_count = workers
        for _ in range(workers):
            self._tasks.append(asyncio.create_task(_download_worker(self)))
        self._tasks.append(asyncio.create_task(_copy_worker(self)))

    def submit(self, rf: ReceivedFile) -> None:
        """登記完成後把下載工作丟進佇列——**不等它做完**，處理函式毫秒級返回。"""
        self.download_queue.put_nowait(rf)

    async def settle(self) -> None:
        """等目前已排入的下載與複製工作全部做完，但保留 worker 繼續服務。"""
        await self.download_queue.join()
        await self.copy_queue.join()

    async def drain(self) -> None:
        """
        等所有工作結束並收掉 worker。收尾（`_finalize_upload`）前必須呼叫，
        對應規格書 §6.3 流程第 5 步「先等待所有尚未完成的下載工作全部落地」。
        """
        if self._stopped:
            return
        self._stopped = True
        await self.settle()
        for _ in range(self._download_worker_count):
            self.download_queue.put_nowait(None)
        self.copy_queue.put_nowait(None)
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def cancel(self) -> None:
        """
        中止所有背景工作（供「🔄 重新開始」使用）。刻意不是 drain——使用者要的是
        中止，繼續把在途的照片複製到目的地只會製造更多需要人工清理的殘留（§6.5）。
        """
        if self._stopped:
            return
        self._stopped = True
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self.buffer.clear()


async def _download_one(pipeline: SessionPipeline, rf: ReceivedFile) -> None:
    """
    把一張照片從 Telegram 下載到暫存區，含重試（規格書 §6.3.2 下載重試層）。

    v2 這段完全沒有例外保護：一旦網路瞬斷，該張照片不落地、不計數、不記錄，
    使用者與管理員都無從得知，直接違背「照片不遺失」與「成功失敗一律記錄」。
    """
    config = pipeline.config
    session = pipeline.session
    retry_times = max(1, int(_cfg(config, "DOWNLOAD_RETRY_TIMES")))
    delays = list(getattr(config, "RETRY_DELAYS", None) or [1])
    last_error = None

    for attempt in range(1, retry_times + 1):
        try:
            await asyncio.to_thread(storage.ensure_dir, session.temp_dir)
            file_obj = await pipeline.context.bot.get_file(rf.file_id)
            if rf.original_name:
                # 文件把原檔名帶在暫存檔名裡，重啟補送時才讀得回來（V2 §3.1）
                local_path = session.temp_dir / f"{rf.file_id}{storage.DOC_NAME_SEP}{rf.original_name}"
            else:
                ext = Path(file_obj.file_path or "photo.jpg").suffix or ".jpg"
                local_path = session.temp_dir / f"{rf.file_id}{ext}"
            await file_obj.download_to_drive(custom_path=str(local_path))
            rf.temp_path = local_path
            rf.filename = local_path.name
            # 指紋在落地當下算一次就好；「兩邊都存」時兩個目的地共用同一個值，
            # 不必對同一個檔案重複做兩次雜湊。
            rf.fingerprint = await asyncio.to_thread(storage.content_fingerprint, local_path)
            rf.downloaded = True
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 網路錯誤種類不定
            last_error = str(exc)
            logger.warning("下載照片失敗（第 %s 次，file_id=%s）：%s", attempt, rf.file_id, exc)
            if attempt < retry_times:
                await asyncio.sleep(delays[min(attempt - 1, len(delays) - 1)])

    rf.download_failed = True
    rf.download_error = last_error or "未知錯誤"


async def _record_download_failure(pipeline: SessionPipeline, rf: ReceivedFile) -> None:
    """
    下載失敗仍要留下紀錄：file_index.csv 記一列並保留 file_id，日後可用
    redownload.py 補救（規格書 §6.3.2、§8、§16.1）。
    """
    _, notifier, _, _, logs = _services(pipeline.context)
    session = pipeline.session
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        await asyncio.to_thread(
            logs.log_file_index_batch,
            [(now_str, session.name, session.telegram_id, session.folder, "下載失敗", "", rf.file_id)],
        )
    except Exception:
        logger.exception("寫入下載失敗紀錄時發生例外")
    await notifier.notify_admin(
        notify.msg_download_failure(session.name, session.folder or "(未命名)", 1, rf.download_error or "未知錯誤")
    )


async def _download_worker(pipeline: SessionPipeline) -> None:
    while True:
        rf = await pipeline.download_queue.get()
        try:
            if rf is None:
                return
            await _download_one(pipeline, rf)
            if rf.downloaded:
                # 必須在 task_done 之前交棒，否則 settle() 可能在照片還沒進到
                # 複製佇列時就誤判為「全部做完」。
                await pipeline.copy_queue.put(rf)
            else:
                await _record_download_failure(pipeline, rf)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("下載 worker 發生非預期例外")
        finally:
            pipeline.download_queue.task_done()


async def _copy_worker(pipeline: SessionPipeline) -> None:
    while True:
        rf = await pipeline.copy_queue.get()
        try:
            if rf is None:
                return
            pipeline.buffer.append(rf)
            config = pipeline.config
            # 只有收件階段才主動分批複製。緩衝期間（STAGE_DEBOUNCE）刻意遞延，
            # 全部留給 _finalize_upload 收齊後一次處理（規格書 §6.3 流程第 4 步）。
            while (pipeline.session.stage == STAGE_RECEIVING_PHOTOS
                   and len(pipeline.buffer) >= config.BATCH_SIZE):
                chunk = pipeline.buffer[:config.BATCH_SIZE]
                del pipeline.buffer[:config.BATCH_SIZE]
                await _copy_and_account(pipeline.context, pipeline.session, chunk)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("複製 worker 發生非預期例外")
        finally:
            pipeline.copy_queue.task_done()


def _ensure_pipeline(context: ContextTypes.DEFAULT_TYPE, session) -> SessionPipeline:
    if session.pipeline is None:
        session.pipeline = SessionPipeline(context, session)
        session.pipeline.start()
    return session.pipeline


# ── 內部小批複製 ─────────────────────────────────────

async def _copy_and_account(context: ContextTypes.DEFAULT_TYPE, session, chunk: list) -> bool:
    """複製一個內部小批並更新 session 的統計（已存好張數／待重試清單）。"""
    _, _, config, _, _ = _services(context)
    dest_targets = _destination_targets(config, session.folder)
    ok = await _copy_chunk_to_destinations(context, session, chunk, dest_targets)
    session.flushed_count += len(chunk)
    if ok:
        for rf in chunk:
            rf.copied = True
        session.stored_count += len(chunk)
    else:
        session.pending_retry_files.extend(chunk)
    return ok


def _dest_filename(rf: ReceivedFile, config) -> str:
    """寫進 Drive 的檔名：文件保留原檔名；照片、影片用拍攝時間＋指紋（V2 §3.1）。撞名由複製時自動加 _(2)。"""
    if rf.original_name:
        return rf.original_name
    return storage.build_filename(
        rf.received_at, ext=Path(rf.temp_path).suffix or ".jpg", source_path=rf.temp_path,
        use_exif=config.USE_EXIF_TIME, fingerprint=rf.fingerprint,
    )


def _dup_key(rf: ReceivedFile):
    """重複判斷的鍵：文件看檔名（再加大小），照片、影片看內容指紋（再加大小）。"""
    return storage.doc_dup_key(rf.original_name) if rf.original_name else rf.fingerprint


async def _copy_chunk_to_destinations(context: ContextTypes.DEFAULT_TYPE, session, chunk: list, dest_targets: dict) -> bool:
    """
    把一個內部小批複製到所有目的地，寫入 file_index，全部目的地成功才清暫存。
    回傳這個小批是否所有目的地都成功。
    """
    _, notifier, config, _, logs = _services(context)
    telegram_id = session.telegram_id
    chunk_ok_labels = {}
    index_rows_by_label: dict[str, list[tuple]] = {}
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    for label, dest_dir in dest_targets.items():
        outcome = session.destinations.setdefault(label, DestinationOutcome(label=label))
        results = []
        actual_filenames = []
        # 這個目的地資料夾裡已經有哪些照片（指紋 → 檔案大小）。整批只掃一次就快取。
        if label not in session.existing_fingerprints:
            session.existing_fingerprints[label] = await asyncio.to_thread(
                storage.scan_existing_fingerprints, dest_dir
            )
        known_fingerprints = session.existing_fingerprints[label]

        # 先把「相簿裡已經有了」的挑出來，**不複製**，留著問使用者要不要還是存（§10）。
        # 判斷同時比對指紋與檔案大小，避免極小機率的指紋碰撞害一張新照片被略過。
        to_copy = []
        for rf in chunk:
            size = rf.temp_path.stat().st_size if rf.temp_path and rf.temp_path.exists() else None
            key = _dup_key(rf)
            if storage.looks_like_duplicate(known_fingerprints, key, size):
                entry = session.duplicate_files.setdefault(rf.file_id, (rf, []))
                if label not in entry[1]:
                    entry[1].append(label)
            else:
                to_copy.append(rf)
                if key:
                    # 先登記，同一批裡若有兩張一模一樣的，第二張也算重複
                    known_fingerprints.setdefault(key, set()).add(size)

        for rf in to_copy:
            try:
                await context.bot.send_chat_action(chat_id=telegram_id, action=ChatAction.UPLOAD_PHOTO)
            except Exception:
                pass  # 視覺提示失敗不影響上傳本身（§6.3.1）

            def _do_copy(rf=rf, dest_dir=dest_dir):
                filename = _dest_filename(rf, config)
                result = storage.copy_file_with_retry(
                    rf.temp_path, dest_dir, filename, config.RETRY_TIMES, config.RETRY_DELAYS
                )
                return filename, result

            attempted_filename, result = await asyncio.to_thread(_do_copy)
            results.append(result)
            # 實際落地的檔名以 dest_path 為準（撞名時會被改成 _(2) 等），
            # 失敗則退而求其次記錄本來要用的檔名，方便追查。
            actual_filenames.append(result.dest_path.name if result.success and result.dest_path else attempted_filename)
            # 大批次的複製可能持續數十分鐘；沿路 touch 才不會被 §6.4 的閒置逾時誤殺。
            session.touch()
            if config.WRITE_THROTTLE_SEC:
                await asyncio.sleep(config.WRITE_THROTTLE_SEC)

        ok = all(r.success for r in results)
        chunk_ok_labels[label] = ok
        if ok:
            # 存 (file_id, 實際寫入路徑) 配對，而非只存路徑：日後（如「這批傳錯了」）
            # 需要反查某個已寫入檔案對應的 file_id 時，才不必依賴容易被重試打亂的順序去猜。
            outcome.written_paths.extend((rf.file_id, r.dest_path) for rf, r in zip(to_copy, results))
        else:
            outcome.failed = True
            outcome.error = next((r.error for r in results if not r.success), "未知錯誤")
            await notifier.notify_admin(
                notify.msg_write_failure(session.name, session.folder, label, outcome.error)
            )

        index_rows_by_label[label] = list(zip(to_copy, actual_filenames))

    # file_index：成功或失敗一律記錄（§16.1），檔名一律是實際寫入目的地的檔名
    index_rows = []
    for label, pairs in index_rows_by_label.items():
        for rf, actual_filename in pairs:
            index_rows.append((now_str, session.name, telegram_id, session.folder, label, actual_filename, rf.file_id))
    # 寫入佇列的 submit 會等待背景執行緒完成（done_event.wait），以 to_thread
    # 執行才不會阻塞事件迴圈（§3）。下方其他 logs.*／members.* 寫入亦同理。
    await _write_record_safe(context, "照片索引 file_index.csv", logs.log_file_index_batch, index_rows)

    chunk_fully_ok = all(chunk_ok_labels.values())
    if chunk_fully_ok:
        for rf in chunk:
            # 被判定為重複、正等使用者決定的照片，暫存檔要留著——使用者可能回答
            # 「還是存一份」，那時得從暫存區複製過去。
            if rf.file_id in session.duplicate_files:
                continue
            await _safe_delete_temp(rf.temp_path, Path(config.TEMP_DIR))
    return chunk_fully_ok


# ── 收照片（事件處理層：毫秒級）────────────────────────

async def _update_status_message(
    context: ContextTypes.DEFAULT_TYPE, session, message, now: datetime, prefer_edit: bool = False
) -> None:
    """
    更新那**唯一一則**狀態訊息（規格書 §6.3.1）。

    收件與確認共用同一則訊息，差別只在句尾的狀態標記——使用者按下結束後才會多出
    「⏳ 確認中…」那一行，那既是狀態說明，也是「你按到了」的回饋。

    更新方式依階段不同：
    - **收件階段**：刪舊發新。使用者正在傳照片，每張照片都會把訊息往上推，
      唯有重發才能讓它持續貼在最下面（這是實測後使用者明確要求保留的設計）。
    - **確認階段**：使用者已停手，改以 `editMessageText` **原地編輯**更新數字——
      單次 API 呼叫、不閃爍、數字直接跳；只有隔了 `COUNTER_REANCHOR_SEC` 秒
      才重新刪舊發新把它拉回底部（緩衝期間仍可能有照片陸續抵達把它推上去）。
    - `prefer_edit`：使用者主動點擊按鈕時用，**一律原地編輯**。他剛點的按鈕就在
      這則訊息上，訊息本來就在眼前，不需要刪掉重發——刪了反而會讓他看到訊息
      憑空消失（實測回饋：「我點選沒照片了，那個訊息就被刪除了！」）。
    """
    config = context.application.bot_data["config"]
    confirming = session.stage == STAGE_DEBOUNCE
    text = notify.user_msg_status(session.received_count, session.stored_count, confirming=confirming)

    use_edit = session.status_message_id is not None and (prefer_edit or confirming)
    if use_edit and not prefer_edit:
        # 自動更新時，隔了夠久還是要重新把訊息拉回對話底部（照片會把它推上去）
        reanchor_sec = _cfg(config, "COUNTER_REANCHOR_SEC")
        if (session.status_last_reanchor is None
                or (now - session.status_last_reanchor).total_seconds() >= reanchor_sec):
            use_edit = False

    if use_edit:
        try:
            await context.bot.edit_message_text(
                chat_id=session.telegram_id, message_id=session.status_message_id,
                text=text, reply_markup=in_session_keyboard(),
            )
            return
        except Exception as exc:  # noqa: BLE001
            if "not modified" in str(exc).lower():
                return  # 內容完全相同，本來就不需要動它
            logger.debug("原地編輯狀態訊息失敗，改為重發：%s", exc)

    await _delete_message_safe(context, session.telegram_id, session.status_message_id)
    try:
        sent = await message.reply_text(text, reply_markup=in_session_keyboard())
        session.status_message_id = sent.message_id
        session.status_last_reanchor = now
    except Exception as exc:  # noqa: BLE001
        logger.warning("更新狀態訊息失敗：%s", exc)


def _classify_media(message, config):
    """
    回傳 (Telegram 檔案物件, 種類, 文件原檔名)。種類：photo／video／document；
    不在允許清單上回 None（V2 §3.1）。原畫質照片以檔案傳也算 photo，照片命名規則不變。
    """
    if message.photo:
        return message.photo[-1], "photo", None  # 最大尺寸的壓縮版
    video = getattr(message, "video", None)
    tg = video or message.document
    if tg is None:
        return None, None, None
    name = getattr(tg, "file_name", None)
    name = name if isinstance(name, str) else ""
    mime = getattr(tg, "mime_type", None)
    mime = mime.lower() if isinstance(mime, str) else ""
    ext = Path(name).suffix.lower() or _MIME_EXT.get(mime, "")
    if video is not None and not ext:
        ext = ".mp4"  # Telegram 一般方式傳的影片都是 mp4
    if ext in _cfg(config, "ALLOWED_PHOTO_EXT"):
        return tg, "photo", None
    if ext in _cfg(config, "ALLOWED_VIDEO_EXT"):
        return tg, "video", None
    if ext in _cfg(config, "ALLOWED_DOC_EXT"):
        return tg, "document", storage.safe_filename(name or f"文件{ext}")
    return tg, None, None


async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    收到照片：**只做登記與畫面更新，毫秒級返回**（規格書 §3.1、§6.3）。
    真正的下載與複製交給 SessionPipeline 的背景 worker。
    """
    members, notifier, config, sessions, logs = _services(context)
    telegram_id = update.effective_user.id
    session = sessions.get(telegram_id)
    now = datetime.now()

    message = update.effective_message
    tg_file, kind, original_name = _classify_media(message, config)
    if tg_file is None:
        return
    # 收不了的每一個都告訴家人（§3.6），不開批次、不計數
    if kind is None:
        await message.reply_text(notify.user_msg_unsupported_media())
        return
    size = getattr(tg_file, "file_size", None)
    limit_mb = _cfg(config, "MAX_DOWNLOAD_MB")
    if isinstance(size, int) and size > limit_mb * 1024 * 1024:
        member = members.get(telegram_id)
        label = getattr(tg_file, "file_name", None) or {"photo": "這張照片", "video": "這段影片"}.get(kind, "這個檔案")
        size_mb = size / 1024 / 1024
        await message.reply_text(notify.user_msg_too_large(label, size_mb, limit_mb))
        await notifier.notify_admin(
            notify.msg_too_large_for_admin(member.name if member else str(telegram_id), label, size_mb)
        )
        return
    is_original = not message.photo  # 一般方式傳的照片是壓縮版

    # 不漏收（§3.6）：任何狀況都先收進暫存區
    if session is None:
        member = members.get(telegram_id)
        session = await _start_batch(context, telegram_id, member.name if member else str(telegram_id))
    elif session.stage == STAGE_PROCESSING:
        # 上一批正在寫入：排隊，上一批結束後接著放同一個資料夾（D24）
        session = sessions.get_queued(telegram_id) or await _start_batch(
            context, telegram_id, session.name, queued=True
        )

    raw_uid = getattr(tg_file, "file_unique_id", None)
    file_unique_id = raw_uid if isinstance(raw_uid, str) else None
    rf = ReceivedFile(
        file_id=tg_file.file_id,
        file_unique_id=file_unique_id,
        media_group_id=getattr(message, "media_group_id", None),
        received_at=now,
        is_original_quality=is_original,
        original_name=original_name,
    )
    if session.first_message_id is None:
        session.first_message_id = message.message_id
    session.add_file(rf)
    session.touch(now)
    _ensure_pipeline(context, session).submit(rf)

    if session.inactivity_prompted:
        session.inactivity_prompted = False  # 不刪除訊息，僅重置旗標以備後續若再次靜置可重新提醒

    if not is_original and not session.compressed_warned:
        session.compressed_warned = True
        try:
            await message.reply_text(notify.user_msg_compressed_hint())
        except Exception:
            pass

    if session.stage == STAGE_AWAITING_FOLDER:
        _schedule_picker(context, telegram_id)
        return
    if session.stage == STAGE_RECEIVING_PHOTOS:
        if should_update_counter(session, now, config.COUNTER_UPDATE_SEC, _cfg(config, "COUNTER_UPDATE_COUNT")):
            session.mark_counter_updated(now)
            await _update_status_message(context, session, message, now)
        _schedule_debounce(context, telegram_id, restart=True)  # 照片停了就自動上傳（D24）
        return
    if session.stage != STAGE_DEBOUNCE:
        return  # 排隊中、等待重新開始確認：照片已收下，等該階段結束再處理

    # ── 緩衝期間（STAGE_DEBOUNCE）──
    if should_update_confirm(session, now, _cfg(config, "CONFIRM_UPDATE_SEC"), _cfg(config, "CONFIRM_UPDATE_COUNT")):
        session.mark_confirm_updated(now)
        await _update_status_message(context, session, message, now)
    _schedule_debounce(context, telegram_id, restart=True)


async def handle_unsupported_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """收不了的訊息（語音、貼圖、動圖等）：每一個都告訴家人（V2 §3.6）。"""
    await update.effective_message.reply_text(notify.user_msg_unsupported_media())


# ── 「我傳完了」+ 結案 Debounce ──────────────────────

def _debounce_job_name(telegram_id: int) -> str:
    return f"debounce:{telegram_id}"


def _schedule_debounce(context: ContextTypes.DEFAULT_TYPE, telegram_id: int, restart: bool = False) -> None:
    job_queue = context.application.job_queue
    if job_queue is None:
        return
    name = _debounce_job_name(telegram_id)
    for job in job_queue.get_jobs_by_name(name):
        job.schedule_removal()
    config = context.application.bot_data["config"]
    job_queue.run_once(_debounce_fire, when=config.FINISH_DEBOUNCE_SEC, name=name, data={"telegram_id": telegram_id})


async def handle_finish_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_answer(query)
    _, _, _, sessions, _ = _services(context)
    telegram_id = update.effective_user.id
    session = sessions.get(telegram_id)
    if session is None:
        return

    # 防誤觸一：照片數為 0 時按了「我傳完了」，回覆提示並硬性攔截（不關閉 session）
    if session.received_count == 0:
        try:
            await query.message.reply_text(notify.user_msg_no_photos_received())
        except Exception:
            pass
        return

    now = datetime.now()
    if session.stage == STAGE_DEBOUNCE:
        # 緩衝期間重複點擊：不重新計時、不重算張數（§6.3），但仍要回覆一次
        # 「確認中」讓使用者知道有被接收到，避免看起來像沒反應而一直猛戳。
        await _update_status_message(context, session, query.message, now, prefer_edit=True)
        session.mark_confirm_updated(now)
        return

    if session.stage != STAGE_RECEIVING_PHOTOS:
        return  # 其餘階段沒有這顆按鈕可點

    session.finish_clicked = True
    await _log_step(context, telegram_id, session.name, "傳完了")
    # 靜置提醒訊息在此結束任務（狀態訊息不刪，它要繼續用下去）
    await _delete_message_safe(context, telegram_id, session.inactivity_prompt_message_id)
    session.inactivity_prompt_message_id = None

    session.enter_stage(STAGE_DEBOUNCE, now)
    # 同一則狀態訊息**原地**換上「⏳ 確認中」標記：使用者立刻看到自己按到了，
    # 訊息不會消失也不會跳位，畫面上更不會多出一則講同樣事情的訊息（§6.3.1）。
    await _update_status_message(context, session, query.message, now, prefer_edit=True)
    session.mark_confirm_updated(now)
    _schedule_debounce(context, telegram_id)


async def handle_continue_receiving_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """使用者在靜置提醒點擊 [📷 我還有照片沒傳完]：重置提醒並保留上傳 Session。"""
    query = update.callback_query
    await _safe_answer(query)
    _, _, _, sessions, _ = _services(context)
    telegram_id = update.effective_user.id
    session = sessions.get(telegram_id)
    if session is None or session.stage != STAGE_RECEIVING_PHOTOS:
        return
    session.inactivity_prompted = False
    session.touch()
    await query.message.reply_text(notify.user_msg_continue_receiving(), reply_markup=in_session_keyboard())
    _schedule_debounce(context, telegram_id)  # V2：舊訊息上的按鈕，照片停了一樣自動上傳


async def _debounce_fire(context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_id = context.job.data["telegram_id"]
    _, _, _, sessions, _ = _services(context)
    session = sessions.get(telegram_id)
    # 收件中（選好資料夾、照片停了）也直接收尾：V2 D24 不用按「傳完了」
    if session is None or session.stage not in (STAGE_RECEIVING_PHOTOS, STAGE_DEBOUNCE):
        return
    if session.received_count == 0:
        return  # 選好資料夾時一張都還沒到（理論上不會），等照片
    await _finalize_upload(context, session, timed_out=False)


# ── 逾時保險（忘記按「我傳完了」）─────────────────────

async def check_session_timeouts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """由 job_queue 定期呼叫，掃描 session 逾時、遺棄清理與待決重複照片（§6.4、§10）。"""
    await check_pending_duplicate_timeouts(context)
    _, _, _cfg_obj, _sess, _ = _services(context)
    # 回收太久以前的已完成批次（純記憶體，不動任何實體檔案，見 §7）
    purged = _sess.purge_expired_batches(_cfg(_cfg_obj, "LAST_BATCH_MAX_MIN"))
    if purged:
        logger.info("已回收 %s 筆過期的完成批次紀錄", purged)

    _, _, config, sessions, _ = _services(context)
    now = datetime.now()
    abandoned_max_min = getattr(config, "ABANDONED_SESSION_MAX_MIN", 60)
    stuck_max_min = _cfg(config, "STAGE_STUCK_MAX_MIN")
    # V2 拿掉「傳完了嗎？」靜置提醒：選好資料夾後照片停了就自動上傳（D24）

    for session in list(sessions.all_sessions()):
        if session.folder is None and session.received_count > 0:
            # 有照片但還沒選資料夾：不能自動上傳，只能依 §3.7 提醒、沒反應才清除
            if session.stage == STAGE_AWAITING_RESTART_CONFIRM and session.is_stage_stuck(stuck_max_min, now):
                session.enter_stage(STAGE_AWAITING_FOLDER)
            elif session.stage == STAGE_AWAITING_FOLDER:
                await _check_pending_reminder(context, session, now)
            continue

        if session.is_idle_timed_out(config.SESSION_TIMEOUT_MIN, now):
            # 收件階段閒置逾時：有照片就自動當一批收尾，沒照片就清掉（§6.3 保險機制）
            if session.received_count > 0:
                await _finalize_upload(context, session, timed_out=True)
            else:
                sessions.clear(session.telegram_id)
                await _log_step(context, session.telegram_id, session.name, "放棄：選好資料夾但沒傳照片")
        elif session.is_abandoned(abandoned_max_min, now):
            # 記憶體安全網：點了「我要上傳」後停在選資料夾就再也沒回來、
            # 且一張都沒傳的 session，超過絕對存活上限即靜默清除（不動任何檔案）。
            sessions.clear(session.telegram_id)
            await _log_step(context, session.telegram_id, session.name, f"放棄：停在{session.stage}")
        elif session.is_stage_stuck(stuck_max_min, now):
            # 兜底：卡在非收件階段回不來（例如按了「重新開始」卻不回答二次確認）。
            # 暫存區可能還有照片，不可靜默丟棄（§6.4）。
            if session.received_count > 0:
                await _finalize_upload(context, session, timed_out=True)
            else:
                sessions.clear(session.telegram_id)


# ── 沒選資料夾的提醒與清除（V2 §3.7）──────────────────

async def _check_pending_reminder(context: ContextTypes.DEFAULT_TYPE, session, now: datetime) -> None:
    """有照片、沒選資料夾：10 分鐘沒動作提醒；提醒送達後又 10 分鐘沒動作才清掉。"""
    _, _, config, _, _ = _services(context)
    if session.reminded_at is not None and session.last_activity_at > session.reminded_at:
        session.reminded_at = None  # 提醒後有任何回應 → 重新計時
    if session.reminded_at is None:
        if now - session.last_activity_at >= timedelta(minutes=_cfg(config, "PENDING_REMIND_MIN")):
            await _send_pending_reminder(context, session, now)
    elif now - session.reminded_at >= timedelta(minutes=_cfg(config, "PENDING_DISCARD_AFTER_REMIND_MIN")):
        await _discard_pending(context, session, by_user=False)


async def _send_pending_reminder(context: ContextTypes.DEFAULT_TYPE, session, now: datetime) -> bool:
    """發提醒（回覆在第一張照片上）。確實送到才記時間；送不出去就等下一輪重送、不清除（§3.7 例外 2）。"""
    kwargs = {}
    if session.first_message_id is not None:
        kwargs["reply_parameters"] = ReplyParameters(
            message_id=session.first_message_id, allow_sending_without_reply=True
        )
    try:
        await context.bot.send_message(
            chat_id=session.telegram_id, text=notify.user_msg_pending_reminder(session.received_count),
            reply_markup=pending_reminder_keyboard(), **kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("沒選資料夾的提醒送不出去（chat_id=%s），下一輪再送：%s", session.telegram_id, exc)
        return False
    session.reminded_at = now
    await _log_step(context, session.telegram_id, session.name, "提醒選資料夾")
    return True


async def _discard_pending(context: ContextTypes.DEFAULT_TYPE, session, by_user: bool) -> int:
    """清掉沒選資料夾的這批：只刪本機暫存區，不碰 Google Drive（§3.7、§4）。回傳張數。"""
    _, notifier, config, sessions, logs = _services(context)
    telegram_id = session.telegram_id
    count = session.received_count
    if session.pipeline is not None:
        await session.pipeline.cancel()  # 先停下載，否則刪暫存時它還在往裡面寫
        session.pipeline = None
    job_queue = context.application.job_queue
    if job_queue is not None:
        for job in job_queue.get_jobs_by_name(_picker_job_name(telegram_id)):
            job.schedule_removal()
    await _delete_message_safe(context, telegram_id, session.picker_message_id)
    await _safe_delete_temp(session.temp_dir, Path(config.TEMP_DIR))
    sessions.clear(telegram_id)

    result = "使用者選不用存" if by_user else "未選資料夾，已清除"
    await _write_record_safe(
        context, "上傳紀錄 upload_log.csv", logs.log_upload,
        datetime.now().strftime("%Y-%m-%d %H:%M"), session.name, telegram_id, "", DEST_DRIVE_LABEL, count, result,
    )
    await _log_step(context, telegram_id, session.name, result)
    if not by_user:
        await _safe_send(context, telegram_id, notify.user_msg_pending_discarded(count))
        await notifier.notify_admin(notify.msg_pending_discarded_for_admin(session.name, count))
    return count


async def handle_pending_keep(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """提醒上的「📥 還要上傳」：重新計時，重發選資料夾畫面。"""
    query = update.callback_query
    await _safe_answer(query)
    session = _services(context)[3].get(update.effective_user.id)
    if session is None:
        await _reply_to_query(query, context, notify.user_msg_pending_gone())
        return
    if session.folder:
        await _reply_to_query(query, context, notify.user_msg_already_chosen(session.folder))
        return
    if session.stage != STAGE_AWAITING_FOLDER:
        return
    session.touch()
    session.reminded_at = None
    session.picker_shown_count = -1  # 強制重發到對話最下面
    await show_upload_picker(context, session)


async def handle_pending_drop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """提醒上的「❌ 不用存」：立刻清掉這批暫存。"""
    query = update.callback_query
    await _safe_answer(query)
    session = _services(context)[3].get(update.effective_user.id)
    if session is None:
        await _reply_to_query(query, context, notify.user_msg_pending_gone())
        return
    if session.folder:
        await _reply_to_query(query, context, notify.user_msg_already_chosen(session.folder))
        return
    if session.stage != STAGE_AWAITING_FOLDER:
        return
    folder_picker.clear(context)
    count = await _discard_pending(context, session, by_user=True)
    await _reply_to_query(query, context, notify.user_msg_pending_dropped(count))


# ── 重啟後接回「待選資料夾」批次（V2 §3.6、§3.7 例外 1）──────

def is_pending_dir(session_dir: Path) -> bool:
    """這個暫存子夾是不是還沒選資料夾的批次（側車檔 folder 是 None；沒側車檔就看名稱）。"""
    info = storage.read_session_info(session_dir)
    if info is not None and "folder" in info:
        return not info["folder"]
    return PENDING_TEMP_LABEL in session_dir.name


def _scan_pending_dirs(temp_root: Path) -> list:
    """找出所有「待選資料夾」暫存子夾：[(telegram_id, 名稱, 子夾, [檔案])]。"""
    found = []
    for user_dir in temp_root.iterdir():
        if not user_dir.is_dir() or "_" not in user_dir.name:
            continue
        tid_str, dir_name = user_dir.name.split("_", 1)
        if not tid_str.isdigit():
            continue
        for session_dir in sorted(p for p in user_dir.iterdir() if p.is_dir()):
            if not is_pending_dir(session_dir):
                continue
            files = [p for p in session_dir.iterdir() if p.is_file() and p.name != storage.SESSION_INFO_FILENAME]
            if files:
                info = storage.read_session_info(session_dir) or {}
                found.append((int(tid_str), info.get("name") or dir_name, session_dir, files))
    return found


def _restored_file(path: Path) -> ReceivedFile:
    """暫存檔還原成 ReceivedFile。文件的暫存檔名是 {file_id}~{原檔名}。"""
    if storage.DOC_NAME_SEP in path.name:
        file_id, original_name = path.name.split(storage.DOC_NAME_SEP, 1)
    else:
        file_id, original_name = path.stem, None
    return ReceivedFile(
        file_id=file_id, received_at=datetime.fromtimestamp(path.stat().st_mtime),
        is_original_quality=True, temp_path=path, filename=path.name, downloaded=True,
        fingerprint=storage.content_fingerprint(path), original_name=original_name,
    )


async def restore_pending_batches(context) -> int:
    """
    啟動時把還沒選資料夾的照片接回來，立刻補發提醒、重新給 10 分鐘（不直接刪）。
    不需要 G:，所以 Drive 還沒掛上也照做。回傳接回幾批。
    """
    _, notifier, config, sessions, _ = _services(context)
    temp_root = Path(config.TEMP_DIR)
    if not temp_root.exists():
        return 0
    now = datetime.now()
    restored = 0
    for telegram_id, name, session_dir, files in await asyncio.to_thread(_scan_pending_dirs, temp_root):
        if sessions.get(telegram_id) is not None:
            # shortcut: 同一人有兩個以上待選批次（極少見）只接回第一個，其餘留在暫存區、通知管理員人工處理
            logger.warning("同一人有多個待選資料夾批次，這個先不接回：%s", session_dir)
            await notifier.notify_admin(f"⚠️ {name} 有多批沒選資料夾的照片，這批先留在暫存區：{session_dir}")
            continue
        session = sessions.start(telegram_id, name, now)
        session.temp_dir = session_dir
        pipeline = _ensure_pipeline(context, session)
        for path in files:
            rf = await asyncio.to_thread(_restored_file, path)
            session.add_file(rf)
            pipeline.buffer.append(rf)  # 已在暫存區，選好資料夾後收尾時直接複製
        # 落在停機期間的到期一律先補提醒；送不出去時讓下一輪巡邏立刻重送
        session.last_activity_at = now - timedelta(minutes=_cfg(config, "PENDING_REMIND_MIN"))
        await _send_pending_reminder(context, session, now)
        restored += 1
        logger.info("已接回 %s 沒選資料夾的 %s 張", name, len(files))
    return restored


# ── 實際處理一次上傳（收尾）───────────────────────────

async def _finalize_upload(context: ContextTypes.DEFAULT_TYPE, session, timed_out: bool) -> None:
    members, notifier, config, sessions, logs = _services(context)
    telegram_id = session.telegram_id

    if session.stage == STAGE_PROCESSING:
        return  # 已在收尾中（debounce 與逾時掃描可能同時觸發），不重入

    # 狀態訊息保留在對話紀錄中不刪除，方便使用者回頭對照「當時到底收到幾張」（§6.3.1）
    session.status_message_id = None
    await _delete_message_safe(context, telegram_id, session.inactivity_prompt_message_id)
    session.inactivity_prompt_message_id = None

    session.enter_stage(STAGE_PROCESSING)

    # 規格書 §6.3 流程第 5 步：先等所有尚未完成的下載工作全部落地，再統計與複製。
    remainder: list = []
    if session.pipeline is not None:
        await session.pipeline.drain()
        remainder = list(session.pipeline.buffer)
        session.pipeline.buffer.clear()

    failed_downloads = [rf for rf in session.files if rf.download_failed]
    total = sum(1 for rf in session.files if rf.downloaded)
    processed = session.stored_count

    # 收件階段已在背景把大部分照片備份完成，緩衝結束時往往所剩無幾。若已全部
    # 完成就不必為了畫面效果假造一段進度條，直接跳「✅ 完成」（§6.3 設計取捨二）。
    progress_message = None
    if total > 0 and remainder:
        progress_message = await _safe_send(
            context, telegram_id, notify.user_msg_uploading(progress_bar(processed, total))
        )

    dest_targets = _destination_targets(config, session.folder)
    all_chunk_failures: list = list(session.pending_retry_files)
    session.pending_retry_files = []

    for chunk in chunk_files(remainder, config.BATCH_SIZE):
        ok = await _copy_and_account(context, session, chunk)
        if not ok:
            all_chunk_failures.extend(chunk)
        processed = session.stored_count
        if progress_message is not None:
            try:
                await progress_message.edit_text(notify.user_msg_uploading(progress_bar(processed, total)))
            except Exception:
                pass

    # 批次後重試（§6.3.2）
    if config.RETRY_AFTER_BATCH and all_chunk_failures:
        for label, dest_dir in dest_targets.items():
            outcome = session.destinations[label]
            if not outcome.failed:
                continue
            retry_results = []
            for rf in all_chunk_failures:
                def _do_copy(rf=rf, dest_dir=dest_dir):
                    return storage.copy_file_with_retry(
                        rf.temp_path, dest_dir, _dest_filename(rf, config), config.RETRY_TIMES, config.RETRY_DELAYS
                    )

                retry_results.append(await asyncio.to_thread(_do_copy))
            if all(r.success for r in retry_results):
                outcome.failed = False
                outcome.written_paths.extend(
                    (rf.file_id, r.dest_path) for rf, r in zip(all_chunk_failures, retry_results)
                )

    overall_ok = all(not o.failed for o in session.destinations.values())
    if overall_ok:
        for rf in all_chunk_failures:
            await _safe_delete_temp(rf.temp_path, Path(config.TEMP_DIR))
        # 有待決的重複照片時，整個暫存子夾要留著——那些照片的檔案還在裡面，
        # 使用者可能回答「還是存一份」，那時得從這裡複製過去。
        # 暫存夾改由 _decide_pending_duplicates 在使用者回答（或逾時）後清除。
        if not session.duplicate_files:
            await _safe_delete_temp(session.temp_dir, Path(config.TEMP_DIR))

    dest_label_text = session.destination
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    # 因為「相簿裡已經有了」而暫緩、尚未複製的張數。session.duplicate_files 以
    # file_id 為鍵，本身就是照片張數，「兩邊都存」也不會重複計算。
    duplicate_photos = len(session.duplicate_files)
    # 完成訊息的張數要扣掉暫緩的，才會跟相簿裡真正多出來的張數對得上
    stored_now = max(0, total - duplicate_photos)

    session.pipeline = None
    sessions.clear(telegram_id)
    await _log_step(context, telegram_id, session.name, "逾時自動完成" if timed_out else "完成")
    folder_index = context.application.bot_data.get("folders")
    if folder_index is not None:
        folder_index.mark_upload_done()  # 全家 10 分鐘沒動作後才在背景重掃（§3.3）
    await _activate_queued(context, session)

    # 有「相簿裡已經有了」而暫緩、尚未複製的照片時，**先問過使用者才發完成訊息**。
    # 反過來的話（先報「✅ 完成！2 張」再問還有 3 張要不要存）等於先報了一個假的
    # 結果——使用者看到「完成」時流程其實還沒結束，那個張數也不是最終的。
    if session.duplicate_files:
        await _ask_about_duplicates(context, session, timed_out=timed_out)
        return

    await _complete_upload(context, session, timed_out=timed_out)


async def _complete_upload(context: ContextTypes.DEFAULT_TYPE, session, timed_out: bool) -> None:
    """
    發出收尾宣告：完成訊息、upload_log、記住這批（供「這批傳錯了」）。

    抽成獨立函式的原因：有重複照片待決時，這一段必須**等使用者回答之後**才執行，
    完成訊息的張數才會是最終確定的（見 `_ask_about_duplicates`）。
    """
    members, notifier, config, sessions, logs = _services(context)
    telegram_id = session.telegram_id
    dest_label_text = session.destination
    total = sum(1 for rf in session.files if rf.downloaded)
    failed_downloads = [rf for rf in session.files if rf.download_failed]
    overall_ok = all(not o.failed for o in session.destinations.values())
    # 實際存進相簿的張數＝已下載的，扣掉最後決定不存的重複照片
    skipped = len(getattr(session, "skipped_duplicates", ()) or ())
    stored_now = max(0, total - skipped)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    await _write_record_safe(
        context, "上傳紀錄 upload_log.csv", logs.log_upload,
        now_str, session.name, telegram_id, session.folder, dest_label_text, stored_now,
        "成功" if overall_ok else "部分失敗",
    )
    await _write_record_safe(
        context, "最近使用的資料夾", members.push_recent_folder,
        telegram_id, session.folder, None, _cfg(config, "RECENT_FOLDERS_COUNT"),
    )

    if overall_ok:
        text = notify.user_msg_done(stored_now, session.folder, dest_label_text,
                                    skipped_count=skipped)
        text += "\n" + notify.user_msg_cloud_note()
        if failed_downloads:
            text += "\n" + notify.user_msg_download_failed_summary(len(failed_downloads))
        await _safe_send(context, telegram_id, text, reply_markup=correction_keyboard())
        await notifier.notify_admin(
            notify.msg_upload_success(session.name, session.folder, stored_now, dest_label_text)
        )

        completed = CompletedBatch(
            telegram_id=telegram_id,
            folder=session.folder,
            destination_label=dest_label_text,
            files=session.files,
            written_paths={label: o.written_paths for label, o in session.destinations.items()},
            completed_at=datetime.now(),
        )
        sessions.set_last_batch(completed)
    else:
        msg = notify.user_msg_partial_pending()
        if failed_downloads:
            msg += "\n" + notify.user_msg_download_failed_summary(len(failed_downloads))
        await _safe_send(context, telegram_id, msg)

    if timed_out and session.received_count > 0:
        await _safe_send(context, telegram_id, "⏱️ 太久沒有動作，已自動幫你把剛剛收到的照片處理完成")


async def _ask_about_duplicates(context: ContextTypes.DEFAULT_TYPE, session, timed_out: bool = False) -> None:
    """
    問使用者：偵測到 N 張跟相簿裡一模一樣的照片，還要再存一份嗎？（§10）

    這些照片**尚未被複製**，暫存檔還留著。**完成訊息也還沒發**——要等使用者
    決定之後才發，張數才會是最終確定的。逾時未回答則視同「不用了」：那份照片
    相簿裡本來就有，跳過不會遺失任何東西。
    """
    _, _, config, sessions, _ = _services(context)
    telegram_id = session.telegram_id
    files = [rf for rf, _labels in session.duplicate_files.values()]
    if not files:
        await _complete_upload(context, session, timed_out=timed_out)
        return

    pending = PendingDuplicates(
        telegram_id=telegram_id,
        name=session.name,
        folder=session.folder,
        destination_label=session.destination,
        files=files,
        dest_targets=_destination_targets(config, session.folder),
        created_at=datetime.now(),
        temp_dir=session.temp_dir,
        session=session,
        timed_out=timed_out,
    )
    sessions.set_pending_duplicates(pending)

    await _safe_send(
        context, telegram_id,
        notify.user_msg_duplicate_ask(len(files), session.folder),
        reply_markup=duplicate_decision_keyboard(),
    )


async def handle_duplicate_copy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """使用者選「要，還是存一份」：把這些重複照片照常複製進去。"""
    query = update.callback_query
    await _safe_answer(query)
    members, notifier, config, sessions, logs = _services(context)
    telegram_id = update.effective_user.id
    pending = sessions.get_pending_duplicates(telegram_id)
    if pending is None or pending.decided:
        return
    pending.decided = True
    sessions.clear_pending_duplicates(telegram_id)

    # 立刻回覆一句，複製可能要跑一陣子（重試、多目的地）——沒有這句的話，
    # 使用者按下按鈕到完成訊息出現之間完全沒反應，正是本專案一路在修的問題。
    await _reply_to_query(
        query, context, notify.user_msg_duplicate_copying(pending.count, pending.folder)
    )

    # 借用一個輕量 session 走既有的複製流程，才能沿用重試、file_index、
    # 待清理清單等所有既有保護（不另闢路徑）。
    holder = UploadSession(
        telegram_id=telegram_id, name=pending.name,
        folder=pending.folder, destination=pending.destination_label,
    )
    for label in pending.dest_targets:
        holder.destinations[label] = DestinationOutcome(label=label)
    # 這些照片是「明知重複仍要存」，不可以再被重複偵測攔一次
    for label, dest_dir in pending.dest_targets.items():
        holder.existing_fingerprints[label] = {}

    ok = await _copy_chunk_to_destinations(context, holder, pending.files, pending.dest_targets)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    cleanup_rows = []
    for label, outcome in holder.destinations.items():
        for _file_id, path in outcome.written_paths:
            path = Path(path)
            cleanup_rows.append((now_str, pending.name, telegram_id, "重複檔案",
                                 str(path.parent), path.name,
                                 "使用者確認後仍選擇存一份，與相簿既有照片是同一張"))

    if cleanup_rows:
        await _write_record_safe(context, "待清理清單", logs.log_cleanup_batch, cleanup_rows)

    if ok:
        await _discard_pending_duplicates(context, pending)

    # 把這批也算進本次上傳的成果，收尾宣告的張數才會是最終的
    if pending.session is not None:
        for label, outcome in holder.destinations.items():
            target = pending.session.destinations.setdefault(label, DestinationOutcome(label=label))
            target.written_paths.extend(outcome.written_paths)
        pending.session.skipped_duplicates = []

    await notifier.notify_admin(
        notify.msg_duplicates_for_admin(pending.name, pending.folder, pending.count)
    )
    if pending.session is not None:
        await _complete_upload(context, pending.session, timed_out=pending.timed_out)


async def handle_duplicate_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """使用者選「不用了，跳過」：不複製，清掉暫存檔。"""
    query = update.callback_query
    await _safe_answer(query)
    _, _, config, sessions, _ = _services(context)
    telegram_id = update.effective_user.id
    pending = sessions.get_pending_duplicates(telegram_id)
    if pending is None or pending.decided:
        return
    pending.decided = True
    sessions.clear_pending_duplicates(telegram_id)
    await _discard_pending_duplicates(context, pending)
    if pending.session is not None:
        pending.session.skipped_duplicates = list(pending.files)
        await _complete_upload(context, pending.session, timed_out=pending.timed_out)


async def _discard_pending_duplicates(context: ContextTypes.DEFAULT_TYPE, pending) -> None:
    """
    清掉這批待決重複照片的暫存檔。**只碰暫存區**，目的地的照片完全不動。

    收尾時因為要保留這些檔案而刻意沒刪的暫存子夾，也在這裡一併清除——
    否則使用者一直不回答的話，暫存區會留著空資料夾。
    """
    _, _, config, _, _ = _services(context)
    temp_root = Path(config.TEMP_DIR)
    for rf in pending.files:
        await _safe_delete_temp(rf.temp_path, temp_root)
    if pending.temp_dir is not None:
        await _safe_delete_temp(pending.temp_dir, temp_root)


async def check_pending_duplicate_timeouts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    逾時未回答的重複照片詢問，一律視同「不用了」（§10）。

    這個預設是安全的：那張照片相簿裡本來就有一模一樣的一份，跳過不會遺失
    任何東西；而且暫存檔留著不清會一直佔空間。
    """
    _, _, config, sessions, _ = _services(context)
    now = datetime.now()
    max_min = _cfg(config, "DUPLICATE_PROMPT_MAX_MIN")
    for pending in sessions.all_pending_duplicates():
        if pending.decided:
            continue
        if (now - pending.created_at) >= timedelta(minutes=max_min):
            pending.decided = True
            sessions.clear_pending_duplicates(pending.telegram_id)
            await _discard_pending_duplicates(context, pending)
            if pending.session is not None:
                pending.session.skipped_duplicates = list(pending.files)
                await _complete_upload(context, pending.session, timed_out=pending.timed_out)


# ── 重新開始 ─────────────────────────────────────────

async def handle_restart_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_answer(query)
    _, _, _, sessions, _ = _services(context)
    session = sessions.get(update.effective_user.id)
    if session is None:
        return
    session.stage_before_restart_confirm = session.stage
    session.enter_stage(STAGE_AWAITING_RESTART_CONFIRM)
    await query.message.reply_text(
        notify.user_msg_restart_confirm(session.received_count),
        reply_markup=restart_confirm_keyboard(),
    )


async def handle_restart_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_answer(query)
    members, notifier, config, sessions, logs = _services(context)
    telegram_id = update.effective_user.id
    session = sessions.get(telegram_id)
    if session is None:
        return

    # 先中止背景 worker，否則接下來刪暫存夾時它們還在往裡面寫、也還在往目的地複製
    # （那只會製造更多需要人工清理的殘留）。
    if session.pipeline is not None:
        await session.pipeline.cancel()
        session.pipeline = None

    residue_rows = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    for label, outcome in session.destinations.items():
        for _file_id, path in outcome.written_paths:
            path = Path(path)
            residue_rows.append((now_str, session.name, telegram_id, "中止殘留", str(path.parent), path.name,
                                  "使用者重新開始，此為已寫入的中止殘留"))
    if residue_rows:
        await _write_record_safe(context, "待清理清單", logs.log_cleanup_batch, residue_rows)
        for label, outcome in session.destinations.items():
            if outcome.written_paths:
                await notifier.notify_admin(
                    notify.msg_restart_residue(session.name, session.folder or "(未命名)", len(outcome.written_paths))
                )

    if session.temp_dir is not None:
        await _safe_delete_temp(session.temp_dir, Path(config.TEMP_DIR))

    sessions.clear(telegram_id)
    folder_picker.clear(context)
    await _log_step(context, telegram_id, session.name, "重新開始")
    await _activate_queued(context, session)
    # 順手清掉可能還留在畫面下方的舊常駐按鈕（功能已移進 ☰ 選單，見 §6.1），
    # 並用文字明確指出下一步該點哪裡，不讓使用者卡在不知道要做什麼的狀態（§5.1）。
    await query.message.reply_text(
        notify.user_msg_restart_done(), reply_markup=clear_persistent_keyboard()
    )


async def handle_restart_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_answer(query)
    members, _, config, sessions, _ = _services(context)
    telegram_id = update.effective_user.id
    session = sessions.get(telegram_id)
    if session is None:
        return

    # 回到「按下重新開始之前」所處的階段，而不是一律假設使用者原本正在傳照片。
    # 「🔄 重新開始」在 session 全程可見（§6.5），使用者很可能是在還沒選資料夾、
    # 或還沒選目的地時誤觸的——那時候根本還沒有「原本的上傳」可以繼續，
    # 直接跳到收件階段會讓他卡在一個沒有資料夾、也沒有目的地的狀態。
    previous = session.stage_before_restart_confirm or STAGE_RECEIVING_PHOTOS
    session.stage_before_restart_confirm = None
    session.enter_stage(previous)

    if previous == STAGE_AWAITING_FOLDER:
        session.picker_message_id = None  # 強制重發一次選資料夾畫面
        await show_upload_picker(context, session)
        return

    await query.message.reply_text("好的，繼續原本的上傳", reply_markup=in_session_keyboard())


# ── 傳錯復原 ─────────────────────────────────────────

def _clear_correction_flag(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data[AWAITING_CORRECTION_FLAG] = False
    context.user_data.pop(CORRECTION_FLAG_AT, None)


def _correction_flag_active(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    「這批傳錯了」的待輸入狀態是否仍有效（規格書 §7 第 10 點）。

    這個狀態**必須會過期**。v2 沒有失效條件，於是：使用者點了「↩️ 這批傳錯了」卻
    改變主意不回覆，接著開新的上傳、輸入資料夾名稱時，那個名稱會被誤判成更正目標
    ——上一批照片被複製到新上傳的資料夾去，而新 session 還卡在等資料夾名稱。
    """
    if not context.user_data.get(AWAITING_CORRECTION_FLAG):
        return False
    started_at = context.user_data.get(CORRECTION_FLAG_AT)
    if started_at is None:
        return True
    config = context.application.bot_data["config"]
    if datetime.now() - started_at >= timedelta(minutes=_cfg(config, "CORRECTION_PROMPT_MAX_MIN")):
        _clear_correction_flag(context)
        return False
    return True


async def handle_upload_again_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """舊完成訊息上的「📷 再傳一批」（V2 已拿掉）：提示直接傳照片就好。"""
    query = update.callback_query
    await _safe_answer(query)
    await _safe_send(context, update.effective_user.id, notify.user_msg_not_started())


async def handle_correction_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_answer(query)
    members, _, _, sessions, _ = _services(context)
    telegram_id = update.effective_user.id
    batch = sessions.get_last_batch(telegram_id)
    if batch is None:
        # 批次紀錄已被回收（§7 的記憶體回收），或程式重啟過。按鈕還在，但程式
        # 已無從得知那批照片是哪些——要講清楚，不能無聲無息什麼都不做，
        # 而且必須說明照片沒有不見，否則使用者會嚇到。
        await _reply_to_query(query, context, notify.user_msg_batch_expired())
        return
    if batch.corrected:
        return  # 重複點擊一律忽略（§6.3）
    context.user_data[AWAITING_CORRECTION_FLAG] = True
    context.user_data[CORRECTION_FLAG_AT] = datetime.now()
    # 新資料夾也用選資料夾畫面（V2 O8）：最近 5 個、搜尋、新建
    await folder_picker.show_picker(
        context, telegram_id, folder_picker.MODE_CORRECTION, "這批要改放到哪個資料夾？"
    )


async def finish_correction_pick(update: Update, context: ContextTypes.DEFAULT_TYPE, folder: str) -> None:
    """選資料夾畫面在更正模式下選好了資料夾。"""
    _clear_correction_flag(context)
    query = update.callback_query
    # query.message 可能是超過 48 小時的 InaccessibleMessage（沒有 reply_text），
    # 傳 None 讓 _apply_correction 走 chat_id 直送的路徑。
    msg = getattr(query, "message", None)
    await _apply_correction(update, context, folder, message=msg if hasattr(msg, "reply_text") else None)


async def _apply_correction(update: Update, context: ContextTypes.DEFAULT_TYPE, new_folder: str, message=None) -> None:
    """
    受理「這批傳錯了」的新資料夾輸入。**只做受理與回覆，實際搬移交給背景**（§3.1）。

    原本整段複製都在這裡跑完才返回，但以 PTB 一次只處理一則 update 的機制，
    100 張「兩邊都存」＝ 200 次網芳複製期間，bot 對所有人完全沒反應——按鈕點了
    沒用、別人傳照片也不會被處理。這正是 v3 重構要根除的問題，更正流程漏掉了。
    """
    _, _, _, sessions, _ = _services(context)
    telegram_id = update.effective_user.id
    message = message or update.effective_message
    batch = sessions.get_last_batch(telegram_id)
    if batch is None or batch.corrected or not new_folder:
        return
    batch.corrected = True  # 首次生效後立即失效，避免重複複製（§6.3）

    if message is not None and hasattr(message, "reply_text"):
        await message.reply_text(notify.user_msg_correction_processing(new_folder))
    else:
        message = None  # 訊息不可存取，後續一律以 chat_id 直送
        await _safe_send(context, telegram_id, notify.user_msg_correction_processing(new_folder))
    # 交給背景執行，本函式立刻返回，不佔住 update 佇列
    context.application.create_task(
        _run_correction(context, telegram_id, batch, new_folder, message)
    )


async def _run_correction(context: ContextTypes.DEFAULT_TYPE, telegram_id: int, batch, new_folder: str, message) -> None:
    """實際把這批照片複製到新資料夾（背景執行，含進度條與寫入節流）。"""
    members, notifier, config, sessions, logs = _services(context)

    uploader = members.get(telegram_id)
    uploader_name = uploader.name if uploader else str(telegram_id)

    cleanup_rows = []
    index_rows = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    roots = {DEST_DRIVE_LABEL: drive_root(config)}
    # 依「每一張照片的 file_id」而非「每一個 (目的地,路徑) 組合」計算成功數，避免
    # 「兩邊都存」時同一張照片在兩個目的地各成功一次卻被算成 2 張，導致回報張數與
    # upload_log 加倍。written_paths 裡存的是寫入當下就配好的 (file_id, 路徑)，
    # 不必事後用順序去猜對應關係。
    # 以「照片」為外層迴圈（而非目的地），進度條的分母才是使用者看得懂的張數：
    # 「兩邊都存」時同一張照片要複製兩次，但對使用者來說仍然只是一張。
    by_photo: dict = {}
    for label, pairs in batch.written_paths.items():
        for file_id, old_path in pairs:
            by_photo.setdefault(file_id, []).append((label, Path(old_path)))

    all_file_ids = set(by_photo)
    failed_file_ids: set = set()
    # 新位置的 (file_id, 路徑) 配對，用來把「最近一批」改指向新資料夾——
    # 搬到 B 之後才發現 B 也不對時，要能再搬到 C。
    new_written_paths: dict = {}
    total_photos = len(by_photo)
    progress_message = None
    last_progress_at = datetime.now()
    if total_photos:
        progress_message = await _safe_send(
            context, telegram_id, notify.user_msg_correction_progress(progress_bar(0, total_photos))
        )  # 一律以 chat_id 直送，來源訊息可能已不可存取

    for done, (file_id, targets) in enumerate(by_photo.items(), start=1):
        for label, old_path in targets:
            new_dir = roots[label] / new_folder

            try:
                await context.bot.send_chat_action(chat_id=telegram_id, action=ChatAction.UPLOAD_PHOTO)
            except Exception:
                pass

            def _do_copy(old_path=old_path, new_dir=new_dir):
                return storage.copy_file_with_retry(old_path, new_dir, old_path.name, config.RETRY_TIMES, config.RETRY_DELAYS)

            result = await asyncio.to_thread(_do_copy)
            # file_index：新位置的每一次寫入嘗試都要記錄，成功或失敗皆記（比照 §16.1 的
            # 一般上傳流程），檔名以實際落地檔名為準，撞名時才不會與真正寫入的檔案對不上。
            actual_filename = result.dest_path.name if result.success and result.dest_path else old_path.name
            index_rows.append((now_str, uploader_name, telegram_id, new_folder, label, actual_filename, file_id))
            if result.success:
                # 第 2 欄是「上傳者」，必須填姓名——v2 誤填成目的地標籤，害管理員
                # 無法依人篩選待清理清單（規格書 §10B）。
                cleanup_rows.append((now_str, uploader_name, telegram_id, "傳錯更正",
                                      str(old_path.parent), old_path.name, f"已改放至「{new_folder}」"))
                new_written_paths.setdefault(label, []).append((file_id, result.dest_path))
            else:
                failed_file_ids.add(file_id)

            # 寫入節流：與一般上傳一致，避免高速連續寫入壓垮網芳（§6.3.2）。
            # 原本更正流程完全沒有節流，會用最高速連打網芳。
            if config.WRITE_THROTTLE_SEC:
                await asyncio.sleep(config.WRITE_THROTTLE_SEC)

        now = datetime.now()
        if progress_message is not None and (
            done == total_photos or (now - last_progress_at).total_seconds() >= config.COUNTER_UPDATE_SEC
        ):
            last_progress_at = now
            try:
                await progress_message.edit_text(
                    notify.user_msg_correction_progress(progress_bar(done, total_photos))
                )
            except Exception:
                pass

    total_moved = len(all_file_ids - failed_file_ids)

    # 照片已經搬完了，先把「使用者看得到的結果」做完，記錄檔擺到後面——
    # 這個順序很重要：原本記錄檔寫在最前面，Excel 鎖檔造成的例外會讓使用者
    # 收不到回覆、新資料夾也進不了「最近使用」，明明照片早就複製好了。
    # 把「最近一批」改指向新位置。這樣搬到 B 之後才發現 B 也不對時，還能再搬到 C；
    # 也讓下面完成訊息上的「↩️ 這批傳錯了」真的可用——若沿用舊批次，它的
    # corrected 已是 True，點下去會完全沒反應，比沒有按鈕更糟。
    if new_written_paths:
        sessions.set_last_batch(CompletedBatch(
            telegram_id=telegram_id,
            folder=new_folder,
            destination_label=batch.destination_label,
            files=batch.files,
            written_paths=new_written_paths,
            completed_at=datetime.now(),
        ))

    done_text = notify.user_msg_correction_done(total_moved, new_folder)
    if message is not None:
        await message.reply_text(done_text, reply_markup=correction_keyboard())
    else:
        await _safe_send(context, telegram_id, done_text, reply_markup=correction_keyboard())
    await _write_record_safe(
        context, "最近使用的資料夾", members.push_recent_folder,
        telegram_id, new_folder, None, _cfg(config, "RECENT_FOLDERS_COUNT"),
    )

    if cleanup_rows:
        await _write_record_safe(context, "待清理清單", logs.log_cleanup_batch, cleanup_rows)
    if index_rows:
        await _write_record_safe(context, "照片索引 file_index.csv", logs.log_file_index_batch, index_rows)
    await _write_record_safe(
        context, "上傳紀錄 upload_log.csv", logs.log_upload,
        now_str, uploader_name, telegram_id,
        f"{batch.folder} → {new_folder}", batch.destination_label, total_moved, "傳錯更正",
    )
    await notifier.notify_admin(
        notify.msg_correction(uploader_name, batch.folder, new_folder, total_moved)
    )
