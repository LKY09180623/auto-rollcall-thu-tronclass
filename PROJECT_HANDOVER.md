# TronClass 自動點名系統 — 專案總結與技術瓶頸交接報告

> **本文件旨在提供給 CODEX 或後續接手工程師進行技術交接與功能升級。**

> 更新：2026-09-09 已實作多帳號環境設定、單容器 Gateway 整合、部署設定修正與手動點名結果修復，詳見本文第 5–7 節及 [部署說明](DEPLOYMENT_GUIDE.md)。第 1–4 節保留原始交接紀錄，其中「正常運作／100%」屬於先前紀錄，本次未以真實學校服務重新驗證。

---

## 1. 專案背景與架構總覽 (Architecture Overview)

* **專案倉庫**：`LKY09180623/tronclass-bot` (分支: `master`)
* **部署環境**：[Render.com](https://render.com) (Free Web Service Docker 容器)
* **雙帳號雲端架構**：
  * **一號機 (`tronclass-bot`)**：監控帳號 `A113280040` (實踐大學 USC)
  * **二號機 (`tronclass-bot-2`)**：監控帳號 `A113280042` (實踐大學 USC)
  * **保活與啟動 (`render_start.py`)**：
    * 內建 Python HTTP Server (Port 10000)，配合 `cron-job.org` 定期發送 HTTP GET 防止 Render 免費實例 15 分鐘休眠。
    * 以 `threading` 多執行緒同時維持：
      1. HTTP Keep-alive Server
      2. `troTHU.tron`（主點名監控進程，每 5~6 秒輪詢一次課堂 API）
      3. `discord-gateway`（Discord Bot 雙向互動控制進程）
* **外部介面與通訊**：
  * **Discord 機器人**：Gateway 模式雙向控制與即時簽到通知。
  * **手機端掃描網頁**：部署於 Netlify (`https://capable-quokka-f130f3.netlify.app/`)，使用 `html5-qrcode` 掃描相機畫面。
  * **MQTT 雲端中繼**：透過公共 Broker `broker.emqx.io:1883` (WebSocket `8084`)，監聽頻道 `tronclass/qr_relay/A113280040_secret` 進行跨雲端實例同步廣播。

---

## 2. 目前已實作並正常運作的功能 (Working Features)

### 🟢 1. 雷達點名 (Radar Rollcall) —— 【100% 全自動】
* **實作位置**：`troTHU/radar_runtime.py:empty_answer_radar`
* **運作原理**：利用 TronClass 後端接收空 JSON 封包 `{}` 判定出席的架構設計失誤，直接完成出席確認 (`on_call_fine`)；若空答案失敗，自動 fallback 至 `global_wgs84` 校園預設經緯度座標偽裝。

### 🟢 2. 動態 QR Code 點名 (QR Relay) —— 【半自動代簽】
* **管道 A (Discord 指令)**：於 Discord 執行 `/tron qr payload:<URL/Token>`。
* **管道 B (手機 Web App)**：開啟 Netlify 掃描頁面，掃描教室大螢幕，手機自動將 QR 資料送至 MQTT 頻道，觸發兩台雲端機器人同時完成簽到（`troTHU/mqtt_relay.py`）。

### 🟢 3. 數字點名手動快速補簽 —— 【半自動】
* **實作位置**：`troTHU/bot_handlers.py:qr_submit`
* **運作方式**：在 Discord 執行 `/tron qr payload:XXXX`（輸入 4 位數字），後端自動判定為數字點名，向 TronClass 抓取當前進行中的活動並送出 `{'numberCode': 'XXXX'}` 簽到，支援即時 Discord 回報。

### 🟢 4. Discord Bot 權限與敏感詞過濾修復
* **實作位置**：`config.advanced.yaml`、`troTHU/discord_adapter.py`
* **修復項目**：
  1. 移除了寫死的單一 profile binding，Admin (`862506419508477953`) 可直接操作當前伺服器活躍 Profile。
  2. 修復了 `troTHU/discord_adapter.py` 中因誤判 `"bot "` 關鍵字導致狀態回報全部被塗黑成 `[redacted]` 的 Bug。

---

## 3. 目前的技術瓶頸與待解決問題 (Technical Bottlenecks for CODEX)

### 🔴 瓶頸 1：隨機 4 位數數字點名無法「全自動無人值守盲猜」（最核心痛點）
* **問題現狀**：學校已打補丁修補了 API JSON 封包明文夾帶 `numberCode` 的漏洞（`direct_read_status = 'no_code'`）。
* **技術限制**：TronClass 伺服器具備嚴格的 Rate Limiting（頻率限制與 429 暫時封鎖）。在課堂點名時限（約 3~5 分鐘）內，單一 IP / 單一帳號無法在被封鎖前窮舉完 10,000 組 (0000-9999) 隨機密碼。
* **給 CODEX 的研究與升級方向**：
  1. **探勘未授權端點**：研究 TronClass 或學校 SSO 是否存在其他未授權查詢當前課堂點名狀態的洩漏點。
  2. **分散式代理池 (Rotating Proxies)**：評估引入免費/便宜 HTTP 代理池將 10,000 次猜測分散至多個 IP 的可行性。

---

### 🟡 瓶頸 2：Discord Gateway 多實例競爭 (Instance Race Condition)
* **問題現狀**：目前一號機與二號機使用同一個 `DISCORD_BOT_TOKEN` 同時啟動 `discord-gateway`。
* **技術限制**：Discord Gateway 在未實作 Sharding 的情況下，會隨機將 Slash Command 分配給其中一台實例。使用者在 Discord 輸入 `/tron status` 時，只會隨機得到其中一台機器的狀態回覆，無法一次查看所有帳號聚合狀態。
* **給 CODEX 的研究與升級方向**：
  1. **跨實例聚合指令**：實作一個聚合指令（例如 `/tron all`），主動透過 MQTT 廣播向所有線上實例請求狀態並匯總回覆。
  2. **主從架構 (Master-Worker)**：改為只在單一主實例啟動 Discord Gateway，其餘實例純作為背景 Worker 透過 MQTT 接受調度與回傳結果。

---

### 🟡 瓶頸 3：單一 Render 實例原生支援多帳號 (Multi-account in Single Instance)
* **問題現狀**：目前是透過在 Render 開兩台免費 Web Service 來支援兩個帳號。
* **技術限制**：`troTHU/simple_config.py` 與 `troTHU/config_runtime.py` 的自定義 YAML 解析器對多帳號格式要求嚴格（包含 `now`、`accounts` 結構），先前在 `render_start.py` 動態生成多帳號設定檔時容易觸發 parser 崩潰。
* **給 CODEX 的研究與升級方向**：
  1. **重構配置載入器**：重構 `config_runtime.py`，增加原生環境變數支援（例如解析 `TRON_ACCOUNTS_JSON`），讓單一實例即可原生啟動多 Profile 輪詢監控，節省伺服器資源。

---

## 4. 關鍵核心程式碼路徑清單 (File Map)

| 模組 / 功能 | 檔案路徑 | 說明 |
| :--- | :--- | :--- |
| **啟動進入點** | `render_start.py` | 雲端啟動、配置注入、假伺服器與多執行緒守護 |
| **點名決策引擎** | `troTHU/rollcall_engine.py`<br>`troTHU/rollcall_runtime.py` | 判斷點名種類、排程回覆、驗證簽到狀態 |
| **數字點名模組** | `troTHU/number_runtime.py`<br>`troTHU/number_rollcall.py` | 數字點名嘗試、直接讀碼、冷卻與限流控制 |
| **雷達點名模組** | `troTHU/radar_runtime.py` | 空答案簽到漏洞實作、WGS84 座標 fallback |
| **QR Code 模組** | `troTHU/qr_runtime.py` | QR Payload 解析、提交與驗證 |
| **Discord 互動介面** | `troTHU/discord_adapter.py`<br>`troTHU/discord_gateway.py`<br>`troTHU/bot_handlers.py` | Discord Slash 指令處理、權限控制與狀態格式化 |
| **MQTT 廣播模組** | `troTHU/mqtt_relay.py`<br>`scanner_app/index.html` | 手機端掃描與雲端中繼通訊 |

---

## 5. 2026-09-09 接手開發更新

已完成的基礎設施改善：

1. **原生環境帳號**：新增 `troTHU/environment_config.py`，驗證 `TRON_ACCOUNTS_JSON`、Profile 名稱及學校。帳密不再由啟動器寫入設定檔，錯誤環境值不會觸發 `config.yaml` 損壞復原。
2. **單容器多程序**：重構 `render_start.py`，每個帳號啟動獨立監控程序，最多啟動一個 Gateway；支援重啟退避、停止回收、`--check` 與程序健康檢查。
3. **帳號隔離**：切換 Profile 保留並套用學校設定，環境憑證依選定 Profile 解析。監控子程序只收到自己的帳號配置，查詢共享待處理紀錄時會排除未配置 Profile。
4. **本機狀態彙整**：新增 `/tron all`，沿用既有帳號查詢權限，回報各帳號 monitor 與 heartbeat 狀態。這是同容器聚合，不是跨 Render 服務的 MQTT RPC。
5. **持續運作與儲存**：移除 Gateway 的 20 事件退出條件；事件歷史有容量上限，閒置時也能停止。共享狀態檔採跨程序鎖及原子替換，MQTT client ID 使用 UUID，避免多程序識別碼重複。

遷移所需的環境變數、Profile 命名、排程星期索引、Discord schema 同步方式與已知界線均列於 [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md)。程式碼尚未推送或部署，沒有更動雲端環境變數。

本輪未實作未授權端點探勘、暴力猜碼或代理池繞過限流；原始數字／雷達策略仍有待獨立檢視，不能由基礎設施測試推論真實簽到成功率。

### 本輪驗證結果

測試環境為 Windows、Python 3.14.7，原始版本基準為 Git `3d796d8`。完整測試使用隔離的專案複本，排除本機憑證設定與執行紀錄；未連線測試真實學校、Discord 或 MQTT。

| 檢查 | 結果 |
| --- | --- |
| 本次相關回歸 | 115 項全部通過，包含 26 項新增測試。 |
| 原始版本完整測試 | 583 項；4 個 failure、13 個 error。 |
| 修改後完整測試 | 609 項；3 個 failure、13 個 error；失敗清單相較原始版本沒有新增項目。 |
| 多程序資料一致性 | 4 個獨立程序共同更新同一 Profile 與各自 Profile，並新增／移除待處理紀錄，最終資料完整。 |
| Gateway 長連線邏輯 | 153 個模擬事件完整處理，第 150 個事件後的狀態指令正常回覆；事件歷史保持 100 筆上限。 |
| 執行環境相容性 | 78 個 Python 來源檔通過 Python 3.10 語法解析；沒有實際建置 Docker 或 Windows exe。 |

剩餘完整測試問題皆已在原始版本重現：

- 13 個 error：數字點名相關測試遇到 `AttributeError: contextlib`，源於既有 `number_runtime.py` 對 runtime context 的引用。
- 2 個 failure：CLI provider 清單測試仍使用加入 USC 前的預期值。
- 1 個 failure：既有 USC 雷達測試預期第一個請求含座標，但目前策略第一步送空物件。

封裝模組清單已補入本次新增模組與原本缺漏的 MQTT 模組，原始版本對應的封裝檢查失敗已解除。完整測試尚未全綠，不應將這次基礎設施驗證視為可直接上線的整體認證。

## 6. 2026-09-09 第二輪：設定正確性與部署憑證保護

本輪針對前一輪實作的設定與部署邊界補強：

- `render_start.py --check` 與啟動前置驗證改用唯讀設定載入。本機設定缺漏／損壞時不會建立預設檔或搬移原檔；進階 YAML 損壞時也不會默默忽略排程與管理員設定。
- 環境帳號的新增、切換、刪除、初始化及簡易設定寫回，在任何變更前即回報須修改環境變數；綁定指令引導至進階設定，不再於儲存失敗後宣稱成功。
- 帳號憑證診斷改為依指定 Profile 計算，不再借用活躍帳號的憑證來源。設定檢查改讀 provider registry，並修正兩項尚未列入 USC 的舊 CLI 測試。
- Compose 收斂為單一啟動器服務，Token 改為環境變數；新增 `.dockerignore`、`.env.example` 與 `config.advanced.example.yaml`。Dockerfile 明確列出需要複製的檔案，本機憑證設定與執行資料不會放入映像。
- 環境帳號新增 `TRON_DISCORD_ADMIN_IDS`，可配置 Discord 管理員；空值清空 Discord 管理員，未設定則保留進階設定值。映像範本預設不授予任何管理員權限。
- Compose 及三個舊 Discord 工具腳本內的固定 Token 已移除。工具腳本只有直接執行時才會發出請求，匯入不會觸發網路操作。

**已曝光 Token 的處理仍需外部操作：** 移除文字不會讓舊 Token 失效；使用者需在 Discord 後台換新 Token，並更新部署環境。本次未代為重設 Token、推送、部署或同步 Discord 指令，也未改寫 Git 歷史。

| 驗證 | 第二輪結果 |
| --- | --- |
| 設定／CLI／部署相關回歸 | 79 項全部通過，本輪新增 13 項測試。 |
| 完整隔離測試 | 622 項；1 個 failure、13 個 error。相較前輪消除 2 個 USC 清單預期失敗，沒有新增失敗項目。 |
| Compose 設定 | `docker compose --env-file .env.example config --quiet` 通過。 |
| Python 3.10 語法 | 78 個來源檔通過解析。實際測試仍使用 Windows／Python 3.14.7。 |
| 固定 Token 複查 | 受 Git 管理的文字檔中，該類 Discord Token 字串格式的匹配數為 0；這不代表 Git 歷史或其他敏感資料已清除。 |
| Docker 實跑 | 本機 Docker Engine 未啟動，未實際建置或執行容器。 |

剩餘 13 個數字點名錯誤與 1 個雷達測試失敗同第 5 節的既有問題，未修改相關繞過策略。部署範本、環境帳號管理方式及 Compose 遷移命令見 [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md)。

## 7. 2026-09-09 第三輪：手動數字、QR 提交與個人結果確認

本輪保留既有 `/tron qr payload:<4 位數字或 QR>`、QR CLI 與 MQTT 接收入口，修正提交及結果判讀。所有修改仍在本機，沒有推送、部署或更換雲端設定。

### 已修正的實際問題

1. **手動數字清單讀取錯誤**：HTTP client 回傳 `RollcallsResult.payload['rollcalls']`，原程式卻讀取不存在的 `.rollcalls`，導致持續判斷沒有活動。現在正確讀取清單，只選數字活動；不把一般 `in_progress` 的 QR／雷達活動當成數字活動，也不選已確認完成的活動。遇到多個候選活動時回報無法判定課程。
2. **手動數字提交路徑不一致**：改用專案既有數字 API 合約的 `answer_number_rollcall` 路徑，保留前導零。每次指令只提交使用者提供的代碼一次；錯碼、登入失效、429、其他 HTTP 錯誤有不同回報。
3. **提交被接受不等於確認出席**：手動數字不再忽略驗證結果；QR 的 Bot handler、多帳號結果與 CLI exit code 不再忽略 `submit_qr_payload()` 的布林回傳值。未確認時回報 `submitted_unconfirmed`，不宣稱成功。
4. **個人身分比對錯誤**：以 `AccountProfile.user` 比對名單的 `user_no`，不再使用 Profile 顯示／設定名稱。個人的點名 feed 回傳 `on_call_fine`，或名單中相符帳號出席，才足以確認；全班統計與活動整體狀態保留供顯示，不代替個人出席證據。
5. **QR 待處理紀錄過早消失**：確認成功後才移除 pending QR；尚未確認時保留。MQTT 在已有配對但提交部分失敗時不再額外重送活躍帳號，避免 CLI 正確回報失敗後觸發重複提交；沒有配對紀錄時，既有直接提交入口仍保留。

### 驗證與相容性界線

| 項目 | 本輪結果 |
| --- | --- |
| 手動數字與 QR 相關回歸 | 38 項通過，包含本輪新增的提交及個人結果案例；成功、錯碼、過期 QR、登入失效、429、未確認、多帳號部分失敗、CLI 登入刷新與 MQTT 既有入口皆有覆蓋。 |
| 完整隔離回歸 | 641 項，耗時 53.460 秒；1 個 failure、13 個 error。與第二輪 622 項結果相比，失敗清單沒有新增項目。 |
| 測試網路限制 | 最終完整測試的主程序加上 socket audit hook，只允許 loopback 連線；非 loopback 連線嘗試計數為 0。使用假帳號及本機模擬 HTTP，未驗證真實學校提交。 |
| 原核心保留 | `radar_runtime.py`、`number_runtime.py`、`number_rollcall.py`、`rollcall_engine.py` 與 Git `3d796d8` 相同；共用的結果確認則有上述修正。 |
| 測試預期修正 | 不再把「其他人全數出席」當作自己的出席；既有雷達整合測試補上模擬的個人活動資料。教師輔助 QR 測試保留由個人 feed 確認成功的驗證。 |
| 語法與差異 | 本輪 5 個 runtime 檔通過 Python 3.10 語法解析；`git diff --check` 通過。實際測試仍為 Windows／Python 3.14.7。 |

可重跑本輪相關測試：

```powershell
python -m unittest tests.test_submission_results tests.test_rollcall_progress tests.test_bot_handlers tests.test_qr_teacher_runtime.QrRuntimeFinalizeTest -q
```

最終完整測試記錄：`.tmp-tests/submission-final-klngz1wy/unittest.log`（本機忽略檔，不加入版本庫）。完整測試應沿用隔離複本、排除本機帳密、限定 `discover -s tests` 的方式；不要在根目錄直接 discover 舊的網路診斷腳本。

**仍不能保證雷達、數字、QR 在真實學校環境全部成功。** 手動數字與 QR 已完成本機成功／失敗驗證；自動數字流程仍有既有的 `contextlib` 錯誤，雷達策略未修復或重新實測。下一步須先對照雲端實際失敗種類與去識別錯誤紀錄，再於有授權的有效點名情境核對官方出席狀態，不能把本機模擬或 Discord 成功文字當成正式出席證明。
