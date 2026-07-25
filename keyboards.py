"""集中管理所有 Telegram inline / reply 按鈕，避免文字散落各處難以維護。"""

from __future__ import annotations

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)

from state import DEST_BOTH_LABEL, DEST_NAS_LABEL, DEST_ONEDRIVE_LABEL, recent_folder_icon

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
CB_DEST_PREFIX = "dest:"
CB_RECENT_FOLDER_PREFIX = "recent:"
# 更正流程的資料夾按鈕另用一組 prefix：它發生在 session 已結束之後，
# 不可與 session 內的選資料夾按鈕（CB_RECENT_FOLDER_PREFIX）混用。
CB_CORRECTION_FOLDER_PREFIX = "corrfolder:"


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


def folder_choice_keyboard(recent_folders: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for f in recent_folders:
        icon = recent_folder_icon(f["last_dest"])
        rows.append([InlineKeyboardButton(f"{icon} {f['name']}", callback_data=f"{CB_RECENT_FOLDER_PREFIX}{f['name']}")])
    return InlineKeyboardMarkup(rows) if rows else InlineKeyboardMarkup([])


def correction_folder_keyboard(recent_folders: list[dict]) -> InlineKeyboardMarkup:
    """
    「↩️ 這批傳錯了」要改放到哪個資料夾（規格書 §7 第 1 點）。
    規格明訂須同時提供「近 3 次資料夾」按鈕與打字輸入兩種方式，不可只接受打字。
    """
    rows = []
    for f in recent_folders:
        icon = recent_folder_icon(f["last_dest"])
        rows.append([InlineKeyboardButton(
            f"{icon} {f['name']}", callback_data=f"{CB_CORRECTION_FOLDER_PREFIX}{f['name']}"
        )])
    return InlineKeyboardMarkup(rows)


def destination_keyboard(enable_nas: bool = True) -> InlineKeyboardMarkup:
    """
    區網硬碟可透過 config.ENABLE_NAS 整個關閉（例如硬碟暫時故障期間）：
    關閉時只留 OneDrive，「兩邊都存」也一併隱藏，避免使用者選到用不了的選項。
    """
    if not enable_nas:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(f"☁️ {DEST_ONEDRIVE_LABEL}", callback_data=f"{CB_DEST_PREFIX}{DEST_ONEDRIVE_LABEL}")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🏠 {DEST_NAS_LABEL}", callback_data=f"{CB_DEST_PREFIX}{DEST_NAS_LABEL}")],
        [InlineKeyboardButton(f"☁️ {DEST_ONEDRIVE_LABEL}", callback_data=f"{CB_DEST_PREFIX}{DEST_ONEDRIVE_LABEL}")],
        [InlineKeyboardButton(f"🏠☁️ {DEST_BOTH_LABEL}", callback_data=f"{CB_DEST_PREFIX}{DEST_BOTH_LABEL}")],
    ])


def in_session_keyboard(show_finish: bool = True) -> InlineKeyboardMarkup:
    rows = []
    if show_finish:
        rows.append([InlineKeyboardButton("⚡ 沒照片了，發送並寫入", callback_data=CB_FINISH)])
        rows.append([InlineKeyboardButton("📷 我還有照片沒傳完", callback_data=CB_CONTINUE_RECEIVING)])
    rows.append([InlineKeyboardButton("🔄 重新開始", callback_data=CB_RESTART)])
    return InlineKeyboardMarkup(rows)


def restart_row() -> list:
    return [InlineKeyboardButton("🔄 重新開始", callback_data=CB_RESTART)]


def with_restart(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """
    在任何 inline keyboard 底下附加一顆「🔄 重新開始」。
    規格書 §6.5：這顆按鈕在 session 進行中全程可見（選資料夾、選目的地、
    傳照片、處理中皆可見），不是只有收到照片之後才出現。
    """
    rows = list(markup.inline_keyboard) + [restart_row()]
    return InlineKeyboardMarkup(rows)


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


def correction_keyboard() -> InlineKeyboardMarkup:
    """
    上傳完成後的按鈕列。

    「📷 再傳一批」是 contextual 入口：連續傳好幾批是很常見的操作，每次都要繞去
    ☰ 選單很煩；做成 inline 按鈕則是需要時才出現，不佔常駐螢幕空間。
    """
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📷 再傳一批", callback_data=CB_UPLOAD_AGAIN),
        InlineKeyboardButton("↩️ 這批傳錯了", callback_data=CB_CORRECTION),
    ]])


def inactivity_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ 確定傳完了，開始上傳", callback_data=CB_FINISH)],
        [InlineKeyboardButton("📷 我還有照片沒傳完", callback_data=CB_CONTINUE_RECEIVING)],
        restart_row(),
    ])
