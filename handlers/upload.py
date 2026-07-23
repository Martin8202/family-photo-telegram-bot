"""上傳照片流程（規格書 §6、§7）。"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes

import notify
import storage
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

AWAITING_CORRECTION_FLAG = "awaiting_correction_folder"


def _services(context: ContextTypes.DEFAULT_TYPE):
    bd = context.application.bot_data
    return bd["members"], bd["notifier"], bd["config"], bd["sessions"], bd["logs"]


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
    if member is None or member.status != "已開通":
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
        ok_nas, err_nas = storage.health_check(Path(config.DEST_NAS))
        ok_od, err_od = storage.health_check(Path(config.DEST_ONEDRIVE))
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
            reply_markup=folder_choice_keyboard(recent),
        )
    else:
        await update.effective_message.reply_text("請直接打字輸入資料夾名稱：")


async def _set_folder_and_ask_destination(update_message, context: ContextTypes.DEFAULT_TYPE, session, folder_name: str):
    folder_name = storage.sanitize_folder_name(folder_name)
    if not folder_name:
        await update_message.reply_text("資料夾名稱不能是空的或全部是特殊符號，請重新輸入")
        return
    session.folder = folder_name
    session.enter_stage(STAGE_AWAITING_DESTINATION)
    await update_message.reply_text(
        f"資料夾：{folder_name}\n請選擇要存到哪裡：",
        reply_markup=destination_keyboard(),
    )


async def handle_folder_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """處理「輸入新資料夾名稱」的文字訊息。回傳 True 代表已處理。"""
    _, _, _, sessions, _ = _services(context)
    telegram_id = update.effective_user.id
    session = sessions.get(telegram_id)
    if session is None:
        return False
    if session.stage == STAGE_AWAITING_FOLDER:
        await _set_folder_and_ask_destination(update.effective_message, context, session, update.effective_message.text)
        return True
    if context.user_data.get(AWAITING_CORRECTION_FLAG):
        context.user_data[AWAITING_CORRECTION_FLAG] = False
        await _apply_correction(update, context, storage.sanitize_folder_name(update.effective_message.text))
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
    session.enter_stage(STAGE_RECEIVING_PHOTOS, now)

    await query.message.reply_text(
        notify.user_msg_upload_ready(session.folder, destination),
        reply_markup=in_session_keyboard(),
    )


# ── 收照片 ───────────────────────────────────────────

async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    members, notifier, config, sessions, logs = _services(context)
    telegram_id = update.effective_user.id
    session = sessions.get(telegram_id)
    if session is None:
        await update.effective_message.reply_text(notify.user_msg_not_started())
        return
    if session.stage != STAGE_RECEIVING_PHOTOS:
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

    if should_update_counter(session, datetime.now(), config.COUNTER_UPDATE_SEC):
        session.counter_last_update = datetime.now()
        await update.effective_message.reply_text(
            notify.user_msg_receiving(session.received_count),
            reply_markup=in_session_keyboard(),
        )

    if session.stage == STAGE_DEBOUNCE:
        # 緩衝期間收到新照片：計入本批並重新計時（§6.3）
        _schedule_debounce(context, telegram_id, restart=True)


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
    if session is None or session.stage != STAGE_RECEIVING_PHOTOS:
        return  # 重複點擊等後續一律忽略（§6.3）
    session.finish_clicked = True
    session.enter_stage(STAGE_DEBOUNCE)
    await query.message.reply_text(notify.user_msg_confirming())
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
    """由 job_queue 定期呼叫，掃描所有 session 是否完全閒置逾時（§6.4）。"""
    _, _, config, sessions, _ = _services(context)
    now = datetime.now()
    for session in list(sessions.all_sessions()):
        if session.is_idle_timed_out(config.SESSION_TIMEOUT_MIN, now):
            if session.received_count > 0:
                await _finalize_upload(context, session, timed_out=True)
            else:
                sessions.clear(session.telegram_id)


# ── 實際處理一次上傳（收尾）───────────────────────────

async def _finalize_upload(context: ContextTypes.DEFAULT_TYPE, session, timed_out: bool) -> None:
    members, notifier, config, sessions, logs = _services(context)
    telegram_id = session.telegram_id
    session.enter_stage(STAGE_PROCESSING)

    total = session.received_count
    chunks = chunk_files(session.files, config.BATCH_SIZE)
    processed = 0
    progress_message = None
    if total > 0:
        progress_message = await context.bot.send_message(
            chat_id=telegram_id, text=notify.user_msg_uploading(progress_bar(0, total))
        )

    dest_targets = _destination_targets(session.destination, config, session.folder)
    all_chunk_failures: list[ReceivedFile] = []

    for chunk in chunks:
        chunk_ok_labels = {}
        for label, dest_dir in dest_targets.items():
            outcome = session.destinations.setdefault(label, DestinationOutcome(label=label))
            results = []
            for rf in chunk:
                def _do_copy(rf=rf, dest_dir=dest_dir):
                    ext = Path(rf.temp_path).suffix or ".jpg"
                    filename = storage.build_filename(
                        rf.received_at, ext=ext, source_path=rf.temp_path, use_exif=config.USE_EXIF_TIME
                    )
                    return storage.copy_file_with_retry(
                        rf.temp_path, dest_dir, filename, config.RETRY_TIMES, config.RETRY_DELAYS
                    )

                result = await asyncio.to_thread(_do_copy)
                results.append(result)
                if config.WRITE_THROTTLE_SEC:
                    await asyncio.sleep(config.WRITE_THROTTLE_SEC)

            ok = all(r.success for r in results)
            chunk_ok_labels[label] = ok
            if ok:
                outcome.written_paths.extend([r.dest_path for r in results])
            else:
                outcome.failed = True
                outcome.error = next((r.error for r in results if not r.success), "未知錯誤")
                await notifier.notify_admin(
                    notify.msg_write_failure(session.name, session.folder, label, outcome.error)
                )

        # file_index：成功或失敗一律記錄（§16.1）
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        index_rows = []
        for label in dest_targets:
            for rf in chunk:
                index_rows.append((now_str, session.name, telegram_id, session.folder, label, rf.filename, rf.file_id))
        logs.log_file_index_batch(index_rows)

        chunk_fully_ok = all(chunk_ok_labels.values())
        if chunk_fully_ok:
            for rf in chunk:
                try:
                    storage.safe_delete_in_temp(rf.temp_path, Path(config.TEMP_DIR))
                except storage.TempFenceViolation:
                    pass
        else:
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
                outcome.written_paths.extend([r.dest_path for r in retry_results])

    overall_ok = all(not o.failed for o in session.destinations.values())
    if overall_ok:
        for rf in all_chunk_failures:
            try:
                storage.safe_delete_in_temp(rf.temp_path, Path(config.TEMP_DIR))
            except storage.TempFenceViolation:
                pass
        try:
            storage.safe_delete_in_temp(session.temp_dir, Path(config.TEMP_DIR))
        except storage.TempFenceViolation:
            pass

    dest_label_text = session.destination
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    logs.log_upload(now_str, session.name, telegram_id, session.folder, dest_label_text, total,
                     "成功" if overall_ok else "部分失敗")
    members.push_recent_folder(telegram_id, session.folder, dest_label_text)

    if overall_ok:
        text = notify.user_msg_done(total, session.folder, dest_label_text)
        if DEST_ONEDRIVE_LABEL in session.destinations:
            text += "\n" + notify.user_msg_onedrive_cloud_note()
        await context.bot.send_message(chat_id=telegram_id, text=text, reply_markup=correction_keyboard())
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
        await context.bot.send_message(chat_id=telegram_id, text=notify.user_msg_partial_pending())
        nas_status = "✅" if not session.destinations.get(DEST_NAS_LABEL, DestinationOutcome(DEST_NAS_LABEL)).failed else "❌ 失敗"
        od_status = "✅" if not session.destinations.get(DEST_ONEDRIVE_LABEL, DestinationOutcome(DEST_ONEDRIVE_LABEL)).failed else "❌ 失敗"
        if session.destination == DEST_BOTH_LABEL:
            await notifier.notify_admin(notify.msg_both_partial_failure(session.name, session.folder, nas_status, od_status))

    if timed_out and session.received_count > 0:
        await context.bot.send_message(chat_id=telegram_id, text="⏱️ 太久沒有動作，已自動幫你把剛剛收到的照片處理完成")

    if config.ONEDRIVE_FREE_SPACE and DEST_ONEDRIVE_LABEL in session.destinations:
        onedrive_paths = session.destinations[DEST_ONEDRIVE_LABEL].written_paths
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
        for path in outcome.written_paths:
            residue_rows.append((now_str, session.name, telegram_id, "中止殘留", str(Path(path).parent), Path(path).name,
                                  "使用者重新開始，此為已寫入的中止殘留"))
    if residue_rows:
        logs.log_cleanup_batch(residue_rows)
        for label, outcome in session.destinations.items():
            if outcome.written_paths:
                await notifier.notify_admin(
                    notify.msg_restart_residue(session.name, session.folder or "(未命名)", len(outcome.written_paths))
                )

    if session.temp_dir is not None:
        try:
            storage.safe_delete_in_temp(session.temp_dir, Path(config.TEMP_DIR))
        except storage.TempFenceViolation:
            pass

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

    total_moved = 0
    cleanup_rows = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    roots = _destination_roots(config)
    for label, paths in batch.written_paths.items():
        new_dir = roots[label] / new_folder
        for old_path in paths:
            old_path = Path(old_path)

            def _do_copy(old_path=old_path, new_dir=new_dir):
                return storage.copy_file_with_retry(old_path, new_dir, old_path.name, config.RETRY_TIMES, config.RETRY_DELAYS)

            result = await asyncio.to_thread(_do_copy)
            if result.success:
                total_moved += 1
                cleanup_rows.append((now_str, batch.destination_label, telegram_id, "傳錯更正",
                                      str(old_path.parent), old_path.name, f"已改放至「{new_folder}」"))

    if cleanup_rows:
        logs.log_cleanup_batch(cleanup_rows)

    await update.effective_message.reply_text(notify.user_msg_correction_done(total_moved, new_folder))
    uploader = members.get(telegram_id)
    uploader_name = uploader.name if uploader else str(telegram_id)
    await notifier.notify_admin(
        notify.msg_correction(uploader_name, batch.folder, new_folder, total_moved)
    )
