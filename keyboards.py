"""集中管理所有 Telegram inline / reply 按鈕，避免文字散落各處難以維護。"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

from state import DEST_BOTH_LABEL, DEST_NAS_LABEL, DEST_ONEDRIVE_LABEL, recent_folder_icon

CB_REGISTER = "register"
CB_APPROVE_PREFIX = "approve:"
CB_REJECT_PREFIX = "reject:"
CB_START_UPLOAD = "start_upload"
CB_RESTART = "restart"
CB_RESTART_CONFIRM = "restart_confirm"
CB_RESTART_CANCEL = "restart_cancel"
CB_FINISH = "finish"
CB_CORRECTION = "correction"
CB_DEST_PREFIX = "dest:"
CB_RECENT_FOLDER_PREFIX = "recent:"


def register_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("📝 我要註冊", callback_data=CB_REGISTER)]])


def approve_reject_keyboard(telegram_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 開通", callback_data=f"{CB_APPROVE_PREFIX}{telegram_id}"),
        InlineKeyboardButton("❌ 拒絕", callback_data=f"{CB_REJECT_PREFIX}{telegram_id}"),
    ]])


def start_upload_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([["📷 我要上傳照片"]], resize_keyboard=True)


def folder_choice_keyboard(recent_folders: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for f in recent_folders:
        icon = recent_folder_icon(f["last_dest"])
        rows.append([InlineKeyboardButton(f"{icon} {f['name']}", callback_data=f"{CB_RECENT_FOLDER_PREFIX}{f['name']}")])
    return InlineKeyboardMarkup(rows) if rows else InlineKeyboardMarkup([])


def destination_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🏠 {DEST_NAS_LABEL}", callback_data=f"{CB_DEST_PREFIX}{DEST_NAS_LABEL}")],
        [InlineKeyboardButton(f"☁️ {DEST_ONEDRIVE_LABEL}", callback_data=f"{CB_DEST_PREFIX}{DEST_ONEDRIVE_LABEL}")],
        [InlineKeyboardButton(f"📦 {DEST_BOTH_LABEL}", callback_data=f"{CB_DEST_PREFIX}{DEST_BOTH_LABEL}")],
    ])


def in_session_keyboard(show_finish: bool = True) -> InlineKeyboardMarkup:
    rows = []
    if show_finish:
        rows.append([InlineKeyboardButton("✅ 我傳完了", callback_data=CB_FINISH)])
    rows.append([InlineKeyboardButton("🔄 重新開始", callback_data=CB_RESTART)])
    return InlineKeyboardMarkup(rows)


def restart_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 確定重來", callback_data=CB_RESTART_CONFIRM),
        InlineKeyboardButton("❌ 取消，繼續原本的", callback_data=CB_RESTART_CANCEL),
    ]])


def correction_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩️ 這批傳錯了", callback_data=CB_CORRECTION)]])
