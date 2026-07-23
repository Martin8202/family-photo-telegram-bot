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
RETRY_TIMES = 3               # 單檔重試次數
RETRY_DELAYS = [1, 3, 5]      # 重試間隔（秒）
WRITE_THROTTLE_SEC = 0.3      # 每張寫入間隔
RETRY_AFTER_BATCH = True      # 本次結束後自動重試失敗檔
ONEDRIVE_FREE_SPACE = True    # session 結束後釋放 OneDrive 本機空間
RECENT_FOLDERS_COUNT = 3      # 記憶最近幾個資料夾
FINISH_DEBOUNCE_SEC = 5       # 按「我傳完了」後的結案緩衝秒數
COUNTER_UPDATE_SEC = 5        # 收件計數訊息更新間隔（API 節流）
HEALTH_CHECK_ON_START = True    # 啟動時執行 SMB 健檢
HEALTH_CHECK_ON_SESSION = True  # 每次開 session 執行 SMB 健檢
RECOVER_ON_START = True         # 啟動時掃描暫存區並復原未完成批次
USE_EXIF_TIME = True            # 檔名優先採用 EXIF 拍攝時間
