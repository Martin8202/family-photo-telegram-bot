"""新成員註冊（規格書 §5）。"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

import notify
from keyboards import approve_reject_keyboard, register_keyboard, start_upload_keyboard
from members import STATUS_APPROVED, STATUS_PENDING, STATUS_REJECTED, MembersStore

AWAITING_NAME_KEY = "awaiting_register_name"


def _services(context: ContextTypes.DEFAULT_TYPE):
    bot_data = context.application.bot_data
    return bot_data["members"], bot_data["notifier"], bot_data["config"]


async def show_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """未登記 / 已拒絕使用者的統一入口：永遠顯示歡迎訊息 + 註冊按鈕（§5.1）。"""
    await update.effective_message.reply_text(notify.user_msg_welcome(), reply_markup=register_keyboard())


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start：使用者首次點 START，bot 主動發出第一則訊息。"""
    members, _, _ = _services(context)
    telegram_id = update.effective_user.id
    member = members.get(telegram_id)
    if member is None:
        await show_welcome(update, context)
        return
    if member.status == STATUS_PENDING:
        await update.effective_message.reply_text(notify.user_msg_pending_review())
        return
    if member.status == STATUS_REJECTED:
        await show_welcome(update, context)
        return
    # 已開通：若正在上傳 session 中，/start 不應該看起來像「重來」，只回報現況，
    # 真正要重來一律走 🔄 重新開始（§6.3、§6.5，避免兩種入口語意混淆）。
    sessions = context.application.bot_data["sessions"]
    session = sessions.get(telegram_id)
    if session is not None:
        await update.effective_message.reply_text(
            f"你正在上傳中喔（資料夾：{session.folder or '（尚未選）'} ／ 已收到 {session.received_count} 張）\n"
            "要重來的話請用下面的『🔄 重新開始』按鈕，不是這裡。"
        )
        return
    await update.effective_message.reply_text("歡迎回來！", reply_markup=start_upload_keyboard())


async def handle_register_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data[AWAITING_NAME_KEY] = True
    await query.message.reply_text(notify.user_msg_ask_name())


async def handle_name_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    處理使用者輸入稱呼的文字訊息。回傳 True 表示這則訊息已被本函式處理完畢
    （呼叫端不需再往下路由到其他 text handler，例如資料夾名稱輸入）。
    """
    if not context.user_data.get(AWAITING_NAME_KEY):
        return False

    members, notifier, config = _services(context)
    telegram_id = update.effective_user.id
    name = (update.effective_message.text or "").strip()
    if not name:
        await update.effective_message.reply_text("請輸入文字稱呼喔")
        return True
    name = name[:30]  # 避免超長字串造成顯示異常（§A9）

    context.user_data[AWAITING_NAME_KEY] = False
    members.register(telegram_id, name)

    await update.effective_message.reply_text(notify.user_msg_pending_review())
    await notifier.notify_admin(
        notify.msg_new_registration(name, telegram_id),
        reply_markup=approve_reject_keyboard(telegram_id),
    )
    return True


async def handle_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    members, notifier, _ = _services(context)
    telegram_id = int(query.data.split(":", 1)[1])
    member = members.approve(telegram_id)
    if member is None:
        return
    await query.edit_message_text(f"已開通：{member.name}（{telegram_id}）")
    await notifier.notify_user(
        telegram_id,
        notify.user_msg_approved() + "\n\n" + notify.user_msg_original_quality_tutorial(),
        reply_markup=start_upload_keyboard(),
    )


async def handle_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    members, _, _ = _services(context)
    telegram_id = int(query.data.split(":", 1)[1])
    member = members.reject(telegram_id)
    if member is None:
        return
    await query.edit_message_text(f"已拒絕：{member.name}（{telegram_id}）")


async def handle_unapproved_contact(update: Update, context: ContextTypes.DEFAULT_TYPE, member) -> None:
    """已登記但未開通者來訊：回覆等待訊息，並通知管理員（§8）。"""
    _, notifier, _ = _services(context)
    await update.effective_message.reply_text(notify.user_msg_pending_review())
    await notifier.notify_admin(notify.msg_unapproved_contact(member.name, member.telegram_id))
