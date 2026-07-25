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

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

import notify
import storage
from members import STATUS_APPROVED
from telegram import InlineKeyboardMarkup

from keyboards import (
    CB_CORRECTION,
    CB_CORRECTION_FOLDER_PREFIX,
    CB_DEST_PREFIX,
    CB_FINISH,
    CB_CONTINUE_RECEIVING,
    CB_RECENT_FOLDER_PREFIX,
    CB_RESTART,
    CB_RESTART_CANCEL,
    CB_RESTART_CONFIRM,
    correction_folder_keyboard,
    correction_keyboard,
    destination_keyboard,
    folder_choice_keyboard,
    in_session_keyboard,
    inactivity_prompt_keyboard,
    restart_confirm_keyboard,
    restart_row,
    start_upload_keyboard,
    with_restart,
)
from state import (
    DEST_BOTH_LABEL,
    DEST_NAS_LABEL,
    DEST_ONEDRIVE_LABEL,
    STAGE_AWAITING_DESTINATION,
    STAGE_AWAITING_FOLDER,
    STAGE_AWAITING_RESTART_CONFIRM,
    STAGE_DEBOUNCE,
    STAGE_PROCESSING,
    STAGE_RECEIVING_PHOTOS,
    CompletedBatch,
    DestinationOutcome,
    ReceivedFile,
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
    "ONEDRIVE_FREE_SPACE_DELAY_MIN": 10,
    "INACTIVITY_PROMPT_TIMEOUT_SEC": 25,
    "AUTO_APPEND_WINDOW_MIN": 3,
}


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
    提醒「請先點『我要上傳照片』」，附冷卻時間避免洗版。
    典型觸發情境：session 已結束後，手機仍在背景陸續傳入先前已選好但尚未
    上傳完成的照片（Telegram 客戶端的傳送佇列），這些訊息會逐一落到這裡；
    沒有冷卻機制的話，每一張都會各自回一則提醒，造成洗版。
    """
    now = datetime.now()
    last = context.user_data.get(NOT_STARTED_REMINDER_KEY)
    if last is not None and (now - last).total_seconds() < NOT_STARTED_REMINDER_COOLDOWN_SEC:
        return
    context.user_data[NOT_STARTED_REMINDER_KEY] = now
    await update.effective_message.reply_text(notify.user_msg_not_started())


def _destination_roots(config) -> dict:
    return {
        DEST_NAS_LABEL: Path(config.DEST_NAS),
        DEST_ONEDRIVE_LABEL: Path(config.DEST_ONEDRIVE),
    }


def _destination_targets(destination: str, config, folder: str) -> dict:
    """依使用者選的目的地，展開成實際要寫入的 {標籤: 資料夾路徑} 對照表（含目標資料夾）。"""
    roots = _destination_roots(config)
    labels = roots.keys() if destination == DEST_BOTH_LABEL else [destination]
    return {label: roots[label] / folder for label in labels}


# ── 啟動上傳 ─────────────────────────────────────────

async def handle_start_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    members, notifier, config, sessions, logs = _services(context)
    telegram_id = update.effective_user.id
    member = members.get(telegram_id)
    if member is None or member.status != STATUS_APPROVED:
        await update.effective_message.reply_text(notify.user_msg_not_started())
        return

    existing = sessions.get(telegram_id)
    if existing is not None:
        # 重複點「我要上傳照片」：不重置，只回報現況（§6.3）
        await update.effective_message.reply_text(
            f"目前狀態：資料夾 {existing.folder or '（尚未選）'} ／ 已收到 {existing.received_count} 張"
        )
        return

    # 開新的上傳一定要清掉「這批傳錯了」的待輸入狀態（§7 第 10 點）：否則使用者
    # 待會輸入的新資料夾名稱，會被誤判成上一批的更正目標。
    _clear_correction_flag(context)

    if config.HEALTH_CHECK_ON_SESSION:
        # 健檢寫測試檔到網芳，SMB 卡住可能耗時數十秒；以 to_thread 執行避免
        # 阻塞事件迴圈、害其他家人同時也被卡住（規格書 §3）。
        ok_nas, err_nas = (True, None)
        if config.ENABLE_NAS:
            ok_nas, err_nas = await asyncio.to_thread(storage.health_check, Path(config.DEST_NAS))
        ok_od, err_od = await asyncio.to_thread(storage.health_check, Path(config.DEST_ONEDRIVE))
        if not (ok_nas and ok_od):
            err = err_nas or err_od
            await notifier.notify_admin(notify.msg_health_check_failed("開 session 健檢", err or "未知錯誤"))
            await update.effective_message.reply_text(notify.user_msg_health_check_failed())
            return

    session = sessions.start(telegram_id, member.name)
    recent = members.get_recent_folders(telegram_id)
    if recent:
        await update.effective_message.reply_text(
            "請選一個最近用過的資料夾，或直接打字輸入新資料夾名稱：",
            reply_markup=with_restart(folder_choice_keyboard(recent)),
        )
    else:
        await update.effective_message.reply_text(
            "請直接打字輸入資料夾名稱：",
            reply_markup=InlineKeyboardMarkup([restart_row()]),
        )


async def _set_folder_and_ask_destination(update_message, context: ContextTypes.DEFAULT_TYPE, session, folder_name: str):
    _, _, config, _, _ = _services(context)
    try:
        folder_name = storage.validate_folder_name(folder_name)
    except storage.FolderNameError as exc:
        # 明確告訴使用者哪裡不合規則、請他改名，而不是靜默改掉他取的名字
        await update_message.reply_text(str(exc))
        return
    session.folder = folder_name
    session.enter_stage(STAGE_AWAITING_DESTINATION)
    await update_message.reply_text(
        f"資料夾：{folder_name}\n請選擇要存到哪裡：",
        reply_markup=with_restart(destination_keyboard(config.ENABLE_NAS)),
    )


async def handle_folder_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """處理「輸入新資料夾名稱」的文字訊息。回傳 True 代表已處理。"""
    # 傳錯復原（↩️ 這批傳錯了）的新資料夾輸入必須最先檢查：這個情境發生在
    # 上一次上傳已經完成、session 早已被清除（session is None）之後，
    # 若放在 session 檢查之後，會被下面的 early return 攔截而永遠進不來。
    if _correction_flag_active(context):
        try:
            new_folder = storage.validate_folder_name(update.effective_message.text or "")
        except storage.FolderNameError as exc:
            # 保留待輸入狀態、明確請他重打，不可靜默無反應（§7 第 10 點）
            await update.effective_message.reply_text(str(exc))
            return True
        _clear_correction_flag(context)
        await _apply_correction(update, context, new_folder)
        return True

    _, _, _, sessions, _ = _services(context)
    telegram_id = update.effective_user.id
    session = sessions.get(telegram_id)
    if session is None:
        return False
    if session.stage == STAGE_AWAITING_FOLDER:
        await _set_folder_and_ask_destination(update.effective_message, context, session, update.effective_message.text)
        return True
    return False


async def handle_recent_folder_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_answer(query)
    _, _, _, sessions, _ = _services(context)
    telegram_id = update.effective_user.id
    session = sessions.get(telegram_id)
    if session is None or session.stage != STAGE_AWAITING_FOLDER:
        return
    folder_name = query.data[len(CB_RECENT_FOLDER_PREFIX):]
    await _set_folder_and_ask_destination(query.message, context, session, folder_name)


async def handle_destination_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_answer(query)
    _, _, config, sessions, _ = _services(context)
    telegram_id = update.effective_user.id
    session = sessions.get(telegram_id)
    if session is None or session.stage != STAGE_AWAITING_DESTINATION:
        return
    destination = query.data[len(CB_DEST_PREFIX):]
    session.destination = destination
    dest_targets = _destination_targets(destination, config, session.folder)
    for label in dest_targets:
        session.destinations[label] = DestinationOutcome(label=label)

    now = datetime.now()
    session.temp_dir = storage.session_temp_dir(
        storage.user_temp_dir(Path(config.TEMP_DIR), telegram_id, session.name), now, session.folder
    )
    # 側車檔記下目的地，程式中斷後復原時才知道該補送到哪裡（見 storage.write_session_info）
    await asyncio.to_thread(
        storage.write_session_info, session.temp_dir,
        {"destination": destination, "folder": session.folder, "telegram_id": telegram_id, "name": session.name},
    )
    session.enter_stage(STAGE_RECEIVING_PHOTOS, now)

    # 讓使用者分得清「這次是開新相簿」還是「加進既有相簿」。存在性檢查可能打到
    # 網芳，以 to_thread 執行避免阻塞事件迴圈（§3）。
    existing_labels = []
    for label, target in dest_targets.items():
        try:
            if await asyncio.to_thread(target.exists):
                existing_labels.append(label)
        except Exception:
            pass  # 檢查失敗不影響上傳本身，頂多少一句提示

    ready_text = notify.user_msg_upload_ready(session.folder, destination)
    if existing_labels:
        ready_text += "\n" + notify.user_msg_folder_exists(session.folder, existing_labels)

    await query.message.reply_text(ready_text, reply_markup=in_session_keyboard(show_finish=False))


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
    dest_targets = _destination_targets(session.destination, config, session.folder)
    ok = await _copy_chunk_to_destinations(context, session, chunk, dest_targets)
    session.flushed_count += len(chunk)
    if ok:
        for rf in chunk:
            rf.copied = True
        session.stored_count += len(chunk)
    else:
        session.pending_retry_files.extend(chunk)
    return ok


async def _copy_chunk_to_destinations(context: ContextTypes.DEFAULT_TYPE, session, chunk: list, dest_targets: dict) -> bool:
    """
    把一個內部小批複製到所有目的地，寫入 file_index，全部目的地成功才清暫存。
    回傳這個小批是否所有目的地都成功。
    """
    _, notifier, config, _, logs = _services(context)
    telegram_id = session.telegram_id
    chunk_ok_labels = {}
    index_rows_by_label: dict[str, list[tuple]] = {}
    duplicate_rows: list[tuple] = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    for label, dest_dir in dest_targets.items():
        outcome = session.destinations.setdefault(label, DestinationOutcome(label=label))
        results = []
        actual_filenames = []
        for rf in chunk:
            try:
                await context.bot.send_chat_action(chat_id=telegram_id, action=ChatAction.UPLOAD_PHOTO)
            except Exception:
                pass  # 視覺提示失敗不影響上傳本身（§6.3.1）

            def _do_copy(rf=rf, dest_dir=dest_dir):
                ext = Path(rf.temp_path).suffix or ".jpg"
                filename = storage.build_filename(
                    rf.received_at, ext=ext, source_path=rf.temp_path,
                    use_exif=config.USE_EXIF_TIME, fingerprint=rf.fingerprint,
                )
                result = storage.copy_file_with_retry(
                    rf.temp_path, dest_dir, filename, config.RETRY_TIMES, config.RETRY_DELAYS
                )
                return filename, result

            attempted_filename, result = await asyncio.to_thread(_do_copy)
            results.append(result)
            # 實際落地的檔名以 dest_path 為準（撞名時會被改成 _(2) 等），
            # 失敗則退而求其次記錄本來要用的檔名，方便追查。
            actual_filenames.append(result.dest_path.name if result.success and result.dest_path else attempted_filename)
            # 撞名代表「這張照片相簿裡已經有了」（指紋檔名相同＝同一張）。
            # 依零刪除原則另存一份，並逐筆寫進待清理清單交給管理員判斷後刪除（§10B）。
            if result.success and result.dest_path and result.dest_path.name != attempted_filename:
                session.duplicate_count += 1
                duplicate_rows.append((
                    now_str, session.name, telegram_id, "重複檔案",
                    str(result.dest_path.parent), result.dest_path.name,
                    f"與相簿既有的「{attempted_filename}」是同一張，確認後可刪除這份副本",
                ))
            # 大批次的複製可能持續數十分鐘；沿路 touch 才不會被 §6.4 的閒置逾時誤殺。
            session.touch()
            if config.WRITE_THROTTLE_SEC:
                await asyncio.sleep(config.WRITE_THROTTLE_SEC)

        ok = all(r.success for r in results)
        chunk_ok_labels[label] = ok
        if ok:
            # 存 (file_id, 實際寫入路徑) 配對，而非只存路徑：日後（如「這批傳錯了」）
            # 需要反查某個已寫入檔案對應的 file_id 時，才不必依賴容易被重試打亂的順序去猜。
            outcome.written_paths.extend((rf.file_id, r.dest_path) for rf, r in zip(chunk, results))
        else:
            outcome.failed = True
            outcome.error = next((r.error for r in results if not r.success), "未知錯誤")
            await notifier.notify_admin(
                notify.msg_write_failure(session.name, session.folder, label, outcome.error)
            )

        index_rows_by_label[label] = list(zip(chunk, actual_filenames))

    # 重複檔案清單先落地，管理員才有依據可以精準刪除（§10B）
    if duplicate_rows:
        await _write_record_safe(context, "待清理清單", logs.log_cleanup_batch, duplicate_rows)

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


async def _start_auto_append_session(update: Update, context: ContextTypes.DEFAULT_TYPE, last_batch, message):
    """
    遲到照片自動併案（規格書 §6.6）：上一批剛完成沒多久又收到照片時，自動開一個
    指向同一個資料夾／目的地的 session，讓照片直接歸案，使用者不必重選一次。

    刻意**不另外寫一條搬檔案的路徑**，而是重用正常流程：背景下載 worker、下載重試、
    失敗通知、file_index、upload_log、written_paths（供「這批傳錯了」使用）
    全部自動繼承，不需要把同一套容易出錯的邏輯再寫一遍。

    直接進入 STAGE_DEBOUNCE：遲到照片可能不只一張，靠既有的結案緩衝收齊後
    一次寫入，使用者連「我傳完了」都不必按。
    """
    members, _, config, sessions, _ = _services(context)
    telegram_id = update.effective_user.id
    member = members.get(telegram_id)
    if member is None:
        return None

    now = datetime.now()
    session = sessions.start(telegram_id, member.name, now)
    session.folder = last_batch.folder
    session.destination = last_batch.destination_label
    session.auto_appended = True
    for label in _destination_targets(session.destination, config, session.folder):
        session.destinations[label] = DestinationOutcome(label=label)

    session.temp_dir = storage.session_temp_dir(
        storage.user_temp_dir(Path(config.TEMP_DIR), telegram_id, session.name), now, session.folder
    )
    # 側車檔照寫，程式中斷後這批遲到照片同樣能被復原（§4.3）
    await asyncio.to_thread(
        storage.write_session_info, session.temp_dir,
        {"destination": session.destination, "folder": session.folder,
         "telegram_id": telegram_id, "name": session.name},
    )
    session.enter_stage(STAGE_DEBOUNCE, now)
    session.status_last_reanchor = now
    session.mark_confirm_updated(now)

    try:
        await message.reply_text(notify.user_msg_auto_appended(last_batch.folder))
    except Exception:
        pass
    return session


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
    is_document = message.document is not None and (message.document.mime_type or "").startswith("image/")
    if message.photo:
        tg_file = message.photo[-1]  # 最大尺寸的壓縮版
        is_original = False
    elif is_document:
        tg_file = message.document
        is_original = True
    else:
        return  # 非圖片訊息，交由其他 handler（若有）處理

    if session is None:
        # 上一批才剛完成沒多久 → 視為遲到照片，自動歸案到同一個相簿（§6.6）
        last_batch = sessions.get_last_batch(telegram_id)
        auto_append_min = _cfg(config, "AUTO_APPEND_WINDOW_MIN")
        if (
            last_batch is not None
            and not last_batch.corrected
            and (now - last_batch.completed_at).total_seconds() <= auto_append_min * 60
        ):
            session = await _start_auto_append_session(update, context, last_batch, message)
        if session is None:
            await remind_not_started(update, context)
            return

    if session.stage not in (STAGE_RECEIVING_PHOTOS, STAGE_DEBOUNCE):
        if session.stage in (STAGE_AWAITING_FOLDER, STAGE_AWAITING_DESTINATION):
            await update.effective_message.reply_text(notify.user_msg_choose_folder_first())
        return

    raw_uid = getattr(tg_file, "file_unique_id", None)
    file_unique_id = raw_uid if isinstance(raw_uid, str) else None
    rf = ReceivedFile(
        file_id=tg_file.file_id,
        file_unique_id=file_unique_id,
        media_group_id=getattr(message, "media_group_id", None),
        received_at=now,
        is_original_quality=is_original,
    )
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

    if session.stage == STAGE_RECEIVING_PHOTOS:
        if should_update_counter(session, now, config.COUNTER_UPDATE_SEC, _cfg(config, "COUNTER_UPDATE_COUNT")):
            session.mark_counter_updated(now)
            await _update_status_message(context, session, message, now)
        return

    # ── 緩衝期間（STAGE_DEBOUNCE）──
    if should_update_confirm(session, now, _cfg(config, "CONFIRM_UPDATE_SEC"), _cfg(config, "CONFIRM_UPDATE_COUNT")):
        session.mark_confirm_updated(now)
        await _update_status_message(context, session, message, now)
    _schedule_debounce(context, telegram_id, restart=True)


async def handle_unsupported_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """非照片檔案（影片、PDF、貼圖、語音等）：合理拒絕並提示，不崩潰（§8）。"""
    _, _, _, sessions, _ = _services(context)
    telegram_id = update.effective_user.id
    session = sessions.get(telegram_id)
    if session is None:
        await remind_not_started(update, context)
        return
    if session.stage not in (STAGE_RECEIVING_PHOTOS, STAGE_DEBOUNCE):
        return
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
    await query.message.reply_text(notify.user_msg_continue_receiving(), reply_markup=in_session_keyboard(show_finish=True))


ONEDRIVE_RELEASE_JOB_PREFIX = "onedrive_release:"


def _data_dir(context: ContextTypes.DEFAULT_TYPE) -> Path:
    return context.application.bot_data["data_dir"]


async def _release_onedrive_space_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """時間到了，真正對這批檔案下 attrib +U -P，並把待辦劃掉。"""
    data = context.job.data
    paths = data["paths"]
    batch_id = data.get("batch_id")
    await asyncio.to_thread(storage.free_onedrive_space, paths)
    if batch_id:
        try:
            await asyncio.to_thread(storage.remove_pending_release, _data_dir(context), batch_id)
        except Exception:
            logger.exception("移除 OneDrive 釋放待辦失敗（batch_id=%s）", batch_id)


async def _schedule_onedrive_release(
    context: ContextTypes.DEFAULT_TYPE, paths: list,
    delay_sec: Optional[float] = None, batch_id: Optional[str] = None, persist: bool = True,
) -> None:
    """
    延遲執行 OneDrive「釋放本機空間」（規格書 §4.2）。

    ⚠️ 不可以在批次一結束就立刻執行：那時 OneDrive 用戶端通常還沒把檔案傳到
    雲端，雲端還沒有副本可以「僅線上」，`attrib +U` 沒有東西可以指向、不會
    生效——之後 OneDrive 自己完成上傳，預設會把剛同步好的檔案留在本機，
    使用者會誤以為「被自動下載」，但其實我們的標記從頭到尾就沒生效過。

    ⚠️ 排程掛在 PTB 的 AsyncIOScheduler，那是**行程內記憶體**，bot 一關就沒了。
    故同步落地一份待辦檔，啟動時掃描補做（見 `startup_resume_onedrive_release`），
    否則凡是在延遲期間關掉 bot 的批次，標記就永遠不會下。
    """
    config = context.application.bot_data["config"]
    if delay_sec is None:
        delay_sec = _cfg(config, "ONEDRIVE_FREE_SPACE_DELAY_MIN") * 60
    if batch_id is None:
        batch_id = f"{datetime.now().timestamp()}"

    if persist:
        due_at = (datetime.now() + timedelta(seconds=delay_sec)).strftime("%Y-%m-%d %H:%M:%S")
        try:
            await asyncio.to_thread(
                storage.add_pending_release, _data_dir(context), batch_id, due_at, paths
            )
        except Exception:
            logger.exception("寫入 OneDrive 釋放待辦失敗，該批重啟後將無法補做")

    job_queue = context.application.job_queue
    if job_queue is None:
        # 沒有 job queue 就沒有延遲排程的能力，退回立即執行（best effort，
        # 仍優於完全不釋放空間），並記 log 說明這不是理想時機。
        logger.warning("job_queue 不存在，OneDrive 釋放空間退回立即執行（可能因太早而不生效）")
        await asyncio.to_thread(storage.free_onedrive_space, paths)
        if persist:
            await asyncio.to_thread(storage.remove_pending_release, _data_dir(context), batch_id)
        return

    job_queue.run_once(
        _release_onedrive_space_job, when=delay_sec,
        name=f"{ONEDRIVE_RELEASE_JOB_PREFIX}{batch_id}",
        data={"paths": paths, "batch_id": batch_id},
    )


async def resume_pending_onedrive_releases(context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    啟動時掃描待辦檔：已到期的立刻執行，未到期的重新排回 job queue。
    回傳處理的批次數，供啟動日誌使用（規格書 §4.2、§17）。
    """
    data_dir = _data_dir(context)
    try:
        entries = await asyncio.to_thread(storage.read_pending_releases, data_dir)
    except Exception:
        logger.exception("讀取 OneDrive 釋放待辦失敗")
        return 0

    now = datetime.now()
    for entry in entries:
        paths = entry.get("paths") or []
        batch_id = entry.get("batch_id")
        if not paths or not batch_id:
            continue
        try:
            due = datetime.strptime(entry.get("due_at", ""), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            due = now  # 時間欄壞掉就當作已到期，寧可早做也不要漏做
        remaining = (due - now).total_seconds()
        # 已到期的不要真的用 0 秒立刻跑——重新排一小段緩衝，讓 OneDrive 在
        # 開機/重啟後有時間把同步狀態接回來。
        await _schedule_onedrive_release(
            context, paths, delay_sec=max(remaining, 30), batch_id=batch_id, persist=False,
        )
    if entries:
        logger.info("已接回 %s 批未完成的 OneDrive 釋放空間排程", len(entries))
    return len(entries)


async def _debounce_fire(context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_id = context.job.data["telegram_id"]
    _, _, _, sessions, _ = _services(context)
    session = sessions.get(telegram_id)
    if session is None or session.stage != STAGE_DEBOUNCE:
        return
    await _finalize_upload(context, session, timed_out=False)


# ── 逾時保險（忘記按「我傳完了」）─────────────────────

async def check_session_timeouts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """由 job_queue 定期呼叫，掃描 session 逾時、靜置提醒與遺棄清理（§6.4）。"""
    _, _, config, sessions, _ = _services(context)
    now = datetime.now()
    abandoned_max_min = getattr(config, "ABANDONED_SESSION_MAX_MIN", 60)
    stuck_max_min = _cfg(config, "STAGE_STUCK_MAX_MIN")
    inactivity_sec = _cfg(config, "INACTIVITY_PROMPT_TIMEOUT_SEC")

    for session in list(sessions.all_sessions()):
        # 主動巡邏提醒：收件階段、已收到照片、連續 25 秒無新照片、且尚未提醒過
        if (
            session.stage == STAGE_RECEIVING_PHOTOS
            and session.received_count > 0
            and not session.inactivity_prompted
            and (now - session.last_activity_at).total_seconds() >= inactivity_sec
        ):
            session.inactivity_prompted = True
            sent = await _safe_send(
                context,
                session.telegram_id,
                notify.user_msg_inactivity_prompt(session.received_count, session.stored_count),
                reply_markup=inactivity_prompt_keyboard(),
            )
            if sent:
                session.inactivity_prompt_message_id = sent.message_id

        if session.is_idle_timed_out(config.SESSION_TIMEOUT_MIN, now):
            # 收件階段閒置逾時：有照片就自動當一批收尾，沒照片就清掉（§6.3 保險機制）
            if session.received_count > 0:
                await _finalize_upload(context, session, timed_out=True)
            else:
                sessions.clear(session.telegram_id)
        elif session.is_abandoned(abandoned_max_min, now):
            # 記憶體安全網：點了「我要上傳」後停在選資料夾/目的地就再也沒回來、
            # 且一張都沒傳的 session，超過絕對存活上限即靜默清除（不動任何檔案）。
            sessions.clear(session.telegram_id)
        elif session.is_stage_stuck(stuck_max_min, now):
            # 兜底：卡在非收件階段回不來（例如按了「重新開始」卻不回答二次確認）。
            # 暫存區可能還有照片，不可靜默丟棄（§6.4）。
            if session.received_count > 0:
                await _finalize_upload(context, session, timed_out=True)
            else:
                sessions.clear(session.telegram_id)


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

    dest_targets = _destination_targets(session.destination, config, session.folder)
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
                    ext = Path(rf.temp_path).suffix or ".jpg"
                    filename = storage.build_filename(
                        rf.received_at, ext=ext, source_path=rf.temp_path,
                        use_exif=config.USE_EXIF_TIME, fingerprint=rf.fingerprint,
                    )
                    return storage.copy_file_with_retry(
                        rf.temp_path, dest_dir, filename, config.RETRY_TIMES, config.RETRY_DELAYS
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
        await _safe_delete_temp(session.temp_dir, Path(config.TEMP_DIR))

    dest_label_text = session.destination
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    await _write_record_safe(
        context, "上傳紀錄 upload_log.csv", logs.log_upload,
        now_str, session.name, telegram_id, session.folder, dest_label_text, total,
        "成功" if overall_ok else "部分失敗",
    )
    await _write_record_safe(
        context, "最近使用的資料夾", members.push_recent_folder,
        telegram_id, session.folder, dest_label_text,
    )

    # 「兩邊都存」時同一張照片會在兩個目的地各記一次重複，換算回實際照片數才不會加倍
    dest_count = max(1, len(session.destinations))
    duplicate_photos = session.duplicate_count // dest_count

    if overall_ok:
        text = notify.user_msg_done(total, session.folder, dest_label_text, duplicate_count=duplicate_photos)
        if DEST_ONEDRIVE_LABEL in session.destinations:
            text += "\n" + notify.user_msg_onedrive_cloud_note()
        if failed_downloads:
            text += "\n" + notify.user_msg_download_failed_summary(len(failed_downloads))
        await _safe_send(context, telegram_id, text, reply_markup=correction_keyboard())
        await notifier.notify_admin(notify.msg_upload_success(session.name, session.folder, total, dest_label_text))
        if duplicate_photos > 0:
            await notifier.notify_admin(
                notify.msg_duplicates_for_admin(session.name, session.folder, duplicate_photos)
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
        nas_status = "✅" if not session.destinations.get(DEST_NAS_LABEL, DestinationOutcome(DEST_NAS_LABEL)).failed else "❌ 失敗"
        od_status = "✅" if not session.destinations.get(DEST_ONEDRIVE_LABEL, DestinationOutcome(DEST_ONEDRIVE_LABEL)).failed else "❌ 失敗"
        if session.destination == DEST_BOTH_LABEL:
            await notifier.notify_admin(notify.msg_both_partial_failure(session.name, session.folder, nas_status, od_status))

    if timed_out and session.received_count > 0:
        await _safe_send(context, telegram_id, "⏱️ 太久沒有動作，已自動幫你把剛剛收到的照片處理完成")

    if config.ONEDRIVE_FREE_SPACE and DEST_ONEDRIVE_LABEL in session.destinations:
        onedrive_paths = [p for _, p in session.destinations[DEST_ONEDRIVE_LABEL].written_paths]
        if onedrive_paths:
            await _schedule_onedrive_release(context, onedrive_paths)

    session.pipeline = None
    sessions.clear(telegram_id)


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
    # §5.1「任何狀態下畫面上永遠有可點的按鈕」：回到起點時要把「📷 我要上傳照片」
    # 重新遞給使用者，不能只留一句話讓他不知道下一步該點哪裡。
    await query.message.reply_text(notify.user_msg_restart_done(), reply_markup=start_upload_keyboard())


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
        recent = members.get_recent_folders(telegram_id)
        if recent:
            await query.message.reply_text(
                "好的，那就繼續。請選一個最近用過的資料夾，或直接打字輸入新資料夾名稱：",
                reply_markup=with_restart(folder_choice_keyboard(recent)),
            )
        else:
            await query.message.reply_text(
                "好的，那就繼續。請直接打字輸入資料夾名稱：",
                reply_markup=InlineKeyboardMarkup([restart_row()]),
            )
        return

    if previous == STAGE_AWAITING_DESTINATION:
        await query.message.reply_text(
            f"好的，那就繼續。\n資料夾：{session.folder}\n請選擇要存到哪裡：",
            reply_markup=with_restart(destination_keyboard(config.ENABLE_NAS)),
        )
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


async def handle_correction_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_answer(query)
    members, _, _, sessions, _ = _services(context)
    telegram_id = update.effective_user.id
    batch = sessions.get_last_batch(telegram_id)
    if batch is None or batch.corrected:
        return  # 重複點擊 / 無可更正批次一律忽略（§6.3）
    context.user_data[AWAITING_CORRECTION_FLAG] = True
    context.user_data[CORRECTION_FLAG_AT] = datetime.now()
    # 規格書 §7 第 1 點：須同時提供「近 3 次資料夾」按鈕與打字輸入兩種方式
    recent = members.get_recent_folders(telegram_id)
    await query.message.reply_text(
        "這批要改放到哪個資料夾？可以點下面用過的，或直接打字輸入新名稱：",
        reply_markup=correction_folder_keyboard(recent) if recent else None,
    )


async def handle_correction_folder_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_answer(query)
    if not _correction_flag_active(context):
        return
    # 這裡的名稱來自我們自己產生的「近期資料夾」按鈕，必然已經是合規的，
    # 仍走一次 sanitize 當作防線即可，不需要對使用者報錯。
    folder_name = storage.sanitize_folder_name(query.data[len(CB_CORRECTION_FOLDER_PREFIX):])
    if not folder_name:
        return
    _clear_correction_flag(context)
    await _apply_correction(update, context, folder_name, message=query.message)


async def _apply_correction(update: Update, context: ContextTypes.DEFAULT_TYPE, new_folder: str, message=None) -> None:
    members, notifier, config, sessions, logs = _services(context)
    telegram_id = update.effective_user.id
    message = message or update.effective_message
    batch = sessions.get_last_batch(telegram_id)
    if batch is None or batch.corrected or not new_folder:
        return
    batch.corrected = True  # 首次生效後立即失效，避免重複複製（§6.3）

    # 搬移可能需要一點時間（重試、多目的地），先讓使用者知道請求已收到、
    # 正在處理中，避免看起來像卡住（規格書 §6.3.1 的即時回饋精神同樣適用於此）。
    await message.reply_text(notify.user_msg_correction_processing(new_folder))

    uploader = members.get(telegram_id)
    uploader_name = uploader.name if uploader else str(telegram_id)

    cleanup_rows = []
    index_rows = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    roots = _destination_roots(config)
    # 依「每一張照片的 file_id」而非「每一個 (目的地,路徑) 組合」計算成功數，避免
    # 「兩邊都存」時同一張照片在兩個目的地各成功一次卻被算成 2 張，導致回報張數與
    # upload_log 加倍。written_paths 裡存的是寫入當下就配好的 (file_id, 路徑)，
    # 不必事後用順序去猜對應關係。
    all_file_ids = {file_id for pairs in batch.written_paths.values() for file_id, _ in pairs}
    failed_file_ids: set = set()
    for label, pairs in batch.written_paths.items():
        new_dir = roots[label] / new_folder
        for file_id, old_path in pairs:
            old_path = Path(old_path)

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
            else:
                failed_file_ids.add(file_id)

    total_moved = len(all_file_ids - failed_file_ids)

    # 照片已經搬完了，先把「使用者看得到的結果」做完，記錄檔擺到後面——
    # 這個順序很重要：原本記錄檔寫在最前面，Excel 鎖檔造成的例外會讓使用者
    # 收不到回覆、新資料夾也進不了「最近使用」，明明照片早就複製好了。
    await message.reply_text(notify.user_msg_correction_done(total_moved, new_folder))
    await _write_record_safe(
        context, "最近使用的資料夾", members.push_recent_folder,
        telegram_id, new_folder, batch.destination_label,
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
