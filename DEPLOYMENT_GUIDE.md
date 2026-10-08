# 多帳號環境設定與 Render 啟動

本版支援在一個容器內啟動多個帳號程序，並由一個 Discord Gateway 查詢這些帳號的本機狀態。帳密從環境變數讀取，不再由 `render_start.py` 拼接或覆寫 `config.yaml`。

## 設定帳號

在服務的環境變數新增 `TRON_ACCOUNTS_JSON`，內容為 JSON 陣列。以下使用示意帳密：

```json
[
  {"name": "primary", "user": "STUDENT_ONE", "passwd": "replace-me-one", "school": "usc"},
  {"name": "secondary", "user": "STUDENT_TWO", "passwd": "replace-me-two", "school": "usc"}
]
```

- `user`、`passwd`、`school` 必填，而且必須是字串。密碼中的冒號、井號、引號、反斜線與前後空白會保留；JSON 的引號與反斜線仍需按照 JSON 規則跳脫。
- `name` 是 Profile 名稱，省略時使用 `user`。名稱限 80 字元，以英數、底線、句點或連字號組成，開頭結尾不能是標點；不得重複，包括只差大小寫的名稱。同校同帳號也不得重複。
- `school` 使用專案既有 provider，例如 `usc`、`thu`、`tku`。未知值會報錯，不會默默改用另一所學校。
- `label` 可省略，預設為學校代碼。
- 要沿用原有 cookie、Discord 綁定與狀態紀錄，`name` 必須與先前的 Profile 名稱相同；新名稱會使用新的 Profile 紀錄。

## 環境變數

| 變數 | 用途與預設 |
| --- | --- |
| `TRON_ACCOUNTS_JSON` | 多帳號來源；優先於舊的 `TRON_USER` / `TRON_PASS`。設成空字串或不合法 JSON 會停止啟動。 |
| `TRON_PROFILE` | 指定一般 CLI 與 Gateway 的預設 Profile；省略時使用 JSON 第一項。Render 仍會為 JSON 的每個帳號啟動一個監控程序。 |
| `TRON_USER`、`TRON_PASS` | 保留單帳號相容性，必須同時提供。 |
| `TRON_SCHOOL` | 舊單帳號環境模式的學校，預設 `usc`。JSON 模式使用各項目的 `school`。 |
| `TRON_DISCORD_GATEWAY_ENABLED` | `auto`（預設）、`true` 或 `false`，也接受 `1` / `0`。`auto` 只在有 Bot token 時啟動 Gateway。 |
| `TRON_DISCORD_ADMIN_IDS` | 環境帳號模式的 Discord 管理員使用者 ID，以逗號分隔。未設定時保留進階設定中的管理員；設成空字串時清空 Discord 管理員，不影響其他平台。 |
| `TRON_MQTT_CHANNEL` | 明確設定既有中繼頻道；必須與掃描頁使用的頻道一致。程式不再注入寫死的共用頻道。 |
| `PORT` | HTTP 監聽 port，預設 `10000`。 |

Discord token、application ID 與使用者綁定使用 `config.advanced.yaml` 中的 `integrations` 設定；管理員可由 `TRON_DISCORD_ADMIN_IDS` 覆寫。Gateway 會使用其中 `token_env` 指定的環境變數名稱。部署範本沒有預設管理員，須填入自己的 Discord 使用者 ID 才能執行管理員查詢。

使用環境帳號時，只讀取 `config.advanced.yaml` 的其餘進階設定，忽略 `config.yaml` 的帳號內容。原本放在 `config.yaml` 的時段若仍要套用，須移到進階設定的 `operating`。進階設定使用 **0 = 星期一、6 = 星期日**；人類可編輯的簡易設定原本使用 0 = 星期日，兩者不可直接照抄日期索引。

例如只在星期一上午運作：

```yaml
operating:
  0:
    enable: true
    range: ["09:00", "12:00"]
  1: {enable: false}
  2: {enable: false}
  3: {enable: false}
  4: {enable: false}
  5: {enable: false}
  6: {enable: false}
```

沒有設定時段的日期使用專案預設值。環境帳號的密碼不會放入可序列化的 config，也不會透過 `save_config()` 寫回檔案；帳號修改請在環境變數完成後重啟。

