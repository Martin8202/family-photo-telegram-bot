"""
Antigravity 獨立撰寫之端對端 (E2E) 與情境整合測試套件。

包含：
1. 完整 Telegram 互動流程 (註冊 -> 審核 -> 開通 -> 選擇資料夾/目的地 -> 上傳照片/非照片拒絕 -> 結案)
2. 傳錯更正 (↩️ 這批傳錯了) 與中途重新開始 (🔄 重新開始) 之殘留與待清理清單比對
3. 程式崩潰與暫存區中斷復原 (startup_recover_temp) 測試
4. 多使用者併發上傳隔離與資料完整性測試
5. 重新下載工具 (redownload.py) 整合測試
"""

from __future__ import annotations

import asyncio
import json
import pytest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import config
import notify
import storage
from bot import startup_recover_temp
from handlers import register, upload
from logs import DataLogs
from members import STATUS_APPROVED, STATUS_PENDING, MembersStore
from redownload import download_rows, load_groups
from state import SessionManager


# ── Mock 類別定義 ────────────────────────────────────

class DummyTelegramFile:
    def __init__(self, file_id: str):
        self.file_id = file_id
        self.file_path = f"https://api.telegram.org/file/bot/{file_id}.jpg"

    async def download_to_drive(self, custom_path: str):
        path = Path(custom_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 寫入一張極小的 Dummy JPEG 資料
        path.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xd9")


class DummyBot:
    def __init__(self):
        self.sent_messages = []
        self.chat_actions = []
        self.edited_messages = []
        self.deleted_messages = []
        self.get_file_error: Exception | None = None  # 設了就讓下載失敗，測 §6.3.2 下載重試
        self.get_file_gate: asyncio.Event | None = None  # 設了就把下載擋住，測事件處理層不等下載

    async def send_message(self, chat_id: int, text: str, reply_markup=None):
        msg = DummyMessage(message_id=len(self.sent_messages) + 100, text=text, chat_id=chat_id, bot=self)
        self.sent_messages.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup, "msg": msg})
        return msg

    async def send_chat_action(self, chat_id: int, action: str):
        self.chat_actions.append((chat_id, action))

    async def get_file(self, file_id: str):
        if self.get_file_gate is not None:
            await self.get_file_gate.wait()
        if self.get_file_error is not None:
            raise self.get_file_error
        return DummyTelegramFile(file_id)

    async def delete_message(self, chat_id: int, message_id: int):
        self.deleted_messages.append((chat_id, message_id))

    async def edit_message_text(self, chat_id: int, message_id: int, text: str, reply_markup=None):
        self.edited_messages.append({"chat_id": chat_id, "message_id": message_id, "text": text})

    async def set_my_commands(self, commands):
        pass


class DummyMessage:
    def __init__(self, message_id: int = 1, text: str = "", chat_id: int = 12345, bot: DummyBot = None, photo=None, document=None):
        self.message_id = message_id
        self.text = text
        self.chat_id = chat_id
        self.bot = bot or DummyBot()
        self.photo = photo
        self.document = document
        self.replies = []

    async def reply_text(self, text: str, reply_markup=None):
        sent = await self.bot.send_message(self.chat_id, text, reply_markup)
        self.replies.append({"text": text, "reply_markup": reply_markup})
        return sent

    async def edit_text(self, text: str, reply_markup=None):
        self.text = text
        return self


class DummyUser:
    def __init__(self, user_id: int, name: str):
        self.id = user_id
        self.first_name = name


class DummyCallbackQuery:
    def __init__(self, data: str, user: DummyUser, message: DummyMessage, answer_error: Exception = None):
        self.data = data
        self.from_user = user
        self.message = message
        self.answered = False
        # Telegram 的 callback query 約 15 秒後失效，逾期呼叫 answer() 會拋 BadRequest。
        self.answer_error = answer_error

    async def answer(self):
        if self.answer_error is not None:
            raise self.answer_error
        self.answered = True

    async def edit_message_text(self, text: str, reply_markup=None):
        self.message.text = text
        return self.message


class DummyUpdate:
    def __init__(self, user: DummyUser, message: DummyMessage = None, callback_query: DummyCallbackQuery = None):
        self.effective_user = user
        self.effective_message = message
        self.callback_query = callback_query


class DummyJob:
    def __init__(self, name: str, data: dict):
        self.name = name
        self.data = data
        self.removed = False

    def schedule_removal(self):
        self.removed = True


class DummyJobQueue:
    def __init__(self):
        self.jobs = []

    def get_jobs_by_name(self, name: str):
        return [j for j in self.jobs if j.name == name and not j.removed]

    def run_once(self, callback, when, name: str, data: dict):
        job = DummyJob(name, data)
        self.jobs.append(job)
        return job


class DummyApplication:
    def __init__(self, data_dir: Path, temp_dir: Path, nas_dir: Path, od_dir: Path):
        self.bot = DummyBot()
        self.job_queue = DummyJobQueue()
        self.bot_data = {
            "members": MembersStore(data_dir / "members.json"),
            "logs": DataLogs(data_dir),
            "sessions": SessionManager(),
            "notifier": notify.Notifier(self.bot, admin_id=999999),
            "config": config,
        }
        # 動態設定測試專用路徑
        config.TEMP_DIR = str(temp_dir)
        config.DEST_NAS = str(nas_dir)
        config.DEST_ONEDRIVE = str(od_dir)
        config.ENABLE_NAS = True
        config.HEALTH_CHECK_ON_SESSION = False
        config.HEALTH_CHECK_ON_START = False
        config.FINISH_DEBOUNCE_SEC = 0
        config.WRITE_THROTTLE_SEC = 0  # 測試不需要對網芳的寫入節流，省去等待
        # v3 背景 worker 與雙門檻節流參數（規格書 §3.1、§6.3.1、§6.3.3）
        config.DOWNLOAD_WORKERS = 2
        config.DOWNLOAD_RETRY_TIMES = 1
        config.COUNTER_UPDATE_SEC = 5
        config.COUNTER_UPDATE_COUNT = 8
        config.CONFIRM_UPDATE_SEC = 2
        config.CONFIRM_UPDATE_COUNT = 3
        config.COUNTER_REANCHOR_SEC = 5
        config.CORRECTION_PROMPT_MAX_MIN = 10
        config.STAGE_STUCK_MAX_MIN = 30


class DummyContext:
    def __init__(self, app: DummyApplication):
        self.application = app
        self.bot = app.bot
        self.user_data = {}


# ── 測試案例 ──────────────────────────────────────────

@pytest.fixture
def env(tmp_path):
    data_dir = tmp_path / "data"
    temp_dir = tmp_path / "temp"
    nas_dir = tmp_path / "nas"
    od_dir = tmp_path / "onedrive"
    for d in [data_dir, temp_dir, nas_dir, od_dir]:
        d.mkdir(parents=True, exist_ok=True)
    app = DummyApplication(data_dir, temp_dir, nas_dir, od_dir)
    return app, data_dir, temp_dir, nas_dir, od_dir


