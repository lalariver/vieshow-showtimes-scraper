# IMDb 電影與影集評分分析工具

整合威秀影城上映電影與 Netflix「我的口袋名單」，透過 IMDb 搜尋影片資料，輸出 SQLite、CSV 與 Plotly 互動式 HTML 儀表板。

## 專案結構

```text
vieshow-showtimes-scraper/
├── viewshow_imdb.py             # 威秀資料、IMDb 查詢與共用輸出功能
├── netflix_imdb.py              # Netflix 主流程、快取與資料更新
├── netflix_scraper.py           # Netflix 分類頁與「我的口袋名單」擷取
├── requirements.txt             # Python 第三方套件
├── README.md
├── .gitignore
├── config/                      # 本機設定；Cookie 不上傳 GitHub
│   ├── netflix_cookies.json
│   └── imdb_cookies.json        # 若有使用
├── data/
│   ├── vieshow/                 # 威秀 CSV／SQLite 資料
│   └── netflix/                 # Netflix 節目與電影 CSV／SQLite 資料
├── reports/                     # HTML 儀表板
└── debug/                       # 截圖與除錯輸出，不上傳 GitHub
```

資料夾由程式自動建立；`config/` 內的 Cookie 檔案需在本機準備，不應提交至公開儲存庫。

## 功能

- **威秀上映電影**：從威秀場次 API 取得上映片名，排除 `PRE`、`TBC`、`LIVE`、`SUPPORT` 等項目。
- **Netflix 節目／電影**：選擇分類，擷取「我的口袋名單」輪播中的片名並去除重複項目。
- **IMDb 資料**：搜尋候選片名，優先使用完全吻合的結果，再參考文字相似度；解析評分、投票數、類型、預算及全球票房。
- **人工驗證**：遇到 IMDb 人機驗證時，可在已開啟的瀏覽器中手動完成，再繼續查詢。
- **快取與更新**：Netflix 可使用既有資料庫片名重新查詢 IMDb；部分查詢會合併結果，完整查詢則以本次成功結果更新資料。

## 環境建置

建議使用 Python 3.12 或以上版本。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
playwright install chromium
```

macOS／Linux 使用上述虛擬環境啟用指令；Windows 可使用 `.venv\Scripts\activate`。

## Netflix Cookie 設定

需要重新擷取 Netflix 分類頁時，請將有效的 Cookie JSON 放在：

```text
config/netflix_cookies.json
```

這個檔案可能含有登入工作階段資訊，**不可提交到 GitHub**。只使用自己的帳號與有權存取的內容。若只使用 SQLite 快取片名重新查詢 IMDb，程式不需要開啟 Netflix 分類頁，也不需要讀取 Netflix Cookie。

## 執行方式

請先在終端機切換到專案根目錄，再執行指令，讓相對路徑指向正確的資料夾。

### 威秀上映電影

```bash
python viewshow_imdb.py
```

### Netflix 節目／電影

```bash
python netflix_imdb.py
```

啟動後依終端機提示選擇「節目」或「電影」，再依畫面處理登入、隱私提示或人機驗證。

## 輸出檔案

| 來源 | 資料檔案 | 儀表板 |
|---|---|---|
| 威秀 | `data/vieshow/viewshow_movies_imdb.csv`、`data/vieshow/viewshow_movies_imdb.db` | `reports/viewshow_movies_dashboard.html` |
| Netflix 電影 | `data/netflix/netflix_movies_imdb.csv`、`data/netflix/netflix_movies_imdb.db` | `reports/netflix_movies_dashboard.html` |
| Netflix 節目 | `data/netflix/netflix_series_imdb.csv`、`data/netflix/netflix_series_imdb.db` | `reports/netflix_series_dashboard.html` |

CSV 與 HTML 報表適合挑選後提交到 GitHub 作為作品展示；SQLite 資料庫、Cookie 與除錯產物預設忽略。HTML 儀表板可能包含較大的互動式 JavaScript，提交前請確認檔案大小。

## Git 提交前檢查

```bash
git status --short
```

確認沒有 `config/*cookies*.json`、SQLite 資料庫、虛擬環境或除錯檔案出現在待提交清單。`.gitignore` 不會自動停止追蹤已經提交過的檔案；若 Cookie 曾經提交到公開儲存庫，請先撤銷／更新相關登入工作階段，再處理 Git 歷史紀錄。
