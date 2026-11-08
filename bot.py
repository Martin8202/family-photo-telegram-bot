"""
主程式：收發 Telegram 訊息、路由到各 handler（規格書 §12）。

啟動時自動執行（§17、V2 §3.6、§3.8）：
1. 檢查 config.py 有沒有缺設定項（缺了就直接列出來、不啟動）
2. Google Drive 資料夾健檢（`G:` 還沒掛上就等一下再試）
3. 「最近資料夾」V1 舊紀錄轉換
4. 掃描暫存區、復原未完成批次（§4.3）
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

import config
import notify
import storage
from folder_search import FolderIndex
from handlers import folder_picker, register, upload
from keyboards import (
    CB_APPROVE_PREFIX,
    CB_CORRECTION,
    CB_DUP_COPY,
    CB_DUP_SKIP,
    CB_FP_PREFIX,
    CB_UPLOAD_AGAIN,
    CB_FINISH,
    CB_CONTINUE_RECEIVING,
    CB_REGISTER,
    CB_REJECT_PREFIX,
    CB_RESTART,
    CB_RESTART_CANCEL,
    CB_RESTART_CONFIRM,
)
from logs import DataLogs
from members import MembersStore, STATUS_APPROVED, STATUS_PENDING
from notify import Notifier
from state import DEST_DRIVE_LABEL, SessionManager

DATA_DIR = Path(__file__).parent / "data"
LOG_DIR = Path(__file__).parent / "logs"

# long polling 的等待秒數（PTB 預設 10）。
#
# 這不是「每 N 秒去問一次有沒有訊息」的空轉輪詢——getUpdates 送出後會**掛在
# 那裡等**，Telegram 一有訊息就立刻回應。所以拉長等待時間**不會讓訊息變慢**，
# 只是讓沒人使用時的請求數變少：10 秒改 50 秒，閒置時的請求量直接降為 1/5。
#
# PTB 會自動把 HTTP 的 read timeout 加上這個秒數（`Bot.get_updates` 內
# `read_timeout = arg_read_timeout + timeout`），所以不必另外調整連線逾時。
POLL_TIMEOUT_SEC = 50

# 開機後 Google Drive 電腦版可能還沒把 G: 掛上：健檢失敗時每 30 秒重試，最多 5 分鐘（V2 §3.6）。
# 等待期間不收訊息，Telegram 會先保留，等好了照常處理。
STARTUP_DRIVE_RETRY_SEC = 30
STARTUP_DRIVE_MAX_WAIT_SEC = 300

# log 保留天數：每天午夜換一支新檔，最多留這麼多支舊檔（約一週）。
LOG_RETENTION_DAYS = 7

# 平常只有例行 INFO、出事才需要看的第三方 logger（見 setup_logging 說明）。
NOISY_LOGGERS = ("httpx", "apscheduler.executors.default")


def setup_logging() -> None:
    """
    設定 log（規格書 §17）。

    這裡有兩個刻意的取捨：

    **① 每日輪替、只留近一週**

    原本用 `logging.FileHandler`，檔案只增不減，而且是 append 模式——重開機
    也不會重來。實測連續跑 5 天就長到 8.8 MB，長期下去真正的錯誤會被埋在
    雜訊裡（查一次 502 得動用 grep 才挖得出來）。改成每天午夜換檔、保留 7 份。

    **② 壓掉第三方套件的例行 INFO**

    那 8.8 MB 的 54,534 行裡有 49,289 行（90%）是雜訊：httpx 每次 getUpdates
    的 `200 OK`，以及 apscheduler 每 30 秒固定兩行的排程紀錄。真正屬於本程式
    的只有 144 行。兩者都降到 WARNING——**出事時照樣會叫，平常不佔版面**。

    > 網路異常不會因此漏掉：httpx 那行 `502 Bad Gateway` 雖然被壓掉了，但
    > `on_error` 仍會完整記下 traceback（含 `NetworkError: Bad Gateway`
    > 這個原因），診斷資訊一點都沒少。

    `force=True` 不是可有可無的：`basicConfig` 在 root logger 已經有 handler 時
    會**靜默地什麼都不做**——只要有任何模組在這之前先碰過 logging，這裡的設定
    就會整個失效，而且不會有任何錯誤訊息。加上它才能保證輪替與雜訊壓制真的生效。
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
        handlers=[
            TimedRotatingFileHandler(
                LOG_DIR / "photo-bot.log",
                when="midnight",
                backupCount=LOG_RETENTION_DAYS,
                encoding="utf-8",
            ),
            logging.StreamHandler(),
        ],
    )
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


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

    if await upload.handle_folder_text(update, context):
        return

    # 沒有對應中的流程（含舊常駐按鈕「📷 我要上傳照片」送來的字）：提示直接傳照片（附冷卻）
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

