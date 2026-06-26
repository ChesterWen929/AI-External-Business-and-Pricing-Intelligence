# Codex Handoff — Macro & AI Monitor 新平台開發指南

> 這份檔案是給 **ChatGPT Codex** 看的交接說明。目的：讓 Codex 能在
> **同一個已部署的服務** (https://macro-ai-monitor.onrender.com/) 上，
> 用既有慣例新增一張「平台卡（card）」，而不會弄壞現有的 10 張卡。
>
> 讀完這份就能動工。所有路徑都相對於 repo 根目錄 `macro-ai-monitor/`。

---

## 0. 一句話總結

這是一個 **單一 Flask app**，把多個獨立的研究儀表板（叫「card / 平台」）
包在同一個登入後面。每張卡 = 一個 Python package（資料夾）+ 一個
Flask **Blueprint**，掛在自己的網址前綴 `/<name>`。首頁 `/` 是一個把所有卡
排成「金字塔」的入口頁。**新增一個平台 = 新增一個資料夾 + 在 `app.py`
和 `templates/portal.html` 各插入幾段。基本上是「加法」，很少改到別人。**

- **部署網址**：https://macro-ai-monitor.onrender.com/
- **GitHub repo**：`ChesterWen929/AI-External-Business-and-Pricing-Intelligence`
- **部署方式**：push 到 `main` → Render 自動部署（`render.yaml` / `Procfile`，gunicorn）
- **語言/框架**：Python 3.11.9、Flask 3、APScheduler、yfinance、anthropic SDK
- **語系**：所有面向使用者的文字都是 **雙語（繁體中文 + English）**

---

## 1. 心智模型：portal + cards

```
app.py                      ← 主程式：登入、i18n、金字塔首頁、排程、把每張卡 register 進來
templates/
  portal.html               ← 首頁（金字塔，列出所有卡）
  login.html                ← 共用登入頁
  <name>.html               ← 每張卡自己的儀表板（自含 CSS/JS，雙語切換）
<name>/                     ← 每張卡一個 package（Blueprint）
  __init__.py               ← 定義 blueprint、load_snapshot()、refresh()、路由
  knowledge_base.json       ← 策展知識庫 / 種子資料
  model.py                  ← 純計算（把 KB + live 資料算成 snapshot）
  collectors.py             ← 抓即時資料（yfinance / FRED…），可選
  analysis.py               ← Claude Opus 4.8 結構化輸出 + 規則後備，可選
  test_<name>.py            ← stdlib unittest（不連網）
data/<name>/snapshot.json   ← 算好的快取（種子先 render，refresh 後覆寫）
```

現有的 10 張卡（可當範本照抄）：
`aibubble / econ / rival / compute / racks / flows / cwengine / earnings / pricing / payback`

**最推薦照抄的範本：`pricing/` 和 `payback/`** — 它們是最新、最完整、
最標準的「KB + model + collectors + Claude/rules analysis + 密碼刷新」結構。

---

## 2. 一張卡的內部契約（檔案職責）

以 `pricing/__init__.py` 為標準樣板，每張卡的 `__init__.py` 一定要 export：

| 名稱 | 型別 | 用途 |
|---|---|---|
| `<name>_bp` | `Blueprint(name, __name__, url_prefix="/<name>")` | 掛載到 app |
| `load_snapshot()` | `() -> dict` | 讀快取；沒有就用種子算一份並存檔。**不可連網、要很快**（首頁會呼叫它） |
| `refresh()` | `() -> dict` | 抓 live + 重算（可含 Claude）+ 存檔。**慢、可連網**，只在刷新時呼叫 |

標準路由（照 `pricing` 抄）：

- `GET /<name>/` → `render_template("<name>.html", snapshot=json.dumps(load_snapshot()))`
- `GET /<name>/api/snapshot` → `jsonify(load_snapshot())`
- `POST /<name>/api/refresh` → 驗密碼後呼叫 `refresh()`，回傳摘要

各檔案分工：

- **`model.py`** — 純函式 `build_snapshot(kb, live=None, generated_at=..., today=...) -> dict`。
  不連網、可決定性（同輸入同輸出），所有分數/警報/結論都在這算。
- **`collectors.py`** — `fetch_bundle(kb) -> dict`，用 yfinance / 免金鑰 FRED 抓即時 proxy。
  失敗要能優雅退回（回 None / 部分），不可讓 app 掛掉。
- **`analysis.py`** — 把 model 算出的數字交給 Claude 寫敘事（L4/L5），
  **一定要有 deterministic rules 後備**，沒有 `ANTHROPIC_API_KEY` 也能完整運作。

---

## 3. 新增一張卡的精確步驟（checklist）

假設新卡叫 `foo`，網址 `/foo`。

### 3a. 建立卡資料夾
複製 `pricing/` 成 `foo/`，改名 `test_pricing.py → test_foo.py`，把裡面的
`pricing` 字串全換成 `foo`，`pricing_bp` 換成 `foo_bp`，`PRICING_REFRESH_PASSWORD`
換成 `FOO_REFRESH_PASSWORD`（預設 `foo2026`）。換掉 `knowledge_base.json` 內容、
`model.py` 的計算、`analysis.py` 的 schema/prompt。

### 3b. 接進 `app.py`（4 處，照現有卡的樣式各加一行/一段）

1. **import**（檔案上方那一串 import 之後）：
   ```python
   from foo import foo_bp
   from foo import load_snapshot as foo_load_snapshot
   from foo import refresh as foo_refresh   # 若有 live refresh
   ```
2. **註冊 blueprint**（一堆 `app.register_blueprint(...)` 之後）：
   ```python
   app.register_blueprint(foo_bp)
   ```
3. **登入守門的 API 前綴白名單**（`require_login()` 裡那個 `request.path.startswith((...))` 的 tuple，加一項）：
   ```python
   "/foo/api/",
   ```
4. **首頁 `portal()`** — 載入 snapshot 並把卡片要顯示的數字傳給 template：
   ```python
   try:
       foo_snap = foo_load_snapshot()
   except Exception:
       foo_snap = None
   # ...在 render_template("portal.html", ...) 的參數裡加：
   foo_updated=(foo_snap.get("as_of") if foo_snap else None),
   foo_score=((foo_snap.get("headline") or {}).get("score")) if foo_snap else None,
   ```

5. **（可選）排程**：只有「免費、可自動跑」的卡才加進 `_refresh_all_bg()`。
   會花 Claude 成本的卡（pricing / payback / flows）**故意不接排程**，只用
   手動密碼按鈕刷新，以控制成本。新卡若用 Claude，預設也走「手動刷新」。

### 3c. 接進金字塔首頁 `templates/portal.html` + `app.py` 的 `STRINGS`

1. 在 `app.py` 的 `STRINGS` dict 加這張卡的雙語字串（照 `pricing_name` /
   `pricing_desc` / `pricing_lbl` 的格式）：
   ```python
   "foo_name": {"en": "Foo Radar", "zh": "Foo 雷達"},
   "foo_desc": {"en": "...", "zh": "..."},
   "foo_lbl":  {"en": "/100", "zh": "/100"},
   ```
2. 在 `portal.html` 對應的 tier（金字塔某一層 `<div class="row nX">`）裡，
   複製一個 `<a class="card" href="/foo/">…</a>` 區塊，改 kicker / 名稱 / 統計欄位。
3. **金字塔分層**（macro → industry，越下面越寬）：
   - Tier I `n1`（1 張，max-width 300）= 總體經濟
   - Tier II `n2`（2 張）= 資金流向 / 泡沫溫度
   - Tier III `n3`（3 張）= AI 需求 / 回本
   - Tier IV `n4`（4 張）= 供應鏈 / 競爭策略
   把新卡放進**主題最相符**的那層。如果那層已滿（例如某層想從 3 張變 4 張），
   要把該 `row` 的 class 從 `n3` 改 `n4` 並調整 `max-width`（CSS 裡 `.row.n3/.n4` 有定義）。

### 3d. 環境變數（如果用 Claude / 密碼刷新）
- `.env.example` 與 `render.yaml` 各加一條 `FOO_REFRESH_PASSWORD`（`sync: false`）。
- Claude 共用既有的 `ANTHROPIC_API_KEY`，不用新增。

### 3e. 測試 + 本機驗證 + 部署
見 §5、§6。

---

## 4. 鐵則（CONVENTIONS — 一定要遵守）

1. **全雙語**：每個面向使用者的字串都是 `{"en": "...", "zh": "..."}`，
   中文用**繁體**。template 內用各卡自己的語言切換。
2. **Claude 一定要有規則後備**：所有 AI 敘事都要在沒有 `ANTHROPIC_API_KEY`
   時用 `model.py` 的數字 deterministically 生成。網站離線/沒金鑰也要 100% 可用。
3. **Claude 用法固定**（見 §7）：model = `claude-opus-4-8`、結構化輸出走
   `output_config` 的 `json_schema`、system block 加 `cache_control` 做 prompt cache。
4. **證據分級 + 不捏造**：策展資料標 `T1/T2/T3`，估計值要標 `is_estimate`，
   機密/不可知的數字**絕不編造**（pricing 卡有明確的反捏造 system prompt 可參考）。
5. **種子先行**：`data/<name>/snapshot.json` 要 commit 進 repo，讓卡在任何
   refresh 之前就能 render（Render 免費方案磁碟是 ephemeral，冷啟動會回到種子）。
6. **密碼刷新**：每張會花錢/抓 live 的卡有自己的 `<NAME>_REFRESH_PASSWORD`
   （程式碼預設 `<name>2026`），跟網站登入分開。
7. **測試**：`test_<name>.py` 用 **stdlib `unittest`，不連網、不依賴 pytest**。
   跑法：`python3 -m unittest <name>.test_<name> -v`。
8. **不要改別的卡**：新增平台應該是純加法。碰到要動 `portal.html` 的共用 CSS
   或 tier 結構時，要確認沒有破壞其他卡的版面。

---

## 5. Claude API 標準接法（直接照抄 `pricing/analysis.py`）

```python
MODEL = "claude-opus-4-8"

# 雙語欄位的 schema building block
_BI = {"type": "object", "additionalProperties": False,
       "properties": {"en": {"type": "string"}, "zh": {"type": "string"}},
       "required": ["en", "zh"]}

SCHEMA = { ... }     # 你這張卡要 Claude 回傳的結構（json_schema）

SYSTEM = """..."""   # 角色設定 + 反捏造硬規則 + 輸出要求

def _claude(kb, l3):
    import anthropic
    client = anthropic.Anthropic()               # 讀 ANTHROPIC_API_KEY
    msg = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        thinking={"type": "adaptive"},
        system=[{"type": "text", "text": SYSTEM,
                 "cache_control": {"type": "ephemeral"}}],   # prompt cache
        output_config={"format": {"type": "json_schema", "schema": SCHEMA},
                       "effort": "medium"},
        messages=[{"role": "user", "content": user_text}],
    )
    text = next((b.text for b in msg.content if b.type == "text"), "")
    return json.loads(text)

def analyze(kb, l3):
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return _claude(kb, l3)
        except Exception:
            log.exception("foo: Claude failed — falling back to rules")
    return _rules(kb, l3)     # ← 一定要有，純規則生成同樣結構
```

> 注意：這個 `output_config` / `thinking=adaptive` 是這個 codebase 用的較新介面，
> 請**沿用既有寫法**，不要改成舊版 `tools=[...]` 或 `response_format`。
> Codex 若不確定 SDK 細節，以 `pricing/analysis.py`、`payback/analysis.py` 現況為準。

---

## 6. 本機跑 / 測試 / 部署

**本機啟動**（從 repo 根目錄）：
```bash
cd macro-ai-monitor
pip install -r requirements.txt
SECRET_KEY=local APP_USERNAME=analyst APP_PASSWORD=preview123 PORT=5267 python3 app.py
# 開 http://localhost:5267  （登入 analyst / preview123）
```

**跑單張卡的測試**：
```bash
python3 -m unittest foo.test_foo -v
```

**重算所有種子快取**（會用到 `.env` 的金鑰）：
```bash
python3 scripts/refresh_all.py
```

**部署**：commit + push 到 `main`，Render 會自動建置部署。新卡若有新環境變數，
要在 Render dashboard 補設（`render.yaml` 標 `sync: false` 的不會自動帶值）。

---

## 7. 已知地雷（Codex 一定要知道）

1. **`bottleneck/` 目錄會撞名 pandas 的選用相依套件 `bottleneck`**，導致 app
   在本機 import pandas 時可能無法 boot。`scripts/refresh_all.py` 的作法是
   refresh 期間把 `bottleneck/` 暫時改名再還原。本機若遇到 import 錯誤，
   先確認是不是這個撞名。（`bottleneck/` 目前**沒有**註冊成 blueprint。）
2. **Render 免費方案磁碟 ephemeral**：冷啟動會丟掉 refresh 後的 `data/` 快取、
   回到 commit 進去的種子。所以「種子 snapshot 必須 commit」這條不能省。
3. **登入守門**：任何新的 `/foo/api/...` JSON 端點，若沒加進 `require_login()`
   的 API 前綴白名單，未登入時會被導去 HTML 登入頁而不是回 401 — 前端 fetch 會壞。
4. **首頁 `portal()` 對每張卡的 `load_snapshot()` 都包了 try/except**：
   你的 `load_snapshot()` 不可拋例外到讓整個首頁掛掉；快取壞掉要能回退種子。

---

## 8. 與 Claude Code 同時開發時的協作（避免 git 衝突）

你（Codex）和 Claude Code 可能會同時動到同一個 repo。降低衝突的作法：

- **動工前先 `git pull`**，完工後盡快 commit + push。
- 新平台幾乎都是**新檔案**（`foo/` 整包 + `templates/foo.html` + `data/foo/`），
  天生不衝突。唯一的共用檔是 `app.py`、`templates/portal.html`、`render.yaml`、
  `.env.example` — 改這幾個時**只加自己的區塊、不要重排或改別人的行**，
  conflict 機率就很低。
- 一張卡一個 commit，訊息寫清楚（例：`Add Foo Radar card (/foo)`），方便回溯。

---

## 9. 快速起步（給 Codex 的第一個任務範例）

> 「在 macro-ai-monitor 新增一張平台卡 `/foo`，主題是 ____。
> 照 `pricing/` 的結構建立 `foo/` package（`__init__.py` / `model.py` /
> `knowledge_base.json` / `test_foo.py`，需要的話加 `collectors.py` /
> `analysis.py`），把它 register 進 `app.py`、加進 `templates/portal.html`
> 金字塔第 __ 層、補 `STRINGS` 雙語字串，commit 一份種子 `data/foo/snapshot.json`，
> 寫好 unittest 並確認 `python3 -m unittest foo.test_foo` 通過。遵守
> CODEX_HANDOFF.md 的所有鐵則（雙語、Claude+rules 後備、種子先行、密碼刷新）。」
```
