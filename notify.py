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


def msg_log_write_failure(what: str, error: str) -> str:
    return (
        f"🔴 寫入「{what}」失敗，這次的紀錄沒有存進去\n"
        f"最可能的原因：你正用 Excel 開著這個檔案，Windows 會鎖住它不讓程式寫入。\n"
        f"請把 Excel 關掉，之後的紀錄就會恢復正常。\n"
        f"（照片本身完全不受影響，已經正常存好了；這次漏掉的內容可到 logs/photo-bot.log 查回）\n"
        f"錯誤訊息：{error}"
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


def user_msg_how_to_start() -> str:
    """
    告訴使用者怎麼開始。入口只有 ☰ 選單一個——輸入框下方的常駐按鈕已經移除，
    因為它在使用者打字時會被 Telegram 用戶端收起來，不是可靠的入口（§6.1）。
    """
    return "要傳照片時，點左下角的選單 ☰ 選「📷 我要上傳照片」就可以開始了"


def user_msg_original_quality_tutorial() -> str:
    return (
        "📸 小提醒：手機預設會壓縮照片畫質。\n"
        "想保留原始畫質的話，傳送時請選「以檔案傳送」，"
        "或到 Telegram 設定裡把「照片壓縮」關掉。"
    )


def user_msg_compressed_hint() -> str:
    return "💡 剛剛這張看起來是壓縮過的畫質喔，之後可以試試「以檔案傳送」保留原始畫質。"


def user_msg_not_started() -> str:
    return "請先點左下角的選單 ☰ 選『📷 我要上傳照片』開始喔"


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
    return "已重新開始。要再傳的話，點左下角選單 ☰ 選「📷 我要上傳照片」"


def user_msg_correction_done(count: int, new_folder: str) -> str:
    return f"✅ 已經幫你把這 {count} 張改放到「{new_folder}」了"


def user_msg_batch_expired() -> str:
    """
    「↩️ 這批傳錯了」點下去，但那批的紀錄已經被回收（或程式重啟過）時的說明。

    照片完全沒事，只是程式不再記得「那批是哪些照片」，沒辦法自動搬。
    要講清楚照片沒有不見，否則使用者會嚇到。
    """
    return (
        "這批已經是比較久以前的了，我這邊不再記得它包含哪些照片，沒辦法自動幫你搬。\n"
        "照片都好好的沒有不見喔！需要調整位置的話再跟管理員說一聲。"
    )


def user_msg_correction_processing(new_folder: str) -> str:
    return f"📦 已收到，正在把這批搬到「{new_folder}」，請稍等…"


def user_msg_correction_progress(progress_bar_text: str) -> str:
    return f"📦 搬移中 {progress_bar_text}"


def user_msg_correction_failed(new_folder: str) -> str:
    return f"⚠️ 搬到「{new_folder}」的過程出了狀況，已通知管理員處理"


def user_msg_health_check_failed() -> str:
    return "現在暫時無法上傳，請稍後再試"


def user_msg_no_photos_received() -> str:
    return "尚未收到任何照片喔！請先在手機上選擇照片傳送給我，傳完再按此按鈕。"


def user_msg_inactivity_prompt(received_count: int, stored_count: int) -> str:
    return (
        f"📥 看起來照片傳得差不多囉！目前共收到 {received_count} 張照片"
        f"（已備份好 {stored_count} 張）。\n請問照片都傳完了嗎？"
    )


def user_msg_continue_receiving() -> str:
    return "好的，請繼續傳送照片，傳完後隨時點下方按鈕："


def user_msg_auto_appended(folder: str) -> str:
    """遲到照片自動併案的開場提示（§6.6）。實際張數等收齊後由完成訊息宣告。"""
    return f"💡 收到補傳的照片，會自動幫你存進剛剛的「{folder}」相簿"


def user_msg_upload_ready(folder: str, dest_label: str) -> str:
    return f"✅ 準備好了！資料夾：{folder} ／ 存到：{dest_label}\n請開始傳照片，傳完後點下方按鈕"


def user_msg_folder_exists(folder: str, dest_labels: list) -> str:
    """
    使用者打的資料夾名稱已經存在時的說明。

    讓他清楚知道這次是「加進既有相簿」而不是「開一本新的」——不講的話，
    使用者無從分辨，可能以為打錯字開了重複的相簿，或反過來以為開了新的
    結果混進舊照片裡。
    """
    where = "、".join(dest_labels)
    return f"📁 「{folder}」這個資料夾已經有了（{where}），照片會直接存進去，不會另外開新的"


def user_msg_status(count: int, stored: int = 0, confirming: bool = False) -> str:
    """
    收件階段與確認階段**共用的單一狀態訊息**（規格書 §6.3.1）。

    v3.1 以前分成「📥 收到照片中… X 張」與「⏳ 確認中…（目前共 X 張）」兩則訊息，
    但兩者講的其實是同一件事——「還在收，目前 X 張」——只是使用者按過結束按鈕
    沒有的差別。為此刪一則、發一則，畫面雜訊大於資訊量。合併成一則之後，
    **句尾的狀態標記**就是唯一的差異，也是使用者「我按到了沒」的回饋。

    一併顯示「已存好 N 張」：背景 worker 正同時把照片複製到目的地，讓使用者
    看得到備份進度，緩衝結束後進度條的起跳點才不會顯得突兀（§6.3 設計取捨二）。
    """
    base = f"📥 已收到 {count} 張"
    if stored > 0:
        base += f"（已存好 {stored} 張）"
    if confirming:
        base += "\n⏳ 確認中，稍等一下，我再看看還有沒有照片進來…"
    return base


def user_msg_download_failed_summary(count: int) -> str:
    """下載失敗於本次結束時彙總告知，不逐張打擾使用者（規格書 §8）。"""
    return f"⚠️ 有 {count} 張沒有收到，麻煩再傳一次"


def user_msg_confirming(count: int) -> str:
    """保留給不需要備份張數的呼叫端（例如緩衝期間重複點擊的即時回覆）。"""
    return user_msg_status(count, confirming=True)


def user_msg_uploading(progress_bar_text: str) -> str:
    return f"📤 上傳中 {progress_bar_text}"


def user_msg_done(count: int, folder: str, dest_label: str, skipped_count: int = 0) -> str:
    """
    完成宣告。**在所有決定都做完之後才發出**，故 `count` 已經是最終確定的張數。

    `count` 是這次**實際存進相簿**的張數——必須跟相簿裡真正多出來的張數對得上，
    否則就是在騙人（使用者實測時「傳 15 張但相簿裡其實只有 6 張不同」的困惑
    正是這樣來的）。`skipped_count` 是使用者選擇「不用存」的重複張數，一併交代
    才不會讓他以為漏傳。
    """
    base = f"✅ 完成！{count} 張 → {folder}（{dest_label}）"
    if skipped_count > 0:
        base += f"\n（另有 {skipped_count} 張相簿裡已經有了，依你的選擇沒有重複存）"
    return base


def user_msg_duplicate_ask(count: int, folder: str) -> str:
    return (
        f"💡 這次有 {count} 張，相簿「{folder}」裡已經有一模一樣的了。\n"
        f"還要再存一份嗎？"
    )


def user_msg_duplicate_copying(count: int, folder: str) -> str:
    """按下「還是存一份」的即時回覆。複製可能要跑一陣子，先讓使用者知道有收到。"""
    return f"好的，正在把這 {count} 張也存進「{folder}」…"


def user_msg_duplicate_copied(count: int, folder: str) -> str:
    return f"✅ 好的，這 {count} 張也存進「{folder}」了"


def user_msg_duplicate_skipped(count: int) -> str:
    return f"👍 好的，這 {count} 張就不重複存了（相簿裡原本那份完全沒動）"


def msg_duplicates_for_admin(uploader: str, folder: str, count: int) -> str:
    return (
        f"⚠️ {uploader}「{folder}」本次有 {count} 張與相簿既有照片重複，"
        f"已依零刪除原則另存副本，檔名逐筆記於待清理清單，請確認後手動刪除"
    )


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
