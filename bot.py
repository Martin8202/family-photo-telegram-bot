"""
主程式：收發 Telegram 訊息、路由到各 handler（規格書 §12）。

啟動時自動執行兩件事（§17）：
1. SMB / OneDrive 寫入健檢（§4.1、§4.2）
2. 掃描暫存區、復原未完成批次（§4.3）
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import notify
import storage
from handlers import register, upload
from keyboards import (
    CB_APPROVE_PREFIX,
    CB_CORRECTION,
    CB_DEST_PREFIX,
    CB_FINISH,
    CB_RECENT_FOLDER_PREFIX,
    CB_REGISTER,
    CB_REJECT_PREFIX,
    CB_RESTART,
    CB_RESTART_CANCEL,
    CB_RESTART_CONFIRM,
)
from logs import DataLogs
from members import MembersStore, STATUS_APPROVED, STATUS_PENDING
from notify import Notifier
from state import SessionManager

DATA_DIR = Path(__file__).parent / "data"
LOG_DIR = Path(__file__).parent / "logs"

UPLOAD_BUTTON_TEXT = "📷 我要上傳照片"


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / "photo-bot.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


logger = logging.getLogger("photo-bot")


# ── 統一文字/照片路由（先檢查身分，再分派）────────────

async def route_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    members: MembersStore = context.application.bot_data["members"]
    telegram_id = update.effective_user.id
    member = members.get(telegram_id)

    if member is None or member.status not in (STATUS_APPROVED, STATUS_PENDING):
        await register.show_welcome(update, context)
        return

    # 註冊姓名輸入（未開通者也可能在填名字）
    if await register.handle_name_input(update, context):
        return

    if member.status == STATUS_PENDING:
        await register.handle_unapproved_contact(update, context, member)
        return

    text = (update.effective_message.text or "").strip()
    if text == UPLOAD_BUTTON_TEXT:
        await upload.handle_start_upload(update, context)
        return

    if await upload.handle_folder_text(update, context):
        return

    # 沒有對應中的流程：提示從按鈕開始
    sessions: SessionManager = context.application.bot_data["sessions"]
    if not sessions.has_active(telegram_id):
        await update.effective_message.reply_text(notify.user_msg_not_started())


async def route_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    members: MembersStore = context.application.bot_data["members"]
    telegram_id = update.effective_user.id
    member = members.get(telegram_id)

    if member is None:
        await register.show_welcome(update, context)
        return
    if member.status == STATUS_PENDING:
        await register.handle_unapproved_contact(update, context, member)
        return
    if member.status != STATUS_APPROVED:
        await register.show_welcome(update, context)
        return

    await upload.handle_photo_message(update, context)


# ── 啟動流程：健檢 + 暫存區復原（§4.1、§4.3、§17）─────

async def startup_health_check(app: Application) -> None:
    cfg = app.bot_data["config"]
    notifier: Notifier = app.bot_data["notifier"]
    if not cfg.HEALTH_CHECK_ON_START:
        return
    for label, path in (("家裡硬碟", cfg.DEST_NAS), ("OneDrive", cfg.DEST_ONEDRIVE)):
        ok, err = storage.health_check(Path(path))
        if not ok:
            logger.error("啟動健檢失敗：%s %s", label, err)
            await notifier.notify_admin(notify.msg_health_check_failed(label, err or "未知錯誤"))
        else:
            logger.info("啟動健檢通過：%s", label)


async def startup_recover_temp(app: Application) -> None:
    """
    掃描 TEMP_DIR，尋找所有未完成的暫存子夾，嘗試自動補送（規格書 §4.3 中斷復原策略）。
    暫存結構：TEMP_DIR / {telegram_id}_{name} / {時間戳}_{folder} / *.jpg
    """
    cfg = app.bot_data["config"]
    if not cfg.RECOVER_ON_START:
        return
    notifier: Notifier = app.bot_data["notifier"]
    logs: DataLogs = app.bot_data["logs"]
    temp_root = Path(cfg.TEMP_DIR)
    if not temp_root.exists():
        return

    report_lines: list[str] = []
    for user_dir in temp_root.iterdir():
        if not user_dir.is_dir() or "_" not in user_dir.name:
            continue
        try:
            telegram_id_str, name = user_dir.name.split("_", 1)
            telegram_id = int(telegram_id_str)
        except ValueError:
            continue

        for session_dir in user_dir.iterdir():
            if not session_dir.is_dir():
                continue
            files = [p for p in session_dir.iterdir() if p.is_file()]
            if not files:
                continue
            # 第二層命名：{時間戳}_{目標資料夾}
            parts = session_dir.name.split("_", 2)
            folder_name = parts[2] if len(parts) >= 3 else session_dir.name

            dest_dir = Path(cfg.DEST_NAS) / folder_name
            success_count = 0
            fail_count = 0
            for f in files:
                result = storage.copy_file_with_retry(f, dest_dir, f.name, cfg.RETRY_TIMES, cfg.RETRY_DELAYS)
                if result.success:
                    success_count += 1
                    try:
                        storage.safe_delete_in_temp(f, temp_root)
                    except storage.TempFenceViolation:
                        pass
                else:
                    fail_count += 1

            if fail_count == 0:
                try:
                    storage.safe_delete_in_temp(session_dir, temp_root)
                except storage.TempFenceViolation:
                    pass
                report_lines.append(f"{name}／{folder_name}：{success_count} 張已補送成功")
            else:
                report_lines.append(f"{name}／{folder_name}：{success_count} 成功／{fail_count} 失敗（保留暫存待人工處理）")

    if report_lines:
        logger.info("暫存區復原完成：%s", "; ".join(report_lines))
        await notifier.notify_admin(notify.msg_recovery_report(report_lines))


async def periodic_timeout_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    await upload.check_session_timeouts(context)


async def on_startup(app: Application) -> None:
    await startup_health_check(app)
    await startup_recover_temp(app)
    await app.bot.set_my_commands([BotCommand("start", "開始使用")])


def build_application() -> Application:
    app = Application.builder().token(config.BOT_TOKEN).post_init(on_startup).build()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    members = MembersStore(DATA_DIR / "members.json")
    data_logs = DataLogs(DATA_DIR)
    sessions = SessionManager()
    notifier = Notifier(app.bot, config.ADMIN_ID)

    app.bot_data["members"] = members
    app.bot_data["logs"] = data_logs
    app.bot_data["sessions"] = sessions
    app.bot_data["notifier"] = notifier
    app.bot_data["config"] = config

    app.add_handler(CommandHandler("start", register.handle_start))

    app.add_handler(CallbackQueryHandler(register.handle_register_button, pattern=f"^{CB_REGISTER}$"))
    app.add_handler(CallbackQueryHandler(register.handle_approve, pattern=f"^{CB_APPROVE_PREFIX}"))
    app.add_handler(CallbackQueryHandler(register.handle_reject, pattern=f"^{CB_REJECT_PREFIX}"))

    app.add_handler(CallbackQueryHandler(upload.handle_recent_folder_button, pattern=f"^{CB_RECENT_FOLDER_PREFIX}"))
    app.add_handler(CallbackQueryHandler(upload.handle_destination_button, pattern=f"^{CB_DEST_PREFIX}"))
    app.add_handler(CallbackQueryHandler(upload.handle_finish_button, pattern=f"^{CB_FINISH}$"))
    app.add_handler(CallbackQueryHandler(upload.handle_restart_button, pattern=f"^{CB_RESTART}$"))
    app.add_handler(CallbackQueryHandler(upload.handle_restart_confirm, pattern=f"^{CB_RESTART_CONFIRM}$"))
    app.add_handler(CallbackQueryHandler(upload.handle_restart_cancel, pattern=f"^{CB_RESTART_CANCEL}$"))
    app.add_handler(CallbackQueryHandler(upload.handle_correction_button, pattern=f"^{CB_CORRECTION}$"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, route_text))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, route_photo))

    if app.job_queue is not None:
        app.job_queue.run_repeating(periodic_timeout_check, interval=30, first=30)

    return app


def main() -> None:
    setup_logging()
    app = build_application()
    logger.info("photo-bot 啟動中…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