@pytest.mark.asyncio
async def test_full_user_registration_and_approval_flow(env):
    """測試全新使用者註冊 -> 管理員收到通知 -> 管理員開通 -> 使用者收到原圖教學與選單。"""
    app, data_dir, _, _, _ = env
    ctx = DummyContext(app)
    user = DummyUser(1001, "媽媽")

    # 1. 首次傳 /start
    msg1 = DummyMessage(1, "/start", 1001, app.bot)
    upd1 = DummyUpdate(user, msg1)
    await register.handle_start(upd1, ctx)
    assert any("登記" in m["text"] for m in app.bot.sent_messages if m["chat_id"] == 1001)

    # 2. 點擊 [📝 我要註冊]
    cb_msg = DummyMessage(2, "", 1001, app.bot)
    cb_upd = DummyUpdate(user, cb_msg, DummyCallbackQuery("register", user, cb_msg))
    await register.handle_register_button(cb_upd, ctx)
    assert ctx.user_data.get("awaiting_register_name") is True

    # 3. 輸入稱呼「媽媽」
    name_msg = DummyMessage(3, "媽媽", 1001, app.bot)
    name_upd = DummyUpdate(user, name_msg)
    handled = await register.handle_name_input(name_upd, ctx)
    assert handled is True
    
    # 檢查 members.json 狀態為 待審核
    members: MembersStore = app.bot_data["members"]
    m = members.get(1001)
    assert m is not None
    assert m.name == "媽媽"
    assert m.status == STATUS_PENDING

    # 檢查管理員 (999999) 是否收到審核按鈕 notification
    admin_msgs = [m for m in app.bot.sent_messages if m["chat_id"] == 999999]
    assert len(admin_msgs) > 0
    assert "媽媽" in admin_msgs[-1]["text"]

    # 4. 管理員點擊 [✅ 開通]
    admin_user = DummyUser(999999, "Admin")
    approve_cb = DummyCallbackQuery("approve:1001", admin_user, admin_msgs[-1]["msg"])
    approve_upd = DummyUpdate(admin_user, admin_msgs[-1]["msg"], approve_cb)
    await register.handle_approve(approve_upd, ctx)

    # 驗證狀態已更新為 已開通，使用者收到開通訊息
    m_after = members.get(1001)
    assert m_after.status == STATUS_APPROVED
    user_latest_msg = [m for m in app.bot.sent_messages if m["chat_id"] == 1001][-1]
    assert "已開通" in user_latest_msg["text"]


@pytest.mark.asyncio
async def test_complete_upload_session_both_destinations(env):
    """測試完整照片上傳流程 (選資料夾 -> 選兩邊都存 -> 傳照片 + 傳非圖片 -> 我傳完了)。"""
    app, data_dir, temp_dir, nas_dir, od_dir = env
    ctx = DummyContext(app)
    user = DummyUser(1001, "媽媽")
    
    # 事前將使用者設為已開通
    members: MembersStore = app.bot_data["members"]
    members.register(1001, "媽媽")
    members.approve(1001)

    # 1. 點擊 📷 我要上傳照片
    msg = DummyMessage(10, "📷 我要上傳照片", 1001, app.bot)
    upd = DummyUpdate(user, msg)
    await upload.handle_start_upload(upd, ctx)
    assert app.bot_data["sessions"].has_active(1001)

    # 2. 輸入資料夾名稱「2026阿嬤八十大壽」
    folder_msg = DummyMessage(11, "2026阿嬤八十大壽", 1001, app.bot)
    folder_upd = DummyUpdate(user, folder_msg)
    await upload.handle_folder_text(folder_upd, ctx)
    session = app.bot_data["sessions"].get(1001)
    assert session.folder == "2026阿嬤八十大壽"

    # 3. 點選目的地 [📦 兩邊都存]
    dest_cb = DummyCallbackQuery("dest:兩邊都存", user, folder_msg)
    dest_upd = DummyUpdate(user, folder_msg, dest_cb)
    await upload.handle_destination_button(dest_upd, ctx)
    assert session.destination == "兩邊都存"

    # 4. 傳送照片 (一張照片 photo，一張檔案原圖 document.image)
    dummy_photo_item = MagicMock()
    dummy_photo_item.file_id = "photo_id_001"
    photo_msg = DummyMessage(12, "", 1001, app.bot, photo=[dummy_photo_item])
    await upload.handle_photo_message(DummyUpdate(user, photo_msg), ctx)

    dummy_doc = MagicMock()
    dummy_doc.file_id = "doc_id_002"
    dummy_doc.mime_type = "image/png"
    doc_msg = DummyMessage(13, "", 1001, app.bot, document=dummy_doc)
    await upload.handle_photo_message(DummyUpdate(user, doc_msg), ctx)

    assert session.received_count == 2

    # 5. 傳送不支援媒體 (影片/語音)
    unsupported_msg = DummyMessage(14, "", 1001, app.bot)
    await upload.handle_unsupported_media(DummyUpdate(user, unsupported_msg), ctx)
    assert any("只支援傳照片" in m["text"] for m in app.bot.sent_messages if m["chat_id"] == 1001)

    # 6. 點擊 [✅ 我傳完了]
    finish_cb = DummyCallbackQuery("finish", user, photo_msg)
    finish_upd = DummyUpdate(user, photo_msg, finish_cb)
    await upload.handle_finish_button(finish_upd, ctx)

    # 觸發 debounce 手動執行
    await upload._debounce_fire(DummyContext(app) if False else ctx_with_job(ctx, 1001))

    # 7. 驗證產生的檔案與 CSV 紀錄
    nas_target = nas_dir / "2026阿嬤八十大壽"
    od_target = od_dir / "2026阿嬤八十大壽"
    assert nas_target.exists()
    assert od_target.exists()
    assert len(list(nas_target.glob("*.jpg"))) == 2
    assert len(list(od_target.glob("*.jpg"))) == 2

    # 驗證 CSV log
    logs: DataLogs = app.bot_data["logs"]
    upload_rows = logs.upload_log.read_all_rows()
    assert len(upload_rows) == 1
    assert upload_rows[0]["張數"] == "2"
    assert upload_rows[0]["資料夾"] == "2026阿嬤八十大壽"
    assert upload_rows[0]["結果"] == "成功"

    file_index_rows = logs.file_index.read_all_rows()
    assert len(file_index_rows) == 4  # 兩張照片 x 兩個目的地 = 4 列


@pytest.mark.asyncio
async def test_debounce_confirm_message_dual_layer_update(env):
    """
    緩衝期間（點了「我傳完了」之後）密集連傳照片時的「確認中」訊息（規格書 §6.3.1）：

    - 未達節流門檻：畫面完全不動，但張數仍要累計、5 秒計時仍要重啟（不漏收是硬要求）。
    - 達到門檻、且仍在重錨視窗內：用 `editMessageText` **原地編輯**，數字直接跳，
      不多發一則訊息（v2 每次都刪舊發新，兩次 API 呼叫且會閃爍，容易觸發 429）。
    - 超過 `COUNTER_REANCHOR_SEC`：才刪舊發新，把訊息重新拉回對話最下方。
    """
    app, data_dir, temp_dir, nas_dir, od_dir = env
    ctx = DummyContext(app)
    user = DummyUser(1002, "秀琴")
    members: MembersStore = app.bot_data["members"]
    members.register(1002, "秀琴")
    members.approve(1002)

    await upload.handle_start_upload(DummyUpdate(user, DummyMessage(1, "📷 我要上傳照片", 1002, app.bot)), ctx)
    await upload.handle_folder_text(DummyUpdate(user, DummyMessage(2, "測試相簿", 1002, app.bot)), ctx)
    dest_msg = DummyMessage(3, "", 1002, app.bot)
    await upload.handle_destination_button(
        DummyUpdate(user, dest_msg, DummyCallbackQuery("dest:家裡硬碟", user, dest_msg)), ctx
    )
    session = app.bot_data["sessions"].get(1002)

    photo1 = MagicMock(); photo1.file_id = "p1"
    await upload.handle_photo_message(DummyUpdate(user, DummyMessage(4, "", 1002, app.bot, photo=[photo1])), ctx)

    # 點「我傳完了」進入緩衝，第一則「確認中」一定要立即送出（使用者主動操作不受節流影響）
    finish_msg = DummyMessage(5, "", 1002, app.bot)
    await upload.handle_finish_button(
        DummyUpdate(user, finish_msg, DummyCallbackQuery("finish", user, finish_msg)), ctx
    )
    assert session.status_message_id is not None
    sent_baseline = len(app.bot.sent_messages)
    edited_baseline = len(app.bot.edited_messages)
    confirm_id_after_first = session.status_message_id

    # ① 兩個門檻都拉到極大 → 畫面不動，但計數與計時照常
    config.CONFIRM_UPDATE_SEC = 9999
    config.CONFIRM_UPDATE_COUNT = 9999
    photo2 = MagicMock(); photo2.file_id = "p2"
    await upload.handle_photo_message(DummyUpdate(user, DummyMessage(6, "", 1002, app.bot, photo=[photo2])), ctx)

    assert session.received_count == 2
    assert len(app.bot.sent_messages) == sent_baseline
    assert len(app.bot.edited_messages) == edited_baseline
    assert session.status_message_id == confirm_id_after_first
    # 舊 job 被換掉，仍只有一個在排隊中——代表計時確實重啟了
    assert len(app.job_queue.get_jobs_by_name("debounce:1002")) == 1

    # ② 張數門檻放行（時間門檻仍極大）→ 應原地編輯，不多發訊息
    config.CONFIRM_UPDATE_COUNT = 1
    photo3 = MagicMock(); photo3.file_id = "p3"
    await upload.handle_photo_message(DummyUpdate(user, DummyMessage(7, "", 1002, app.bot, photo=[photo3])), ctx)

    assert session.received_count == 3
    assert len(app.bot.sent_messages) == sent_baseline, "重錨視窗內不該重發訊息"
    assert len(app.bot.edited_messages) == edited_baseline + 1
    assert "3" in app.bot.edited_messages[-1]["text"]
    assert "確認中" in app.bot.edited_messages[-1]["text"], "確認標記要一直帶著"
    assert session.status_message_id == confirm_id_after_first

    # ③ 超過重錨秒數 → 才刪舊發新，把訊息拉回對話最下方
    session.status_last_reanchor = datetime(2000, 1, 1)
    photo4 = MagicMock(); photo4.file_id = "p4"
    await upload.handle_photo_message(DummyUpdate(user, DummyMessage(8, "", 1002, app.bot, photo=[photo4])), ctx)

    assert session.received_count == 4
    assert len(app.bot.sent_messages) == sent_baseline + 1
    assert "4" in app.bot.sent_messages[-1]["text"]
    assert session.status_message_id != confirm_id_after_first


