"""
設定範本 —— 複製這個檔案為 config.py 並填入真實值。
config.py 已列在 .gitignore，不會被 git 追蹤，可放心填入機密資訊。
"""

# ── Telegram 設定 ──────────────────────────────
BOT_TOKEN = "請填入向 @BotFather 申請的 token"
ADMIN_ID = 0  # 管理員的 Telegram ID（數字），用 @userinfobot 之類的工具查詢

# ── 路徑設定（日後可自由調整）──────────────────
ENABLE_NAS = True             # 區網硬碟目的地開關；硬碟故障/尚未設置時可設 False，只留 OneDrive
DEST_NAS = r"\\192.168.1.1\g\照片"          # 區網硬碟
DEST_ONEDRIVE = r"C:\Users\User\OneDrive\照片"  # OneDrive 同步夾
TEMP_DIR = r"D:\photo-bot-temp"              # 暫存區（建議放空間大的磁碟）
REDOWNLOAD_DIR = r"D:\重新下載"               # redownload.py 的預設下載位置

# ── 連線帳密（做法 A 留空，靠 Windows 憑證）──────
NAS_USER = ""
NAS_PASSWORD = ""

# ── 行為參數 ────────────────────────────────
BATCH_SIZE = 20               # 內部小批張數
SESSION_TIMEOUT_MIN = 10      # session 逾時（分鐘）；亦為「忘記按我傳完了」的保險
ABANDONED_SESSION_MAX_MIN = 60  # 遺棄 session 的絕對存活上限（分鐘）：點了上傳卻停在選資料夾/目的地且沒傳照片，超過即靜默清除
STAGE_STUCK_MAX_MIN = 30      # 任何階段卡住不動的兜底逾時（分鐘），見規格書 §6.4
RETRY_TIMES = 3               # 單檔寫入重試次數
RETRY_DELAYS = [1, 3, 5]      # 重試間隔（秒）
WRITE_THROTTLE_SEC = 0.3      # 每張寫入間隔
RETRY_AFTER_BATCH = True      # 本次結束後自動重試失敗檔
ONEDRIVE_FREE_SPACE = True    # session 結束後釋放 OneDrive 本機空間
RECENT_FOLDERS_COUNT = 3      # 記憶最近幾個資料夾
FINISH_DEBOUNCE_SEC = 4       # 靜置自動結案倒數秒數（4 秒自適應倒數）
AUTO_APPEND_WINDOW_MIN = 3    # 遲到照片自動併案窗口（分鐘）

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
INACTIVITY_PROMPT_TIMEOUT_SEC = 25 # 靜置無新照片時主動詢問的沉寂秒數
# ⚠️ CONFIRM_UPDATE_SEC 必須明顯小於 FINISH_DEBOUNCE_SEC，否則「確認中」的張數
#    根本來不及更新就結案了（v2 兩者都是 5 秒，正是「數字不會跳」的成因）。
CONFIRM_UPDATE_SEC = 2        # 「確認中」張數：時間門檻（秒）
CONFIRM_UPDATE_COUNT = 3      # 「確認中」張數：張數門檻
COUNTER_REANCHOR_SEC = 5      # 「確認中」多久才用一次「刪舊發新」拉回對話底部

# ── 其他 ────────────────────────────────────
CORRECTION_PROMPT_MAX_MIN = 10  # 「這批傳錯了」等待輸入新資料夾的有效時間（分鐘），見規格書 §7
HEALTH_CHECK_ON_START = True    # 啟動時執行 SMB 健檢
HEALTH_CHECK_ON_SESSION = True  # 每次開 session 執行 SMB 健檢
RECOVER_ON_START = True         # 啟動時掃描暫存區並復原未完成批次
USE_EXIF_TIME = True            # 檔名優先採用 EXIF 拍攝時間