環境帳號模式下，`account add/switch/remove`、`init` 寫入及 `config compact --write` 會在變更前回報無法執行；不會先改設定、刪除 cookie 或寫入 keyring。使用 `TRON_PROFILE` 選擇預設帳號。`account bind/unbind` 也不會回報虛假的儲存成功，請改為編輯進階設定的 `integrations.bindings` 並重啟。`account list/show/doctor` 仍可讀取狀態，憑證來源會依每個指定的 Profile 計算。

`config.advanced.yaml` 若不是有效 YAML mapping，環境模式會停止啟動並回報不含敏感原文的錯誤，不會默默忽略排程或管理員設定。

## Docker Compose 與映像設定

Compose 現在只啟動一個 `auto-rollcall` 服務，內部由 `render_start.py` 管理各帳號程序與 Gateway。所有帳密與 Bot Token 均由環境變數提供。

在 PowerShell 中建立尚未存在的本機設定檔，再填入實際帳號、**新 Token**、Discord application ID 及管理員 ID：

```powershell
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}
if (-not (Test-Path -LiteralPath 'config.advanced.yaml')) {
    Copy-Item -LiteralPath 'config.advanced.example.yaml' -Destination 'config.advanced.yaml'
}
```

先檢查 Compose；準備啟動時需有運作中的 Docker Engine：

```console
docker compose config --quiet
docker compose up -d --build --remove-orphans
```

`--remove-orphans` 用於移除同一 Compose 專案的舊 monitor／discord 服務，避免它們與新服務重複運作；請先確認使用的是原本的 Compose 專案目錄／名稱。資料仍放在主機的 `state` 與 `log` bind mount 中。

映像只包含執行所需程式與 `config.advanced.example.yaml`，不會複製本機 `config.yaml`、`config.advanced.yaml`、`.env`、cookie、紀錄、測試或原始碼壓縮檔。Compose 在執行時唯讀掛載本機的進階設定；Render 的映像則預設使用不含憑證的範本，管理員等資料請透過環境變數配置。自訂進階排程需另行提供執行時設定，不能再依賴把本機設定直接烘焙進映像。

已從 Compose 及三個舊 Discord 工具腳本移除固定 Token。這不會撤銷舊 Token，也不會改寫 Git 歷史；先前出現在檔案中的 Token 仍須在 Discord 後台換新，再更新部署環境。本次沒有代為重設 Token 或改動外部服務。

## 啟動與查詢

先驗證設定，這個指令不啟動監控、不連 Discord 或學校：

```console
python render_start.py --check
```

兩個帳號、有設定 Discord token 時，結果會列出兩個 `monitor:<name>` 與一個 `discord-gateway`，不會輸出密碼或 token。檢查及啟動前置驗證皆為唯讀：未設定環境帳號且本機設定缺漏／損壞，或環境、進階設定不合法時，以 exit code 2 結束，不會建立、備份或重建設定檔，也不會啟動子程序。

正式啟動入口仍是：

```console
python render_start.py
```

每個監控程序只收到自己的帳號資料，使用既有 `run --no-input` 入口。各程序有獨立的 Python 全域狀態與登入 session，Gateway 可以讀取同一個容器內的帳號狀態檔。

程式退出後由啟動器重啟，連續快速失敗時等待 3、6、12、24、48、60 秒，最長 60 秒。持續運作超過 60 秒後再次退出會重設等待時間。啟動器收到停止訊號會終止並回收子程序。

新增 `/tron all`，與 `/tron accounts` 共用權限規則：管理員可看所有已配置帳號，一般已綁定使用者只看自己的帳號，未綁定使用者不能查詢。回覆包含 monitor 狀態、cookie 是否存在及有效、待處理數量與過期 heartbeat 提示，最多顯示 10 個帳號。

部署程式碼後，若 Discord 選單還沒有 `all`，可由部署管理員執行既有 schema 同步指令：

```console
python -m troTHU.tron bot discord-sync --apply
```

本次開發沒有執行這項外部同步，也沒有推送或部署服務。

## 多服務遷移的界線

要一次查看兩個帳號，請將兩個帳號設在同一個容器的 JSON 裡，並停止舊服務對同一批帳號的重複監控。

如果仍維持多個 Render 服務，同一組 Bot token 只能指定一個 Gateway 擁有者，其餘服務設 `TRON_DISCORD_GATEWAY_ENABLED=false`。**目前 `/tron all` 只彙整同一容器內的狀態，沒有跨服務 MQTT 狀態 RPC、選主或自動故障轉移。** 不同容器不會因使用同一個 token 或 MQTT 頻道而自動共享狀態檔。

