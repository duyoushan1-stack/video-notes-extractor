# Video Notes Extractor MVP

以瀏覽器提交 YouTube URL，Docker 容器執行 yt-dlp + FFmpeg，擷取場景切換截圖、可用字幕與章節，輸出圖文 Markdown ZIP。

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

## MVP 限制

- 不公開影片只在伺服器可直接存取時能下載；不支援登入 Cookie 或私人影片授權。
- 依賴 YouTube 提供的字幕，不會自動語音辨識；無字幕時仍可產出截圖。
- 畫面變化偵測使用 全片每 2 秒取樣 + Pillow 亮度/清晰度過濾 + 影格差異去重，並非投影片 OCR/語意辨識。短於取樣間隔的投影片仍可能漏抓，長時間存在的網址列等遮擋仍可能誤留。超出張數上限時，會跨全片均勻選取，不會只留下前半段。
- 逐字稿按畫面區間配對；無畫面變化的長段落仍保留於完整逐字稿。
- 任務狀態只存在記憶體，重啟會遺失；下載結果存在 `data/`，需要定期手動清理。
- 單一 worker、最多 1 個同時執行的任務；不適合公用服務。
- 影片檔案上限設定為 1.5GB，但 yt-dlp 依格式/下載階段的限制並非完整磁碟配額控制。需要外部儲存與資源限制才能公開部署。
- 未串接 AI 摘要、Whisper、資料庫、雲端物件儲存、登入、進度串流、重試、排程或 n8n。
- 請只處理有權存取、保存與使用的影片，並遵守來源平台條款。

## 檢查

`python -m compileall app`

此版本已通過 Python 語法編譯檢查；未在本環境對 YouTube 影片做端到端測試。遠端主機可能因 YouTube 防機器人限制而無法下載。

## 本次沿用使用者下載設定

保留 `-f bv*[height<=720]+ba/b[height<=720]/b` 與 `--ignore-errors --retries 5 --sleep-subtitles 3`，未覆寫使用者對字幕 429 的容錯設定。