@pytest.mark.asyncio
async def test_debounce_defers_flush_until_finalize(env):
    """
    點下「我傳完了」進入緩衝期後，收到的新照片仍照常下載落地，但**不該再觸發內部
    小批複製**（複製 worker 只在收件階段主動分批）。所有緩衝期間收到的照片，應遞延到
    debounce 結束、_finalize_upload 收尾時才一次寫入目的地（規格書 §6.3 流程第 4 步）。
    """
    app, data_dir, temp_dir, nas_dir, od_dir = env
    original_batch_size = config.BATCH_SIZE
    try:
        config.BATCH_SIZE = 2  # 縮小批次門檻，方便測試觸發「滿一批」
        ctx = DummyContext(app)
        user = DummyUser(1003, "大姊")
        members: MembersStore = app.bot_data["members"]
        members.register(1003, "大姊")
        members.approve(1003)

        await upload.handle_start_upload(DummyUpdate(user, DummyMessage(1, "📷 我要上傳照片", 1003, app.bot)), ctx)
        await upload.handle_folder_text(DummyUpdate(user, DummyMessage(2, "烤肉", 1003, app.bot)), ctx)
        dest_msg = DummyMessage(3, "", 1003, app.bot)
        await upload.handle_destination_button(
            DummyUpdate(user, dest_msg, DummyCallbackQuery("dest:家裡硬碟", user, dest_msg)), ctx
        )
        session = app.bot_data["sessions"].get(1003)
        dest_dir = nas_dir / "烤肉"

        # 收件階段：滿一批（2 張）應該複製到目的地。v3 的複製發生在背景 worker，
        # 所以要先 settle() 等它做完，事件處理層本身是不等的（規格書 §3.1）。
        for i in range(2):
            photo = MagicMock(); photo.file_id = f"recv_{i}"
            await upload.handle_photo_message(
                DummyUpdate(user, DummyMessage(10 + i, "", 1003, app.bot, photo=[photo])), ctx
            )
        await session.pipeline.settle()
        assert session.flushed_count == 2
        assert session.stored_count == 2
        assert len(list(dest_dir.glob("*.jpg"))) == 2

        # 點「我傳完了」進入緩衝期
        finish_msg = DummyMessage(20, "", 1003, app.bot)
        await upload.handle_finish_button(
            DummyUpdate(user, finish_msg, DummyCallbackQuery("finish", user, finish_msg)), ctx
        )
        assert session.stage == "debounce"

        # 緩衝期間再收到 2 張（達到 BATCH_SIZE 門檻），不應該觸發複製
        for i in range(2):
            photo = MagicMock(); photo.file_id = f"debounce_{i}"
            await upload.handle_photo_message(
                DummyUpdate(user, DummyMessage(21 + i, "", 1003, app.bot, photo=[photo])), ctx
            )
        await session.pipeline.settle()
        assert session.received_count == 4
        assert session.flushed_count == 2  # 緩衝期間收到的 2 張已落地暫存，但刻意還沒複製
        assert len(session.pipeline.buffer) == 2  # 已落地、等著收尾時一次寫入
        assert len(list(dest_dir.glob("*.jpg"))) == 2  # 目的地檔案數量不變

        # debounce 結束，_finalize_upload 應該一次把剩下的 2 張處理完
        await upload._debounce_fire(ctx_with_job(ctx, 1003))
        assert len(list(dest_dir.glob("*.jpg"))) == 4

        logs: DataLogs = app.bot_data["logs"]
        upload_rows = logs.upload_log.read_all_rows()
        assert len(upload_rows) == 1
        assert upload_rows[0]["張數"] == "4"
    finally:
        config.BATCH_SIZE = original_batch_size


def ctx_with_job(ctx, user_id):
    ctx.job = MagicMock()
    ctx.job.data = {"telegram_id": user_id}
    return ctx


@pytest.mark.asyncio
async def test_correction_flow_and_cleanup_csv(env):
    """測試「↩️ 這批傳錯了」流程，確保舊位置檔案不刪除，並精準記錄到 待清理清單.csv。"""
    app, data_dir, temp_dir, nas_dir, od_dir = env
    ctx = DummyContext(app)
    user = DummyUser(1001, "秀琴")
    members: MembersStore = app.bot_data["members"]
    members.register(1001, "秀琴")
    members.approve(1001)

    # 模擬先完成一次上傳到「舊相簿」
    dest_dir = nas_dir / "舊相簿"
    dest_dir.mkdir(parents=True, exist_ok=True)
    fake_photo = dest_dir / "20260724_100000_123456.jpg"
    fake_photo.write_bytes(b"test photo content")

    sessions: SessionManager = app.bot_data["sessions"]
    batch = upload.CompletedBatch(
        telegram_id=1001,
        folder="舊相簿",
        destination_label="家裡硬碟",
        files=[],
        written_paths={"家裡硬碟": [("fake_file_id_001", fake_photo)]},
        completed_at=datetime.now(),
    )
    sessions.set_last_batch(batch)

    # 點擊 [↩️ 這批傳錯了]
    corr_cb = DummyCallbackQuery("correction", user, DummyMessage(20, "", 1001, app.bot))
    await upload.handle_correction_button(DummyUpdate(user, DummyMessage(20, "", 1001, app.bot), corr_cb), ctx)
    assert ctx.user_data.get(upload.AWAITING_CORRECTION_FLAG) is True

    # 輸入新資料夾名稱「新相簿」
    new_folder_msg = DummyMessage(21, "新相簿", 1001, app.bot)
    handled = await upload.handle_folder_text(DummyUpdate(user, new_folder_msg), ctx)
    assert handled is True

    # 驗證新相簿已建立且有照片
    new_dir = nas_dir / "新相簿"
    assert new_dir.exists()
    assert (new_dir / "20260724_100000_123456.jpg").exists()

    # 關鍵驗證：原資料夾的照片絕對不被刪除！
    assert fake_photo.exists()

    # 驗證 待清理清單.csv 有正確寫入原位置與原因
    logs: DataLogs = app.bot_data["logs"]
    cleanup_rows = logs.cleanup_list.read_all_rows()
    assert len(cleanup_rows) == 1
    # 「上傳者」欄必須是姓名——v2 誤填成目的地標籤（"家裡硬碟"），害管理員無法依人篩選（§10B）
    assert cleanup_rows[0]["上傳者"] == "秀琴"
    assert cleanup_rows[0]["類型"] == "傳錯更正"
    assert cleanup_rows[0]["檔名"] == "20260724_100000_123456.jpg"
    assert "舊相簿" in cleanup_rows[0]["待刪位置"]

    # 更正後的新位置也要寫入 file_index.csv，file_id 沿用原本那張照片的
    index_rows = logs.file_index.read_all_rows()
    assert len(index_rows) == 1
    assert index_rows[0]["file_id"] == "fake_file_id_001"
    assert index_rows[0]["目標資料夾"] == "新相簿"
    assert index_rows[0]["檔名"] == "20260724_100000_123456.jpg"

    # upload_log.csv 也要記一筆「傳錯更正」的動作紀錄
    upload_rows = logs.upload_log.read_all_rows()
    assert len(upload_rows) == 1
    assert upload_rows[0]["結果"] == "傳錯更正"
    assert upload_rows[0]["資料夾"] == "舊相簿 → 新相簿"
    assert upload_rows[0]["張數"] == "1"