def missing_config_keys(cfg) -> list[str]:
    """
    config.py 比 config.example.py 少了哪些設定項（V2 §3.8）。

    `git pull` 更新後常會多出新的設定項，舊的 config.py 沒有就會在執行到一半才出錯。
    以 config.example.py 為準逐項比對，啟動前就把缺的全部列出來。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("config_example", Path(__file__).parent / "config.example.py")
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)
    return [k for k in vars(example) if k.isupper() and not hasattr(cfg, k)]


async def startup_health_check(app: Application, sleep=asyncio.sleep) -> bool:
    """
    確認 Google Drive 的 `照片` 資料夾可用。回傳是否可用。

    開機後 Drive 電腦版可能還沒把 G: 掛上，失敗時先等一下再試，不要直接放棄；
    等到上限仍失敗才通知管理員（照片照樣收進暫存區，之後由復原補送）。
    健檢碰的是 G:，可能卡住，以 to_thread 執行（規格書 §3）。
    """
    cfg = app.bot_data["config"]
    notifier: Notifier = app.bot_data["notifier"]
    if not cfg.HEALTH_CHECK_ON_START:
        return True
    root = upload.drive_root(cfg)
    waited = 0
    while True:
        ok, err = await asyncio.to_thread(storage.health_check, root)
        if ok:
            logger.info("啟動健檢通過：%s", root)
            return True
        if waited >= STARTUP_DRIVE_MAX_WAIT_SEC:
            logger.error("啟動健檢失敗：%s", err)
            await notifier.notify_admin(notify.msg_health_check_failed("Google Drive", err or "未知錯誤"))
            return False
        logger.warning("Google Drive 資料夾還不能用，%s 秒後再試：%s", STARTUP_DRIVE_RETRY_SEC, err)
        await sleep(STARTUP_DRIVE_RETRY_SEC)
        waited += STARTUP_DRIVE_RETRY_SEC


async def startup_migrate_recent_folders(app: Application) -> None:
    """V1 的「最近資料夾」舊紀錄轉換（V2 §3.2）。只能在 Drive 健檢通過後呼叫。"""
    members: MembersStore = app.bot_data["members"]
    root = upload.drive_root(app.bot_data["config"])
    dropped = await asyncio.to_thread(members.migrate_recent_folders, lambda name: (root / name).is_dir())
    if dropped:
        logger.info("最近資料夾舊紀錄在 Google Drive 找不到，已移除：%s", "、".join(dropped))


async def startup_recover_temp(app: Application) -> None:
    """
    掃描 TEMP_DIR，尋找所有未完成的暫存子夾，嘗試自動補送（規格書 §4.3 中斷復原策略）。
    暫存結構：TEMP_DIR / {telegram_id}_{name} / {時間戳}_{folder} / *.jpg + _session_info.json

    V2 只有 Google Drive 一個目的地；資料夾名稱優先讀側車檔（`storage.read_session_info()`），
    沒有側車檔才從暫存子夾名稱推回來。補送結果一律寫回 upload_log.csv 與 file_index.csv，
    與正常上傳流程記錄一致。呼叫端要先確認 Drive 健檢通過。
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

            info = storage.read_session_info(session_dir)
            if info:
                folder_name = info.get("folder", folder_name)
                name = info.get("name", name)
            destination = DEST_DRIVE_LABEL

            dest_targets = upload._destination_targets(cfg, folder_name)

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
                report_lines.append(f"{name}／{folder_name}：{len(files)} 張已補送成功")
            else:
                try:
                    logs.log_upload(now_str, name, telegram_id, folder_name, destination, len(files), "部分失敗(復原)")
                except Exception:
                    logger.exception("復原批次寫入 upload_log 失敗")
                report_lines.append(
                    f"{name}／{folder_name}：{success_count} 成功／{fail_count} 失敗（保留暫存待人工處理）"
                )

    if report_lines:
        logger.info("暫存區復原完成：%s", "; ".join(report_lines))
        await notifier.notify_admin(notify.msg_recovery_report(report_lines))


