"""V2 階段 3 §3.8：/status 只有管理員能用、選單、待清理清單整理、data 備份。"""

from datetime import date, datetime

import pytest

import config
from bot import setup_menu
from handlers import admin

from test_antigravity_e2e_suite import (  # noqa: F401 - env 是 fixture，靠 import 帶進來
    DummyContext,
    DummyMessage,
    DummyUpdate,
    DummyUser,
    _send_photo,
    env,
)


@pytest.mark.asyncio
async def test_status_only_for_admin(env, monkeypatch):
    app, *_ = env
    monkeypatch.setattr(config, "ADMIN_ID", 999999)
    app.bot_data["started_at"] = datetime.now()
    ctx = DummyContext(app)
    fam = DummyUser(6001, "媽媽")
    app.bot_data["members"].register(6001, "媽媽")
    app.bot_data["members"].approve(6001)
    await _send_photo(app, ctx, fam, "p1", 5)  # 一批沒選資料夾

    n = len(app.bot.sent_messages)
    await admin.handle_status(DummyUpdate(fam, DummyMessage(1, "/status", 6001, app.bot)), ctx)
    assert len(app.bot.sent_messages) == n, "非管理員完全不回應"

    boss = DummyUser(999999, "管理員")
    await admin.handle_status(DummyUpdate(boss, DummyMessage(2, "/status", 999999, app.bot)), ctx)
    text = app.bot.sent_messages[-1]["text"]
    assert text.startswith("📊 機器人狀態")
    assert "G:：✅ 正常" in text
    assert "沒選資料夾 1" in text
    assert "這 7 天：開始 1" in text
    await app.bot_data["sessions"].get(6001).pipeline.drain()


@pytest.mark.asyncio
async def test_menu_family_find_admin_also_status(env, monkeypatch):
    app, *_ = env
    monkeypatch.setattr(config, "ADMIN_ID", 999999)
    await setup_menu(app)
    (family, scope_all), (admin_cmds, scope_admin) = app.bot.commands_set
    assert [c.command for c in family] == ["find"] and scope_all is None
    assert [c.command for c in admin_cmds] == ["find", "status"]
    assert scope_admin.chat_id == 999999


def test_prune_only_drops_rows_whose_file_is_gone(env):
    app, _, _, nas_dir, _ = env
    logs = app.bot_data["logs"]
    folder = nas_dir / "2026-07-02金門"
    folder.mkdir()
    (folder / "still_here.jpg").write_bytes(b"x")
    logs.log_cleanup_batch([
        ("t", "媽媽", 1, "中止殘留", str(folder), "still_here.jpg", ""),
        ("t", "媽媽", 1, "中止殘留", str(folder), "already_deleted.jpg", ""),
        ("t", "媽媽", 1, "傳錯", r"\\NAS\舊路徑", "v1.jpg", "V1 的舊路徑，看不到就留著"),
    ])

    assert admin.prune_cleanup_list(logs, nas_dir) == (1, 2)
    left = [r["檔名"] for r in logs.cleanup_list.read_all_rows()]
    assert left == ["still_here.jpg", "v1.jpg"]
    assert (folder / "still_here.jpg").exists(), "只看不刪"


def test_prune_skips_everything_when_drive_missing(env):
    app, _, _, nas_dir, _ = env
    logs = app.bot_data["logs"]
    logs.log_cleanup_batch([("t", "媽媽", 1, "中止殘留", str(nas_dir / "x"), "gone.jpg", "")])
    assert admin.prune_cleanup_list(logs, nas_dir / "G槽沒掛上") is None
    assert len(logs.cleanup_list.read_all_rows()) == 1, "G: 不能用時一筆都不能拿掉"


def test_backup_only_when_due_and_never_overwrites(env):
    app, data_dir, _, nas_dir, _ = env
    (data_dir / "members.json").write_text("{}", encoding="utf-8")
    (data_dir / "notes.txt").write_text("不備份", encoding="utf-8")
    root = nas_dir.parent / "_機器人備份"

    first = admin.backup_data(data_dir, root, date(2026, 11, 1), 7)
    assert first == root / "2026-11-01"
    assert (first / "members.json").exists() and (first / "upload_log.csv").exists()
    assert not (first / "notes.txt").exists()

    assert admin.backup_data(data_dir, root, date(2026, 11, 7), 7) is None, "未滿 7 天不備份"
    second = admin.backup_data(data_dir, root, date(2026, 11, 8), 7)
    assert second == root / "2026-11-08"

    (data_dir / "members.json").write_text('{"changed": 1}', encoding="utf-8")
    admin.backup_data(data_dir, root, date(2026, 11, 8), 0)  # 同一天再跑（例如設定改成 0 天）
    assert (second / "members.json").read_text(encoding="utf-8") == "{}", "既有備份不覆蓋"
    assert (second / "members_(2).json").exists()


def test_backup_root_defaults_next_to_photos(monkeypatch):
    monkeypatch.setattr(config, "DRIVE_PHOTO_ROOT", r"G:\我的雲端硬碟\!家裡硬碟\照片")
    monkeypatch.setattr(config, "BACKUP_DIR", "")
    assert str(admin.backup_root(config)) == r"G:\我的雲端硬碟\!家裡硬碟\_機器人備份"
