"""V2 §3.7 沒選資料夾的提醒與清除，以及重啟後接回「待選資料夾」批次（§3.6）。"""

from datetime import datetime, timedelta

import pytest

import storage
from bot import startup_recover_temp
from handlers import upload

from test_antigravity_e2e_suite import (  # noqa: F401 - env 是 fixture，靠 import 帶進來
    DummyCallbackQuery,
    DummyContext,
    DummyMessage,
    DummyUpdate,
    DummyUser,
    _press,
    _send_photo,
    ctx_with_job,
    env,
)


@pytest.fixture(autouse=True)
def _ten_minutes(monkeypatch):
    """測試固定用規格的 10 分鐘，不受本機 config.py（例如手機實測時縮短）影響。"""
    import config
    monkeypatch.setattr(config, "PENDING_REMIND_MIN", 10, raising=False)
    monkeypatch.setattr(config, "PENDING_DISCARD_AFTER_REMIND_MIN", 10, raising=False)


def _approve(app, user):
    members = app.bot_data["members"]
    members.register(user.id, user.first_name)
    members.approve(user.id)


def _texts(app, chat_id):
    return [m["text"] for m in app.bot.sent_messages if m["chat_id"] == chat_id]


async def _pending(app, ctx, user, n=3):
    _approve(app, user)
    for i in range(n):
        await _send_photo(app, ctx, user, f"{user.id}_{i}", 50 + i)
    session = app.bot_data["sessions"].get(user.id)
    await session.pipeline.settle()
    return session


def _age(session, minutes):
    """把「最後動作時間」往前推，模擬過了幾分鐘沒動作。"""
    session.last_activity_at = datetime.now() - timedelta(minutes=minutes)


async def _button(app, ctx, user, handler, data):
    m = DummyMessage(95, "", user.id, app.bot)
    await handler(DummyUpdate(user, m, DummyCallbackQuery(data, user, m)), ctx)


@pytest.mark.asyncio
async def test_no_response_reminds_then_discards_after_20_min(env):
    app, data_dir, *_ = env
    ctx = DummyContext(app)
    user = DummyUser(4001, "媽媽")
    session = await _pending(app, ctx, user)
    temp_dir = session.temp_dir

    _age(session, 9)
    await upload.check_session_timeouts(ctx)
    assert session.reminded_at is None, "未滿 10 分鐘不提醒"

    _age(session, 10)
    await upload.check_session_timeouts(ctx)
    reminder = app.bot.sent_messages[-1]
    assert reminder["text"] == "你剛剛傳的 3 張還沒選要放哪裡喔"
    assert reminder["reply_parameters"].message_id == 50, "提醒回覆在這批第一張照片上"
    assert session.reminded_at is not None

    session.reminded_at -= timedelta(minutes=10)
    await upload.check_session_timeouts(ctx)
    assert app.bot_data["sessions"].get(4001) is None
    assert not temp_dir.exists(), "清掉本機暫存"
    assert "已經先清掉了" in _texts(app, 4001)[-1]
    assert "媽媽 3 張未選資料夾，已自動清除" in _texts(app, 999999)[-1]
    rows = app.bot_data["logs"].upload_log.read_all_rows()
    assert rows[-1]["結果"] == "未選資料夾，已清除"


@pytest.mark.asyncio
async def test_any_response_after_reminder_restarts_the_clock(env):
    app, *_ = env
    ctx = DummyContext(app)
    user = DummyUser(4002, "爸爸")
    session = await _pending(app, ctx, user)

    _age(session, 10)
    await upload.check_session_timeouts(ctx)
    first_reminder = session.reminded_at
    session.reminded_at = first_reminder - timedelta(minutes=10)
    await _send_photo(app, ctx, user, "late", 70)  # 有回應
    await upload.check_session_timeouts(ctx)

    assert app.bot_data["sessions"].get(4002) is session, "有回應就不清除"
    assert session.reminded_at is None, "重新計時，之後沒回應會再提醒"
    await session.pipeline.settle()


@pytest.mark.asyncio
async def test_drop_button_clears_immediately(env):
    app, *_ = env
    ctx = DummyContext(app)
    user = DummyUser(4003, "阿姨")
    session = await _pending(app, ctx, user, n=2)
    temp_dir = session.temp_dir

    await _button(app, ctx, user, upload.handle_pending_drop, "pend:drop")
    assert app.bot_data["sessions"].get(4003) is None
    assert not temp_dir.exists()
    assert _texts(app, 4003)[-1] == "好的，這 2 張不存了"


