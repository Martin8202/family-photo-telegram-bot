"""
家裡硬碟壞掉時，OneDrive 要照常上傳。

這是實際踩到的狀況：家裡硬碟故障，開 session 的健檢一失敗就整個 return，
使用者連「只存 OneDrive」都做不到——硬碟壞掉的那幾天所有照片完全沒地方存，
與「照片不遺失」的第一原則相反。壞的那個不給選，好的那個照常走。
"""
import pytest

import config
from handlers import upload
from keyboards import destination_keyboard
from state import DEST_BOTH_LABEL, DEST_NAS_LABEL, DEST_ONEDRIVE_LABEL, STAGE_AWAITING_DESTINATION

from test_antigravity_e2e_suite import (  # noqa: F401 - env 是 fixture，靠 import 帶進來
    DummyCallbackQuery,
    DummyContext,
    DummyMessage,
    DummyUpdate,
    DummyUser,
    env,
)


def _kill_nas(tmp_path) -> None:
    """把 DEST_NAS 指到一個「檔案底下的子路徑」，mkdir 必定失敗＝模擬硬碟壞掉。"""
    dead = tmp_path / "壞掉的硬碟"
    dead.write_text("x", encoding="utf-8")
    config.DEST_NAS = str(dead / "照片")


def _button_texts(keyboard) -> list:
    return [b.text for row in keyboard.inline_keyboard for b in row]


def test_keyboard_hides_only_the_dead_destination():
    texts = _button_texts(destination_keyboard({DEST_ONEDRIVE_LABEL}))
    assert any(DEST_ONEDRIVE_LABEL in t for t in texts)
    assert not any(DEST_NAS_LABEL in t for t in texts)
    assert not any(DEST_BOTH_LABEL in t for t in texts)   # 少一邊就不能「兩邊都存」
    assert len(_button_texts(destination_keyboard())) == 3  # 兩邊都活著時三個選項都在


@pytest.mark.asyncio
async def test_health_check_judges_each_destination_separately(env, tmp_path):
    _kill_nas(tmp_path)
    healthy, failed = await upload._health_check_destinations(config)
    assert healthy == {DEST_ONEDRIVE_LABEL}
    assert DEST_NAS_LABEL in failed


@pytest.mark.asyncio
async def test_dead_nas_does_not_block_onedrive_upload(env, tmp_path):
    app, _data_dir, _temp_dir, _nas_dir, _od_dir = env
    config.HEALTH_CHECK_ON_SESSION = True
    _kill_nas(tmp_path)

    ctx = DummyContext(app)
    user = DummyUser(1001, "媽媽")
    members = app.bot_data["members"]
    members.register(1001, "媽媽")
    members.approve(1001)

    await upload.handle_start_upload(
        DummyUpdate(user, DummyMessage(10, "📷 我要上傳照片", 1001, app.bot)), ctx
    )
    # 舊行為會在健檢這一步直接 return，session 根本開不起來
    assert app.bot_data["sessions"].has_active(1001)
    assert ctx.user_data[upload.HEALTHY_DESTS_KEY] == [DEST_ONEDRIVE_LABEL]
    assert any("連不上" in m["text"] for m in app.bot.sent_messages if m["chat_id"] == 1001)

    folder_msg = DummyMessage(11, "硬碟壞掉那幾天", 1001, app.bot)
    await upload.handle_folder_text(DummyUpdate(user, folder_msg), ctx)
    session = app.bot_data["sessions"].get(1001)
    assert session.folder == "硬碟壞掉那幾天"

    # 從舊訊息點到「兩邊都存」：擋下來並重新給選項，不可以放行去寫壞掉的硬碟
    both_cb = DummyCallbackQuery(f"dest:{DEST_BOTH_LABEL}", user, folder_msg)
    await upload.handle_destination_button(DummyUpdate(user, folder_msg, both_cb), ctx)
    assert session.destination is None
    assert session.stage == STAGE_AWAITING_DESTINATION

    # 只存 OneDrive 照常放行
    od_cb = DummyCallbackQuery(f"dest:{DEST_ONEDRIVE_LABEL}", user, folder_msg)
    await upload.handle_destination_button(DummyUpdate(user, folder_msg, od_cb), ctx)
    assert session.destination == DEST_ONEDRIVE_LABEL
    assert set(session.destinations) == {DEST_ONEDRIVE_LABEL}
