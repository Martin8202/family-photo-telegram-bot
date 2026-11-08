"""
找東西（V2 規格 §3.5）：☰「🔍 找照片／檔案」→ 打字 → 列出相近資料夾 → 點一下回位置文字。

- 跟選資料夾畫面共用同一個搜尋（folder_search），但按鈕是「🔗」、點下去只回位置，不上傳。
- 點選單之後「下一句話」才當成要找的字，找完就回到平常（打字不會一直被當成搜尋）。
- 拿不到 Drive 連結（沒用 API），所以回「在『照片』裡的『…』」，請家人到 Drive App 搜尋名稱。
- 按鈕只帶編號，路徑存在 context.user_data（callback_data 最多 64 bytes）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import ContextTypes

import notify
from folder_search import SEP, location_text
from keyboards import CB_FIND_PICK_PREFIX, CB_FIND_RETYPE, find_none_keyboard, find_results_keyboard

FIND_KEY = "find"
WAIT_MAX_MIN = 10  # 點了選單 10 分鐘都沒打字，下一句話就不當成搜尋


def _label(path: str) -> str:
    return f"🔗 {path.split(SEP)[-1]}{location_text(path)}"


def _wait(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.setdefault(FIND_KEY, {})["waiting_since"] = datetime.now()


async def handle_find_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """選單「🔍 找照片／檔案」（/find）。"""
    _wait(context)
    await update.effective_message.reply_text(notify.user_msg_find_prompt())


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """剛點過「找照片／檔案」：這句話就是要找的字。回傳 True 代表已處理。"""
    st = context.user_data.get(FIND_KEY) or {}
    since = st.pop("waiting_since", None)
    if since is None or datetime.now() - since > timedelta(minutes=WAIT_MAX_MIN):
        return False
    text = (update.effective_message.text or "").strip()
    index = context.application.bot_data.get("folders")
    results = index.search(text) if index is not None else []
    st["choices"] = [f["path"] for f in results]
    context.user_data[FIND_KEY] = st
    if results:
        await update.effective_message.reply_text(
            notify.user_msg_find_results(), reply_markup=find_results_keyboard([_label(p) for p in st["choices"]])
        )
    else:
        await update.effective_message.reply_text(notify.user_msg_find_none(text), reply_markup=find_none_keyboard())
    return True


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """所有 find: 按鈕。"""
    from handlers import upload  # 區域匯入，跟 folder_picker 一樣避免循環匯入

    query = update.callback_query
    await upload._safe_answer(query)
    data = query.data
    if data == CB_FIND_RETYPE:
        _wait(context)
        await upload._reply_to_query(query, context, notify.user_msg_find_prompt())
        return
    if not data.startswith(CB_FIND_PICK_PREFIX):
        return
    choices = (context.user_data.get(FIND_KEY) or {}).get("choices") or []
    try:
        path = choices[int(data[len(CB_FIND_PICK_PREFIX):])]
    except (ValueError, IndexError):
        return  # 程式重啟過或結果已換掉，舊按鈕不做事
    await upload._reply_to_query(query, context, notify.user_msg_find_location(path.split(SEP)[-1], location_text(path)))
