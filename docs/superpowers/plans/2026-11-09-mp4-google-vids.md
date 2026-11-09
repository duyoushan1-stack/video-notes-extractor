# MP4 與 Google Vids Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 上傳 MP4 或貼 Google Vids 連結，產生與 YouTube 相同格式的逐字稿、圖片、摘要與翻譯筆記。

**Architecture:** 沿用現有 FastAPI 單一背景任務、FFmpeg／Pillow、Gemini 與 Markdown renderer。取得並驗證本地影片後，共用後續處理；Google OAuth 僅為 Vids 來源服務，token 留在記憶體。

**Tech Stack:** Python 3.12 Docker、FastAPI、Pydantic、FFmpeg／FFprobe、Pillow、Gemini REST、google-auth-oauthlib／google-auth、unittest；API 測試可新增 httpx 為開發依賴。

**Spec:** `docs/superpowers/specs/2026-11-09-mp4-google-vids-design.md`；GitHub 規格 issue 為公開交付的同內容版本。

**Issues:** [父規格 #2](https://github.com/duyoushan1-stack/video-notes-extractor/issues/2)；Task 1 = [#3](https://github.com/duyoushan1-stack/video-notes-extractor/issues/3)，Task 2 = [#4](https://github.com/duyoushan1-stack/video-notes-extractor/issues/4)，Task 3 = [#5](https://github.com/duyoushan1-stack/video-notes-extractor/issues/5)。

## Global Constraints

- 單檔上限 **1,500,000,000 bytes**；multipart 請求總量另以單檔上限加 1 MiB 封頂；只允許一個檔案。
- 在讀取 multipart 大型 body 前，以同一把 lock 預留唯一任務／上傳名額；閒置 60 秒無新資料中止上傳。
- 單聲道 16 kHz、64 kbps MP3；**300 秒**不重疊切段。
- Gemini 暫時錯誤重試至多 3 次，等待 15／30／45 秒；每次呼叫 timeout 600 秒。
- Vids 匯出輪詢每 5 秒一次，最多 30 分鐘。
- OAuth state 一次性且 10 分鐘過期，使用 PKCE；token 僅留記憶體並綁 owner。
- 原 JSON 端點省略 source 仍代表 youtube；不新增 YouTube 語音轉錄 fallback。
- 不新增資料庫、前端框架、通用外掛層或第二套任務系統。

## Review Focus

1. multipart 在路由函數前已 spool：Task 1 用串流和同時兩請求測試證明 admission／大小限制早於無限制寫盤。
2. 第二段時間戳重置或 hallucination：Task 2 驗證 offset、有限數值與越界失敗，不默默匯入錯誤 cue。
3. Google 連線或 callback 被另一瀏覽器使用：Task 1 建 owner 邊界，Task 3 測 state 重放與跨 owner 使用。
4. Vids 的官方下載 URI／redirect 洩漏 Bearer：Task 3 核對官方契約並測未知主機／跳轉不攜帶 token。
5. partial transcript 被摘要呈現成完整內容：Task 2 測 UI、README、summary、翻譯都標示不完整；全部失敗則不呼叫文字 AI。

## 檔案責任

| 檔案 | 責任 |
|---|---|
| `app/main.py` | 既有任務、API、admission、owner 隔離、共用處理整合 |
| `app/media.py`（新增） | FFprobe 驗證及 FFmpeg 音訊分段；不引入來源抽象階層 |
| `app/gemini.py` | 延用 client，新增音訊轉錄與回覆驗證 |
| `app/google_vids.py`（新增） | OAuth 與 Vids metadata／匯出／下載 |
| `app/notes.py` | 來源正確的連結及逐字稿完整度呈現 |
| `static/index.html` | 來源選擇、上傳、Google 連線、轉錄狀態與警告 |
| `tests/test_upload.py`, `tests/test_transcription.py`, `tests/test_google_vids.py`（新增） | 各 slice 的行為測試 |
| `tests/test_notes.py` | renderer 既有與新增來源 regression |
| `requirements.txt`, `requirements-dev.txt`（視需要新增） | OAuth runtime 與 API 測試依賴分開 |
| `README.md`, `compose.yaml`, `.env.example`（新增） | 設定、啟動、限制、隱私與手動驗收說明 |

## Task 1：可上傳 MP4 並取得圖片筆記 ZIP

Blocked by：無。交付包含 UI／API／worker／輸出與文件的最小完整路徑；完整語音功能待 Task 2。

**Interfaces**

- `app/media.py`: `probe_video(path: Path) -> dict`，產出 `duration: float`、`has_audio: bool`，無效 MP4 拋 ValueError。
- 共用 worker 接收固定 job 目錄下的本地影片、title、source、source_url、既有 options，不能依賴 yt-dlp 特有 info 檔。
- `POST /api/jobs/upload` 接受規格 multipart，回 202 job；既有 JSON 回 200 job。
- `render_notes` 保留舊呼叫相容，新增可選 source／transcript_status 資訊；MP4 不合成觀看 URL。
- admission 與 owner 共用同一任務狀態；成功、錯誤和斷線均有唯一明確的 release 路徑。

- [x] 撰寫 failing tests：有效 MP4 POST=202、超過測試用小上限=413、偽裝媒體=415、第二請求=429、其他 owner GET=404、上傳中斷後下一請求能接受；確認 temp 清理、ZIP 相對圖片與 source.json 沒有絕對路徑。
- [x] 執行 `python -m unittest discover -s tests -p test_upload.py -v`，確認失敗原因是缺少新行為。
- [x] 實作 early admission／request limit、單檔串流及 FFprobe 驗證；以現有 worker 提取最少共用步驟，不先大幅重構畫面演算法。
- [x] 新增來源 UI 與 upload fetch／狀態；套用 owner cookie、更新 Markdown 來源呈現，YouTube 預設輸入與行為保持可用。
- [x] README 說明此 slice 的暫時圖片筆記能力與待加入轉錄；清理失敗及成功的原片、音訊與 samples，產物仍保留。
- [x] 執行 `python -m unittest discover -s tests -v` 和 `python -m compileall -q app`；新增原 YouTube JSON→ZIP mock smoke test，確認所有測試通過。
- [x] Commit：`feat: support MP4 upload and shared image notes pipeline`。

## Task 2：MP4 帶時間戳轉錄、摘要與翻譯

Blocked by：Task 1。

**Interfaces**

- `app/media.py`: `audio_chunks(video: Path, folder: Path) -> list[tuple[Path, float, float]]`，每筆為 MP3 路徑、原片 offset、該段 duration。
- `app/gemini.py`: `transcribe_audio(path: Path, duration: float) -> list[dict]`，回片段相對 `start`／`text`；驗證或呼叫失敗則拋可安全呈現的錯誤。
- worker 合併 offset，寫 transcript_source／transcript_status／warnings 並串接既有分章、摘要與翻譯。
- 沿用 `generate` 的 HTTP 與 retry 程式，可增加 audio parts 參數，但不破壞原文字 caller。

- [ ] 撰寫 failing tests：兩段 cue 相對 start=2 及 3 合併成 2／303 秒；最後短片段 start 等於 duration 拒絕；NaN、錯誤 JSON、非 STOP、429 重試及不可重試錯誤行為。
- [ ] 撰寫 failing worker tests：第二段失敗仍有第一段＋partial 警告；全部失敗與無音軌不呼叫文字 AI；summary 與翻譯檔都顯示缺漏警告。
- [ ] 執行 `python -m unittest discover -s tests -p test_transcription.py -v`，確認新行為測試失敗。
- [ ] 按官方 Gemini 文件核對既有模型的 inline MP3／JSON 支援，以 FFmpeg 分段與既有 HTTP client 實作；不新增模型供應商或 Files API。
- [ ] 串接 API key 檢查、transcribing 進度、缺漏範圍、source.json、完成警告與中文音訊外送說明；YouTube 無字幕維持原行為。
- [ ] 跑全套 unittest、compileall；Docker 手動用超過 300 秒有聲 MP4 與無聲 MP4 檢查 ZIP 和時間戳，分開記錄 mock／真實 API 證據。
- [ ] Commit：`feat: transcribe MP4 audio with Gemini and reuse AI notes outputs`。

## Task 3：Google OAuth 與 Vids 到完整筆記

Blocked by：Task 2。

**Interfaces**

- `app/google_vids.py`: `parse_vids_url(url: str) -> str`，僅回合法 Vids ID。
- `download_vids(file_id: str, credentials, destination: Path) -> dict`：在規格 timeout／大小限制內取得 MP4，回 title／source_url；認證不得放入 metadata。
- OAuth connect／callback／status／disconnect 路由按 spec 實作；憑證與 pending state 在記憶體中以 browser owner 索引。
- JSON `POST /api/jobs` 的 source=google_vids 使用該 owner 憑證，檔案取得後串接 Task 1／2 的驗證與處理。

- [ ] 撰寫 failing tests：Vids URL allowlist、無連線=401、未設定=503、state 不符／過期／重放、跨 owner callback、refresh 失敗清除連線、進行中 disconnect=409。
- [ ] 撰寫 failing Drive tests：非 Vids／禁止下載拒絕；pending→done 只啟動一次；403／404 不洩漏原始回覆；30 分鐘逾時；串流超量清理；未知下載主機／redirect 不收到 token。
- [ ] 執行 `python -m unittest discover -s tests -p test_google_vids.py -v`，確認缺少新能力導致失敗。
- [ ] 新增成熟 Google OAuth 依賴，實作 state／PKCE／readonly scope／記憶體 token／撤銷與固定 callback；按 Drive 官方文件核對 download URI 和 bearer 使用方式。
- [ ] 實作 metadata、files.download、operation 輪詢與串流下載；整合中文 UI、來源欄位及既有任務進度，不另建 Google 專用 worker。
- [ ] 更新 compose、.env.example 和 README：Google Cloud 啟用 Drive API、OAuth consent／測試帳號、redirect URI、scope、重啟需重連、工作區限制及手動 MP4 替代路徑。範例只放 placeholder。
- [ ] 全套 unittest、compileall、`git diff --check`、`docker compose build`；用可存取的 Vids 與無權限帳號各手動驗收，未取得可用憑證時明確記錄仍未驗證。
- [ ] Commit：`feat: import Google Vids through OAuth and shared video processing`。

## 完成條件與交接

每個 task 建一個 GitHub issue，包含依賴、驗收與相關規格連結；另建父規格 issue 彙整。任一 issue 未驗證時不得把整體功能標示完成。

本輪只寫規格、計畫及發布 issues，不修改產品程式。實作時先檢查 main 最新狀態、建立功能分支，依 issue 順序進行；真實 Google OAuth／Gemini／Vids 驗收需另外取得部署設定與有權限的測試來源。
