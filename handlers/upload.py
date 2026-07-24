"""上傳照片流程（規格書 §6、§7）。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

import notify
import storage
from members import STATUS_APPROVED
from telegram import InlineKeyboardMarkup

from keyboards import (
    CB_CORRECTION,
    CB_DEST_PREFIX,
    CB_FINISH,
    CB_RECENT_FOLDER_PREFIX,
    CB_RESTART,
    CB_RESTART_CANCEL,
    CB_RESTART_CONFIRM,
    correction_keyboard,
    destination_keyboard,
    folder_choice_keyboard,
    in_session_keyboard,
    restart_confirm_keyboard,
    restart_row,
    with_restart,
)
from state import (
    DEST_BOTH_LABEL,
    DEST_NAS_LABEL,
    DEST_ONEDRIVE_LABEL,
    STAGE_AWAITING_CORRECTION_FOLDER,
    STAGE_AWAITING_DESTINATION,
    STAGE_AWAITING_FOLDER,
    STAGE_AWAITING_RESTART_CONFIRM,
    STAGE_DEBOUNCE,
    STAGE_PROCESSING,
    STAGE_RECEIVING_PHOTOS,
    CompletedBatch,
    DestinationOutcome,
    ReceivedFile,
    chunk_files,
    progress_bar,
    should_update_counter,
)

logger = logging.getLogger("photo-bot.upload")

AWAITING_CORRECTION_FLAG = "awaiting_correction_folder"


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


async def _safe_delete_temp(path, temp_root) -> None:
    """
    刪除暫存區內的檔案／資料夾，以 to_thread 執行避免阻塞事件迴圈（規格書 §3），
    並吞下刪除圍籬例外（非暫存區路徑一律拒絕，屬預期行為）。
    """
    try:
        await asyncio.to_thread(storage.safe_delete_in_temp, path, temp_root)
    except storage.TempFenceViolation:
        pass
NOT_STARTED_REMINDER_KEY = "last_not_started_reminder_at"
NOT_STARTED_REMINDER_COOLDOWN_SEC = 30


def _services(context: ContextTypes.DEFAULT_TYPE):
    bd = context.application.bot_data
    return bd["members"], bd["notifier"], bd["config"], bd["sessions"], bd["logs"]


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
    folder_name = storage.sanitize_folder_name(folder_name)
    if not folder_name:
        await update_message.reply_text("資料夾名稱不能是空的或全部是特殊符號，請重新輸入")
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
    if context.user_data.get(AWAITING_CORRECTION_FLAG):
        context.user_data[AWAITING_CORRECTION_FLAG] = False
        await _apply_correction(update, context, storage.sanitize_folder_name(update.effective_message.text))
        return True

    _, _, _, sessions, _ = _services(context)
    telegram_id = update.effective_user.id
    session = sessions.get(telegram_id)
    if session is None:
        return False
    if session.stage == STAGE_AWAITING_FOLDER:
        await _set_folder_and_ask_destination(update.effective_message, context, session, update.effective_message.text)
        return True
    if session.stage == STAGE_RECEIVING_PHOTOS:
        # 尚未選資料夾但已是 receiving 階段不會發生；保留保險：等待選擇時仍可視為忽略
        return False
    return False


async def handle_recent_folder_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, _, _, sessions, _ = _services(context)
    telegram_id = update.effective_user.id
    session = sessions.get(telegram_id)
    if session is None or session.stage != STAGE_AWAITING_FOLDER:
        return
    folder_name = query.data[len(CB_RECENT_FOLDER_PREFIX):]
    await _set_folder_and_ask_destination(query.message, context, session, folder_name)


async def handle_destination_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, _, config, sessions, _ = _services(context)
    telegram_id = update.effective_user.id
    session = sessions.get(telegram_id)
    if session is None or session.stage != STAGE_AWAITING_DESTINATION:
        return
    destination = query.data[len(CB_DEST_PREFIX):]
    session.destination = destination
    for label in _destination_targets(destination, config, session.folder):
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

    await query.message.reply_text(
        notify.user_msg_upload_ready(session.folder, destination),
        reply_markup=in_session_keyboard(),
    )


# ── 內部小批複製（收照片中progressive flush、與收尾remainder共用）────

async def _copy_chunk_to_destinations(context: ContextTypes.DEFAULT_TYPE, session, chunk: list, dest_targets: dict) -> bool:
    """
    把一個內部小批複製到所有目的地，寫入 file_index，全部目的地成功才清暫存。
    規格書 §6.3 point 2：每滿 20 張（內部小批）即複製到目的地，不必等使用者按
    「我傳完了」——這裡同時被「收照片中progressive flush」與「收尾remainder」呼叫。
    回傳這個小批是否所有目的地都成功。
    """
    _, notifier, config, _, logs = _services(context)
    telegram_id = session.telegram_id
    chunk_ok_labels = {}
    index_rows_by_label: dict[str, list[tuple]] = {}

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
                    rf.received_at, ext=ext, source_path=rf.temp_path, use_exif=config.USE_EXIF_TIME
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

    # file_index：成功或失敗一律記錄（§16.1），檔名一律是實際寫入目的地的檔名
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    index_rows = []
    for label, pairs in index_rows_by_label.items():
        for rf, actual_filename in pairs:
            index_rows.append((now_str, session.name, telegram_id, session.folder, label, actual_filename, rf.file_id))
    # 寫入佇列的 submit 會等待背景執行緒完成（done_event.wait），以 to_thread
    # 執行才不會阻塞事件迴圈（§3）。下方其他 logs.*／members.* 寫入亦同理。
    await asyncio.to_thread(logs.log_file_index_batch, index_rows)

    chunk_fully_ok = all(chunk_ok_labels.values())
    if chunk_fully_ok:
        for rf in chunk:
            await _safe_delete_temp(rf.temp_path, Path(config.TEMP_DIR))
    return chunk_fully_ok


async def _flush_ready_chunks(context: ContextTypes.DEFAULT_TYPE, session) -> None:
    """
    收件階段（STAGE_RECEIVING_PHOTOS）只要累積滿一個內部小批就立刻複製到目的地（§6.3 point 2）。
    只在呼叫端限定於收件階段呼叫——按下「我傳完了」進入緩衝期（STAGE_DEBOUNCE）後收到的
    照片，一律遞延到 _finalize_upload 收齊結案時才一次處理，讓「確認中」階段不再有背景複製動作。
    """
    _, _, config, _, _ = _services(context)
    dest_targets = _destination_targets(session.destination, config, session.folder)
    while len(session.files) - session.flushed_count >= config.BATCH_SIZE:
        chunk = session.files[session.flushed_count: session.flushed_count + config.BATCH_SIZE]
        ok = await _copy_chunk_to_destinations(context, session, chunk, dest_targets)
        session.flushed_count += len(chunk)
        if not ok:
            session.pending_retry_files.extend(chunk)


# ── 收照片 ───────────────────────────────────────────

async def _update_counter_message(update: Update, context: ContextTypes.DEFAULT_TYPE, session) -> None:
    """
    永遠只保留一則「收到照片中… N 張」訊息，不洗版（§6.3.1）。
    做法是刪掉舊的那則、在最下面重發一則新的——而不是原地編輯——
    這樣這則狀態訊息會持續跟著對話移到最下面，不會卡在畫面中間看起來像卡住。
    """
    text = notify.user_msg_receiving(session.received_count)
    markup = in_session_keyboard()
    telegram_id = session.telegram_id

    if session.counter_message_id is not None:
        try:
            await context.bot.delete_message(chat_id=telegram_id, message_id=session.counter_message_id)
        except Exception:
            pass  # 刪不掉（例如使用者手動刪過）也無妨，重發一則新的即可

    sent = await update.effective_message.reply_text(text, reply_markup=markup)
    session.counter_message_id = sent.message_id


async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    members, notifier, config, sessions, logs = _services(context)
    telegram_id = update.effective_user.id
    session = sessions.get(telegram_id)
    if session is None:
        await remind_not_started(update, context)
        return
    if session.stage not in (STAGE_RECEIVING_PHOTOS, STAGE_DEBOUNCE):
        # 緩衝（STAGE_DEBOUNCE）期間仍要正常收照片、計入本批（§6.3）；
        # 其餘非收件階段（選資料夾/目的地中）才提示「請先選資料夾」。
        if session.stage == STAGE_AWAITING_FOLDER or session.stage == STAGE_AWAITING_DESTINATION:
            await update.effective_message.reply_text(notify.user_msg_choose_folder_first())
        return

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

    try:
        await context.bot.send_chat_action(chat_id=telegram_id, action=ChatAction.TYPING)
    except Exception:
        pass  # 「正在輸入中」純粹是視覺提示，失敗不影響正事（§6.3.1）

    storage.ensure_dir(session.temp_dir)
    file_obj = await context.bot.get_file(tg_file.file_id)
    ext = Path(file_obj.file_path or "photo.jpg").suffix or ".jpg"
    local_name = f"{tg_file.file_id}{ext}"
    local_path = session.temp_dir / local_name
    await file_obj.download_to_drive(custom_path=str(local_path))

    rf = ReceivedFile(
        temp_path=local_path,
        filename=local_name,
        file_id=tg_file.file_id,
        media_group_id=getattr(message, "media_group_id", None),
        received_at=datetime.now(),
        is_original_quality=is_original,
    )
    session.add_file(rf)
    session.touch()

    if not is_original and not session.compressed_warned:
        session.compressed_warned = True
        await update.effective_message.reply_text(notify.user_msg_compressed_hint())

    if session.stage == STAGE_RECEIVING_PHOTOS and should_update_counter(session, datetime.now(), config.COUNTER_UPDATE_SEC):
        session.counter_last_update = datetime.now()
        await _update_counter_message(update, context, session)

    if session.stage == STAGE_RECEIVING_PHOTOS:
        # 每滿一個內部小批（預設 20 張）就立刻複製到目的地，不必等「我傳完了」（§6.3 point 2）。
        # 緩衝期間（STAGE_DEBOUNCE）收到的照片刻意不在這裡分批複製，全部遞延到
        # debounce 結束、確定收齊後才一次處理（見 _finalize_upload），
        # 避免「確認中」階段還在背景默默複製造成混淆。
        await _flush_ready_chunks(context, session)

    if session.stage == STAGE_DEBOUNCE:
        # 緩衝期間收到新照片：一律計入本批、一律重新計時（§6.3，確保不漏收）。
        # 畫面上「確認中」訊息的更新則節流至固定每 COUNTER_UPDATE_SEC 秒一次（跟收件
        # 階段共用同一套節流），避免密集連傳時頻繁刪訊息/發訊息觸發 Telegram 限流（429）。
        if should_update_counter(session, datetime.now(), config.COUNTER_UPDATE_SEC):
            session.counter_last_update = datetime.now()
            if session.confirm_message_id is not None:
                try:
                    await context.bot.delete_message(chat_id=telegram_id, message_id=session.confirm_message_id)
                except Exception:
                    pass
            try:
                sent = await update.effective_message.reply_text(notify.user_msg_confirming(session.received_count))
                session.confirm_message_id = sent.message_id
            except Exception:
                pass
        _schedule_debounce(context, telegram_id, restart=True)


async def handle_unsupported_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """非照片檔案（影片、PDF、貼圖、語音等）：合理拒絕並提示，不崩潰（§8 E12）。"""
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
    await query.answer()
    _, _, _, sessions, _ = _services(context)
    telegram_id = update.effective_user.id
    session = sessions.get(telegram_id)
    if session is None:
        return
    if session.stage == STAGE_DEBOUNCE:
        # 緩衝期間重複點擊：不重新計時、不重算張數（§6.3），但仍要回覆一次
        # 「確認中」讓使用者知道有被接收到，避免原本完全無回應、看起來像沒反應而一直猛戳。
        if session.confirm_message_id is not None:
            try:
                await context.bot.delete_message(chat_id=telegram_id, message_id=session.confirm_message_id)
            except Exception:
                pass
        sent = await query.message.reply_text(notify.user_msg_confirming(session.received_count))
        session.confirm_message_id = sent.message_id
        session.counter_last_update = datetime.now()  # 重設節流起點，避免緊接著的照片又立刻觸發一次更新
        return
    if session.stage != STAGE_RECEIVING_PHOTOS:
        return  # 其餘階段沒有這顆按鈕可點
    session.finish_clicked = True
    if session.counter_message_id is not None:
        # 收件階段的「📥 收到照片中…」訊息在此結束任務，點了我傳完了之後
        # 一律改由「⏳ 確認中…」接手，避免兩則訊息同時留在畫面上。
        try:
            await context.bot.delete_message(chat_id=telegram_id, message_id=session.counter_message_id)
        except Exception:
            pass
        session.counter_message_id = None
    session.enter_stage(STAGE_DEBOUNCE)
    sent = await query.message.reply_text(notify.user_msg_confirming(session.received_count))
    session.confirm_message_id = sent.message_id
    session.counter_last_update = datetime.now()  # 節流起點歸零，緩衝期間的更新從這裡開始算
    _schedule_debounce(context, telegram_id)


async def _debounce_fire(context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_id = context.job.data["telegram_id"]
    _, _, _, sessions, _ = _services(context)
    session = sessions.get(telegram_id)
    if session is None or session.stage != STAGE_DEBOUNCE:
        return
    await _finalize_upload(context, session, timed_out=False)


# ── 逾時保險（忘記按「我傳完了」）─────────────────────

async def check_session_timeouts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """由 job_queue 定期呼叫，掃描 session 逾時與遺棄清理（§6.4）。"""
    _, _, config, sessions, _ = _services(context)
    now = datetime.now()
    abandoned_max_min = getattr(config, "ABANDONED_SESSION_MAX_MIN", 60)
    for session in list(sessions.all_sessions()):
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


# ── 實際處理一次上傳（收尾）───────────────────────────

async def _finalize_upload(context: ContextTypes.DEFAULT_TYPE, session, timed_out: bool) -> None:
    members, notifier, config, sessions, logs = _services(context)
    telegram_id = session.telegram_id

    if session.confirm_message_id is not None:
        # 收齊結案、正式開始寫入目的地：「⏳ 確認中…」的任務結束，改由下面的
        # 「📤 上傳中」進度條接手，避免兩則狀態訊息同時留在畫面上。
        try:
            await context.bot.delete_message(chat_id=telegram_id, message_id=session.confirm_message_id)
        except Exception:
            pass
        session.confirm_message_id = None

    session.enter_stage(STAGE_PROCESSING)

    total = session.received_count
    # 收照片過程中已經以內部小批分次寫入目的地（§6.3 point 2），這裡只需要
    # 處理「剩餘不足一個小批」的尾巴，加上先前分批失敗、待重試的部分。
    remainder = session.files[session.flushed_count:]
    processed = session.flushed_count
    progress_message = None
    if total > 0:
        progress_message = await _safe_send(
            context, telegram_id, notify.user_msg_uploading(progress_bar(processed, total))
        )

    dest_targets = _destination_targets(session.destination, config, session.folder)
    all_chunk_failures: list[ReceivedFile] = list(session.pending_retry_files)
    session.pending_retry_files = []

    if remainder:
        for chunk in chunk_files(remainder, config.BATCH_SIZE):
            ok = await _copy_chunk_to_destinations(context, session, chunk, dest_targets)
            session.flushed_count += len(chunk)
            if not ok:
                all_chunk_failures.extend(chunk)

            processed += len(chunk)
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
                        rf.received_at, ext=ext, source_path=rf.temp_path, use_exif=config.USE_EXIF_TIME
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
    await asyncio.to_thread(
        logs.log_upload, now_str, session.name, telegram_id, session.folder, dest_label_text, total,
        "成功" if overall_ok else "部分失敗",
    )
    await asyncio.to_thread(members.push_recent_folder, telegram_id, session.folder, dest_label_text)

    if overall_ok:
        text = notify.user_msg_done(total, session.folder, dest_label_text)
        if DEST_ONEDRIVE_LABEL in session.destinations:
            text += "\n" + notify.user_msg_onedrive_cloud_note()
        await _safe_send(context, telegram_id, text, reply_markup=correction_keyboard())
        await notifier.notify_admin(notify.msg_upload_success(session.name, session.folder, total, dest_label_text))

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
        await _safe_send(context, telegram_id, notify.user_msg_partial_pending())
        nas_status = "✅" if not session.destinations.get(DEST_NAS_LABEL, DestinationOutcome(DEST_NAS_LABEL)).failed else "❌ 失敗"
        od_status = "✅" if not session.destinations.get(DEST_ONEDRIVE_LABEL, DestinationOutcome(DEST_ONEDRIVE_LABEL)).failed else "❌ 失敗"
        if session.destination == DEST_BOTH_LABEL:
            await notifier.notify_admin(notify.msg_both_partial_failure(session.name, session.folder, nas_status, od_status))

    if timed_out and session.received_count > 0:
        await _safe_send(context, telegram_id, "⏱️ 太久沒有動作，已自動幫你把剛剛收到的照片處理完成")

    if config.ONEDRIVE_FREE_SPACE and DEST_ONEDRIVE_LABEL in session.destinations:
        onedrive_paths = [p for _, p in session.destinations[DEST_ONEDRIVE_LABEL].written_paths]
        await asyncio.to_thread(storage.free_onedrive_space, onedrive_paths)

    sessions.clear(telegram_id)


# ── 重新開始 ─────────────────────────────────────────

async def handle_restart_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, _, _, sessions, _ = _services(context)
    session = sessions.get(update.effective_user.id)
    if session is None:
        return
    session.enter_stage(STAGE_AWAITING_RESTART_CONFIRM)
    await query.message.reply_text(
        notify.user_msg_restart_confirm(session.received_count),
        reply_markup=restart_confirm_keyboard(),
    )


async def handle_restart_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    members, notifier, config, sessions, logs = _services(context)
    telegram_id = update.effective_user.id
    session = sessions.get(telegram_id)
    if session is None:
        return

    residue_rows = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    for label, outcome in session.destinations.items():
        for _file_id, path in outcome.written_paths:
            path = Path(path)
            residue_rows.append((now_str, session.name, telegram_id, "中止殘留", str(path.parent), path.name,
                                  "使用者重新開始，此為已寫入的中止殘留"))
    if residue_rows:
        await asyncio.to_thread(logs.log_cleanup_batch, residue_rows)
        for label, outcome in session.destinations.items():
            if outcome.written_paths:
                await notifier.notify_admin(
                    notify.msg_restart_residue(session.name, session.folder or "(未命名)", len(outcome.written_paths))
                )

    if session.temp_dir is not None:
        await _safe_delete_temp(session.temp_dir, Path(config.TEMP_DIR))

    sessions.clear(telegram_id)
    await query.message.reply_text(notify.user_msg_restart_done())


async def handle_restart_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, _, _, sessions, _ = _services(context)
    session = sessions.get(update.effective_user.id)
    if session is None:
        return
    session.enter_stage(STAGE_RECEIVING_PHOTOS)
    await query.message.reply_text("好的，繼續原本的上傳", reply_markup=in_session_keyboard())


# ── 傳錯復原 ─────────────────────────────────────────

async def handle_correction_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, _, _, sessions, _ = _services(context)
    telegram_id = update.effective_user.id
    batch = sessions.get_last_batch(telegram_id)
    if batch is None or batch.corrected:
        return  # 重複點擊 / 無可更正批次一律忽略（§6.3）
    context.user_data[AWAITING_CORRECTION_FLAG] = True
    await query.message.reply_text("這批要改放到哪個資料夾？請直接打字輸入：")


async def _apply_correction(update: Update, context: ContextTypes.DEFAULT_TYPE, new_folder: str) -> None:
    members, notifier, config, sessions, logs = _services(context)
    telegram_id = update.effective_user.id
    batch = sessions.get_last_batch(telegram_id)
    if batch is None or batch.corrected or not new_folder:
        return
    batch.corrected = True  # 首次生效後立即失效，避免重複複製（§6.3）

    # 搬移可能需要一點時間（重試、多目的地），先讓使用者知道請求已收到、
    # 正在處理中，避免看起來像卡住（規格書 §6.3.1 的即時回饋精神同樣適用於此）。
    await update.effective_message.reply_text(notify.user_msg_correction_processing(new_folder))

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
    failed_file_ids: set[str] = set()
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
                cleanup_rows.append((now_str, batch.destination_label, telegram_id, "傳錯更正",
                                      str(old_path.parent), old_path.name, f"已改放至「{new_folder}」"))
            else:
                failed_file_ids.add(file_id)

    total_moved = len(all_file_ids - failed_file_ids)

    if cleanup_rows:
        await asyncio.to_thread(logs.log_cleanup_batch, cleanup_rows)
    if index_rows:
        await asyncio.to_thread(logs.log_file_index_batch, index_rows)

    await update.effective_message.reply_text(notify.user_msg_correction_done(total_moved, new_folder))
    await asyncio.to_thread(
        logs.log_upload, now_str, uploader_name, telegram_id,
        f"{batch.folder} → {new_folder}", batch.destination_label, total_moved, "傳錯更正",
    )
    await notifier.notify_admin(
        notify.msg_correction(uploader_name, batch.folder, new_folder, total_moved)
    )
