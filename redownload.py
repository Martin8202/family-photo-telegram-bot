"""
照片重新下載工具（規格書 §16）。

單一 Python 檔，互動式問答，執行完即結束，不做多指令 CLI。
僅下載，不自動寫入區網硬碟或 OneDrive；不具備任何刪除能力。
"""

from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from pathlib import Path

import config
from logs import DataLogs

try:
    from telegram import Bot
    from telegram.error import TelegramError
except ImportError:
    print("需要先安裝 python-telegram-bot： pip install -r requirements.txt")
    sys.exit(1)


def load_groups(data_dir: Path):
    """
    依「目標資料夾＋上傳者＋日期」分組。同一張照片在「📦 兩邊都存」時，file_index
    會有兩列（家裡硬碟、OneDrive）但 file_id 相同——必須依 file_id 去重，否則
    工具會對同一張照片重複下載兩次、且顯示張數加倍（review 問題二）。
    """
    logs = DataLogs(data_dir)
    rows = logs.file_index.read_all_rows()
    groups = defaultdict(list)
    seen = defaultdict(set)  # 每組已收錄的 file_id，用於去重
    for row in rows:
        key = (row["目標資料夾"], row["上傳者"], row["時間"][:10])
        file_id = row.get("file_id")
        if file_id and file_id in seen[key]:
            continue
        seen[key].add(file_id)
        groups[key].append(row)
    return groups


def print_menu(groups: dict) -> list:
    print("=" * 40)
    print("  照片重新下載工具")
    print("=" * 40)
    items = list(groups.items())
    for i, (key, rows) in enumerate(items, start=1):
        folder, uploader, date = key
        print(f"{i}. {folder}\t({uploader}, {date}, {len(rows)} 張)")
    print()
    return items


def parse_selection(text: str, count: int) -> list[int]:
    text = text.strip().lower()
    if text == "all":
        return list(range(count))
    result = []
    for part in text.split(","):
        part = part.strip()
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < count:
                result.append(idx)
    return result


async def download_rows(bot: Bot, rows: list, dest_dir: Path) -> tuple[list, list]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    succeeded, failed = [], []
    total = len(rows)
    for i, row in enumerate(rows, start=1):
        filename = row["檔名"]
        file_id = row["file_id"]
        try:
            tg_file = await bot.get_file(file_id)
            await tg_file.download_to_drive(custom_path=str(dest_dir / filename))
            succeeded.append(filename)
        except TelegramError:
            failed.append(filename)
        bar_len = 20
        filled = round(i / total * bar_len)
        bar = "▓" * filled + "░" * (bar_len - filled)
        pct = round(i / total * 100)
        print(f"\r下載中… {bar} {pct}% ({i}/{total})", end="", flush=True)
    print()
    return succeeded, failed


async def async_main() -> None:
    data_dir = Path(__file__).parent / "data"
    groups = load_groups(data_dir)
    if not groups:
        print("目前沒有任何照片索引紀錄（data/file_index.csv 是空的）。")
        return

    items = print_menu(groups)
    selection_text = input("請輸入編號（可多選，用逗號分隔），或輸入 all 全部下載：\n> ")
    indices = parse_selection(selection_text, len(items))
    if not indices:
        print("沒有選擇任何項目，結束。")
        return

    dest_input = input(f"下載到哪裡？（直接按 Enter 用預設 {config.REDOWNLOAD_DIR}）\n> ").strip()
    base_dir = Path(dest_input) if dest_input else Path(config.REDOWNLOAD_DIR)

    bot = Bot(token=config.BOT_TOKEN)
    for idx in indices:
        (folder, uploader, date), rows = items[idx]
        target_dir = base_dir / folder
        print(f"\n開始下載「{folder}」（{uploader}, {date}）…")
        succeeded, failed = await download_rows(bot, rows, target_dir)
        print("\n完成！")
        print(f"  ✅ 成功 {len(succeeded)} 張 → {target_dir}\\")
        if failed:
            print(f"  ❌ 失敗 {len(failed)} 張（file_id 已失效，需請上傳者重傳）")
            for name in failed:
                print(f"     {name}")
            fail_list_path = target_dir / "失敗清單.txt"
            fail_list_path.write_text("\n".join(failed), encoding="utf-8")
            print(f"     詳見 {fail_list_path}")


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
