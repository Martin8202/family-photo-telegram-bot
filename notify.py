"""
通知管理員（規格書 §9）。所有訊息文字集中於此，方便日後調整措辭。
只給「資料夾 + 張數」，不列出檔名。
"""

from __future__ import annotations

import logging
from typing import Optional


def msg_upload_success(uploader: str, folder: str, count: int, dest_label: str) -> str:
    return f"✅ {uploader} 上傳成功：{folder}／{dest_label}／{count} 張"


def msg_correction(uploader: str, from_folder: str, to_folder: str, remain_count: int) -> str:
    return (
        f"⚠️ {uploader} 傳錯更正：從「{from_folder}」改到「{to_folder}」，"
        f"「{from_folder}」裡有 {remain_count} 張需要你手動清理，詳見待清理清單"
    )


def msg_restart_residue(uploader: str, folder: str, count: int) -> str:
    return f"🔄 {uploader} 中途重新開始，「{folder}」已寫入 {count} 張為中止殘留，詳見待清理清單"


def msg_write_failure(uploader: str, folder: str, dest_label: str, error: str) -> str:
    return f"🔴 寫入失敗：{uploader}／{folder}／{dest_label}\n原因：{error}\n已保留於暫存區，將自動重試"


def msg_both_partial_failure(uploader: str, folder: str, nas_status: str, onedrive_status: str) -> str:
    return (
        f"⚠️ {uploader}「{folder}」兩邊都存，部分失敗：\n"
        f"家裡硬碟 {nas_status}／OneDrive {onedrive_status}"
    )


def msg_new_registration(name: str, telegram_id: int) -> str:
    return f"🆕 新成員註冊：{name}（ID: {telegram_id}），請審核"


def msg_unapproved_contact(name: str, telegram_id: int) -> str:
    return f"🔔 已登記未開通者來訊：{name}（ID: {telegram_id}）"


def msg_health_check_failed(dest_label: str, error: str) -> str:
    return f"🔴 健檢失敗：{dest_label}\n原因：{error}"


def msg_download_failure(uploader: str, folder: str, count: int, error: str) -> str:
    return (
        f"🔴 從 Telegram 下載照片失敗：{uploader}／{folder}／{count} 張\n"
        f"原因：{error}\n"
        f"已記入 file_index.csv（保留 file_id），可用 redownload.py 事後補救"
    )


def msg_unhandled_error(where: str, error_type: str, error: str) -> str:
    return f"🔴 未預期的程式例外（{where}）\n{error_type}: {error}"


def msg_recovery_report(lines: list[str]) -> str:
    body = "\n".join(lines) if lines else "（無未完成批次）"
    return f"🔁 程式重啟復原報告：\n{body}"


# ── 使用者端訊息（簡單、不揭露背後細節） ────────────

def user_msg_welcome() -> str:
    return "👋 歡迎！第一次使用要先登記一下\n請點下方按鈕開始"


def user_msg_ask_name() -> str:
    return "請告訴我怎麼稱呼你？（例如：媽媽、大姊）\n直接打字告訴我就好"


def user_msg_pending_review() -> str:
    return "已收到登記，等管理員開通後就能使用"


def user_msg_approved() -> str:
    return "🎉 已開通，可以開始用了！"


def user_msg_original_quality_tutorial() -> str:
    return (
        "📸 小提醒：手機預設會壓縮照片畫質。\n"
        "想保留原始畫質的話，傳送時請選「以檔案傳送」，"
        "或到 Telegram 設定裡把「照片壓縮」關掉。"
    )


def user_msg_compressed_hint() -> str:
    return "💡 剛剛這張看起來是壓縮過的畫質喔，之後可以試試「以檔案傳送」保留原始畫質。"


def user_msg_not_started() -> str:
    return "請點下方的『📷 我要上傳照片』開始喔"


def user_msg_choose_folder_first() -> str:
    return "請先選資料夾"


def user_msg_unsupported_media() -> str:
    return "目前只支援傳照片喔，這個檔案類型還不支援，請見諒"


def user_msg_error_generic() -> str:
    return "出了點狀況，已通知管理員"


def user_msg_partial_pending() -> str:
    return "有一部分還在處理中，已通知管理員"


def user_msg_restart_confirm(count: int) -> str:
    return f"⚠️ 確定要重新開始嗎？\n目前已收到的 {count} 張照片將不會繼續上傳，需要重新傳送。"


def user_msg_restart_done() -> str:
    return "已重新開始，請重新選擇資料夾"


def user_msg_correction_done(count: int, new_folder: str) -> str:
    return f"✅ 已經幫你把這 {count} 張改放到「{new_folder}」了"


def user_msg_correction_processing(new_folder: str) -> str:
    return f"📦 已收到，正在把這批搬到「{new_folder}」，請稍等…"


def user_msg_health_check_failed() -> str:
    return "現在暫時無法上傳，請稍後再試"


def user_msg_upload_ready(folder: str, dest_label: str) -> str:
    return f"✅ 準備好了！資料夾：{folder} ／ 存到：{dest_label}\n請開始傳照片，傳完後點下方按鈕"


def user_msg_receiving(count: int, stored: int = 0) -> str:
    """
    收件計數。背景 worker 正同時把已收到的照片複製到目的地，故一併顯示「已存好
    N 張」，讓使用者看得到備份進度，緩衝結束後進度條的起跳點才不會顯得突兀
    （規格書 §6.3.1、§6.3 設計取捨二）。
    """
    if stored > 0:
        return f"📥 收到照片中… {count} 張（已存好 {stored} 張）"
    return f"📥 收到照片中… {count} 張"


def user_msg_download_failed_summary(count: int) -> str:
    """下載失敗於本次結束時彙總告知，不逐張打擾使用者（規格書 §8）。"""
    return f"⚠️ 有 {count} 張沒有收到，麻煩再傳一次"


def user_msg_confirming(count: int) -> str:
    return f"⏳ 確認中…（目前共 {count} 張，稍等一下確認沒有漏收）"


def user_msg_uploading(progress_bar_text: str) -> str:
    return f"📤 上傳中 {progress_bar_text}"


def user_msg_done(count: int, folder: str, dest_label: str) -> str:
    return f"✅ 完成！{count} 張 → {folder}（{dest_label}）"


def user_msg_onedrive_cloud_note() -> str:
    return "☁️ 雲端同步需要一點時間，稍後再到 OneDrive 查看"


class Notifier:
    """
    實際發送 Telegram 訊息的薄封裝（依賴 telegram.Bot，交由呼叫端注入）。

    通知失敗（例如管理員／使用者尚未對 bot 按過 START，Telegram 回
    "Chat not found"）絕不可讓呼叫端的核心流程（健檢、上傳、復原…）
    整個崩潰，因此這裡一律吞下例外並記錄 log，而非往外拋。
    """

    def __init__(self, bot, admin_id: int):
        self._bot = bot
        self._admin_id = admin_id
        self._logger = logging.getLogger("photo-bot.notify")

    async def _send(self, chat_id: int, text: str, reply_markup=None) -> bool:
        try:
            await self._bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
            return True
        except Exception as exc:  # noqa: BLE001 - 通知失敗不可中斷主流程
            self._logger.error("發送 Telegram 訊息失敗（chat_id=%s）：%s", chat_id, exc)
            return False

    async def notify_admin(self, text: str, reply_markup=None) -> bool:
        return await self._send(self._admin_id, text, reply_markup)

    async def notify_user(self, telegram_id: int, text: str, reply_markup=None) -> bool:
        return await self._send(telegram_id, text, reply_markup)
