"""V2 階段 2a：單一入口、選資料夾畫面、搜尋與新建、自動上傳、上傳中又傳照片（D17、D23、D24）。"""

from datetime import datetime

import pytest

from folder_search import FolderIndex
from handlers import upload

from test_antigravity_e2e_suite import (  # noqa: F401 - env 是 fixture，靠 import 帶進來
    DummyContext,
    DummyMessage,
    DummyUpdate,
    DummyUser,
    _open_session,
    _press,
    _send_photo,
    ctx_with_job,
    env,
)


def _approve(app, user):
    members = app.bot_data["members"]
    members.register(user.id, user.first_name)
    members.approve(user.id)


def _texts(app, chat_id):
    return [m["text"] for m in app.bot.sent_messages if m["chat_id"] == chat_id]


@pytest.mark.asyncio
async def test_picker_waits_then_reanchors_when_more_photos_arrive(env):
    app, *_ = env
    ctx = DummyContext(app)
    user = DummyUser(2001, "媽媽")
    _approve(app, user)

    for i in range(3):
        await _send_photo(app, ctx, user, f"p{i}", 10 + i)
    assert not any("收到" in t for t in _texts(app, 2001)), "照片還在進來時不跳畫面"
    assert app.job_queue.get_jobs_by_name("picker:2001"), "排好 3 秒後的畫面"

    await upload._picker_fire(ctx_with_job(ctx, 2001))
    session = app.bot_data["sessions"].get(2001)
    first_id = session.picker_message_id
    assert _texts(app, 2001)[-1] == "收到 3 張！要放進哪個資料夾？"

    for i in range(3, 5):
        await _send_photo(app, ctx, user, f"p{i}", 10 + i)
    await upload._picker_fire(ctx_with_job(ctx, 2001))
    assert (2001, first_id) in app.bot.deleted_messages, "舊畫面要刪掉、新的發在最下面"
    assert _texts(app, 2001)[-1] == "收到 5 張！要放進哪個資料夾？"

    n = len(app.bot.sent_messages)
    await upload._picker_fire(ctx_with_job(ctx, 2001))
    assert len(app.bot.sent_messages) == n, "張數沒變就不重發"
    await session.pipeline.drain()


@pytest.mark.asyncio
async def test_search_picks_second_level_folder_and_uploads_there(env):
    app, data_dir, _, nas_dir, _ = env
    (nas_dir / "家人" / "2026-07-02金門泉州之旅").mkdir(parents=True)
    index = FolderIndex(data_dir / "folders.json", nas_dir)
    index.rescan()
    app.bot_data["folders"] = index
    ctx = DummyContext(app)
    user = DummyUser(2002, "爸爸")
    _approve(app, user)

    await _send_photo(app, ctx, user, "km1", 10)
    await upload.handle_folder_text(DummyUpdate(user, DummyMessage(11, "0702金門", 2002, app.bot)), ctx)
    markup = app.bot.sent_messages[-1]["reply_markup"]
    buttons = [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]
    assert buttons[0] == ("📥 2026-07-02金門泉州之旅（在「照片」裡的「家人」裡）", "fp:pick:0")
    assert all(len(cb.encode()) <= 64 for _, cb in buttons), "callback_data 不可超過 64 bytes"
    assert any(cb == "fp:create" for _, cb in buttons), "最後一顆是「都不是，新建」"

    await _press(app, ctx, user, "fp:pick:0")
    session = app.bot_data["sessions"].get(2002)
    assert session.folder == "家人\\2026-07-02金門泉州之旅"
    await session.pipeline.settle()
    await upload._debounce_fire(ctx_with_job(ctx, 2002))
    assert len(list((nas_dir / "家人" / "2026-07-02金門泉州之旅").glob("*.jpg"))) == 1
    assert index.dirty, "上傳完成後標記要重掃（等全家閒置 10 分鐘）"


@pytest.mark.asyncio
async def test_new_folder_without_date_offers_today(env):
    app, data_dir, _, nas_dir, _ = env
    index = FolderIndex(data_dir / "folders.json", nas_dir)
    index.load()
    app.bot_data["folders"] = index
    ctx = DummyContext(app)
    user = DummyUser(2003, "阿姨")
    _approve(app, user)

    await _send_photo(app, ctx, user, "bbq1", 10)
    await upload.handle_folder_text(DummyUpdate(user, DummyMessage(11, "烤肉", 2003, app.bot)), ctx)
    assert "找不到叫「烤肉」的資料夾" in _texts(app, 2003)[-1]
    await _press(app, ctx, user, "fp:create")
    assert "好像沒有日期" in _texts(app, 2003)[-1]
    await _press(app, ctx, user, "fp:adddate")

    expected = f"{datetime.now():%Y-%m-%d}烤肉"
    assert app.bot_data["sessions"].get(2003).folder == expected
    assert any(f["path"] == expected for f in index.folders), "新建的立刻進資料夾清單"
    await app.bot_data["sessions"].get(2003).pipeline.drain()


@pytest.mark.asyncio
async def test_photos_during_processing_go_to_same_folder_next(env):
    """D24：上一批寫入中又傳照片 → 先收下排隊，上一批結束後接著放同一個資料夾。"""
    app, _, _, nas_dir, _ = env
    ctx = DummyContext(app)
    user = DummyUser(2004, "姊姊")
    session = await _open_session(app, ctx, user, "2026-08-01海邊")
    await _send_photo(app, ctx, user, "first", 10)
    await session.pipeline.settle()

    session.stage = "processing"  # 模擬正在寫入
    await _send_photo(app, ctx, user, "late", 11)
    sessions = app.bot_data["sessions"]
    assert sessions.get(2004) is session, "寫入中的批次不受影響"
    queued = sessions.get_queued(2004)
    assert queued is not None and queued.received_count == 1, "晚到的照片不可以漏收"

    session.stage = "debounce"
    await upload._finalize_upload(ctx, session, timed_out=False)
    assert sessions.get(2004) is queued
    assert queued.folder == "2026-08-01海邊" and queued.stage == "receiving_photos"
    assert any("又收到 1 張" in t for t in _texts(app, 2004))

    await queued.pipeline.settle()
    await upload._debounce_fire(ctx_with_job(ctx, 2004))
    assert len(list((nas_dir / "2026-08-01海邊").glob("*.jpg"))) == 2
    assert sessions.get(2004) is None