@pytest.mark.asyncio
async def test_keep_button_resends_picker(env):
    app, *_ = env
    ctx = DummyContext(app)
    user = DummyUser(4004, "姊姊")
    session = await _pending(app, ctx, user, n=2)
    await upload._picker_fire(ctx_with_job(ctx, 4004))
    old_picker = session.picker_message_id
    _age(session, 10)
    await upload.check_session_timeouts(ctx)

    await _button(app, ctx, user, upload.handle_pending_keep, "pend:keep")
    assert session.reminded_at is None
    assert _texts(app, 4004)[-1] == "收到 2 張！要放進哪個資料夾？"
    assert (4004, old_picker) in app.bot.deleted_messages, "畫面移到對話最下面"
    await session.pipeline.drain()


@pytest.mark.asyncio
async def test_reminder_send_failure_never_discards(env):
    app, *_ = env
    ctx = DummyContext(app)
    user = DummyUser(4005, "哥哥")
    session = await _pending(app, ctx, user)

    _age(session, 60)
    app.bot.send_error = RuntimeError("Bad Gateway")
    await upload.check_session_timeouts(ctx)
    await upload.check_session_timeouts(ctx)
    assert session.reminded_at is None, "送不出去不開始算「再等 10 分鐘」"
    assert app.bot_data["sessions"].get(4005) is session and session.temp_dir.exists()

    app.bot.send_error = None
    await upload.check_session_timeouts(ctx)
    assert session.reminded_at is not None, "下一輪重送成功才開始算"
    await session.pipeline.drain()


@pytest.mark.asyncio
async def test_restart_restores_pending_batch_and_reminds(env):
    """停機期間到期：重啟後先補提醒、重新給 10 分鐘；選好資料夾後照常上傳。"""
    app, _, temp_dir, nas_dir, _ = env
    user = DummyUser(4006, "媽媽")
    _approve(app, user)
    pending_dir = storage.session_temp_dir(
        storage.user_temp_dir(temp_dir, 4006, "媽媽"), datetime(2026, 11, 8, 9, 0), upload.PENDING_TEMP_LABEL
    )
    storage.write_session_info(pending_dir, {"destination": "Google Drive", "folder": None,
                                             "telegram_id": 4006, "name": "媽媽"})
    (pending_dir / "photoA.jpg").write_bytes(b"photo-a")
    (pending_dir / f"docB{storage.DOC_NAME_SEP}旅遊行程.pdf").write_bytes(b"doc-b")

    # 補送不可以把它當成已選好資料夾的批次（2a 的漏洞：側車檔 folder 是 None）
    await startup_recover_temp(app)
    assert pending_dir.exists() and not list(nas_dir.iterdir())

    ctx = DummyContext(app)
    assert await upload.restore_pending_batches(ctx) == 1
    session = app.bot_data["sessions"].get(4006)
    assert session.received_count == 2 and session.stage == "awaiting_folder"
    assert _texts(app, 4006)[-1] == "你剛剛傳的 2 張還沒選要放哪裡喔"
    assert session.reminded_at is not None

    await _button(app, ctx, user, upload.handle_pending_keep, "pend:keep")
    await _press(app, ctx, user, "fp:new")
    await upload.handle_folder_text(DummyUpdate(user, DummyMessage(80, "2026-11-01秋遊", 4006, app.bot)), ctx)
    await _press(app, ctx, user, "fp:create")
    await upload._debounce_fire(ctx_with_job(ctx, 4006))

    names = sorted(p.name for p in (nas_dir / "2026-11-01秋遊").iterdir())
    assert "旅遊行程.pdf" in names and len(names) == 2
    assert app.bot_data["sessions"].get(4006) is None
    rows = app.bot_data["logs"].upload_log.read_all_rows()
    assert rows[-1]["張數"] == "2" and rows[-1]["結果"] == "成功", "接回的批次張數要記對"
    assert len([r for r in app.bot_data["logs"].file_index.read_all_rows() if r["目標資料夾"] == "2026-11-01秋遊"]) == 2
