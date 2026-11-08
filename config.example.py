"""
設定範本 —— 複製這個檔案為 config.py 並依這台電腦調整。
config.py 已列在 .gitignore，不會被 git 追蹤。
機器人 token 不寫在這裡，放在程式資料夾外面的 secrets 資料夾（見 BOT_TOKEN_FILE）。
"""

# ── Telegram 設定 ──────────────────────────────
# 機器人 token 檔（內容只有一行 token）。放在 photo-bot 資料夾外，git 不可能把它提交上去。
# 相對路徑以 photo-bot 資料夾為準；開發用測試機器人時改成 test_bot_token.txt。
BOT_TOKEN_FILE = r"..\secrets\bot_token.txt"
ADMIN_ID = 0  # 管理員的 Telegram ID（數字），用 @userinfobot 之類的工具查詢

# ── 路徑設定（日後可自由調整）──────────────────
# Google Drive 電腦版裡的「照片」資料夾：唯一的上傳目的地（V2 D6、D11）
DRIVE_PHOTO_ROOT = r"G:\我的雲端硬碟\!家裡硬碟\照片"
TEMP_DIR = r"D:\photo-bot-temp"              # 暫存區（建議放空間大的磁碟）
REDOWNLOAD_DIR = r"D:\重新下載"               # redownload.py 的預設下載位置

# ── 行為參數 ────────────────────────────────
BATCH_SIZE = 20               # 內部小批張數
SESSION_TIMEOUT_MIN = 10      # session 逾時（分鐘）；亦為「忘記按我傳完了」的保險
ABANDONED_SESSION_MAX_MIN = 60  # 遺棄 session 的絕對存活上限（分鐘）：點了上傳卻停在選資料夾且沒傳照片，超過即靜默清除
STAGE_STUCK_MAX_MIN = 30      # 任何階段卡住不動的兜底逾時（分鐘），見規格書 §6.4
RETRY_TIMES = 3               # 單檔寫入重試次數
RETRY_DELAYS = [1, 3, 5]      # 重試間隔（秒）
WRITE_THROTTLE_SEC = 0.3      # 每張寫入間隔
RETRY_AFTER_BATCH = True      # 本次結束後自動重試失敗檔
RECENT_FOLDERS_COUNT = 5      # 選資料夾畫面列出全家最近用過的幾個資料夾（V2 D19）
FINISH_DEBOUNCE_SEC = 4       # 選好資料夾後，照片停了幾秒就自動開始上傳（V2 D24）
PICKER_QUIET_SEC = 3          # 照片停了幾秒才出現選資料夾畫面（V2 §3.1）

# ── 資料夾清單（data/folders.json，V2 §3.3）──────
FOLDER_SCAN_DEPTH = 2         # 只掃「照片」底下第幾層以內
FOLDER_SCAN_EXCLUDE = []      # 不列入搜尋的資料夾名稱
FOLDER_RESCAN_IDLE_MIN = 10   # 有上傳完成後，全家多久沒動作才在背景重掃

# ── 背景 worker（見規格書 §3.1、§6.3.3）──────────
# 並行度固定、不隨照片數量增加：一次傳 10 張與一次傳 3000 張，對磁碟與網芳的
# 瞬時壓力完全相同。複製到目的地固定單一 worker（網芳最怕並行寫入），不開放調整。
DOWNLOAD_WORKERS = 3          # 同時下載照片的背景 worker 數
DOWNLOAD_RETRY_TIMES = 3      # 從 Telegram 下載單張照片的重試次數

# ── 畫面更新節流：時間與張數雙門檻（見規格書 §6.3.1）──
# 只用時間門檻會導致「一批照片在節流秒數內全部抵達」時，畫面停在第 1 張不動；
# 加上張數門檻，小批量快速傳送時畫面同樣會明顯跳動。
COUNTER_UPDATE_SEC = 5        # 收件計數：時間門檻（秒）
COUNTER_UPDATE_COUNT = 8      # 收件計數：張數門檻（新增滿幾張就更新一次）
# ⚠️ CONFIRM_UPDATE_SEC 必須明顯小於 FINISH_DEBOUNCE_SEC，否則「確認中」的張數
#    根本來不及更新就結案了（v2 兩者都是 5 秒，正是「數字不會跳」的成因）。
CONFIRM_UPDATE_SEC = 2        # 「確認中」張數：時間門檻（秒）
CONFIRM_UPDATE_COUNT = 3      # 「確認中」張數：張數門檻
COUNTER_REANCHOR_SEC = 5      # 「確認中」多久才用一次「刪舊發新」拉回對話底部

# ── 其他 ────────────────────────────────────
CORRECTION_PROMPT_MAX_MIN = 10  # 「這批傳錯了」等待輸入新資料夾的有效時間（分鐘），見規格書 §7
DUPLICATE_PROMPT_MAX_MIN = 10   # 「重複照片還要存嗎」等待回答的時間（分鐘）；逾時視同不存，見 §10.1
LAST_BATCH_MAX_MIN = 720        # 已完成批次的紀錄保留多久（分鐘，預設 12 小時）後回收記憶體，見 §7
                                # 只影響「↩️ 這批傳錯了」能不能自動搬，不影響任何實體檔案
HEALTH_CHECK_ON_START = True    # 啟動時確認 Google Drive 資料夾可用（只看、不寫）
HEALTH_CHECK_ON_SESSION = True  # 每次開 session 確認 Google Drive 資料夾可用
RECOVER_ON_START = True         # 啟動時掃描暫存區並復原未完成批次
USE_EXIF_TIME = True            # 檔名優先採用 EXIF 拍攝時間
