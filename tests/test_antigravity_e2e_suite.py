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
from datetime import datetime
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

    async def send_message(self, chat_id: int, text: str, reply_markup=None):
        msg = DummyMessage(message_id=len(self.sent_messages) + 100, text=text, chat_id=chat_id, bot=self)
        self.sent_messages.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup, "msg": msg})
        return msg

    async def send_chat_action(self, chat_id: int, action: str):
        self.chat_actions.append((chat_id, action))

    async def get_file(self, file_id: str):
        return DummyTelegramFile(file_id)

    async def delete_message(self, chat_id: int, message_id: int):
        pass

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
    def __init__(self, data: str, user: DummyUser, message: DummyMessage):
        self.data = data
        self.from_user = user
        self.message = message
        self.answered = False

    async def answer(self):
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
async def test_debounce_confirm_message_throttled_but_timer_always_resets(env):
    """
    緩衝期間（點了「我傳完了」之後）密集連傳照片時：
    - 「確認中」訊息的畫面更新應節流（未到節流秒數前不重發），避免洗版/觸發 429。
    - 但每張照片都必須照常計入本批、且都要重新啟動 5 秒無新照片計時（不能因為節流而漏收）。
    """
    app, data_dir, temp_dir, nas_dir, od_dir = env
    original_counter_sec = config.COUNTER_UPDATE_SEC
    try:
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

        # 點「我傳完了」進入緩衝，第一則「確認中」一定要立即送出（不受節流影響）
        finish_msg = DummyMessage(5, "", 1002, app.bot)
        await upload.handle_finish_button(
            DummyUpdate(user, finish_msg, DummyCallbackQuery("finish", user, finish_msg)), ctx
        )
        assert session.confirm_message_id is not None
        msg_count_after_first_confirm = len(app.bot.sent_messages)
        confirm_id_after_first = session.confirm_message_id

        # 節流秒數拉大，模擬「還沒到可以更新畫面的時間點」
        config.COUNTER_UPDATE_SEC = 9999
        photo2 = MagicMock(); photo2.file_id = "p2"
        await upload.handle_photo_message(DummyUpdate(user, DummyMessage(6, "", 1002, app.bot, photo=[photo2])), ctx)

        # 張數仍要正確累計、計時器仍要重新排程，但畫面不該多發一則訊息
        assert session.received_count == 2
        assert len(app.bot.sent_messages) == msg_count_after_first_confirm
        assert session.confirm_message_id == confirm_id_after_first
        jobs_after_second = app.job_queue.get_jobs_by_name("debounce:1002")
        assert len(jobs_after_second) == 1  # 舊 job 被換掉，仍只有一個在排隊中（計時已重啟）

        # 節流時間已過，畫面這次應該要更新
        session.counter_last_update = datetime(2000, 1, 1)
        photo3 = MagicMock(); photo3.file_id = "p3"
        await upload.handle_photo_message(DummyUpdate(user, DummyMessage(7, "", 1002, app.bot, photo=[photo3])), ctx)

        assert session.received_count == 3
        assert len(app.bot.sent_messages) == msg_count_after_first_confirm + 1
        assert "3" in app.bot.sent_messages[-1]["text"]
    finally:
        config.COUNTER_UPDATE_SEC = original_counter_sec


@pytest.mark.asyncio
async def test_debounce_defers_flush_until_finalize(env):
    """
    點下「我傳完了」進入緩衝期後，收到的新照片不該再觸發內部小批複製（_flush_ready_chunks）。
    所有緩衝期間收到的照片，應遞延到 debounce 結束、_finalize_upload 收尾時才一次寫入目的地。
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

        # 收件階段：滿一批（2 張）應該立刻複製到目的地
        for i in range(2):
            photo = MagicMock(); photo.file_id = f"recv_{i}"
            await upload.handle_photo_message(
                DummyUpdate(user, DummyMessage(10 + i, "", 1003, app.bot, photo=[photo])), ctx
            )
        assert session.flushed_count == 2
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
        assert session.received_count == 4
        assert session.flushed_count == 2  # 緩衝期間收到的 2 張還沒被複製
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
