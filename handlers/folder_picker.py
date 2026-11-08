"""
選資料夾畫面（V2 規格 §3.1～§3.4）。上傳與「↩️ 這批傳錯了」共用。

- 畫面：全家最近 5 個、🔍 找資料夾、➕ 新的資料夾、（上傳時）🔄 重新開始。
- 打字一律先搜尋（方案 A）；只有按「新建」才會開新資料夾，建在 `照片` 底下。
- 新建的名稱沒有日期時提醒一句，不強制。
- 按鈕只帶編號，資料夾路徑存在 context.user_data["picker"]（callback_data 最多 64 bytes）。
  路徑只會來自我們自己的清單（最近資料夾、folders.json），不信任按鈕送來的文字。

選定後交給 upload.py（上傳 → on_folder_chosen；更正 → finish_correction_pick）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes

import notify
import storage
from folder_search import SEP, has_date, location_text
from keyboards import (
    CB_FP_ADD_DATE,
    CB_FP_CREATE,
    CB_FP_NEW,
    CB_FP_NO_DATE,
    CB_FP_PICK_PREFIX,
    CB_FP_RETYPE,
    CB_FP_SEARCH,
    add_date_keyboard,
    picker_keyboard,
    search_none_keyboard,
    search_results_keyboard,
)

logger = logging.getLogger("photo-bot.picker")

PICKER_KEY = "picker"
MODE_UPLOAD = "upload"
MODE_CORRECTION = "correction"


def folder_label(path: str) -> str:
    """按鈕文字：第 1 層只寫名稱，第 2 層補上在哪個資料夾裡。"""
    name = path.split(SEP)[-1]
    return f"📥 {name}" if SEP not in path else f"📥 {name}{location_text(path)}"


def _state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.setdefault(PICKER_KEY, {"mode": MODE_UPLOAD, "choices": [], "new_name": None})


def clear(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(PICKER_KEY, None)


def current_mode(context: ContextTypes.DEFAULT_TYPE):
    st = context.user_data.get(PICKER_KEY)
    return st["mode"] if st else None


async def _send(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, reply_markup=None):
    from handlers.upload import _safe_send  # 區域匯入：upload 也匯入本模組
    return await _safe_send(context, chat_id, text, reply_markup=reply_markup)


async def show_picker(context: ContextTypes.DEFAULT_TYPE, chat_id: int, mode: str, header: str):
    """發出選資料夾畫面，回傳送出的訊息（失敗為 None）。"""
    members = context.application.bot_data["members"]
    config = context.application.bot_data["config"]
    recent = [f["name"] for f in members.family_recent_folders(getattr(config, "RECENT_FOLDERS_COUNT", 5))]
    context.user_data[PICKER_KEY] = {"mode": mode, "choices": recent, "new_name": None}
    return await _send(
        context, chat_id, header,
        reply_markup=picker_keyboard([folder_label(p) for p in recent], mode == MODE_UPLOAD),
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str) -> None:
    """打字＝搜尋（D13 方案 A、D23）。"""
    text = (update.effective_message.text or "").strip()
    st = _state(context)
    st["mode"] = mode
    index = context.application.bot_data.get("folders")
    results = index.search(text) if index is not None else []
    try:
        new_name = storage.validate_folder_name(text)
        name_error = None
    except storage.FolderNameError as exc:
        new_name, name_error = None, str(exc)
    st["choices"] = [f["path"] for f in results]
    st["new_name"] = new_name
    with_restart = mode == MODE_UPLOAD
    chat_id = update.effective_user.id

    if results:
        await _send(
            context, chat_id, notify.user_msg_search_results(),
            reply_markup=search_results_keyboard([folder_label(p) for p in st["choices"]], new_name, with_restart),
        )
        return
    msg = notify.user_msg_search_none(text)
    if name_error:
        msg += "\n" + name_error
    await _send(context, chat_id, msg, reply_markup=search_none_keyboard(new_name, with_restart))


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """所有 fp: 按鈕。"""
    from handlers import upload

    query = update.callback_query
    await upload._safe_answer(query)
    chat_id = update.effective_user.id
    st = context.user_data.get(PICKER_KEY)
    if st is None:
        return  # 程式重啟過或畫面已用完，舊按鈕不做事
    mode = st["mode"]
    if not upload.picker_still_wanted(context, chat_id, mode):
        return
    data = query.data

    if data.startswith(CB_FP_PICK_PREFIX):
        try:
            folder = st["choices"][int(data[len(CB_FP_PICK_PREFIX):])]
        except (ValueError, IndexError):
            return
        await _finish(update, context, mode, folder, is_new=False)
    elif data == CB_FP_SEARCH:
        await _send(context, chat_id, notify.user_msg_picker_search_prompt())
    elif data in (CB_FP_NEW, CB_FP_RETYPE):
        await _send(context, chat_id, notify.user_msg_picker_new_prompt())
    elif data == CB_FP_CREATE:
        name = st.get("new_name")
        if not name:
            return
        if not has_date(name):
            await _send(context, chat_id, notify.user_msg_ask_add_date(name), reply_markup=add_date_keyboard())
            return
        await _finish(update, context, mode, name, is_new=True)
    elif data in (CB_FP_ADD_DATE, CB_FP_NO_DATE):
        name = st.get("new_name")
        if not name:
            return
        if data == CB_FP_ADD_DATE:
            name = f"{datetime.now():%Y-%m-%d}{name}"
        await _finish(update, context, mode, name, is_new=True)


async def _finish(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str, folder: str, is_new: bool) -> None:
    from handlers import upload

    clear(context)
    if is_new:
        index = context.application.bot_data.get("folders")
        if index is not None:
            try:
                await asyncio.to_thread(index.add_folder, folder)  # 新建的立刻進清單（§3.3）
            except Exception:  # noqa: BLE001 - 清單寫不進去不影響上傳
                logger.exception("新資料夾寫進 folders.json 失敗")
    if mode == MODE_UPLOAD:
        await upload.on_folder_chosen(context, update.effective_user.id, folder)
    else:
        await upload.finish_correction_pick(update, context, folder)
