# 家庭照片上傳機器人

讓家人透過 Telegram 對話，用手機把照片、影片、文件上傳到 Google Drive 的指定資料夾（寫進 Google Drive 電腦版的本機資料夾，由它同步上雲）。

核心原則：**簡單、防呆、零刪除、不漏收**。`docs/照片上傳機器人_專案規格書.md` 是 V1 的完整設計說明；V2 的改動摘要見下方「V2 做了什麼」。

## 怎麼用（家人）

1. 直接把照片傳給機器人（從相簿或 LINE 分享過來也可以），不用點選單。
2. 照片停了約 3 秒，出現「收到 N 張！要放進哪個資料夾？」：
   - 點全家最近用過的資料夾（最多 5 個），或
   - 直接打字找（`金門`、`7/2`、`0702`、`7月2日` 都可以），找不到就從結果新建，或
   - 「📂 建在某個資料夾裡」：先選上層，再點裡面原本的資料夾或打新名稱。
3. 選好資料夾、照片停了，就自動開始上傳，完成後會說放進哪裡。放錯了按「↩️ 這批傳錯了」。
4. 要找以前放在哪：☰ 選單「🔍 找照片／檔案」，打資料夾名稱裡的字。

## V2 做了什麼

- 目的地只剩 Google Drive（不再寫 NAS、OneDrive），家人不用選「存哪裡」。
- 單一入口：直接傳就開一批；任何狀況都先收進暫存區（還沒選資料夾、上一批還在存、機器人重開都不漏收）。
- 選資料夾：全家共用最近 5 個、打字搜尋（日期格式統一）、新建最多到「照片」底下第 2 層。
- 可以傳：照片 jpg/png/heic、影片 mp4/mov、文件 pdf/docx/xlsx/pptx/txt；文件保留原檔名。超過 20 MB 的檔案（Telegram 機器人的下載上限）會請家人改用 Drive App。
- 重複判斷：照片看內容指紋、文件看檔名，都再加檔案大小；一樣的會先問要不要再存一份。
- 沒選資料夾的照片：10 分鐘提醒一次，提醒後 10 分鐘沒反應才清掉暫存，並通知家人與管理員。
- 管理員：`/status` 看狀態；每天整理待清理清單（只看檔案在不在，不刪）；`data/` 每 7 天備份進 Drive。

## 安裝

```bash
pip install -r requirements.txt
```

## 設定

1. 複製 `config.example.py` 為 `config.py`。
2. 向 [@BotFather](https://t.me/BotFather) 申請 Bot Token，存成 photo-bot 資料夾**外面**的
   `..\secrets\bot_token.txt`（只有一行 token）。token 不寫進 `config.py`，`config.py` 的
   `BOT_TOKEN_FILE` 指向這個檔；放在程式資料夾外，git 不可能把它提交上去。
3. 填入你的 Telegram ID 到 `ADMIN_ID`（可用 [@userinfobot](https://t.me/userinfobot) 查詢）。
4. 依實際環境調整 `DRIVE_PHOTO_ROOT`（Google Drive 電腦版裡的「照片」資料夾）、`TEMP_DIR`、`BACKUP_DIR` 等。
   `config.py` 少了任何設定項時，程式啟動會直接列出來，請對照 `config.example.py` 補上。

`config.py` 已列在 `.gitignore`，不會被 git 追蹤，可放心填入機密資訊。

## 執行

```bash
python bot.py
```

手動啟動（雙擊或於終端機執行），電腦需保持開機且已登入桌面工作階段
（Google Drive 電腦版要在執行中，`G:` 才會掛上；開機後還沒掛上時，程式會等最多 5 分鐘）。
第一次啟動沒有 `data/folders.json` 時，會在背景掃一次「照片」底下兩層的資料夾，掃完前搜尋找不到東西。

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

單元與流程測試涵蓋不依賴真實 Telegram / Google Drive 的邏輯（檔案操作、刪除圍籬、命名與重複判斷、
選資料夾與搜尋、不漏收、提醒與清除、重開機接回、成員、CSV 紀錄、管理員功能）。
實際情境（真的用手機傳照片、拔網路線、Drive 沒開等）用測試機器人手動走一遍。

## 專案結構

```
photo-bot/
├── bot.py                # 主程式：收發 Telegram 訊息、路由、啟動健檢與暫存區復原、排程
├── redownload.py         # 維運工具：從 Telegram 重新下載照片
├── config.example.py     # 設定範本（假值）
├── config.py             # 真實設定（不進版控）
├── handlers/
│   ├── upload.py         # 上傳流程：收件、不漏收、提醒與清除、寫入、傳錯更正
│   ├── folder_picker.py  # 選資料夾畫面：最近、搜尋、新建、建在某個資料夾裡
│   ├── search.py         # 找照片／檔案（/find）
│   ├── admin.py          # 管理員 /status、待清理清單整理、data 備份
│   └── register.py       # 新成員註冊
├── folder_search.py      # 資料夾清單（data/folders.json）、日期正規化、相近比對
├── storage.py            # 檔案操作（只複製、只建資料夾），只能刪暫存區
├── notify.py             # 給管理員 / 家人的訊息
├── members.py            # 成員清單、全家最近資料夾
├── state.py              # 批次狀態、逾時判斷
├── logs.py               # 四種 CSV 紀錄檔（上傳紀錄、待清理清單、檔案索引、步驟紀錄）
├── writequeue.py         # 併發寫入控制（單一寫入佇列 + 原子寫入）
├── keyboards.py          # Telegram 按鈕集中管理
├── docs/                 # V1 專案規格書
└── data/                 # 執行期資料（不進版控）
```

## 授權

MIT License，詳見 `LICENSE`。
