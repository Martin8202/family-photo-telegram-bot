"""
Google Drive 資料夾健檢（V2：只剩一個目的地）。

V1 這裡測的是「家裡硬碟壞掉時 OneDrive 照常上傳」；V2 只有 Google Drive，
改測三件事：健檢只看不寫、Drive 不能用時不開 session、開機時 G: 還沒掛上要等一下再試。
"""
import types

import pytest

import bot
import config
import storage
from handlers import upload

from test_antigravity_e2e_suite import (  # noqa: F401 - env 是 fixture，靠 import 帶進來
    DummyMessage,
    DummyUpdate,
    DummyUser,
    DummyContext,
    env,
)


def test_health_check_reads_only(tmp_path):
    """健檢不可以寫測試檔、也不可以建資料夾：在 G: 上寫再刪＝雲端多一次新增＋刪除。"""
    ok, err = storage.health_check(tmp_path)
    assert ok and err is None
    assert list(tmp_path.iterdir()) == [], "健檢不可以留下或寫入任何檔案"

    missing = tmp_path / "照片"
    ok, err = storage.health_check(missing)
    assert not ok and "找不到" in err
    assert not missing.exists(), "路徑不對時要回報失敗，不可以自己開一個新的"


@pytest.mark.asyncio
async def test_dead_drive_blocks_session_and_notifies(env, tmp_path):
    app, *_ = env
    config.HEALTH_CHECK_ON_SESSION = True
    config.DRIVE_PHOTO_ROOT = str(tmp_path / "G槽沒掛上" / "照片")
    ctx = DummyContext(app)
    user = DummyUser(1001, "媽媽")
    members = app.bot_data["members"]
    members.register(1001, "媽媽")
    members.approve(1001)

    await upload.handle_start_upload(DummyUpdate(user, DummyMessage(10, "📷 我要上傳照片", 1001, app.bot)), ctx)

    assert not app.bot_data["sessions"].has_active(1001)
    assert any("暫時無法上傳" in m["text"] for m in app.bot.sent_messages if m["chat_id"] == 1001)
    assert any("Google Drive" in m["text"] for m in app.bot.sent_messages if m["chat_id"] == 999999)


@pytest.mark.asyncio
async def test_startup_waits_for_drive_to_mount(env, tmp_path):
    """開機時 G: 還沒掛上：等一下再試，掛上了就通過，不要直接放棄。"""
    app, *_ = env
    root = tmp_path / "晚一點才出現" / "照片"
    config.DRIVE_PHOTO_ROOT = str(root)
    config.HEALTH_CHECK_ON_START = True
    sleeps = []

    async def fake_sleep(sec):
        sleeps.append(sec)
        root.mkdir(parents=True)  # 第一次等待之後 Drive 掛上了

    assert await bot.startup_health_check(app, sleep=fake_sleep) is True
    assert sleeps == [bot.STARTUP_DRIVE_RETRY_SEC]
    assert not [m for m in app.bot.sent_messages if m["chat_id"] == 999999], "等到了就不必通知管理員"


@pytest.mark.asyncio
async def test_startup_gives_up_after_max_wait_and_notifies(env, tmp_path):
    app, *_ = env
    config.DRIVE_PHOTO_ROOT = str(tmp_path / "一直沒掛上" / "照片")
    config.HEALTH_CHECK_ON_START = True
    sleeps = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    assert await bot.startup_health_check(app, sleep=fake_sleep) is False
    assert sum(sleeps) == bot.STARTUP_DRIVE_MAX_WAIT_SEC
    assert any("Google Drive" in m["text"] for m in app.bot.sent_messages if m["chat_id"] == 999999)


def test_missing_config_keys_are_listed():
    cfg = types.SimpleNamespace(**{k: v for k, v in vars(config).items() if k.isupper()})
    cfg.DRIVE_PHOTO_ROOT = "x"
    assert "DRIVE_PHOTO_ROOT" not in bot.missing_config_keys(cfg)
    del cfg.DRIVE_PHOTO_ROOT
    assert bot.missing_config_keys(cfg) == ["DRIVE_PHOTO_ROOT"]
