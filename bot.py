"""
主程式：收發 Telegram 訊息、路由到各 handler（規格書 §12）。

啟動時自動執行兩件事（§17）：
1. SMB / OneDrive 寫入健檢（§4.1、§4.2）
2. 掃描暫存區、復原未完成批次（§4.3）
"""

from __future__ import annotations

import asyncio
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
    CB_CORRECTION_FOLDER_PREFIX,
    CB_DEST_PREFIX,
    CB_FINISH,
    CB_CONTINUE_RECEIVING,
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

    # 註冊姓名輸入必須最先檢查：此時 member 必然還是 None（登記流程的必經狀態），
    # 若先做下方的 member is None 判斷會永遠攔截掉，使用者的名字就進不了 members.json。
    if await register.handle_name_input(update, context):
        return

    member = members.get(telegram_id)

    if member is None or member.status not in (STATUS_APPROVED, STATUS_PENDING):
        await register.show_welcome(update, context)
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

    # 沒有對應中的流程：提示從按鈕開始（附冷卻，避免連續訊息洗版）
    sessions: SessionManager = context.application.bot_data["sessions"]
    if not sessions.has_active(telegram_id):
        await upload.remind_not_started(update, context)


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


async def route_unsupported_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """影片、一般文件、語音、貼圖等非照片訊息：合理拒絕並提示，不崩潰（§8 E12）。"""
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

    await upload.handle_unsupported_media(update, context)


# ── 啟動流程：健檢 + 暫存區復原（§4.1、§4.3、§17）─────

async def startup_health_check(app: Application) -> None:
    cfg = app.bot_data["config"]
    notifier: Notifier = app.bot_data["notifier"]
    if not cfg.HEALTH_CHECK_ON_START:
        return
    targets = [("OneDrive", cfg.DEST_ONEDRIVE)]
    if cfg.ENABLE_NAS:
        targets.insert(0, ("家裡硬碟", cfg.DEST_NAS))
    for label, path in targets:
        # 健檢會對網芳寫入測試檔，SMB 卡住時可能耗時數十秒；必須以 to_thread 執行，
        # 否則會阻塞整個 asyncio 事件迴圈、讓 bot 對所有人失去回應（規格書 §3）。
        ok, err = await asyncio.to_thread(storage.health_check, Path(path))
        if not ok:
            logger.error("啟動健檢失敗：%s %s", label, err)
            await notifier.notify_admin(notify.msg_health_check_failed(label, err or "未知錯誤"))
        else:
            logger.info("啟動健檢通過：%s", label)


