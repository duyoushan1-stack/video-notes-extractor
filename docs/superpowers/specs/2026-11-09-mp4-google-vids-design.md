# MP4 與 Google Vids 影片筆記規格

日期：2026-11-09。狀態：規格與 issue 規劃；產品程式尚未實作。

審閱：已完成獨立 builder check，結果為無重大待釐清問題；實作計畫已按需求覆蓋、介面一致性及錯誤情境完成自我檢查。

GitHub：[父規格 #2](https://github.com/duyoushan1-stack/video-notes-extractor/issues/2)；實作順序為 [MP4 圖片筆記 #3](https://github.com/duyoushan1-stack/video-notes-extractor/issues/3) → [Gemini 轉錄 #4](https://github.com/duyoushan1-stack/video-notes-extractor/issues/4) → [Google Vids #5](https://github.com/duyoushan1-stack/video-notes-extractor/issues/5)。

## 需求與已選方案

使用者要在現有 YouTube 功能之外，上傳 MP4 或貼上 Google Vids 連結，取得帶時間戳的逐字稿、截圖、章節與選用摘要／翻譯，下載相同格式的 Markdown ZIP。

使用者已選 Gemini 語音轉錄與 Google OAuth 授權方案。以下大小、分段、逾時、錯誤處理與憑證生命週期是本次規劃的工程預設，不代表已經過真實影片驗證。

## 現況與限制

- FastAPI 單一程序，以記憶體 JOBS 和 LOCK 限制最多一個任務，背景 thread 處理，前端輪詢並在重新整理後接回任務。
- YouTube 使用 yt-dlp 取得影片、字幕與章節。目前只有字幕下載，沒有語音辨識；缺字幕時仍輸出圖片筆記。
- FFmpeg 每 2 秒取樣，Pillow 過濾、去重與限制截圖數；render_notes 依章節組合圖片與字幕。
- Gemini 已提供文字分章、摘要、翻譯及重試；沿用 GEMINI_API_KEY 與 GEMINI_MODEL。
- 個人使用、localhost 部署；本案不新增公用多使用者平台、資料庫、持久佇列或前端框架。

## Approaches

1. **採用：共用處理流程，Gemini 轉錄，Google OAuth + Drive API 取得 Vids MP4。** 沿用既有 API、畫面分析與輸出；新增最少的來源取得和音訊處理邏輯，音訊送往 Gemini 並使用其額度。
2. 本機 Whisper 轉錄。免轉錄 API，但需新增模型與運算資源；使用者未選。
3. 先手動把 Vids 匯出 MP4 再上傳。可做權限失敗時的替代操作，但不能取代使用者要求的直接貼 Vids 連結。

Ponytail 原則：來源取得後直接進共用流程，不建立來源外掛系統、通用媒體框架或第二套任務系統。新增依賴僅限 OAuth 所需的成熟 Google 授權函式庫；音訊、HTTP、上傳處理沿用 FFmpeg、標準函式庫與 FastAPI 既有能力。

## 使用者流程

1. 選擇「YouTube 網址」「上傳 MP4」「Google Vids 網址」，只顯示所選來源的必要輸入。
2. MP4 選檔後顯示檔名與大小。Vids 顯示 Google 連線狀態，未連線時提供授權按鈕。
3. 三種來源共用敏感度、最短截圖間隔、最大截圖數、摘要勾選、摘要語系及翻譯語系。
4. MP4／Vids 有音軌時自動語音轉錄；畫面明示音訊會傳送至 Gemini。逐字稿保留原語言；摘要與翻譯語系延用既有選項。
5. 顯示上傳／下載或匯出、語音轉錄、擷取畫面、分章、摘要與翻譯、完成／失敗等中文狀態。上傳百分比只在瀏覽器確有傳輸進度時顯示，不能假裝伺服器已完成處理。
6. 完成可下載 ZIP；部分轉錄失敗或其他選用 AI 功能失敗時，以警告指出缺漏，仍能下載圖片與已成功產生的內容。

## 功能需求與驗收

| ID | 需求 | 可觀察的驗收結果 |
|---|---|---|
| FR-01 | 上傳一個 MP4 | 有效有聲 MP4 可得到非空逐字稿、相對路徑截圖、章節、勾選時的摘要與選用翻譯 |
| FR-02 | 直接使用 Vids 連結 | 連接有權限的 Google 帳號後，貼 `/videos/d/{id}/...` 連結即可建立背景匯出／處理任務 |
| FR-03 | 語音與時間對齊 | 逐字稿每段含原片秒數 start 與 text；跨音訊分段仍按時間排序，對齊原片截圖 |
| FR-04 | 共用圖片與 AI 功能 | 三來源適用相同截圖參數、分章、摘要語系、翻譯選項與 ZIP 結構 |
| FR-05 | 保留 YouTube 相容性 | 原 JSON 建任務、字幕優先、缺字幕可輸出圖片、狀態／預覽／下載端點保持可用 |
| FR-06 | 容錯 | 無音軌、無語音、轉錄部分／全部失敗、摘要失敗均有明確狀態／警告，不偽稱完整逐字稿 |
| FR-07 | 資源與輸入邊界 | 無效檔案、超量、忙碌、未授權可預期拒絕；失敗後下一任務可正常執行 |
| FR-08 | OAuth 與來源連結 | 不能偽造授權 callback；憑證不出現在瀏覽器、JOBS、ZIP、log 或 repository；本機 MP4 不產生假的 YouTube 觀看網址 |

## API 契約

- 保留 `POST /api/jobs` JSON。`source` 新增為 `youtube | google_vids`，省略仍代表 youtube；url 和所有既有選項維持名稱與預設值。
- 新增 `POST /api/jobs/upload` multipart：單一 `file`，及同名的 threshold、min_gap、max_frames、summary、summary_language、translate 表單欄位。成功回 202 與 job snapshot；既有 JSON 端點成功碼維持目前 200。
- MP4／Vids 建任務前必須有 GEMINI_API_KEY，否則 400；即使不勾摘要仍需金鑰以履行自動轉錄。YouTube 原本不需金鑰的流程保留。
- 所有來源共用參數與語系驗證；無效來源／網址／檔案為 400，不支援媒體內容為 415，超過大小為 413，忙碌為 429。Pydantic 結構／型別錯誤可維持 422。
- Vids 未連線為 401，OAuth 未設定為 503；已接受的背景任務若遇 Drive 403／404，轉 failed 並顯示「無法存取影片或沒有下載權限」，不冒充不存在的確定結論。
- snapshot 保留既有欄位；新增 `source`、`title`、`transcript_source`（youtube_captions / gemini / none）、`transcript_status`（complete / partial / unavailable）、`warnings`。新欄位從 queued 起給合理預設；不得包含路徑或 token。
- `/api/jobs/active`、`/{jid}`、`/{jid}/preview`、`/{jid}/download` 及重新整理接回行為延用；active 只回本次瀏覽器擁有的任務。
- 用不透明 HttpOnly、SameSite=Lax cookie 綁定 browser owner。所有新建來源的任務與 Google 連線綁定該 owner，查詢、active、預覽與下載對其他 owner 回 404。這是個人服務的來源隔離，不是公用登入功能。

## MP4 與資源限制

- 單檔上限 **1,500,000,000 bytes**，以串流實際累計位元組強制執行，不信任 Content-Length、附檔名或 MIME。
- 在讀取 multipart 大型 body 前，以同一把 lock 預留唯一任務／上傳名額；解析表單、串流與處理期間都佔用名額。FastAPI 的路由依賴若在表單解析後才執行就不足，需用早於解析的 admission gate（middleware 或手動 Request.stream）。
- multipart 請求總量另以單檔上限加 1 MiB 封頂，避免表單解析器先無限制 spool；欄位限制為既有小型選項且只允許一個檔案。最多讀取 1 MiB 一塊；閒置 60 秒無新資料中止上傳。
- 檔案只存伺服器產生的 job 目錄和固定檔名；原始檔名僅作跳脫後的顯示文字，不用作 filesystem path。
- FFprobe 驗證真實 MP4 容器、有限正數 duration 及至少一條視訊軌；拒絕偽裝、損毀與只有音訊的檔案。Google 匯出所得檔案套用相同驗證及大小限制。
- 上傳中斷、超量、媒體驗證失敗與 worker 失敗都釋放名額並清除部分原片、音訊及取樣暫存。完成也刪原片與音訊；保留輸出檔及 ZIP，延續既有手動清理 data 的方式。

## Gemini 語音轉錄

- FFmpeg 抽取單聲道 16 kHz、64 kbps MP3，按 **300 秒**不重疊切段，最後一段可較短；逐段提交 inline audio 與轉錄要求，避免新增 Files API 上傳生命週期。
- 請求沿用 GEMINI_API_KEY／GEMINI_MODEL；預設模型維持既有設定。實作前按當時官方文件確認該模型支援音訊及 JSON 回覆，不自行引入另一個供應商。
- 每段要求 JSON array `{start: number, text: string}`，start 為片段相對秒數；只轉錄聽見的語音，不生成摘要、虛構對話或翻譯。無語音回空 array。
- 所有數值須有限、start >= 0 且 < 該段長度、文字非空；驗證後加該片段 offset。格式錯誤、截斷、越界 cue 為該段轉錄失敗，不將錯誤內容當逐字稿。可在同一秒有多 cue，保持穩定排序。
- 429／500／502／503／504 重試至多 3 次，等待 15／30／45 秒；其他錯誤不重試。每次呼叫 timeout 600 秒。重試共用既有 Gemini 錯誤處理，不複製一套文字 API client。
- 任一段失敗，保留其他成功段並記錄失敗時間範圍；有至少一失敗段為 partial，全部失敗為 unavailable。無音軌／全程無語音為 unavailable 並說明原因；皆可繼續圖片筆記。
- partial 有文字時仍可做分章、摘要與翻譯，但 UI 及 README／summary／翻譯結果開頭須註明來源逐字稿不完整。unavailable 不請求文字分章、摘要或翻譯，提示略過原因。
- 不擴增 YouTube 自動語音辨識 fallback：這次新轉錄流程用於 MP4／Vids；YouTube 字幕策略維持現況。
- Gemini 時間戳不承諾字級／影格級準確；手動驗收須抽查開頭、中間、結尾，確認無跨段時間重置或累積位移。

## Google Vids 與 Google 授權

- 只接受 HTTPS `docs.google.com/videos/d/{id}` 網址；解析 ID 後固定呼叫官方 API，不抓取任意 URL 或繞過分享／組織限制。
- 新增 `/api/google/status`、`/api/google/connect`、`/api/google/callback`、`POST /api/google/disconnect`。status 僅回 configured／connected，不回 token。
- Web server OAuth authorization-code flow，採成熟的 `google-auth-oauthlib`／`google-auth`，驗證隨機 state 與原 owner，state 一次性且 10 分鐘過期，使用 PKCE；callback 只能回固定 app 首頁，不接收任意 return URL。
- 任意貼上既有 Vids 連結需 `https://www.googleapis.com/auth/drive.readonly`。不假設 drive.file 已允許所有既有檔案；此較廣的唯讀 scope 需在連線畫面與 README 說明，工作區可能限制授權。
- 設定 `GOOGLE_CLIENT_ID`、`GOOGLE_CLIENT_SECRET`、`GOOGLE_REDIRECT_URI`，callback 必須與 Google Cloud 設定完全相符；個人 localhost 允許 HTTP，遠端必須 HTTPS。
- access／refresh token 僅留本程序記憶體並綁 owner，重啟需重新連線；不新增 token 檔案或資料庫。過期時能 refresh，失敗清除連線並要求重連；不寫入 token 回覆或原始 exception。
- 同源 Origin 檢查保護 disconnect 等狀態變更。disconnect 撤銷 Google grant 並清除本地憑證；該 owner 有進行中的 Vids 任務時先回 409，避免中途斷開的含糊語意。
- 讀 metadata，確認 `application/vnd.google-apps.vid` 與 capabilities.canDownload；拒絕其他 Drive 類型，不隱性擴充一般 Drive MP4 支援。
- 使用 `files.download(fileId, mimeType=video/mp4)`，輪詢 operations.get，完成後串流下載官方回傳的 download URI。**不使用 files.export 或自組公開下載 URL。**
- 匯出輪詢每 5 秒一次，最多 30 分鐘；處理暫時性 429／5xx，不重複啟動匯出。URI 必須 HTTPS，禁止任意跳轉和把 OAuth header 轉送至未經官方文件確認的主機；依官方 API 的下載認證契約處理。
- 下載串流持續套用大小與逾時限制。錯誤須可辨識：未連線、授權過期、無法存取／禁止下載、匯出逾時、Google 暫時錯誤、取得的媒體無效。
- 本對話提供的 Vids ID 為 `187cnNk81GnN_pqRPrlhmpt2TX43pFptnuUlZ0FPf90A`。目前連接的 Drive 工具回 404；只作手動驗收候選，不當公開 fixture 或假設已可讀取。ChatGPT 的 Google 連接憑證不能沿用到這個 app。

## 輸出與來源語意

- 延用 README.md、screenshots/、frames.json、chapters.json、transcript.json、選用 summary.md／README.<語系>.md。
- 額外輸出 source.json，含 source、title、原始遠端 URL 或上傳檔名、duration、transcript_source、transcript_status、warnings；不含本機絕對路徑、憑證、內部下載 URI 或原片。
- YouTube 保留可跳時間的觀看連結。Vids README 保留原始連結並用純文字時間戳，不假設 YouTube 的 `?t=` 適用；MP4 使用檔名與純文字時間戳，不產生不存在的原片連結。
- transcript.json 延用 start／text list；source.json 和 README 說明完整度與缺漏時間。無語音時的措辭改為符合來源的「沒有可用逐字稿」，不能宣稱從未執行轉錄。
- 原始檔名、標題及 AI 回傳文字的前端顯示使用 textContent；ZIP 不包含原片。圖片仍是相對路徑，解壓後可閱讀。

## 實作切分與測試

1. **MP4 圖片筆記完整路徑**：來源選擇、上傳 admission／驗證、共用 worker、來源正確的 Markdown、ZIP 與清理；是轉錄的基礎，尚不宣稱滿足完整 MP4 功能。
2. **MP4 Gemini 逐字稿與 AI 輸出**：分段、時間偏移、容錯、摘要／翻譯、進度、文件與測試，完成完整 MP4 體驗；依賴 1。
3. **Google Vids 授權與完整處理**：OAuth、匯出／下載、UI 與共用 MP4 轉錄流程、權限與安全測試；依賴 2。

每個 issue 都包含 UI、API、處理流程、文件及自己新增行為的測試，不另開純 UI／純測試 issue。依序完成，無需多代理並行實作。

- 保留現有 unittest，新增 API 層驗收；以 mock 取代外部 Google／Gemini／yt-dlp，測外部行為和產物，不把正式 API 呼叫放入自動測試。
- MP4：有效、小尺寸無聲、損毀、偽裝、路徑檔名、實際超量（測試可調小限制）、multipart 超量／多檔、斷線、忙碌競爭、失敗清理與重新整理。
- Gemini：兩段以上、300 秒邊界、最後短片段、無語音、錯誤 JSON／NaN／越界、截斷、重試、部分失敗，以及不完整來源在摘要的標示。
- OAuth／Drive：state 錯誤／過期／重放、跨 owner、callback 錯誤、refresh、disconnect、403／404、canDownload=false、非 Vids、pending→done、逾時、受限下載 URI、下載超量。
- YouTube regression：既有 11 項測試及原 JSON 建任務到 ZIP 的 mock smoke test；未設任何 Google 設定仍可使用原功能。
- 手動驗收：Docker build；有聲 MP4 及無聲 MP4；有權限與無權限 Vids；實際中文／英文混合語音與跨段時間戳；截圖參數、重新整理、ZIP 解壓圖片、憑證不洩漏。需另記錄使用模型、影片長度、結果，不將 mock 當真實服務驗證。

## 明確不做

本機 Whisper、逐字編輯器、說話者辨識、多檔批次、斷點續傳、Google Picker、公用登入／多租戶、資料庫、持久任務與 token、OCR、音訊檔獨立上傳、一般 Drive 檔案來源，以及新的 YouTube 轉錄 fallback。

## 參考與待驗證項目

- [官方 Ponytail skill](https://github.com/DietrichGebert/ponytail/blob/main/skills/ponytail/SKILL.md)：本規劃使用 full 原則；未安裝或修改全域 skill。
- [Google Vids 長時間下載操作](https://developers.google.com/workspace/drive/api/guides/long-running-operations)：下載 Vids MP4 使用 files.download 與 operation 輪詢。
- [Drive files.download](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/download)：實作時核對 URI 與下載認證需求。
- [Google Web Server OAuth](https://developers.google.com/identity/protocols/oauth2/web-server)：callback、state、scope 與 refresh。
- [Gemini 音訊處理](https://ai.google.dev/gemini-api/docs/audio)：實作時核對既有模型的 inline MP3／JSON 能力與當時限制；本次未實際呼叫或估算 API 費用。
