"""集中管理所有 Telegram inline / reply 按鈕，避免文字散落各處難以維護。"""

from __future__ import annotations

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)

CB_REGISTER = "register"
CB_APPROVE_PREFIX = "approve:"
CB_REJECT_PREFIX = "reject:"
CB_START_UPLOAD = "start_upload"
CB_RESTART = "restart"
CB_RESTART_CONFIRM = "restart_confirm"
CB_RESTART_CANCEL = "restart_cancel"
CB_FINISH = "finish"
CB_CONTINUE_RECEIVING = "continue_receiving"
CB_CORRECTION = "correction"
CB_UPLOAD_AGAIN = "upload_again"
CB_DUP_COPY = "dup_copy"
CB_DUP_SKIP = "dup_skip"
CB_FIND_PREFIX = "find:"        # 找東西的按鈕（只回位置文字，不上傳）
CB_FIND_PICK_PREFIX = "find:pick:"
CB_FIND_RETYPE = "find:retype"
CB_PENDING_KEEP = "pend:keep"
CB_PENDING_DROP = "pend:drop"
# 選資料夾畫面（handlers/folder_picker.py）。資料夾一律用編號（fp:pick:3），名稱存在
# context.user_data：Telegram 的 callback_data 最多 64 bytes，第 2 層中文路徑放不下。
CB_FP_PREFIX = "fp:"
CB_FP_PICK_PREFIX = "fp:pick:"
CB_FP_SEARCH = "fp:search"
CB_FP_NEW = "fp:new"
CB_FP_CREATE = "fp:create"
CB_FP_RETYPE = "fp:retype"
CB_FP_ADD_DATE = "fp:adddate"
CB_FP_NO_DATE = "fp:nodate"
CB_FP_SUB = "fp:sub"                 # 📂 建在某個資料夾裡
CB_FP_PARENT_PREFIX = "fp:parent:"   # 選了上層資料夾（帶編號）
CB_FP_NEW_PARENT = "fp:newparent"    # 上層也是新的


def register_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("📝 我要註冊", callback_data=CB_REGISTER)]])


def approve_reject_keyboard(telegram_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 開通", callback_data=f"{CB_APPROVE_PREFIX}{telegram_id}"),
        InlineKeyboardButton("❌ 拒絕", callback_data=f"{CB_REJECT_PREFIX}{telegram_id}"),
    ]])


def clear_persistent_keyboard() -> ReplyKeyboardRemove:
    """
    清除輸入框下方的常駐按鈕（v3.12）。

    上傳功能已移進 Telegram 的 ☰ 指令選單（見規格書 §6.1），常駐按鈕變成多餘——
    而且它在使用者開始打字時會被 Telegram 用戶端自動收起來，本來就不是可靠的入口。

    ⚠️ 必須**主動送出這個移除指令**：Telegram 的常駐鍵盤是「每個對話的持續狀態」，
    只是不再附上新的鍵盤並不會讓舊的消失——已經看得到按鈕的使用者會一直看得到。
    """
    return ReplyKeyboardRemove()


def picker_keyboard(labels: list[str], with_restart_button: bool) -> InlineKeyboardMarkup:
    """
    選資料夾畫面（V2 §3.1）：最近 5 個 → 🔍 找資料夾／建新的 →（上傳時）🔄 重新開始。
    11/08 實測後拿掉「➕ 新的資料夾」：打字一律先搜尋，找不到再從結果新建，不用分兩個入口。
    CB_FP_NEW 仍保留處理，舊訊息上的按鈕被點到時不會沒反應。
    """
    rows = [[InlineKeyboardButton(label, callback_data=f"{CB_FP_PICK_PREFIX}{i}")] for i, label in enumerate(labels)]
    rows.append([InlineKeyboardButton("🔍 找資料夾／建新的", callback_data=CB_FP_SEARCH)])
    rows.append([InlineKeyboardButton("📂 建在某個資料夾裡", callback_data=CB_FP_SUB)])
    if with_restart_button:
        rows.append(restart_row())
    return InlineKeyboardMarkup(rows)


def search_results_keyboard(labels: list[str], new_label, with_restart_button: bool) -> InlineKeyboardMarkup:
    """
    搜尋結果（V2 §3.3）：相近資料夾，最後一顆是「➕ 都不是，新建」（名稱不合規則時不給）。
    new_label 是帶引號的顯示文字，例如「金門」或「行程」（在「照片」裡的「金門」裡）。
    """
    rows = [[InlineKeyboardButton(label, callback_data=f"{CB_FP_PICK_PREFIX}{i}")] for i, label in enumerate(labels)]
    if new_label:
        rows.append([InlineKeyboardButton(f"➕ 都不是，新建{new_label}", callback_data=CB_FP_CREATE)])
    if with_restart_button:
        rows.append(restart_row())
    return InlineKeyboardMarkup(rows)


