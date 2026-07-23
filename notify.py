"""
通知管理員（規格書 §9）。所有訊息文字集中於此，方便日後調整措辭。
只給「資料夾 + 張數」，不列出檔名。
"""

from __future__ import annotations

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


def user_msg_health_check_failed() -> str:
    return "現在暫時無法上傳，請稍後再試"


def user_msg_upload_ready(folder: str, dest_label: str) -> str:
    return f"✅ 準備好了！資料夾：{folder} ／ 存到：{dest_label}\n請開始傳照片，傳完後點下方按鈕"


def user_msg_receiving(count: int) -> str:
    return f"📥 收到照片中… {count} 張"


def user_msg_confirming() -> str:
    return "⏳ 確認中…"


def user_msg_uploading(progress_bar_text: str) -> str:
    return f"📤 上傳中 {progress_bar_text}"


def user_msg_done(count: int, folder: str, dest_label: str) -> str:
    return f"✅ 完成！{count} 張 → {folder}（{dest_label}）"


def user_msg_onedrive_cloud_note() -> str:
    return "☁️ 雲端同步需要一點時間，稍後再到 OneDrive 查看"


class Notifier:
    """實際發送 Telegram 訊息的薄封裝（依賴 telegram.Bot，交由呼叫端注入）。"""

    def __init__(self, bot, admin_id: int):
        self._bot = bot
        self._admin_id = admin_id

    async def notify_admin(self, text: str, reply_markup=None) -> None:
        await self._bot.send_message(chat_id=self._admin_id, text=text, reply_markup=reply_markup)

    async def notify_user(self, telegram_id: int, text: str, reply_markup=None) -> None:
        await self._bot.send_message(chat_id=telegram_id, text=text, reply_markup=reply_markup)