@pytest.mark.asyncio
async def test_startup_recover_temp_orphan_files(env):
    """測試當機器人非預期當機重啟時 (startup_recover_temp)，能自暫存區補送照片與寫入 CSV。"""
    app, data_dir, temp_dir, nas_dir, od_dir = env
    
    # 手動建立殘留在暫存區的檔案 structure:
    # TEMP_DIR / 1001_秀琴 / 20260724_1200_過年聚會
    user_temp = temp_dir / "1001_秀琴"
    session_temp = user_temp / "20260724_1200_過年聚會"
    session_temp.mkdir(parents=True, exist_ok=True)

    orphan_photo = session_temp / "file_id_999.jpg"
    orphan_photo.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xd9")

    # 寫入側車檔
    storage.write_session_info(session_temp, {
        "destination": "OneDrive",
        "folder": "過年聚會",
        "telegram_id": 1001,
        "name": "秀琴",
    })

    # 執行復原
    await startup_recover_temp(app)

    # 驗證照片已被自動補送到 OneDrive / 過年聚會
    od_target = od_dir / "過年聚會"
    assert od_target.exists()
    recovered_files = list(od_target.glob("*.jpg"))
    assert len(recovered_files) == 1

    # 驗證暫存檔已被安全清理
    assert not orphan_photo.exists()
    assert not session_temp.exists()

    # 驗證 upload_log.csv 與 file_index.csv 記為「成功(復原)」
    logs: DataLogs = app.bot_data["logs"]
    rows = logs.upload_log.read_all_rows()
    assert len(rows) == 1
    assert rows[0]["結果"] == "成功(復原)"
    assert rows[0]["資料夾"] == "過年聚會"


# ── v3：事件處理與背景工作分離（規格書 §3.1、§6.3、§6.3.1、§6.3.2、§7）────

async def _open_session(app, ctx, user, folder: str, dest: str = "家裡硬碟"):
    """把某使用者帶到「已選好資料夾與目的地、可以開始傳照片」的狀態。"""
    members: MembersStore = app.bot_data["members"]
    if members.get(user.id) is None:
        members.register(user.id, user.first_name)
        members.approve(user.id)
    await upload.handle_start_upload(DummyUpdate(user, DummyMessage(1, "📷 我要上傳照片", user.id, app.bot)), ctx)
    await upload.handle_folder_text(DummyUpdate(user, DummyMessage(2, folder, user.id, app.bot)), ctx)
    dest_msg = DummyMessage(3, "", user.id, app.bot)
    await upload.handle_destination_button(
        DummyUpdate(user, dest_msg, DummyCallbackQuery(f"dest:{dest}", user, dest_msg)), ctx
    )
    return app.bot_data["sessions"].get(user.id)


async def _send_photo(app, ctx, user, file_id: str, msg_id: int):
    photo = MagicMock(); photo.file_id = file_id
    await upload.handle_photo_message(
        DummyUpdate(user, DummyMessage(msg_id, "", user.id, app.bot, photo=[photo])), ctx
    )


@pytest.mark.asyncio
async def test_photo_handler_returns_without_waiting_for_download(env):
    """
    §3.1 的核心保證：收到照片的事件處理函式**不等下載**就返回。

    這是「按了『我傳完了』卻要等很久才有反應」的根治手段——v2 把下載與複製同步
    寫在處理函式裡，後面排隊的按鈕點擊只能等，超過 Telegram callback 的約 15 秒
    有效期還會整個失效。這裡把下載用一道閘門擋住，驗證處理函式仍然立刻返回、
    張數也已經正確登記。
    """
    app, *_ = env
    ctx = DummyContext(app)
    user = DummyUser(1010, "媽媽")
    session = await _open_session(app, ctx, user, "不等下載")

    gate = asyncio.Event()
    app.bot.get_file_gate = gate

    # 下載被閘門擋住，但這一行必須立刻返回（若會等下載，這裡就永遠 hang）
    await asyncio.wait_for(_send_photo(app, ctx, user, "blocked_1", 10), timeout=2)

    assert session.received_count == 1, "登記是純記憶體動作，收到當下就要準確"
    assert session.files[0].downloaded is False, "下載還被擋著，代表處理函式確實沒等它"

    # 放行後，背景 worker 才把照片真的抓下來
    gate.set()
    await asyncio.wait_for(session.pipeline.settle(), timeout=5)
    assert session.files[0].downloaded is True
    assert session.files[0].temp_path.exists()


@pytest.mark.asyncio
async def test_counter_updates_on_count_threshold_within_time_window(env):
    """
    §6.3.1 雙門檻節流：即使時間門檻還沒到，只要新增張數達到門檻就要更新畫面。

    v2 只看時間，於是「一批照片在節流秒數內全部抵達」時（張數不多、手機網路快
    時很常見），畫面會停在第 1 張的數字完全不動，直到按下「我傳完了」才第一次
    看到正確總數——這正是使用者回報「感覺卡住」的成因。
    """
    app, *_ = env
    ctx = DummyContext(app)
    user = DummyUser(1011, "大姊")
    session = await _open_session(app, ctx, user, "小批量")

    config.COUNTER_UPDATE_SEC = 9999  # 時間門檻永遠不會到
    config.COUNTER_UPDATE_COUNT = 3   # 只靠張數門檻放行

    await _send_photo(app, ctx, user, "c1", 10)  # 第一張一律立即顯示
    baseline = len(app.bot.sent_messages)

    await _send_photo(app, ctx, user, "c2", 11)
    await _send_photo(app, ctx, user, "c3", 12)
    assert len(app.bot.sent_messages) == baseline, "還沒滿 3 張，不該更新"

    await _send_photo(app, ctx, user, "c4", 13)
    assert len(app.bot.sent_messages) == baseline + 1
    assert "4 張" in app.bot.sent_messages[-1]["text"]


@pytest.mark.asyncio
async def test_counter_shows_backup_progress(env):
    """§6.3.1：收件計數要同時顯示「已存好 N 張」，讓使用者看得到背景備份進度。"""
    app, *_ = env
    original_batch = config.BATCH_SIZE
    try:
        config.BATCH_SIZE = 2
        ctx = DummyContext(app)
        user = DummyUser(1012, "阿姨")
        session = await _open_session(app, ctx, user, "備份進度")

        await _send_photo(app, ctx, user, "s1", 10)
        await _send_photo(app, ctx, user, "s2", 11)
        await session.pipeline.settle()
        assert session.stored_count == 2

        config.COUNTER_UPDATE_COUNT = 1  # 讓下一張一定會更新畫面
        await _send_photo(app, ctx, user, "s3", 12)
        assert "已存好 2 張" in app.bot.sent_messages[-1]["text"]
    finally:
        config.BATCH_SIZE = original_batch


@pytest.mark.asyncio
async def test_download_failure_is_recorded_not_silently_swallowed(env):
    """
    §6.3.2／§8：下載失敗絕不可靜默吞掉。

    v2 的下載完全沒有例外保護，一旦網路瞬斷，該張照片不落地、不計數、不記錄，
    使用者與管理員都無從得知。v3 要求：重試後仍失敗則記入 file_index.csv
    （目的地欄標「下載失敗」並保留 file_id 供 redownload.py 補救）、通知管理員、
    並於本次結束時彙總告知使用者。
    """
    app, data_dir, *_ = env
    ctx = DummyContext(app)
    user = DummyUser(1013, "小妹")
    session = await _open_session(app, ctx, user, "斷線測試")

    app.bot.get_file_error = RuntimeError("網路瞬斷")
    await _send_photo(app, ctx, user, "broken_1", 10)
    await session.pipeline.settle()

    assert session.received_count == 1
    assert session.files[0].download_failed is True

    logs: DataLogs = app.bot_data["logs"]
    index_rows = logs.file_index.read_all_rows()
    assert len(index_rows) == 1
    assert index_rows[0]["目的地"] == "下載失敗"
    assert index_rows[0]["file_id"] == "broken_1", "file_id 必須保留，才能事後補救"

    assert any("下載照片失敗" in m["text"] for m in app.bot.sent_messages if m["chat_id"] == 999999)

    # 收尾時要彙總告知使用者，而不是讓他以為都收到了
    await upload._finalize_upload(ctx, session, timed_out=False)
    user_texts = [m["text"] for m in app.bot.sent_messages if m["chat_id"] == 1013]
    assert any("沒有收到" in t for t in user_texts)


