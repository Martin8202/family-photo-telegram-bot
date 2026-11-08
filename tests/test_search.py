"""V2 階段 3 §3.5 找東西：找得到／找不到／點下去回傳正確位置，而且不會變成上傳。"""

import pytest

from folder_search import FolderIndex
from handlers import search, upload

from test_antigravity_e2e_suite import (  # noqa: F401 - env 是 fixture，靠 import 帶進來
    DummyCallbackQuery,
    DummyContext,
    DummyMessage,
    DummyUpdate,
    DummyUser,
    _send_photo,
    env,
)


def _setup(app, data_dir, nas_dir):
    (nas_dir / "2026-07-02金門泉州之旅").mkdir()
    (nas_dir / "家人" / "2007-7-2~8金門旅遊").mkdir(parents=True)
    index = FolderIndex(data_dir / "folders.json", nas_dir)
    index.rescan()
    app.bot_data["folders"] = index
    user = DummyUser(5001, "媽媽")
    members = app.bot_data["members"]
    members.register(5001, "媽媽")
    members.approve(5001)
    return user


async def _type(app, ctx, user, text, msg_id=10):
    return await search.handle_text(DummyUpdate(user, DummyMessage(msg_id, text, user.id, app.bot)), ctx)


async def _press(app, ctx, user, data):
    m = DummyMessage(90, "", user.id, app.bot)
    await search.handle_button(DummyUpdate(user, m, DummyCallbackQuery(data, user, m)), ctx)


@pytest.mark.asyncio
async def test_find_lists_folders_and_tells_location(env):
    app, data_dir, _, nas_dir, _ = env
    user = _setup(app, data_dir, nas_dir)
    ctx = DummyContext(app)

    await search.handle_find_command(DummyUpdate(user, DummyMessage(9, "/find", 5001, app.bot)), ctx)
    assert "要找什麼" in app.bot.sent_messages[-1]["text"]
    assert await _type(app, ctx, user, "金門") is True

    markup = app.bot.sent_messages[-1]["reply_markup"]
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert all(t.startswith("🔗") for t in labels[:-1]), "按鈕是 🔗，跟上傳的 📥 不同"
    assert "🔗 2007-7-2~8金門旅遊（在「照片」裡的「家人」裡）" in labels
    assert all(len(b.callback_data.encode()) <= 64 for row in markup.inline_keyboard for b in row)

    idx = labels.index("🔗 2007-7-2~8金門旅遊（在「照片」裡的「家人」裡）")
    await _press(app, ctx, user, f"find:pick:{idx}")
    text = app.bot.sent_messages[-1]["text"]
    assert "「2007-7-2~8金門旅遊」（在「照片」裡的「家人」裡）" in text
    assert "Google Drive App 搜尋" in text


@pytest.mark.asyncio
async def test_find_none_and_retype(env):
    app, data_dir, _, nas_dir, _ = env
    user = _setup(app, data_dir, nas_dir)
    ctx = DummyContext(app)

    await search.handle_find_command(DummyUpdate(user, DummyMessage(9, "/find", 5001, app.bot)), ctx)
    await _type(app, ctx, user, "火星")
    assert app.bot.sent_messages[-1]["text"] == "找不到叫「火星」的資料夾"
    assert await _type(app, ctx, user, "金門", 11) is False, "找完一次就回到平常，打字不會一直被當成搜尋"

    await _press(app, ctx, user, "find:retype")
    assert await _type(app, ctx, user, "金門", 12) is True


@pytest.mark.asyncio
async def test_find_does_not_touch_pending_upload(env):
    """有一批還沒選資料夾時去找東西：那批照片不受影響，找完打字又回到選資料夾的搜尋。"""
    app, data_dir, _, nas_dir, _ = env
    user = _setup(app, data_dir, nas_dir)
    ctx = DummyContext(app)
    await _send_photo(app, ctx, user, "p1", 5)
    session = app.bot_data["sessions"].get(5001)

    await search.handle_find_command(DummyUpdate(user, DummyMessage(9, "/find", 5001, app.bot)), ctx)
    await _type(app, ctx, user, "金門")
    await _press(app, ctx, user, "find:pick:0")
    assert session.folder is None and session.stage == "awaiting_folder"

    assert await _type(app, ctx, user, "金門", 13) is False
    assert await upload.handle_folder_text(DummyUpdate(user, DummyMessage(13, "金門", 5001, app.bot)), ctx) is True
    assert app.bot.sent_messages[-1]["reply_markup"].inline_keyboard[0][0].text.startswith("📥")
    await session.pipeline.drain()
