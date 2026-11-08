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
import re
from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes

import notify
import storage
from folder_search import SEP, has_date, location_text, match
from keyboards import (
    CB_FP_ADD_DATE,
    CB_FP_CREATE,
    CB_FP_NEW,
    CB_FP_NEW_PARENT,
    CB_FP_NO_DATE,
    CB_FP_PARENT_PREFIX,
    CB_FP_PICK_PREFIX,
    CB_FP_RETYPE,
    CB_FP_SEARCH,
    CB_FP_SUB,
    add_date_keyboard,
    parent_keyboard,
    picker_keyboard,
    subfolder_keyboard,
    search_none_keyboard,
    search_results_keyboard,
)

logger = logging.getLogger("photo-bot.picker")

PICKER_KEY = "picker"
MODE_UPLOAD = "upload"
MODE_CORRECTION = "correction"
SUBFOLDER_LIST_MAX = 8  # 選好上層後最多列幾個裡面原本的資料夾
_PATH_SPLIT = re.compile(r"[/\\／＼]")  # 「上層/新名稱」的分隔符號（半形、全形都算）


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


def parse_new_folder(text: str, folders: list) -> tuple:
    """
    打的字當成新資料夾時的路徑。回傳 (路徑或 None, 錯誤說明或 None, 要拿去搜尋的字)。
    - 「名稱」→ 建在「照片」底下（第 1 層）
    - 「上層/名稱」→ 建在第 1 層資料夾裡（第 2 層，11/08 實測補上）；/、\\、全形／都算
      上層還不存在時兩層一起建（11/08 實測補上），拿上層去搜尋，讓人看得到是不是打錯了既有的名稱
    """
    parts = _PATH_SPLIT.split(text)
    if len(parts) == 1:
        try:
            return storage.validate_folder_name(text), None, text
        except storage.FolderNameError as exc:
            return None, str(exc), text
    parent, child = parts[0].strip(), parts[-1].strip()
    parent_exists = any(f["path"] == parent for f in folders)
    query = child if (child and parent_exists) else parent
    if len(parts) > 2:
        return None, "新資料夾只能建在「照片」底下，或「照片」裡的某個資料夾裡：請打「上層/新名稱」", query
    if not parent:
        return None, "「/」前面要打上層資料夾的名稱", child
    if not child:
        return None, f"要建在「{parent}」裡面的話，請在「/」後面打新資料夾的名稱", query
    try:
        if not parent_exists:
            parent = storage.validate_folder_name(parent)
        return f"{parent}{SEP}{storage.validate_folder_name(child)}", None, query
    except storage.FolderNameError as exc:
        return None, str(exc), query


def new_folder_label(path: str, folders: list = ()) -> str:
    """新建按鈕上的名稱：「金門」、「行程」（在「照片」裡的「金門」裡）、上層也是新的就寫清楚兩個一起建。"""
    name = path.split(SEP)[-1]
    if SEP not in path:
        return f"「{name}」"
    parent = path.split(SEP)[0]
    if not any(f["path"] == parent for f in folders):
        return f"「{parent}」和裡面的「{name}」"
    return f"「{name}」{location_text(path)}"


def _with_date(path: str) -> str:
    """在最後一段名稱前面加今天的日期（第 2 層只加在新資料夾名稱上）。"""
    head, _, name = path.rpartition(SEP)
    dated = f"{datetime.now():%Y-%m-%d}{name}"
    return f"{head}{SEP}{dated}" if head else dated


def _first_level(folders: list) -> list:
    return [f for f in folders if SEP not in f["path"]]


async def _show_parents(context: ContextTypes.DEFAULT_TYPE, chat_id: int, st: dict, paths: list, header: str,
                        new_parent=None):
    st["parents"] = paths
    st["new_parent"] = new_parent
    return await _send(
        context, chat_id, header,
        reply_markup=parent_keyboard([f"📂 {p}" for p in paths], st["mode"] == MODE_UPLOAD, new_parent),
    )


async def _start_sub(context: ContextTypes.DEFAULT_TYPE, chat_id: int, st: dict) -> None:
    """📂 建在某個資料夾裡：先列出最近用過的資料夾的第 1 層（第 2 層的取它的上層），也可以打字找。"""
    members = context.application.bot_data["members"]
    recent = [f["name"] for f in members.family_recent_folders(10)]
    parents = list(dict.fromkeys(p.split(SEP)[0] for p in recent))[:5]
    st["stage"] = "parent"
    await _show_parents(context, chat_id, st, parents, notify.user_msg_sub_parent_prompt(bool(parents)))