@pytest.mark.asyncio
async def test_finish_button_survives_expired_callback_query(env):
    """
    §8：callback query 逾時（約 15 秒）不可讓整個點擊處理中斷。

    v2 的 `handle_finish_button` 第一行就是沒有保護的 `await query.answer()`，
    大批次造成佇列積壓時它會拋 BadRequest，使用者按了「✅ 我傳完了」完全沒反應，
    session 一路卡到 10 分鐘逾時才被保險機制收掉。
    """
    app, *_ = env
    ctx = DummyContext(app)
    user = DummyUser(1014, "舅媽")
    session = await _open_session(app, ctx, user, "逾時按鈕")
    await _send_photo(app, ctx, user, "e1", 10)

    finish_msg = DummyMessage(20, "", 1014, app.bot)
    expired = DummyCallbackQuery(
        "finish", user, finish_msg,
        answer_error=RuntimeError("Query is too old and response timeout expired"),
    )
    await upload.handle_finish_button(DummyUpdate(user, finish_msg, expired), ctx)

    assert session.stage == "debounce", "answer() 失敗不該中斷後面的結案流程"
    assert session.status_message_id is not None
    shown = [m["text"] for m in app.bot.sent_messages if m["chat_id"] == 1014]
    shown += [m["text"] for m in app.bot.edited_messages if m["chat_id"] == 1014]
    assert any("確認中" in t for t in shown)


@pytest.mark.asyncio
async def test_correction_flag_cleared_when_new_upload_starts(env):
    """
    §7 第 10 點：「這批傳錯了」的待輸入狀態必須在開新上傳時清除。

    v2 沒有失效條件，於是使用者點了「↩️ 這批傳錯了」卻改變主意不回覆，接著開新的
    上傳、輸入資料夾名稱時，那個名稱會被誤判成更正目標——上一批照片被複製到新
    上傳的資料夾去，而新 session 還卡在等資料夾名稱。
    """
    app, data_dir, temp_dir, nas_dir, od_dir = env
    ctx = DummyContext(app)
    user = DummyUser(1015, "表姊")
    members: MembersStore = app.bot_data["members"]
    members.register(1015, "表姊")
    members.approve(1015)

    old_dir = nas_dir / "舊的一批"
    old_dir.mkdir(parents=True, exist_ok=True)
    old_photo = old_dir / "20260724_100000_000001.jpg"
    old_photo.write_bytes(b"x")
    batch = upload.CompletedBatch(
        telegram_id=1015, folder="舊的一批", destination_label="家裡硬碟", files=[],
        written_paths={"家裡硬碟": [("fid_1", old_photo)]}, completed_at=datetime.now(),
    )
    app.bot_data["sessions"].set_last_batch(batch)

    corr_msg = DummyMessage(20, "", 1015, app.bot)
    await upload.handle_correction_button(
        DummyUpdate(user, corr_msg, DummyCallbackQuery("correction", user, corr_msg)), ctx
    )
    assert ctx.user_data.get(upload.AWAITING_CORRECTION_FLAG) is True

    # 改變主意，直接開一次新的上傳
    await upload.handle_start_upload(DummyUpdate(user, DummyMessage(21, "📷 我要上傳照片", 1015, app.bot)), ctx)
    assert ctx.user_data.get(upload.AWAITING_CORRECTION_FLAG) is False

    # 輸入的資料夾名稱要進到新 session，不可被當成更正目標
    handled = await upload.handle_folder_text(
        DummyUpdate(user, DummyMessage(22, "全新的一批", 1015, app.bot)), ctx
    )
    assert handled is True
    assert app.bot_data["sessions"].get(1015).folder == "全新的一批"
    assert batch.corrected is False, "上一批不該被更正"
    assert not (nas_dir / "全新的一批").exists(), "舊照片不該被複製到新上傳的資料夾"


@pytest.mark.asyncio
async def test_correction_flag_expires_after_timeout(env):
    """§7 第 10 點的第二道失效條件：超過 CORRECTION_PROMPT_MAX_MIN 自動失效。"""
    app, *_ = env
    ctx = DummyContext(app)
    user = DummyUser(1016, "阿嬤")

    ctx.user_data[upload.AWAITING_CORRECTION_FLAG] = True
    ctx.user_data[upload.CORRECTION_FLAG_AT] = datetime.now() - timedelta(minutes=30)
    config.CORRECTION_PROMPT_MAX_MIN = 10

    handled = await upload.handle_folder_text(
        DummyUpdate(user, DummyMessage(30, "隨便打的字", 1016, app.bot)), ctx
    )
    assert handled is False, "狀態已過期，這則文字不該被當成更正目標"
    assert ctx.user_data.get(upload.AWAITING_CORRECTION_FLAG) is False


@pytest.mark.asyncio
async def test_correction_offers_recent_folder_buttons(env):
    """§7 第 1 點：更正流程須同時提供近期資料夾按鈕與打字輸入，不可只接受打字。"""
    app, data_dir, temp_dir, nas_dir, od_dir = env
    ctx = DummyContext(app)
    user = DummyUser(1017, "姑姑")
    members: MembersStore = app.bot_data["members"]
    members.register(1017, "姑姑")
    members.approve(1017)
    members.push_recent_folder(1017, "去年過年", "家裡硬碟")

    old_dir = nas_dir / "打錯的夾"
    old_dir.mkdir(parents=True, exist_ok=True)
    old_photo = old_dir / "20260724_110000_000002.jpg"
    old_photo.write_bytes(b"y")
    batch = upload.CompletedBatch(
        telegram_id=1017, folder="打錯的夾", destination_label="家裡硬碟", files=[],
        written_paths={"家裡硬碟": [("fid_2", old_photo)]}, completed_at=datetime.now(),
    )
    app.bot_data["sessions"].set_last_batch(batch)

    corr_msg = DummyMessage(20, "", 1017, app.bot)
    await upload.handle_correction_button(
        DummyUpdate(user, corr_msg, DummyCallbackQuery("correction", user, corr_msg)), ctx
    )
    markup = app.bot.sent_messages[-1]["reply_markup"]
    assert markup is not None, "必須附上近期資料夾按鈕"
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "corrfolder:去年過年" in callbacks

    # 點下按鈕就直接完成更正，不需要打字
    pick_msg = DummyMessage(21, "", 1017, app.bot)
    await upload.handle_correction_folder_button(
        DummyUpdate(user, pick_msg, DummyCallbackQuery("corrfolder:去年過年", user, pick_msg)), ctx
    )
    assert (nas_dir / "去年過年" / old_photo.name).exists()
    assert old_photo.exists(), "原位置的照片絕不刪除"


