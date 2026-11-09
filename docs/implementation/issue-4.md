# Issue #4：MP4 Gemini 轉錄驗收紀錄

日期：2026-11-09。來源分支：`feat/issue-3-mp4-upload`，起點 `e5db2c0ccc7ee376fec70787b9a387d9e56628c6`。

本次實作 `audio_chunks`、inline MP3 轉錄與嚴格 cue 驗證、原片 offset 合併、逐字稿完整度、分章／摘要／翻譯整合、前端提示及音訊清理。依使用者指示，預設模型改為 `gemini-3.8-flash`，仍允許 `GEMINI_MODEL` 覆寫。

## 自動與 Docker 驗證

- 先執行新轉錄測試，確認缺少轉錄／切段介面而失敗，再實作。
- `python -m unittest discover -s tests -v`：40 tests，通過。
- `node --test tests/test_ui.cjs`：3 tests，通過；包含實際前端提交摘要／翻譯參數及 partial 警告顯示。
- `python -m compileall -q app`：通過。
- `git diff --check`：通過。
- `docker build -t video-notes-extractor-issue4:latest .`：通過。
- 正式映像執行 `tests/smoke_transcription.py`，網路關閉：310 秒有聲 MP4 的兩段回覆合併為 2／302 秒；有聲／無聲各產出 78 張截圖，ZIP 與原片／音訊／取樣暫存清理均通過。只有 Gemini 傳輸層使用模擬回覆，HTTP、FFprobe、FFmpeg、編碼及回覆解析為實際執行。
- 獨立程式碼審查發現後段 FFmpeg 失敗會丟棄先前成功片段。已補兩個測試觀察失敗，再修正為保留可用片段、由 worker 記錄缺漏時間；全套回歸通過。

## 真實 API：未通過完整度驗收

沿用本地 `.env` 的既有 Gemini 設定；未輸出或提交金鑰。以 Windows 本地語音合成製作短英文語句，在 310 秒影片的 2／152／302 秒各放入一次，實際呼叫 `gemini-3.8-flash`。

- 轉錄回覆時間為 1.48、5.76、301.48、305.4 秒，原語句可辨識，跨段 offset 未歸零。
- **模型未回覆 152 秒附近的中段重複語句。** FFmpeg 音量檢查確認該區段有音訊（150–160 秒，max -3.9 dB）。
- 補強提示，要求不可省略重複語句、完整聽取長靜音之後內容及保留原時間軸，再對首段重試，仍僅回覆 1.76 秒一筆；檢查中段 cue 的 assertion 失敗。
- 真實分章與 `summary.md` 已產生；翻譯尚未完成時停止此輪測試，避免在已確認完整度失敗後繼續消耗額度。真實 API 的最終 ZIP、翻譯及全流程清理未驗證；上述網路關閉的 Docker smoke 已驗證相應程式流程。

因此 PR 保留 Draft，**不宣稱 issue #4 的真實 API 驗收已完成**。`complete` 只表示所有片段請求與格式驗證成功，無法偵測模型靜默漏錄；重要內容仍須對照原片。此限制已記錄於 README；不得用 mock 成功取代實際語音完整度證據。

## 實作決策

- 依使用者要求在目前 checkout 建立新分支；既有未追蹤的 `docs/implementation/issue-3.md` 不納入本次提交。
- 維持原介面：`audio_chunks` 只回成功擷取的片段，worker 依影片長度與預期的 300 秒邊界記錄未擷取的時間範圍。
- 不加入另一個模型供應商、Files API 或 YouTube 語音 fallback。真實漏錄問題仍待後續解決與重驗。

## 官方契約核對

- [Gemini 音訊理解](https://ai.google.dev/gemini-api/docs/audio)：inline MP3、音訊理解與轉錄；每段約 2.4 MB，base64 後約 3.2 MB，低於官方 inline 請求大小限制。
- [Gemini 結構化輸出](https://ai.google.dev/gemini-api/docs/structured-output)：JSON 回覆模式仍須由應用程式驗證時間戳與內容型別。
- [既有 2.5 Flash 模型能力](https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash)：核對舊設定支援音訊與 structured outputs 後，依使用者更正改用其現有 3.8 Flash 設定。