async def _sub_text(update: Update, context: ContextTypes.DEFAULT_TYPE, st: dict, text: str) -> None:
    """建子資料夾流程中打字：選上層時＝找第 1 層資料夾；選好上層後＝新資料夾名稱。"""
    chat_id = update.effective_user.id
    if st["stage"] == "parent":
        index = context.application.bot_data.get("folders")
        found = match(text, _first_level(index.folders)) if index is not None else []
        paths = [f["path"] for f in found]
        # 找不到、或找到的都不是：可以新建這個上層（11/08 實測補上）；打的名稱剛好已存在就不給
        try:
            new_parent = storage.validate_folder_name(text) if text not in paths else None
            name_error = None
        except storage.FolderNameError as exc:
            new_parent, name_error = None, str(exc)
        header = notify.user_msg_sub_parent_prompt(True) if paths else notify.user_msg_sub_parent_none(text)
        if name_error:
            header += "\n" + name_error
        await _show_parents(context, chat_id, st, paths, header, new_parent)
        return
    try:
        if _PATH_SPLIT.search(text):
            raise storage.FolderNameError("這裡只要打新資料夾的名稱，不用打「/」")
        name = storage.validate_folder_name(text)
    except storage.FolderNameError as exc:
        await _send(context, chat_id, str(exc))
        return
    st["new_name"] = f"{st['parent']}{SEP}{name}"
    st["stage"] = None
    if not has_date(name):
        await _send(context, chat_id, notify.user_msg_ask_add_date(name), reply_markup=add_date_keyboard())
        return
    await _finish(update, context, st["mode"], st["new_name"], is_new=True)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str) -> None:
    """打字＝搜尋（D13 方案 A、D23）；找不到或都不是，再從結果新建。"""
    text = (update.effective_message.text or "").strip()
    st = _state(context)
    st["mode"] = mode
    if st.get("stage"):
        await _sub_text(update, context, st, text)
        return
    index = context.application.bot_data.get("folders")
    folders = index.folders if index is not None else []
    new_name, name_error, query = parse_new_folder(text, folders)
    results = index.search(query) if index is not None else []
    st["choices"] = [f["path"] for f in results]
    st["new_name"] = new_name
    with_restart = mode == MODE_UPLOAD
    chat_id = update.effective_user.id
    new_label = new_folder_label(new_name, folders) if new_name else None
    new_parent = None
    if new_name and SEP in new_name and not any(f["path"] == new_name.split(SEP)[0] for f in folders):
        new_parent = new_name.split(SEP)[0]  # 上層也是新的，提醒一句，看看是不是打錯既有的名稱

    # 不能新建時一定說明原因（11/08 實測：有搜尋結果時原本沒說，看起來像壞掉）
    if results:
        msg = notify.user_msg_search_results()
        if name_error:
            msg += "\n\n⚠️ 不能新建：" + name_error
        if new_parent:
            msg += "\n\n" + notify.user_msg_new_parent_note(new_parent, True)
        await _send(
            context, chat_id, msg,
            reply_markup=search_results_keyboard([folder_label(p) for p in st["choices"]], new_label, with_restart),
        )
        return
    msg = notify.user_msg_search_none(query)
    if name_error:
        msg += "\n" + name_error
    if new_parent:
        msg += "\n" + notify.user_msg_new_parent_note(new_parent, False)
    await _send(context, chat_id, msg, reply_markup=search_none_keyboard(new_label, with_restart))


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
        st["stage"] = None
        await _send(context, chat_id, notify.user_msg_picker_search_prompt())
    elif data in (CB_FP_NEW, CB_FP_RETYPE):
        st["stage"] = None
        await _send(context, chat_id, notify.user_msg_picker_new_prompt())
    elif data == CB_FP_SUB:
        await _start_sub(context, chat_id, st)
    elif data == CB_FP_NEW_PARENT:
        parent = st.get("new_parent")
        if not parent:
            return
        st["parent"], st["stage"], st["choices"] = parent, "name", []
        await _send(context, chat_id, notify.user_msg_new_parent_name_prompt(parent))
    elif data.startswith(CB_FP_PARENT_PREFIX):
        try:
            parent = st.get("parents", [])[int(data[len(CB_FP_PARENT_PREFIX):])]
        except (ValueError, IndexError):
            return
        st["parent"], st["stage"] = parent, "name"
        # 先讓人看到裡面原本有哪些資料夾（新的在前），點了就放進去，避免重複建（11/08 實測）
        index = context.application.bot_data.get("folders")
        subs = [f for f in (index.folders if index is not None else [])
                if f["path"].startswith(parent + SEP) and f["path"].count(SEP) == 1]
        subs.sort(key=lambda f: f.get("mtime", 0), reverse=True)
        st["choices"] = [f["path"] for f in subs[:SUBFOLDER_LIST_MAX]]
        await _send(
            context, chat_id, notify.user_msg_sub_name_prompt(parent, bool(st["choices"])),
            reply_markup=subfolder_keyboard(
                [f"📥 {p.split(SEP)[-1]}" for p in st["choices"]], st["mode"] == MODE_UPLOAD
            ),
        )
    elif data == CB_FP_CREATE:
        name = st.get("new_name")
        if not name:
            return
        if not has_date(name.split(SEP)[-1]):
            await _send(context, chat_id, notify.user_msg_ask_add_date(name.split(SEP)[-1]),
                        reply_markup=add_date_keyboard())
            return
        await _finish(update, context, mode, name, is_new=True)
    elif data in (CB_FP_ADD_DATE, CB_FP_NO_DATE):
        name = st.get("new_name")
        if not name:
            return
        if data == CB_FP_ADD_DATE:
            name = _with_date(name)
        await _finish(update, context, mode, name, is_new=True)


async def _finish(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str, folder: str, is_new: bool) -> None:
    from handlers import upload

    clear(context)
    if is_new:
        index = context.application.bot_data.get("folders")
        if index is not None:
            try:
                if SEP in folder:  # 上層也可能是新的，一起進清單（add_folder 已存在就略過）
                    await asyncio.to_thread(index.add_folder, folder.split(SEP)[0])
                await asyncio.to_thread(index.add_folder, folder)  # 新建的立刻進清單（§3.3）
            except Exception:  # noqa: BLE001 - 清單寫不進去不影響上傳
                logger.exception("新資料夾寫進 folders.json 失敗")
    if mode == MODE_UPLOAD:
        await upload.on_folder_chosen(context, update.effective_user.id, folder)
    else:
        await upload.finish_correction_pick(update, context, folder)