@pytest.mark.asyncio
async def test_stage_stuck_session_is_finalized_not_leaked(env):
    """
    §6.4 兜底逾時：卡在非收件階段（例如按了「重新開始」卻不回答二次確認）的
    session，超過 STAGE_STUCK_MAX_MIN 要收尾並清掉，不可永久佔記憶體、
    也不可讓暫存區的照片無人處理。
    """
    app, data_dir, temp_dir, nas_dir, od_dir = env
    ctx = DummyContext(app)
    user = DummyUser(1018, "叔叔")
    session = await _open_session(app, ctx, user, "卡住的批次")
    await _send_photo(app, ctx, user, "stuck_1", 10)
    await session.pipeline.settle()

    # 按下「🔄 重新開始」後就再也沒回來
    restart_msg = DummyMessage(20, "", 1018, app.bot)
    await upload.handle_restart_button(
        DummyUpdate(user, restart_msg, DummyCallbackQuery("restart", user, restart_msg)), ctx
    )
    assert session.stage == "awaiting_restart_confirm"

    # 這個階段既不算閒置逾時、也不符合「遺棄」條件（已收到照片）
    assert session.is_idle_timed_out(10, datetime.now() + timedelta(hours=1)) is False
    assert session.is_abandoned(60, datetime.now() + timedelta(hours=1)) is False

    session.last_activity_at = datetime.now() - timedelta(minutes=31)
    await upload.check_session_timeouts(ctx)

    assert app.bot_data["sessions"].get(1018) is None, "卡住的 session 必須被收掉"
    logs: DataLogs = app.bot_data["logs"]
    assert len(logs.upload_log.read_all_rows()) == 1, "照片要被當一批處理完成，不可靜默丟棄"


@pytest.mark.asyncio
async def test_redownload_integration(env):
    """測試照片重新下載工具 (redownload.py) 讀取 CSV 分組與下檔。"""
    app, data_dir, _, _, _ = env
    logs: DataLogs = app.bot_data["logs"]

    # 寫入兩筆測試紀錄到 file_index.csv
    logs.log_file_index("2026-07-24 10:00", "秀琴", 1001, "淡水一日遊", "家裡硬碟", "photo1.jpg", "file_abc_123")
    logs.log_file_index("2026-07-24 10:00", "秀琴", 1001, "淡水一日遊", "OneDrive", "photo1.jpg", "file_abc_123")  # 同檔名同file_id

    # 載入分組並驗證已自動去重
    groups = load_groups(data_dir)
    assert len(groups) == 1
    key = ("淡水一日遊", "秀琴", "2026-07-24")
    assert key in groups
    assert len(groups[key]) == 1  # 已由 file_id 去重為 1 張

    # 模擬下載
    target_download_dir = data_dir / "download_test"
    dummy_bot = DummyBot()
    succeeded, failed = await download_rows(dummy_bot, groups[key], target_download_dir)

    assert len(succeeded) == 1
    assert (target_download_dir / "photo1.jpg").exists()


@pytest.mark.asyncio
async def test_restart_cancel_returns_to_previous_stage_not_receiving(env):
    """
    §6.5：「🔄 重新開始」在 session 全程可見，使用者可能在**還沒選資料夾**時誤觸。
    此時按「❌ 取消」應該回到選資料夾，而不是跳到「繼續原本的上傳」——那時候
    根本還沒有原本的上傳，直接進收件階段會讓他卡在沒有資料夾也沒有目的地的狀態。
    """
    app, *_ = env
    ctx = DummyContext(app)
    user = DummyUser(1020, "小舅")
    members: MembersStore = app.bot_data["members"]
    members.register(1020, "小舅")
    members.approve(1020)
    members.push_recent_folder(1020, "上次的夾", "兩邊都存")

    await upload.handle_start_upload(DummyUpdate(user, DummyMessage(1, "📷 我要上傳照片", 1020, app.bot)), ctx)
    session = app.bot_data["sessions"].get(1020)
    assert session.stage == "awaiting_folder"

    # 在選資料夾階段誤觸「🔄 重新開始」
    r_msg = DummyMessage(2, "", 1020, app.bot)
    await upload.handle_restart_button(DummyUpdate(user, r_msg, DummyCallbackQuery("restart", user, r_msg)), ctx)
    assert session.stage == "awaiting_restart_confirm"

    # 按「取消」
    c_msg = DummyMessage(3, "", 1020, app.bot)
    await upload.handle_restart_cancel(
        DummyUpdate(user, c_msg, DummyCallbackQuery("restart_cancel", user, c_msg)), ctx
    )

    assert session.stage == "awaiting_folder", "要回到選資料夾，不是跳到收件階段"
    last = app.bot.sent_messages[-1]
    assert "資料夾" in last["text"]
    callbacks = [b.callback_data for row in last["reply_markup"].inline_keyboard for b in row]
    assert "recent:上次的夾" in callbacks, "要重新給他近期資料夾按鈕"


@pytest.mark.asyncio
async def test_restart_cancel_from_receiving_stage_continues_upload(env):
    """在收件階段誤觸重新開始並取消時，維持原本行為：繼續原本的上傳。"""
    app, *_ = env
    ctx = DummyContext(app)
    user = DummyUser(1021, "小姨")
    session = await _open_session(app, ctx, user, "繼續傳")
    await _send_photo(app, ctx, user, "r1", 10)

    r_msg = DummyMessage(20, "", 1021, app.bot)
    await upload.handle_restart_button(DummyUpdate(user, r_msg, DummyCallbackQuery("restart", user, r_msg)), ctx)
    c_msg = DummyMessage(21, "", 1021, app.bot)
    await upload.handle_restart_cancel(
        DummyUpdate(user, c_msg, DummyCallbackQuery("restart_cancel", user, c_msg)), ctx
    )

    assert session.stage == "receiving_photos"
    assert "繼續原本的上傳" in app.bot.sent_messages[-1]["text"]


@pytest.mark.asyncio
async def test_folder_name_with_newline_is_rejected_with_explanation(env):
    """
    實測 crash 的端對端回歸：帶換行的資料夾名稱不可以進到路徑裡，
    要明確請使用者改名，而且 session 必須留在選資料夾階段讓他重打。
    """
    app, *_ = env
    ctx = DummyContext(app)
    user = DummyUser(1022, "阿伯")
    members: MembersStore = app.bot_data["members"]
    members.register(1022, "阿伯")
    members.approve(1022)

    await upload.handle_start_upload(DummyUpdate(user, DummyMessage(1, "📷 我要上傳照片", 1022, app.bot)), ctx)
    session = app.bot_data["sessions"].get(1022)

    handled = await upload.handle_folder_text(
        DummyUpdate(user, DummyMessage(2, "2026-07-0\n25大量測試", 1022, app.bot)), ctx
    )
    assert handled is True
    assert "換行" in app.bot.sent_messages[-1]["text"]
    assert session.folder is None, "不合規則的名稱不可以被採用"
    assert session.stage == "awaiting_folder", "要留在選資料夾階段讓他重打"

    # 改成合規的名稱就能正常往下走
    await upload.handle_folder_text(
        DummyUpdate(user, DummyMessage(3, "2026-07-25大量測試", 1022, app.bot)), ctx
    )
    assert session.folder == "2026-07-25大量測試"
    assert session.stage == "awaiting_destination"


@pytest.mark.asyncio
async def test_finish_button_with_zero_photos_blocked(env):
    """防誤觸測試：當尚未收到任何照片時按『我傳完了』，必須提示並拒絕關閉 session。"""
    app, *_ = env
    ctx = DummyContext(app)
    user = DummyUser(1030, "小明")
    session = await _open_session(app, ctx, user, "空測試相簿")
    assert session.received_count == 0

    finish_msg = DummyMessage(5, "", 1030, app.bot)
    finish_cb = DummyCallbackQuery("finish", user, finish_msg)
    await upload.handle_finish_button(DummyUpdate(user, finish_msg, finish_cb), ctx)

    # 驗證 session 仍然維持在 receiving_photos 階段，未進入 debounce，且提示「尚未收到任何照片」
    assert session.stage == "receiving_photos"
    assert "尚未收到任何照片" in app.bot.sent_messages[-1]["text"]


@pytest.mark.asyncio
async def test_inactivity_prompt_workflow(env):
    """靜置提醒測試：傳照片後靜置 25 秒主動跳出確認選單，點『我還要繼續傳』恢復接收狀態。"""
    from datetime import timedelta
    app, *_ = env
    ctx = DummyContext(app)
    user = DummyUser(1031, "大姊")
    session = await _open_session(app, ctx, user, "靜置測試相簿")
    await _send_photo(app, ctx, user, "photo_idle_1", 1)
    assert session.received_count == 1

    # 模擬靜置 30 秒
    session.last_activity_at = datetime.now() - timedelta(seconds=30)
    await upload.check_session_timeouts(ctx)

    # 驗證主動跳出「看起來照片傳得差不多囉」詢問訊息
    assert session.inactivity_prompted is True
    assert "看起來照片傳得差不多囉" in app.bot.sent_messages[-1]["text"]

    # 使用者點擊 [📷 我還有照片沒傳完]
    cont_msg = DummyMessage(10, "", 1031, app.bot)
    cont_cb = DummyCallbackQuery("continue_receiving", user, cont_msg)
    await upload.handle_continue_receiving_button(DummyUpdate(user, cont_msg, cont_cb), ctx)

    assert session.inactivity_prompted is False
    assert "繼續傳送照片" in app.bot.sent_messages[-1]["text"]

    # 再次模擬靜置 30 秒，驗證仍可二次觸發詢問
    session.last_activity_at = datetime.now() - timedelta(seconds=30)
    await upload.check_session_timeouts(ctx)
    assert session.inactivity_prompted is True


