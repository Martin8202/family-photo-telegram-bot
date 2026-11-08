r"""
管理員與維運（V2 規格 §3.8）。

- `/status`：只有管理員能用，其他人打了完全不回應。
- 待清理清單整理：檔案已經不在 G: 的那一筆從清單拿掉。**只看檔案在不在，不刪 G: 上任何東西。**
  G: 健檢不通過就整個跳過——不然 G: 沒掛上時每一筆都會被當成「已經刪掉」。
- data\ 備份：最新一份滿 BACKUP_EVERY_DAYS 天才再備份，複製到 `!家裡硬碟\_機器人備份\<日期>`，只新增不覆蓋。

排程由 bot.py 每 24 小時呼叫 `run_maintenance` 一次（hn143 手動開機，半夜不一定開著，所以不綁固定時間）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes

import notify
import storage

logger = logging.getLogger("photo-bot.admin")

_DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WEEK_STEPS = [  # (步驟紀錄裡的字, /status 顯示的名稱)
    ("開始上傳", "開始"),
    ("完成", "完成"),
    ("逾時自動完成", "逾時自動完成"),
    ("未選資料夾，已清除", "沒選資料夾被清掉"),
    ("使用者選不用存", "按「不用存」"),
    ("重新開始", "按「重新開始」"),
]


def _drive_root(config) -> Path:
    return Path(config.DRIVE_PHOTO_ROOT)


def backup_root(config) -> Path:
    """空白＝「照片」上一層的 _機器人備份（!家裡硬碟\\_機器人備份）。"""
    custom = getattr(config, "BACKUP_DIR", "")
    return Path(custom) if custom else _drive_root(config).parent / "_機器人備份"


# ── 待清理清單整理 ───────────────────────────────────

def prune_cleanup_list(logs, drive_root: Path) -> Optional[tuple[int, int]]:
    """拿掉檔案已不在的列，回傳 (拿掉幾筆, 剩幾筆)；G: 不能用時回傳 None、什麼都不動。"""
    ok, err = storage.health_check(drive_root)
    if not ok:
        logger.warning("G: 不能用，這次不整理待清理清單：%s", err)
        return None
    root = drive_root.resolve()

    def keep(row: dict) -> bool:
        location, name = (row.get("待刪位置") or "").strip(), (row.get("檔名") or "").strip()
        if not location or not name:
            return True
        path = Path(location) / name
        try:
            path.resolve().relative_to(root)
        except ValueError:
            return True  # 不在「照片」底下的（例如 V1 的舊路徑）看不到，留給人工判斷
        try:
            return path.exists()
        except OSError:
            return True  # 看不到就當作還在，寧可多留
    return logs.cleanup_list.rewrite_rows(keep)


# ── data\ 備份 ──────────────────────────────────────

def latest_backup(root: Path) -> Optional[date]:
    if not root.is_dir():
        return None
    dates = []
    for e in os.scandir(root):
        if e.is_dir() and _DATE_DIR.match(e.name[:10]):
            try:
                dates.append(date.fromisoformat(e.name[:10]))
            except ValueError:
                continue
    return max(dates) if dates else None


def backup_data(data_dir: Path, root: Path, today: date, every_days: int) -> Optional[Path]:
    """到期才備份，回傳這次的備份資料夾；沒到期回傳 None。只複製 data\\ 第一層的 json／csv。"""
    if not root.parent.is_dir():
        raise FileNotFoundError(f"找不到備份位置的上層：{root.parent}（Google Drive 電腦版有開嗎？）")
    last = latest_backup(root)
    if last is not None and (today - last).days < every_days:
        return None
    target = root / today.isoformat()
    for src in sorted(Path(data_dir).iterdir()):
        if src.is_file() and src.suffix.lower() in (".json", ".csv"):
            storage.copy_file(src, target, src.name)  # 撞名自動改名，絕不覆蓋
    logger.info("data 已備份到 %s", target)
    return target


async def run_maintenance(context: ContextTypes.DEFAULT_TYPE) -> None:
    """每 24 小時：整理待清理清單、到期就備份。任何一步失敗都只通知管理員，不影響機器人。"""
    bd = context.application.bot_data
    config, logs, notifier = bd["config"], bd["logs"], bd["notifier"]
    try:
        counts = await asyncio.to_thread(prune_cleanup_list, logs, _drive_root(config))
        if counts and counts[0]:
            await notifier.notify_admin(notify.msg_cleanup_pruned(*counts))
    except Exception as exc:  # noqa: BLE001
        logger.exception("整理待清理清單失敗")
        await notifier.notify_admin(notify.msg_log_write_failure("待清理清單（整理）", str(exc)))
    try:
        await asyncio.to_thread(
            backup_data, bd["data_dir"], backup_root(config), date.today(), getattr(config, "BACKUP_EVERY_DAYS", 7)
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("data 備份失敗")
        await notifier.notify_admin(notify.msg_backup_failed(str(exc)))


# ── /status ─────────────────────────────────────────

def _dir_size_mb(path: Path) -> float:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total / 1024 / 1024


def _week_summary(step_rows: list[dict], now: datetime) -> str:
    since = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    recent = [r for r in step_rows if (r.get("時間") or "") >= since]
    parts = [f"{label} {sum(1 for r in recent if r.get('步驟') == step)}" for step, label in _WEEK_STEPS]
    return "、".join(parts)


def build_status(bot_data: dict, now: Optional[datetime] = None) -> str:
    """組 /status 的內容（同步，會碰 G: 與暫存區，呼叫端放進背景執行緒）。"""
    now = now or datetime.now()
    config, logs, sessions = bot_data["config"], bot_data["logs"], bot_data["sessions"]
    started = bot_data.get("started_at")
    uptime = str(now - started).split(".")[0] if started else "不明"
    ok, err = storage.health_check(_drive_root(config))
    temp_root = Path(config.TEMP_DIR)
    temp_mb = _dir_size_mb(temp_root) if temp_root.exists() else 0.0
    all_sessions = sessions.all_sessions()
    pending = sum(1 for s in all_sessions if s.folder is None and s.received_count > 0)
    uploads = [r for r in logs.upload_log.read_all_rows() if (r.get("結果") or "").startswith("成功")]
    last = uploads[-1] if uploads else None
    index = bot_data.get("folders")
    folders = f"{len(index.folders)} 個" + ("（掃描中）" if index.scanning else "") if index is not None else "沒有"
    cleanup_left = len(logs.cleanup_list.read_all_rows())
    last_backup = latest_backup(backup_root(config))

    lines = [
        "📊 機器人狀態",
        f"已經開了 {uptime}",
        f"G:：{'✅ 正常' if ok else '🔴 ' + (err or '不能用')}",
        f"暫存區：{temp_mb:.1f} MB",
        f"進行中的批次：{len(all_sessions)}（沒選資料夾 {pending}）",
        "最後一次上傳：" + (f"{last['時間']} {last['上傳者']}→{last['資料夾']} {last['張數']} 張" if last else "還沒有"),
        f"資料夾清單：{folders}",
        f"待清理清單：{cleanup_left} 筆",
        f"最近一次備份：{last_backup.isoformat() if last_backup else '還沒有'}",
        f"這 7 天：{_week_summary(logs.step_log.read_all_rows(), now)}",
    ]
    return "\n".join(lines)


async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status：只有管理員能用，其他人完全不回應。"""
    bd = context.application.bot_data
    if update.effective_user.id != bd["config"].ADMIN_ID:
        return
    text = await asyncio.to_thread(build_status, bd)
    await update.effective_message.reply_text(text)