async def note_activity(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """每則 update 都記下「全家最後動作時間」，資料夾清單據此決定何時重掃（V2 §3.3）。"""
    index = context.application.bot_data.get("folders")
    if index is not None:
        index.note_activity()


async def periodic_timeout_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    await upload.check_session_timeouts(context)
    index: FolderIndex = context.application.bot_data.get("folders")
    if index is not None and index.due():
        # 重掃丟到背景：剛開機第一次可能要好幾分鐘，不能卡住這個每 30 秒的排程
        index.scanning = True
        context.application.create_task(asyncio.to_thread(index.rescan))


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
    # Drive 不能用時不轉換舊紀錄（會全部被當成找不到）、也不補送（照片留在暫存區，下次啟動再補）
    if await startup_health_check(app):
        await startup_migrate_recent_folders(app)
        await startup_recover_temp(app)
    # V2 D17：選單拿掉「📷 我要上傳照片」（直接傳照片就開始）。階段 3 再放「🔍 找照片／檔案」。
    # /start 仍然保留為可用指令（註冊流程靠它），只是不出現在選單裡。
    await app.bot.delete_my_commands()


def build_application() -> Application:
    missing = missing_config_keys(config)
    if missing:
        raise SystemExit(
            "config.py 少了這些設定項，請參考 config.example.py 補上後再啟動：\n  " + "\n  ".join(missing)
        )
    token = storage.read_bot_token(config.BOT_TOKEN_FILE, Path(__file__).parent)
    app = Application.builder().token(token).post_init(on_startup).build()

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
    app.bot_data["data_dir"] = DATA_DIR
    folders = FolderIndex(
        DATA_DIR / "folders.json", upload.drive_root(config),
        config.FOLDER_SCAN_DEPTH, config.FOLDER_SCAN_EXCLUDE, config.FOLDER_RESCAN_IDLE_MIN,
    )
    folders.load()  # 沒有 JSON 時會標記要重掃，由 periodic_timeout_check 在背景掃
    app.bot_data["folders"] = folders

    app.add_handler(TypeHandler(Update, note_activity), group=-1)
    app.add_handler(CommandHandler("start", register.handle_start))
    # 舊選單的 /upload（V2 已拿掉）：提示直接傳照片
    app.add_handler(CommandHandler("upload", upload.remind_not_started))

    app.add_handler(CallbackQueryHandler(register.handle_register_button, pattern=f"^{CB_REGISTER}$"))
    app.add_handler(CallbackQueryHandler(register.handle_approve, pattern=f"^{CB_APPROVE_PREFIX}"))
    app.add_handler(CallbackQueryHandler(register.handle_reject, pattern=f"^{CB_REJECT_PREFIX}"))

    app.add_handler(CallbackQueryHandler(folder_picker.handle_button, pattern=f"^{CB_FP_PREFIX}"))
    app.add_handler(CallbackQueryHandler(upload.handle_finish_button, pattern=f"^{CB_FINISH}$"))
    app.add_handler(CallbackQueryHandler(
        upload.handle_continue_receiving_button, pattern=f"^{CB_CONTINUE_RECEIVING}$"
    ))
    app.add_handler(CallbackQueryHandler(upload.handle_restart_button, pattern=f"^{CB_RESTART}$"))
    app.add_handler(CallbackQueryHandler(upload.handle_restart_confirm, pattern=f"^{CB_RESTART_CONFIRM}$"))
    app.add_handler(CallbackQueryHandler(upload.handle_restart_cancel, pattern=f"^{CB_RESTART_CANCEL}$"))
    app.add_handler(CallbackQueryHandler(upload.handle_correction_button, pattern=f"^{CB_CORRECTION}$"))
    app.add_handler(CallbackQueryHandler(upload.handle_upload_again_button, pattern=f"^{CB_UPLOAD_AGAIN}$"))
    app.add_handler(CallbackQueryHandler(upload.handle_duplicate_copy, pattern=f"^{CB_DUP_COPY}$"))
    app.add_handler(CallbackQueryHandler(upload.handle_duplicate_skip, pattern=f"^{CB_DUP_SKIP}$"))

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
    app.run_polling(allowed_updates=Update.ALL_TYPES, timeout=POLL_TIMEOUT_SEC)


if __name__ == "__main__":
    main()