@pytest.mark.asyncio
async def test_auto_append_late_photos(env):
    """遲到照片測試：3 分鐘內完工的批次，若傳來遲到的照片，自動補存入同個資料夾。"""
    app, data_dir, temp_dir, nas_dir, od_dir = env
    ctx = DummyContext(app)
    user = DummyUser(1032, "二哥")

    # 1. 正常完成一次上傳
    session = await _open_session(app, ctx, user, "家庭聚餐")
    await _send_photo(app, ctx, user, "p1", 1)
    await session.pipeline.settle()
    await upload._finalize_upload(ctx, session, timed_out=False)

    # 驗證 session 已結束，且紀錄留在 last_batch
    assert app.bot_data["sessions"].get(1032) is None
    last_batch = app.bot_data["sessions"].get_last_batch(1032)
    assert last_batch is not None
    assert last_batch.folder == "家庭聚餐"

    # 2. 3 分鐘窗口內丟入一張遲到的照片
    gate = asyncio.Event()
    app.bot.get_file_gate = gate
    dummy_photo_item = MagicMock()
    dummy_photo_item.file_id = "late_p2"
    dummy_photo_item.file_unique_id = "uniq_late_p2"
    late_msg = DummyMessage(50, "", 1032, app.bot, photo=[dummy_photo_item])
    await asyncio.wait_for(upload.handle_photo_message(DummyUpdate(user, late_msg), ctx), timeout=2)

    # 自動開了一個指向同一個相簿的 session，且照樣是「登記完就返回」不等下載（§3.1）
    late_session = app.bot_data["sessions"].get(1032)
    assert late_session is not None
    assert late_session.auto_appended is True
    assert late_session.folder == "家庭聚餐"
    assert late_session.destination == last_batch.destination_label
    assert late_session.stage == "debounce", "直接進緩衝，收齊後自動結案，使用者不必按我傳完了"
    assert late_session.received_count == 1
    assert late_session.files[0].downloaded is False, "下載還被擋著，代表沒有在事件處理裡同步等它"
    assert any("自動幫你存進剛剛的「家庭聚餐」" in m["text"] for m in app.bot.sent_messages)

    # 3. 放行下載，讓緩衝結束收尾——走的是正常流程，不是另一條土炮路徑
    gate.set()
    app.bot.get_file_gate = None
    await upload._debounce_fire(ctx_with_job(ctx, 1032))

    assert len(list((nas_dir / "家庭聚餐").glob("*.jpg"))) == 2
    logs: DataLogs = app.bot_data["logs"]
    rows = logs.upload_log.read_all_rows()
    assert len(rows) == 2, "補存批次要照常寫入 upload_log"
    assert rows[-1]["上傳者"] == "二哥", "上傳者要用註冊的稱呼，不是 Telegram 暱稱"
    # file_index 也要有補存那張，日後才找得回來
    assert any(r["file_id"] == "late_p2" for r in logs.file_index.read_all_rows())


@pytest.mark.asyncio
async def test_duplicate_photo_never_overwrites_and_is_listed_for_cleanup(env):
    """
    同一張照片重複上傳時（指紋檔名相同）：
    - **絕不覆蓋**既有檔案，另存一份（§2、§8、§10）
    - 重複的那份逐筆寫進待清理清單，讓管理員有依據可以刪（§10B）
    - 完成訊息要說明有幾張重複，避免使用者以為漏傳
    """
    app, data_dir, temp_dir, nas_dir, od_dir = env
    ctx = DummyContext(app)
    user = DummyUser(1033, "三姊")
    dest_dir = nas_dir / "重複測試"

    # 第一次上傳
    session = await _open_session(app, ctx, user, "重複測試")
    await _send_photo(app, ctx, user, "dup_1", 10)
    await session.pipeline.settle()
    await upload._finalize_upload(ctx, session, timed_out=False)
    assert len(list(dest_dir.glob("*.jpg"))) == 1
    first = list(dest_dir.glob("*.jpg"))[0]
    first_bytes = first.read_bytes()

    # 第二次傳同一張（同樣的 file_id → 同樣的指紋 → 同樣的檔名）
    session2 = await _open_session(app, ctx, user, "重複測試")
    await _send_photo(app, ctx, user, "dup_1", 20)
    await session2.pipeline.settle()
    await upload._finalize_upload(ctx, session2, timed_out=False)

    files = sorted(dest_dir.glob("*.jpg"))
    assert len(files) == 2, "撞名要另存一份，不可覆蓋"
    assert first.exists() and first.read_bytes() == first_bytes, "原本那張必須完好無損"
    assert any("_(2)" in f.name for f in files)

    # 待清理清單要有這筆重複，且欄位對得上
    logs: DataLogs = app.bot_data["logs"]
    dup_rows = [r for r in logs.cleanup_list.read_all_rows() if r["類型"] == "重複檔案"]
    assert len(dup_rows) == 1
    assert dup_rows[0]["上傳者"] == "三姊"
    assert "_(2)" in dup_rows[0]["檔名"]

    # 使用者訊息要講清楚有幾張重複；管理員要另外收到清理通知
    user_texts = [m["text"] for m in app.bot.sent_messages if m["chat_id"] == 1033]
    assert any("1 張跟相簿裡已經有的照片是同一張" in t for t in user_texts)
    admin_texts = [m["text"] for m in app.bot.sent_messages if m["chat_id"] == 999999]
    assert any("待清理清單" in t and "重複" in t for t in admin_texts)


# ── 記錄檔被鎖住（Excel 開著）不可中斷照片處理（實測 bug 回歸）────────

class _LockedCsv:
    """模擬被 Excel 鎖住的 CSV：任何寫入都拋 PermissionError。"""
    def __init__(self, real):
        self._real = real
        self.path = real.path
        self.header = real.header

    def append_row(self, row):
        self.append_rows([row])

    def append_rows(self, rows):
        raise PermissionError(13, "Permission denied", str(self.path))

    def read_all_rows(self):
        return self._real.read_all_rows()


@pytest.mark.asyncio
async def test_locked_csv_does_not_break_correction(env):
    """
    實測 bug：管理員用 Excel 開著「待清理清單.csv」時，「這批傳錯了」會整個炸掉。

    照片其實**已經複製到新資料夾了**，但寫清單的 PermissionError 一路往上炸，
    導致：使用者收不到完成回覆、file_index 沒寫、新資料夾沒進「最近使用」，
    而且 batch.corrected 已被設為 True 連重試都不行。

    規格書 §2「通知失敗不可中斷本體工作」同樣適用於記錄檔。
    """
    app, data_dir, temp_dir, nas_dir, od_dir = env
    ctx = DummyContext(app)
    user = DummyUser(1040, "四弟")
    members: MembersStore = app.bot_data["members"]
    members.register(1040, "四弟")
    members.approve(1040)

    old_dir = nas_dir / "打錯了"
    old_dir.mkdir(parents=True, exist_ok=True)
    old_photo = old_dir / "20260725_120000_aabbccdd.jpg"
    old_photo.write_bytes(b"photo")
    batch = upload.CompletedBatch(
        telegram_id=1040, folder="打錯了", destination_label="家裡硬碟", files=[],
        written_paths={"家裡硬碟": [("fid_lock", old_photo)]}, completed_at=datetime.now(),
    )
    app.bot_data["sessions"].set_last_batch(batch)

    # 把待清理清單換成「被鎖住」的版本
    logs: DataLogs = app.bot_data["logs"]
    logs.cleanup_list = _LockedCsv(logs.cleanup_list)

    corr_msg = DummyMessage(20, "", 1040, app.bot)
    await upload.handle_correction_button(
        DummyUpdate(user, corr_msg, DummyCallbackQuery("correction", user, corr_msg)), ctx
    )
    # 這一行以前會拋 PermissionError
    await upload.handle_folder_text(DummyUpdate(user, DummyMessage(21, "正確的夾", 1040, app.bot)), ctx)

    # 照片照樣搬到新資料夾、原檔保留
    assert (nas_dir / "正確的夾" / old_photo.name).exists()
    assert old_photo.exists()

    # 使用者要收到完成回覆（以前會因為例外而收不到）
    user_texts = [m["text"] for m in app.bot.sent_messages if m["chat_id"] == 1040]
    assert any("已經幫你把" in t and "正確的夾" in t for t in user_texts)

    # 新資料夾要進「最近使用」（以前被崩潰擋在後面永遠執行不到）
    recent = [f["name"] for f in members.get_recent_folders(1040)]
    assert "正確的夾" in recent

    # 管理員要收到「檔案被鎖住」的說明，而不是一則看不懂的例外
    admin_texts = [m["text"] for m in app.bot.sent_messages if m["chat_id"] == 999999]
    assert any("Excel" in t and "待清理清單" in t for t in admin_texts)


