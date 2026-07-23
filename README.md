# 家庭照片上傳機器人

讓家人透過 Telegram 對話，用手機把照片上傳到指定資料夾，同時分流存到「區網硬碟」與「OneDrive」。

核心原則：**簡單、防呆、零刪除**。完整設計說明請見上層目錄的
`照片上傳機器人_專案規格書.md`；驗收測試清單見 `照片上傳機器人_測試計畫.md`。

## 功能特色

- 按鈕引導的家人友善流程，不需要知道背後任何技術細節。
- 對目的地（區網硬碟／OneDrive）只複製、只建資料夾，**程式碼完全不含刪除或移動指令**。
- 分批上傳、進度條回報、SMB 網芳寫入重試、程式中斷後自動復原。
- 逐張記錄 Telegram `file_id`，可用 `redownload.py` 從 Telegram 重新取回照片。

## 安裝

```bash
pip install -r requirements.txt
```

## 設定

1. 複製 `config.example.py` 為 `config.py`。
2. 向 [@BotFather](https://t.me/BotFather) 申請 Bot Token，填入 `config.py` 的 `BOT_TOKEN`。
3. 填入你的 Telegram ID 到 `ADMIN_ID`（可用 [@userinfobot](https://t.me/userinfobot) 查詢）。
4. 依實際環境調整 `DEST_NAS`、`DEST_ONEDRIVE`、`TEMP_DIR` 等路徑。

`config.py` 已列在 `.gitignore`，不會被 git 追蹤，可放心填入機密資訊。

## 執行

```bash
python bot.py
```

手動啟動（雙擊或於終端機執行），電腦需保持開機且已登入桌面工作階段
（UNC 網芳憑證與 OneDrive 同步都需要登入態）。

## 重新下載工具

```bash
python redownload.py
```

互動式問答，從 Telegram 依 `file_id` 重新取回照片，僅下載、不寫回任何目的地。

## 開發 / 測試

```bash
pip install -r requirements-dev.txt
pytest
```

單元測試涵蓋不依賴真實 Telegram / 網芳 / OneDrive 的核心邏輯（檔案操作、
刪除圍籬、命名規則、成員狀態機、CSV 紀錄、session 狀態機）。實際情境測試
（真的傳照片、拔網路線等）請依 `照片上傳機器人_測試計畫.md` 手動執行。

## 專案結構

```
photo-bot/
├── bot.py              # 主程式：收發 Telegram 訊息、路由到各 handler
├── redownload.py        # 維運工具：從 Telegram 重新下載照片
├── config.example.py    # 設定範本（假值）
├── config.py             # 真實設定（不進版控）
├── handlers/
│   ├── upload.py         # 上傳照片流程
│   └── register.py       # 新成員註冊
├── storage.py            # 檔案操作（只複製、只建夾），集中管理
├── notify.py              # 通知管理員 / 使用者的訊息模板
├── members.py             # 成員清單讀寫邏輯
├── state.py               # 使用者狀態、近 3 次資料夾、逾時判斷
├── logs.py                 # 三種 CSV 紀錄檔的寫入邏輯
├── writequeue.py           # 併發寫入控制（單一寫入佇列 + 原子寫入）
├── keyboards.py             # Telegram 按鈕集中管理
└── data/                    # 執行期資料（不進版控）
```

## 授權

MIT License，詳見 `LICENSE`。