def search_none_keyboard(new_label, with_restart_button: bool) -> InlineKeyboardMarkup:
    rows = []
    if new_label:
        rows.append([InlineKeyboardButton(f"✅ 新建{new_label}", callback_data=CB_FP_CREATE)])
    rows.append([InlineKeyboardButton("✏️ 重打", callback_data=CB_FP_RETYPE)])
    if with_restart_button:
        rows.append(restart_row())
    return InlineKeyboardMarkup(rows)


def parent_keyboard(labels: list[str], with_restart_button: bool, new_parent=None) -> InlineKeyboardMarkup:
    """「📂 建在某個資料夾裡」第一步：選上層資料夾（只有第 1 層）；打的名稱不存在時可以新建上層。"""
    rows = [[InlineKeyboardButton(label, callback_data=f"{CB_FP_PARENT_PREFIX}{i}")] for i, label in enumerate(labels)]
    if new_parent:
        rows.append([InlineKeyboardButton(f"➕ 新建上層「{new_parent}」", callback_data=CB_FP_NEW_PARENT)])
    if with_restart_button:
        rows.append(restart_row())
    return InlineKeyboardMarkup(rows)


def subfolder_keyboard(labels: list[str], with_restart_button: bool) -> InlineKeyboardMarkup:
    """選好上層後：裡面已經有的資料夾（點了就放進去）＋換一個上層。要新建就直接打名稱。"""
    rows = [[InlineKeyboardButton(label, callback_data=f"{CB_FP_PICK_PREFIX}{i}")] for i, label in enumerate(labels)]
    rows.append([InlineKeyboardButton("↩️ 換一個上層資料夾", callback_data=CB_FP_SUB)])
    if with_restart_button:
        rows.append(restart_row())
    return InlineKeyboardMarkup(rows)


def add_date_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📅 加今天的日期", callback_data=CB_FP_ADD_DATE),
        InlineKeyboardButton("不用", callback_data=CB_FP_NO_DATE),
    ]])


def in_session_keyboard(show_finish: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if show_finish:
        rows.append([InlineKeyboardButton("⚡ 沒照片了，發送並寫入", callback_data=CB_FINISH)])
        rows.append([InlineKeyboardButton("📷 我還有照片沒傳完", callback_data=CB_CONTINUE_RECEIVING)])
    rows.append([InlineKeyboardButton("🔄 重新開始", callback_data=CB_RESTART)])
    return InlineKeyboardMarkup(rows)


def restart_row() -> list:
    return [InlineKeyboardButton("🔄 重新開始", callback_data=CB_RESTART)]


def restart_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 確定重來", callback_data=CB_RESTART_CONFIRM),
        InlineKeyboardButton("❌ 取消，繼續原本的", callback_data=CB_RESTART_CANCEL),
    ]])


def duplicate_decision_keyboard() -> InlineKeyboardMarkup:
    """偵測到重複照片時，問使用者要不要還是存一份（§10）。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ 要，還是存一份", callback_data=CB_DUP_COPY)],
        [InlineKeyboardButton("❌ 不用了，跳過", callback_data=CB_DUP_SKIP)],
    ])


def find_results_keyboard(labels: list[str]) -> InlineKeyboardMarkup:
    """找東西的結果（V2 §3.5）：按鈕是 🔗，跟上傳時的 📥 明顯不同。"""
    rows = [[InlineKeyboardButton(label, callback_data=f"{CB_FIND_PICK_PREFIX}{i}")] for i, label in enumerate(labels)]
    rows.append([InlineKeyboardButton("✏️ 重打", callback_data=CB_FIND_RETYPE)])
    return InlineKeyboardMarkup(rows)


def find_none_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✏️ 重打", callback_data=CB_FIND_RETYPE)]])


def pending_reminder_keyboard() -> InlineKeyboardMarkup:
    """沒選資料夾的提醒（V2 §3.7）。"""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📥 還要上傳", callback_data=CB_PENDING_KEEP),
        InlineKeyboardButton("❌ 不用存", callback_data=CB_PENDING_DROP),
    ]])


def correction_keyboard() -> InlineKeyboardMarkup:
    """
    上傳完成後的按鈕列。V2 拿掉「📷 再傳一批」：直接傳照片就會開新的一批（D17）。
    CB_UPLOAD_AGAIN 仍保留，舊訊息上的按鈕被點到時回一句提示。
    """
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("↩️ 這批傳錯了", callback_data=CB_CORRECTION),
    ]])
