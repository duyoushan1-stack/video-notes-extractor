# Video Notes Extractor MVP

以瀏覽器提交 YouTube URL 或上傳 MP4，Docker 容器執行 yt-dlp + FFmpeg，擷取場景切換截圖、可用字幕與章節，輸出圖文 Markdown ZIP。

## MP4 上傳與語音轉錄

在「影片來源」選擇「上傳 MP4」，選擇檔案後使用相同的敏感度、最短截圖間隔與截圖張數設定。畫面顯示檔名與大小；按下開始後請保持頁面開啟直到上傳完成，背景處理開始後可重新整理接回任務。

MP4 有音軌時會自動以 Gemini 產生原語言、帶時間戳的逐字稿，並沿用 AI 分章與選用摘要／翻譯。**音訊會傳送至 Gemini，使用你的 API 額度**；即使不勾摘要，仍須設定 `GEMINI_API_KEY`。Google Vids 由 [issue #5](https://github.com/duyoushan1-stack/video-notes-extractor/issues/5) 加入。

音訊轉為單聲道 16 kHz、64 kbps MP3，每 300 秒不重疊切段，以 inline audio 提交；不使用 Files API。逐字稿合併回原影片秒數。任一片段失敗仍保留成功內容，介面、README、摘要與翻譯開頭都標註不完整及缺漏時間；無音軌、無語音或全部轉錄失敗時仍產生圖片筆記，略過文字 AI。`source.json` 記錄 `transcript_source`、`transcript_status` 與警告。時間戳為模型估計，不保證字級或影格級精準。

`complete` 表示所有片段的請求與格式驗證成功，不代表逐字內容已人工核對。真實 `gemini-3.8-flash` 驗收曾省略長靜音之後的重複語句；重要內容應對照原片，詳見 [issue #4 驗收紀錄](docs/implementation/issue-4.md)。

- 接受單一真實 MP4 視訊，上限 **1,500,000,000 bytes**；不僅依副檔名或 MIME 判斷，會用 FFprobe 驗證容器、視訊軌與片長。
- 上傳與處理共用單一名額，忙碌時回 429；接收的實際大小超量回 413，媒體無效回 415，60 秒未收到新資料回 408。
- 成功、失敗或上傳中斷均清除原片與取樣暫存；成功產物保留在 `data/` 供下載，需定期清理。ZIP 不含原片，本機來源只顯示檔名與時間戳，不提供假的線上觀看連結。
- 任務綁定目前瀏覽器的 HttpOnly cookie；重新整理可接續，其他瀏覽器／清除 cookie 後無法查詢舊任務。這是個人服務隔離，仍須維持 localhost 或既有受保護的部署方式。

API：`POST /api/jobs/upload` 使用 multipart，單一 `file` 與 `threshold`、`min_gap`、`max_frames`、`summary`、`summary_language`、`translate` 表單欄位，成功回 202 與 job snapshot。原本 `POST /api/jobs` JSON 回 200 並保持 YouTube 預設行為。API 用戶須保存並在輪詢、預覽、下載時帶回 cookie（例如 curl 的 `-c cookies.txt`／`-b cookies.txt`）。

## 啟動

1. 安裝 Docker Desktop（或部署到有 Docker 的遠端 Linux 主機）。不需在電腦直接安裝 Python、FFmpeg 或 yt-dlp。
2. 在本目錄執行 `docker compose up --build -d`。
3. 瀏覽器開啟 `http://localhost:8000`。
4. 選擇參數，按「開始擷取」，完成後下載 ZIP。
5. 停止：`docker compose down`。

若部署至遠端，請透過 VPN、SSH port forwarding 或有身分驗證的 HTTPS 反向代理存取。compose 預設只綁定 localhost，不能直接對外公開。

## 輸出

- `README.md`：章節、截圖、逐字稿，使用相對圖片路徑
- `screenshots/`：擷取影格
- `frames.json`：影格原始時間戳
- `chapters.json`：原始章節
- `transcript.json`：字幕文字與起始時間
- `source.json`：來源類型、標題、來源網址或上傳檔名、片長、逐字稿來源及完整度、警告；不含原片或本機絕對路徑
- `summary.md`：勾選「產出摘要」時產出（總覽 + 各章重點 + 關鍵術語），語系另外指定，預設繁體中文
- `README.<語系代碼>.md`（如 `README.en.md`）：選擇「翻譯語系」時產出，結構與圖片路徑同 `README.md`

摘要、翻譯語系為下拉選單，API 只接受代碼 `zh-TW`、`zh-CN`、`en`、`ja`、`ko`（翻譯可為空字串表示不翻譯），其他值回 400。新增語系需同時改 `app/gemini.py` 的 `LANGUAGES` 與 `static/index.html` 的選單。

`README.md` 階層為「章節 > 截圖時間 > 內文」。影片沒有 YouTube 章節時，若有設定 Gemini 金鑰，會依逐字稿自動分章；未設定則整支影片歸在單一章節。

## Gemini（選用）

在 compose 同目錄建立 `.env`：

```
GEMINI_API_KEY=你的金鑰
GEMINI_MODEL=gemini-3.8-flash
```

未設定金鑰時，送出摘要或翻譯會被伺服器拒絕（400），其他功能照常。摘要語系與翻譯語系分開設定。
遇到 429/5xx 會自動等待重試 3 次。AI 步驟失敗只會顯示警告，不影響基本筆記；翻譯以章為單位，失敗的章節保留原文並標註。

NotebookLM 沒有公開的個人版 API；直接把 `README.md` 上傳成 NotebookLM 來源即可。

## MVP 限制

- 不公開影片只在伺服器可直接存取時能下載；不支援登入 Cookie 或私人影片授權。
- YouTube 依賴既有字幕，不會自動語音辨識；MP4 自動轉錄，沒有可用逐字稿時仍可產出截圖。
- 畫面變化偵測使用 全片每 2 秒取樣 + Pillow 亮度/清晰度過濾 + 影格差異去重，並非投影片 OCR/語意辨識。短於取樣間隔的投影片仍可能漏抓，長時間存在的網址列等遮擋仍可能誤留。超出張數上限時，會跨全片均勻選取，不會只留下前半段。
- 逐字稿按畫面區間配對；無畫面變化的長段落仍保留於完整逐字稿。
- 任務狀態只存在記憶體，重啟會遺失；下載結果存在 `data/`，需要定期手動清理。
- 單一 worker、最多 1 個同時執行的任務；不適合公用服務。
- MP4 上傳有實際串流大小上限；YouTube 的 yt-dlp 檔案上限仍依格式／下載階段生效，並非完整磁碟配額控制。需要外部儲存與資源限制才能公開部署。
- 未串接 Whisper、資料庫、雲端物件儲存、登入、進度串流、重試、排程或 n8n。
- 請只處理有權存取、保存與使用的影片，並遵守來源平台條款。

## 檢查

安裝開發依賴並執行（Python 3.12）：

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
python -m compileall -q app
```

有 Node.js 時可用 `node --test tests/test_ui.cjs` 檢查前端語法及來源切換的表單欄位狀態。

已有本機 runtime 映像時，可掛載最新程式驗證實際 MP4（以下使用 PowerShell，映像名稱依本機調整）：

```powershell
docker run --rm --network none --mount "type=bind,source=$($PWD.Path)/app,target=/srv/app,readonly" --mount "type=bind,source=$($PWD.Path)/static,target=/srv/static,readonly" --mount "type=bind,source=$($PWD.Path)/tests,target=/srv/tests,readonly" --entrypoint python video-notes-extractor-extractor:latest /srv/tests/smoke_mp4.py
```

smoke script 以臨時伺服器和自動生成的有聲／無聲 MP4 驗證實際 HTTP、FFprobe、FFmpeg、ZIP 與清理，使用假金鑰且不呼叫 Gemini。若檔案受鎖定而清理失敗，任務仍結束並釋放名額，畫面會提醒手動清理 `data/`。

`tests/smoke_transcription.py` 另驗證 310 秒有聲／無聲 MP4，預設只模擬 Gemini 傳輸層，仍執行實際音訊切段、回覆驗證、摘要／翻譯整合與 ZIP 清理：

```powershell
docker run --rm --network none --mount "type=bind,source=$($PWD.Path),target=/srv" --entrypoint python video-notes-extractor-extractor:latest tests/smoke_transcription.py
```

真實 API 驗收需移除 `--network none`、加入 `--env-file .env`，並傳入 `--live --speech /srv/path/to/speech.wav`。請提供短語音 WAV，腳本會在 2／152／302 秒放入語音，檢查三個位置是否都有轉錄；會傳送合成音訊並使用 API 額度。未通過時不可當作真實轉錄完整度已驗證。

自動測試 mock 外部 yt-dlp／FFmpeg／Gemini，驗證串流大小、並行 admission、上傳中斷／逾時清理、來源隔離、圖片 ZIP、無效媒體與 YouTube 相容性。真實 MP4 的 FFmpeg 處理須另用 Docker 驗收；YouTube 真實下載仍可能受平台防機器人限制。

## 本次沿用使用者下載設定

保留 `-f bv*[height<=720]+ba/b[height<=720]/b` 與 `--ignore-errors --retries 5 --sleep-subtitles 3`，未覆寫使用者對字幕 429 的容錯設定。
