"""
V2 §3.6 不漏收保證：每一列狀況都要證明照片有進暫存區、張數等於實際收到的張數。

表中其他列已有測試：
- 上一批正在上傳 → test_upload_picker.test_photos_during_processing_go_to_same_folder_next
- 機器人重新啟動 → test_pending_reminder.test_restart_restores_pending_batch_and_reminds
- 下載失敗 → test_antigravity_e2e_suite 的下載重試測試
"""

import pytest

import storage
from handlers import upload

from test_antigravity_e2e_suite import (  # noqa: F401 - env 是 fixture，靠 import 帶進來
    DummyContext,
    DummyMessage,
    DummyUpdate,
    DummyUser,
    _open_session,
    _send_photo,
    ctx_with_job,
    env,
)


class FakeTgFile:
    """Telegram 的 Document／Video：只放測試用得到的欄位（不用 MagicMock，避免假屬性混進判斷）。"""

    def __init__(self, file_id, file_name=None, mime_type=None, file_size=1000):
        self.file_id = file_id
        self.file_unique_id = f"u_{file_id}"
        self.file_name = file_name
        self.mime_type = mime_type
        self.file_size = file_size


def _approve(app, user):
    members = app.bot_data["members"]
    members.register(user.id, user.first_name)
    members.approve(user.id)


async def _send_file(app, ctx, user, tg_file, msg_id, video=False):
    msg = DummyMessage(msg_id, "", user.id, app.bot, document=None if video else tg_file)
    msg.video = tg_file if video else None
    await upload.handle_photo_message(DummyUpdate(user, msg), ctx)


def _texts(app, chat_id):
    return [m["text"] for m in app.bot.sent_messages if m["chat_id"] == chat_id]


@pytest.mark.asyncio
async def test_no_session_photo_goes_to_temp(env):
    app, _, temp_dir, _, _ = env
    ctx = DummyContext(app)
    user = DummyUser(3001, "媽媽")
    _approve(app, user)

    await _send_photo(app, ctx, user, "first", 10)
    session = app.bot_data["sessions"].get(3001)
    assert session is not None and session.stage == "awaiting_folder"
    await session.pipeline.settle()
    assert session.received_count == 1
    assert len([p for p in session.temp_dir.iterdir() if p.name != storage.SESSION_INFO_FILENAME]) == 1


@pytest.mark.asyncio
async def test_before_folder_chosen_photos_merge_into_batch(env):
    app, *_ = env
    ctx = DummyContext(app)
    user = DummyUser(3002, "爸爸")
    _approve(app, user)

    for i in range(4):
        await _send_photo(app, ctx, user, f"m{i}", 10 + i)
    session = app.bot_data["sessions"].get(3002)
    await session.pipeline.settle()
    assert session.received_count == 4
    assert sum(1 for rf in session.files if rf.downloaded) == 4


@pytest.mark.asyncio
async def test_too_large_and_unsupported_each_told_not_counted(env):
    app, *_ = env
    ctx = DummyContext(app)
    user = DummyUser(3003, "阿姨")
    _approve(app, user)

    await _send_file(app, ctx, user, FakeTgFile("big", "旅行.mp4", "video/mp4", 25 * 1024 * 1024), 10, video=True)
    await _send_file(app, ctx, user, FakeTgFile("zip", "資料.zip", "application/zip"), 11)
    await _send_file(app, ctx, user, FakeTgFile("big2", "大.pdf", "application/pdf", 21 * 1024 * 1024), 12)

    texts = _texts(app, 3003)
    assert sum("超過 20 MB" in t for t in texts) == 2, "每一個太大的都要告訴家人"
    assert any("這種檔案不能傳" in t for t in texts)
    admin = _texts(app, 999999)
    assert any("旅行.mp4" in t for t in admin) and any("大.pdf" in t for t in admin), "也要通知管理員"
    assert app.bot_data["sessions"].get(3003) is None, "收不了的檔案不開批次、不計數"


@pytest.mark.asyncio
async def test_video_and_document_are_received(env):
    app, _, _, nas_dir, _ = env
    ctx = DummyContext(app)
    user = DummyUser(3004, "姊姊")
    session = await _open_session(app, ctx, user, "2026-09-01開學")

    await _send_file(app, ctx, user, FakeTgFile("vid1", None, "video/mp4"), 10, video=True)
    await _send_file(app, ctx, user, FakeTgFile("doc1", "成績單.pdf", "application/pdf"), 11)
    await _send_file(app, ctx, user, FakeTgFile("img1", "IMG_0001.HEIC", "image/heic"), 12)
    assert session.received_count == 3
    await session.pipeline.settle()
    await upload._debounce_fire(ctx_with_job(ctx, 3004))

    names = sorted(p.name for p in (nas_dir / "2026-09-01開學").iterdir())
    assert "成績單.pdf" in names, "文件保留原檔名"
    assert len(names) == 3


@pytest.mark.asyncio
async def test_document_duplicate_by_name_and_size(env):
    """文件：檔名＋大小都一樣才問「已經有了」；同名不同大小就另存 _(2)。"""
    app, _, _, nas_dir, _ = env
    folder = nas_dir / "2026-10-01文件"
    folder.mkdir()
    ctx = DummyContext(app)
    user = DummyUser(3005, "哥哥")
    session = await _open_session(app, ctx, user, "2026-10-01文件")

    # 測試用的下載內容固定是「JPEG 頭＋file_id＋結尾」，先放一份一模一樣大小的同名檔
    await _send_file(app, ctx, user, FakeTgFile("same", "報告.pdf", "application/pdf"), 10)
    await session.pipeline.settle()
    rf = session.files[0]
    (folder / "報告.pdf").write_bytes(rf.temp_path.read_bytes())
    (folder / "收據.pdf").write_bytes(b"different size content")
    await _send_file(app, ctx, user, FakeTgFile("rcpt", "收據.pdf", "application/pdf"), 11)
    await session.pipeline.settle()
    await upload._debounce_fire(ctx_with_job(ctx, 3005))

    assert any("已經有了" in t or "一模一樣" in t for t in _texts(app, 3005)), "同名同大小要問"
    pending = app.bot_data["sessions"].get_pending_duplicates(3005)
    assert pending is not None and [f.original_name for f in pending.files] == ["報告.pdf"]
    assert (folder / "收據_(2).pdf").exists(), "同名但大小不同是不同檔案，另存不覆蓋"