`/health`、`/healthz` 及 `/` 會依子程序狀態回傳 HTTP 200 或 503；只公開預期與運作中程序數，不公開學號、Profile 名稱或憑證。這是程序存活檢查，不代表登入成功、API 可用或已完成簽到。

本機 JSON 狀態與待處理清單已加入跨程序交易鎖及原子替換，避免並行寫入遺失其他帳號紀錄。這些鎖適用於同一主機的本機檔案系統，不是跨容器的分散式鎖。Gateway 的事件紀錄只保留最近 100 筆，連線不再因收到 20 筆事件而自行退出。

## 驗證方式與限制

相關回歸可使用以下命令，只使用模擬帳號、假 Gateway 或本機 HTTP：

```console
python -m unittest tests.test_environment_config tests.test_render_start tests.test_multi_process_state tests.test_multi_account_status tests.test_discord_gateway tests.test_discord_adapter tests.test_bot_handlers tests.test_bot_status tests.test_simple_config tests.test_group_runtime tests.test_account_runtime_store tests.test_package_diagnostics tests.test_tron_facade tests.test_ux_phase -q
```

第二輪設定與部署修正的回歸：

```console
python -m unittest tests.test_deployment_files tests.test_environment_config tests.test_render_start tests.test_config_view tests.test_cli tests.test_tron_facade tests.test_multi_account_status -q
docker compose --env-file .env.example config --quiet
```

第二輪相關回歸 79 項通過；完整隔離測試 622 項，剩餘 1 個 failure、13 個 error，均為前一輪已確認的數字／雷達既有問題。Docker Engine 在本機未啟動，所以只驗證了 Compose 設定與映像輸入清單，尚未實際建置或啟動容器。

本次不修改未授權端點探勘、暴力猜碼、代理輪換或繞過限流策略。測試沒有連線驗證學校出席結果、公共 MQTT 廣播、Discord 真實連線或 Render 的資源容量。

## 主頁狀態更新與點名辨識

主頁約每秒讀取一次本機 `/api/status`；切回分頁或 App 前景會立即再讀一次。監控程序在上課時段且沒有點名時每 3 秒查詢學校一次，啟動後最初 30 秒每秒一次；偵測到活動後短時間加快，HTTP／網路錯誤則至少等待 6 秒再查。這些是每次請求完成後的等待時間，實際延遲還包含學校 API、登入、簽到率查詢及網路耗時。官方 App 是否使用伺服器推播未經驗證，因此不能保證與官方通知同時出現。

主頁以最近一次學校回應顯示數字、雷達、QR 點名，並標示上次掃描距今多久；超過 12 秒沒有新回應會顯示狀態逾時。排程外顯示休眠，並不會查詢學校。新活動出現時主頁顯示提示及短震動。若沒有收到提示，先看卡片是「休眠」「需登入」「監控未運作」或「狀態逾時」，再用 `python -m troTHU.tron rollcall-status --json` 做唯讀診斷。此指令不會提交點名。

數字點名可從自己的正常點名清單辨識活動；老師公布的 4 位碼仍須由老師提供。儀表板開著時會提示目前帳號與點名活動，輸入第 4 位數字後立即提交；QR 提示可貼上或掃描，取得內容後立即提交。若有多個待處理活動，先選擇正確帳號與點名。提交後仍以個人官方出席狀態為準；未確認時會顯示待確認。雷達可辨識活動與自己的回應狀態，但自己的網路封包不能取代官方位置及出席判定。動態 QR 通常需要掃描當下顯示的 QR；學生帳號清單只表示有 QR 活動，不能保證包含有效 QR 內容。若要排查延遲，可在自己的帳號、已授權的課堂中，記錄「老師開始點名時間、官方 App 顯示時間、主頁首次偵測時間、個人出席確認時間」與去識別化的狀態碼。不要分享 cookie、QR 原文、點名碼或其他同學名單。

目前公開 Web Dashboard 的 `/api/status`、`/api/submit`、`/api/force_check` 和 `/api/reauth` 尚無身分驗證。它不適合直接以公開網址提供操作；部署時應先加存取控制。MQTT 中繼收到訊息只代表已交給 broker，不代表學校接受或個人出席已確認，主頁會以等待確認顯示。