async def startup_recover_temp(app: Application) -> None:
    """
    掃描 TEMP_DIR，尋找所有未完成的暫存子夾，嘗試自動補送（規格書 §4.3 中斷復原策略）。
    暫存結構：TEMP_DIR / {telegram_id}_{name} / {時間戳}_{folder} / *.jpg + _session_info.json

    目的地資訊優先讀取 `storage.read_session_info()` 側車檔（記錄實際選擇的目的地，
    支援「兩邊都存」）；沒有側車檔（例如更早期留下的暫存、或選目的地前就中斷）
    才退回只補送到區網硬碟的舊行為，並在報告中註明是用猜的。
    補送結果一律寫回 upload_log.csv 與 file_index.csv，與正常上傳流程記錄一致。
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
            telegram_id_str, dir_name = user_dir.name.split("_", 1)
            telegram_id = int(telegram_id_str)
        except ValueError:
            continue

        for session_dir in user_dir.iterdir():
            if not session_dir.is_dir():
                continue
            files = [p for p in session_dir.iterdir() if p.is_file() and p.name != storage.SESSION_INFO_FILENAME]
            if not files:
                continue

            # 第二層命名：{時間戳}_{目標資料夾}（側車檔存在時以側車檔內容為準，更準確）
            parts = session_dir.name.split("_", 2)
            folder_name = parts[2] if len(parts) >= 3 else session_dir.name
            name = dir_name
            destination_guessed = False

            info = storage.read_session_info(session_dir)
            if info:
                folder_name = info.get("folder", folder_name)
                name = info.get("name", name)
                destination = info.get("destination")
            else:
                destination = None
            if not destination:
                destination = "家裡硬碟"
                destination_guessed = True

            dest_targets = upload._destination_targets(destination, cfg, folder_name)

            success_count = 0
            fail_count = 0
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            index_rows = []
            for label, dest_dir in dest_targets.items():
                for f in files:
                    # 復原時同樣套用檔名規則（EXIF 拍攝時間優先），與正常流程一致，
                    # 不可直接用暫存檔名 {file_id}.jpg（那不是給人看的正式檔名，§10）。
                    # 沒有原始接收時間，改用暫存檔的修改時間當回退基準（EXIF 有的話仍優先）。
                    received_fallback = datetime.fromtimestamp(f.stat().st_mtime)
                    target_name = storage.build_filename(
                        received_fallback, ext=f.suffix or ".jpg", source_path=f, use_exif=cfg.USE_EXIF_TIME
                    )
                    result = storage.copy_file_with_retry(f, dest_dir, target_name, cfg.RETRY_TIMES, cfg.RETRY_DELAYS)
                    file_id = f.stem  # 暫存檔名格式固定為 {file_id}{副檔名}
                    actual_name = result.dest_path.name if result.success and result.dest_path else target_name
                    index_rows.append((now_str, name, telegram_id, folder_name, label, actual_name, file_id))
                    if result.success:
                        success_count += 1
                    else:
                        fail_count += 1

            if index_rows:
                try:
                    logs.log_file_index_batch(index_rows)
                except Exception:
                    logger.exception("復原批次寫入 file_index 失敗，繼續處理其餘批次")

            all_ok = fail_count == 0
            guess_note = "（未找到目的地紀錄，已預設補送到區網硬碟，請確認是否正確）" if destination_guessed else ""
            if all_ok:
                for f in files:
                    try:
                        storage.safe_delete_in_temp(f, temp_root)
                    except storage.TempFenceViolation:
                        pass
                try:
                    storage.safe_delete_in_temp(session_dir, temp_root)
                except storage.TempFenceViolation:
                    pass
                try:
                    logs.log_upload(now_str, name, telegram_id, folder_name, destination, len(files), "成功(復原)")
                except Exception:
                    logger.exception("復原批次寫入 upload_log 失敗")
                report_lines.append(f"{name}／{folder_name}：{len(files)} 張已補送成功{guess_note}")
            else:
                try:
                    logs.log_upload(now_str, name, telegram_id, folder_name, destination, len(files), "部分失敗(復原)")
                except Exception:
                    logger.exception("復原批次寫入 upload_log 失敗")
                report_lines.append(
                    f"{name}／{folder_name}：{success_count} 成功／{fail_count} 失敗（保留暫存待人工處理）{guess_note}"
                )

    if report_lines:
        logger.info("暫存區復原完成：%s", "; ".join(report_lines))
        await notifier.notify_admin(notify.msg_recovery_report(report_lines))


async def periodic_timeout_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    await upload.check_session_timeouts(context)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    全域錯誤處理（規格書 §17）：接住所有沒被個別 handler 攔下的例外。

    v2 完全沒有註冊 error handler，於是「✅ 我傳完了」因 callback query 逾時而
    整個處理函式爆掉時，例外只是被框架靜默記掉——使用者沒反應、管理員沒通知、
    也沒有任何線索可循。這裡一律記 log ＋ 通知管理員，並對使用者回一句不揭露
    技術細節的訊息。
    """
    err = context.error
    logger.error("未處理的例外", exc_info=err)
    notifier: Notifier = context.application.bot_data.get("notifier")
    if notifier is not None:
        where = type(update).__name__ if update is not None else "unknown"
        await notifier.notify_admin(notify.msg_unhandled_error(where, type(err).__name__, str(err)))
    try:
        if isinstance(update, Update) and update.effective_message is not None:
            await update.effective_message.reply_text(notify.user_msg_error_generic())
    except Exception:
        pass  # 連錯誤通知都送不出去時，不能再往外拋而讓錯誤處理本身變成新的錯誤


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
    app.add_handler(CallbackQueryHandler(
        upload.handle_continue_receiving_button, pattern=f"^{CB_CONTINUE_RECEIVING}$"
    ))
    app.add_handler(CallbackQueryHandler(upload.handle_restart_button, pattern=f"^{CB_RESTART}$"))
    app.add_handler(CallbackQueryHandler(upload.handle_restart_confirm, pattern=f"^{CB_RESTART_CONFIRM}$"))
    app.add_handler(CallbackQueryHandler(upload.handle_restart_cancel, pattern=f"^{CB_RESTART_CANCEL}$"))
    app.add_handler(CallbackQueryHandler(upload.handle_correction_button, pattern=f"^{CB_CORRECTION}$"))
    app.add_handler(CallbackQueryHandler(
        upload.handle_correction_folder_button, pattern=f"^{CB_CORRECTION_FOLDER_PREFIX}"
    ))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, route_text))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, route_photo))
    # 非照片訊息（影片、一般文件、語音、貼圖…）：合理拒絕並提示，不靜默忽略（§8 E12）
    app.add_handler(MessageHandler(
        filters.VIDEO | filters.ANIMATION | filters.AUDIO | filters.VOICE
        | filters.Sticker.ALL | (filters.Document.ALL & ~filters.Document.IMAGE),
        route_unsupported_media,
    ))

    # 全域錯誤處理必須註冊（規格書 §17），否則未攔下的例外會靜默消失
    app.add_error_handler(on_error)

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