@pytest.mark.asyncio
async def test_existing_folder_is_announced_to_user(env):
    """使用者打的資料夾名稱已經存在時，要明確說明照片會存進既有資料夾。"""
    app, data_dir, temp_dir, nas_dir, od_dir = env
    ctx = DummyContext(app)
    user = DummyUser(1041, "五妹")
    members: MembersStore = app.bot_data["members"]
    members.register(1041, "五妹")
    members.approve(1041)

    (nas_dir / "去年中秋").mkdir(parents=True, exist_ok=True)  # 先讓資料夾存在

    await upload.handle_start_upload(DummyUpdate(user, DummyMessage(1, "📷 我要上傳照片", 1041, app.bot)), ctx)
    await upload.handle_folder_text(DummyUpdate(user, DummyMessage(2, "去年中秋", 1041, app.bot)), ctx)
    dest_msg = DummyMessage(3, "", 1041, app.bot)
    await upload.handle_destination_button(
        DummyUpdate(user, dest_msg, DummyCallbackQuery("dest:家裡硬碟", user, dest_msg)), ctx
    )

    ready = app.bot.sent_messages[-1]["text"]
    assert "準備好了" in ready
    assert "已經有了" in ready and "去年中秋" in ready

    # 全新的資料夾就不該出現這句
    user2 = DummyUser(1042, "六弟")
    members.register(1042, "六弟"); members.approve(1042)
    await upload.handle_start_upload(DummyUpdate(user2, DummyMessage(1, "📷 我要上傳照片", 1042, app.bot)), ctx)
    await upload.handle_folder_text(DummyUpdate(user2, DummyMessage(2, "全新的夾", 1042, app.bot)), ctx)
    dest_msg2 = DummyMessage(3, "", 1042, app.bot)
    await upload.handle_destination_button(
        DummyUpdate(user2, dest_msg2, DummyCallbackQuery("dest:家裡硬碟", user2, dest_msg2)), ctx
    )
    assert "已經有了" not in app.bot.sent_messages[-1]["text"]


@pytest.mark.asyncio
async def test_finish_button_edits_status_in_place_never_deletes(env):
    """
    實測回饋：「我點選沒照片了，那個訊息就被刪除了！」

    按下結束按鈕時，狀態訊息必須**原地編輯**加上「⏳ 確認中」那一行——
    使用者剛點的按鈕就在這則訊息上，它本來就在眼前，不需要刪掉重發；
    刪了反而會讓他看到訊息憑空消失。
    """
    app, *_ = env
    ctx = DummyContext(app)
    user = DummyUser(1050, "七叔")
    session = await _open_session(app, ctx, user, "不要刪我")
    await _send_photo(app, ctx, user, "keep_1", 10)

    status_id_before = session.status_message_id
    assert status_id_before is not None
    deleted_before = len(app.bot.deleted_messages)
    edited_before = len(app.bot.edited_messages)
    sent_before = len(app.bot.sent_messages)

    finish_msg = DummyMessage(20, "", 1050, app.bot)
    await upload.handle_finish_button(
        DummyUpdate(user, finish_msg, DummyCallbackQuery("finish", user, finish_msg)), ctx
    )

    assert session.stage == "debounce"
    # 關鍵：那則訊息不可以被刪掉，id 也不可以換
    assert len(app.bot.deleted_messages) == deleted_before, "狀態訊息不該被刪除"
    assert session.status_message_id == status_id_before, "應該是同一則訊息，不是重發的新訊息"
    assert len(app.bot.sent_messages) == sent_before, "不該多發一則訊息"
    # 而是原地編輯，把「確認中」那一行長出來
    assert len(app.bot.edited_messages) == edited_before + 1
    edited = app.bot.edited_messages[-1]
    assert edited["message_id"] == status_id_before
    assert "確認中" in edited["text"]
    assert "已收到 1 張" in edited["text"], "張數要留著，這則訊息從頭到尾講同一件事"


@pytest.mark.asyncio
async def test_onedrive_release_is_deferred_not_immediate(env, monkeypatch):
    """
    實測 bug 回歸：OneDrive 的「釋放本機空間」不可以在批次一結束就立刻執行。

    那時 OneDrive 用戶端通常還沒把檔案傳到雲端，雲端沒有副本可以「僅線上」，
    `attrib +U` 沒有東西可以指向、不會生效。實測結果是 24 個檔案的屬性全都
    只有 A、完全沒有 U——標記從頭到尾就沒生效過。之後 OneDrive 自己完成上傳
    並把檔案留在本機，使用者看起來就像「照片被自動下載回來了」。

    正確行為（規格書 §4.2）：延遲到 OneDrive 有足夠時間同步完成後才執行。
    """
    app, data_dir, temp_dir, nas_dir, od_dir = env
    ctx = DummyContext(app)
    user = DummyUser(1060, "八嬸")

    called = []
    monkeypatch.setattr(storage, "free_onedrive_space", lambda paths: called.append(list(paths)))

    session = await _open_session(app, ctx, user, "雲端測試", dest="OneDrive")
    await _send_photo(app, ctx, user, "od_1", 10)
    await session.pipeline.settle()
    await upload._finalize_upload(ctx, session, timed_out=False)

    # 收尾當下絕對不可以已經執行——那是太早的時機
    assert called == [], "釋放空間不可以在批次結束當下立刻執行"

    # 而是排了一個延遲的工作
    release_jobs = [j for j in app.job_queue.jobs if j.name.startswith("onedrive_release:")]
    assert len(release_jobs) == 1, "應該排一個延遲執行的釋放空間工作"
    assert release_jobs[0].data["paths"], "要帶著這批實際寫入 OneDrive 的檔案清單"

    # 時間到了才真的執行
    ctx.job = release_jobs[0]
    await upload._release_onedrive_space_job(ctx)
    assert len(called) == 1
    assert len(called[0]) == 1, "剛剛那一張照片要被標記為僅線上"


@pytest.mark.asyncio
async def test_onedrive_release_skipped_when_disabled(env, monkeypatch):
    """ONEDRIVE_FREE_SPACE = False 時，完全不排程、也不碰檔案屬性。"""
    app, *_ = env
    ctx = DummyContext(app)
    user = DummyUser(1061, "九叔")

    called = []
    monkeypatch.setattr(storage, "free_onedrive_space", lambda paths: called.append(list(paths)))
    original = config.ONEDRIVE_FREE_SPACE
    try:
        config.ONEDRIVE_FREE_SPACE = False
        session = await _open_session(app, ctx, user, "不釋放", dest="OneDrive")
        await _send_photo(app, ctx, user, "od_off", 10)
        await session.pipeline.settle()
        await upload._finalize_upload(ctx, session, timed_out=False)

        assert called == []
        assert not [j for j in app.job_queue.jobs if j.name.startswith("onedrive_release:")]
    finally:
        config.ONEDRIVE_FREE_SPACE = original
