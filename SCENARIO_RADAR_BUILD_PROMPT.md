# BUILD PROMPT — Scenario Radar（情境雷達 · 資本市場狀態機）

> 在已部署的 **macro-ai-monitor**（Flask, https://macro-ai-monitor.onrender.com/）金字塔頂層 **Tier I、與經濟面（econ）並排**，新增第 11 張卡 `/scenario`。它是 **capstone / meta 平台**：不自己抓 yfinance/FRED，而是**讀取金字塔下方所有 sibling 平台的 snapshot**，把整座金字塔的即時狀態合成成「資本市場下一步可能進入哪些狀態」的一組**機率分布**（probabilities sum to 100），並把每個機率**歸因**到是哪些 driver 訊號推上/拉下的。
>
> 這是「五輪 prompt 打磨後施工」的最終 build-ready 規格。**請完整照做，不要再問問題。**
>
> **最高優先指令（含一條重要例外）**：完全比照 `flows/`（Capital Flow Radar）這個 canonical sibling 的封裝、契約與 idiom——任何模稜兩可之處，一律以「flows 怎麼做、我就怎麼做」為準，做出來必須像 flows 同一位作者寫的。**唯一例外**：本平台是 meta/synthesis 平台，輸出是一組必須加總 100 的機率分布，而 flows 不做任何 Claude 輸出 sanitize。**「copy flows」指的是「複製 call 機制（client 參數、cache、thinking、structured output 形狀、_rules/analyze 結構），不是複製 SCHEMA 內文，也不是複製 flows 那種「不 sanitize」的 _claude()」。** 本平台在 §L4 明列的 `_claude()` 後處理（id 白名單 / renormalize / argmax 校驗 / baseline join）是**對 flows 的刻意 deviation，必做、不可省**。凡本文件與「copy flows」衝突處，**以本文件為準**。
>
> **風格守則**：所有 code-facing identifier / JSON key / schema 一律英文；UI 與註解雙語，中文用**繁體**；散文用「繁中夾英文技術詞」。

---

## 0. 不可違反的工程契約（Blueprint Contract — 逐字比照 flows）

新平台是一個自包含 Python package `scenario/`，對外只暴露三個東西，與 `flows` 一模一樣：

1. `scenario_bp` = `Blueprint("scenario", __name__, url_prefix="/scenario")`
2. `load_snapshot()` — 回傳 dict；先讀 `data/scenario/snapshot.json`，讀不到就用 **seed** 即時 `_compute()` 並落檔。**seed 必須能在沒有任何 API key、且任何 sibling 都讀不到時離線 render。`load_snapshot()` 絕不讀 siblings（只走 cache/seed），只有 `refresh()` 才讀 siblings 即時狀態。**
3. `refresh()` — 重新讀 siblings → 重算 L3 → 跑 L4/L5（Claude 或 rules）→ 落檔 → 回傳 snapshot。

**直接複製 `flows/__init__.py` 改名**：`PKG/KB/DATA/SNAPSHOT` 路徑常數、`_kb()`、`_now()`（`"%Y-%m-%d %H:%M UTC"`）、`_compute()`、`_save()`、`load_snapshot()`（先讀檔、讀不到 seed-compute 並 try/except 落檔）、三條 route、密碼 helper，全部照抄、只把 `flows→scenario`、`CAPFLOW→SCENARIO`、default `flows2026→scenario2026`。

三條 route 逐字對齊 `flows/__init__.py`：
- `GET  /scenario/` → `render_template("scenario.html", snapshot=json.dumps(load_snapshot(), ensure_ascii=False))`
- `GET  /scenario/api/snapshot` → `jsonify(load_snapshot())`
- `POST /scenario/api/refresh` → 密碼閘（`hmac.compare_digest`），成功才 `refresh()`，回 `{ok, generated_at, engine, ...}`；密碼錯回 `403 {"ok":false,"error":"wrong_password"}`；refresh 例外回 `500 {"ok":false,"error":"refresh_failed"}`。

```python
_DEFAULT_REFRESH_PW = "scenario2026"
def _refresh_password() -> str:
    return os.environ.get("SCENARIO_REFRESH_PASSWORD", "") or _DEFAULT_REFRESH_PW
```

**Refresh 政策**：密碼閘**手動按鈕 ONLY**，env `SCENARIO_REFRESH_PASSWORD`。**絕對不要**把 scenario 接進 `app.py` 的 weekly APScheduler（`_refresh_all_bg`）——刻意讓 Opus 成本 on-demand，與 flows/pricing/payback 一致。

### 0a. `_compute()` / `build_snapshot()` 的 seam（明確定義，flows 沒有對應 meta 形狀）

flows 是 `_compute(live=None)` → `build_snapshot(kb, live=live, ...)`。scenario 改成 **siblings dict**：

```python
def _compute(siblings=None):                       # 無參數 = seed/cold path
    return model.build_snapshot(_kb(), siblings=siblings,
                                generated_at=_now(), today=_today())

def refresh():
    siblings = _gather_siblings()                  # 真讀 siblings(見下),每條 lazy + try/except
    snap = _compute(siblings=siblings)             # siblings 為 dict → source 可能 "live"
    _save(snap)
    return snap
```

- `load_snapshot()`（cold / portal path）：先讀檔；讀不到 → `_compute()`（**無參數 → siblings=None → 純 seed**）並 try/except 落檔。**不 gather siblings。**
- `refresh()`：先 `_gather_siblings()` 再 `_compute(siblings=...)`。

**`_gather_siblings()` 回傳的 dict schema（`_extract_drivers` 消費的就是它）**：key = platform id，value = 該平台 `load_snapshot()` 回傳的 raw snapshot dict **或 None**（缺平台 / import 失敗 / loader 回 None）：

```python
def _gather_siblings():
    out = {}
    out["econ"]      = _safe(lambda: __import__("econ")._load_snapshot())   # 注意:econ 可能回 None(非 raise)
    out["aibubble"]  = _safe(lambda: __import__("aibubble").fetcher.load_snapshot())
    out["flows"]     = _safe(lambda: __import__("flows").load_snapshot())
    out["payback"]   = _safe(lambda: __import__("payback").load_snapshot())
    out["compute"]   = _safe(lambda: __import__("compute").load_snapshot())
    out["cwengine"]  = _safe(lambda: __import__("cwengine").load_snapshot())
    out["pricing"]   = _safe(lambda: __import__("pricing").load_snapshot())
    out["racks"]     = _safe(lambda: __import__("racks").load_snapshot())
    out["earnings"]  = _safe(lambda: __import__("earnings").load_snapshot())
    out["rival"]     = _safe(lambda: __import__("rival").load_kb())
    out["bottleneck"]= _safe(lambda: __import__("bottleneck").load_snapshot())  # OPTIONAL,可能 ImportError
    return out
# _safe 吞所有例外回 None;import 一律 lazy(在函式內),不可放 module top。
```

> **關鍵防呆**：`econ._load_snapshot()` 在 `latest.json` 不存在時**回傳 None（不是 raise）**，try/except 抓不到 None。因此 `_extract_drivers` 對每個 sibling **先做 `if snap is None` 判斷**，None 與「import 失敗」一視同仁 → driver `available=False`、`source="unavailable"`。

### 0b. envelope 簽名（明確列出 KEEP 與 DROP，不要「verbatim 全保留」）

`model.build_snapshot(kb, siblings=None, generated_at="", today="")` 回傳的 envelope **不是** flows envelope 的逐字複本。明確規定：

**KEEP（與 flows 同名同義，讓輸出 blob 與 sibling 可互換）**：
`generated_at, as_of, source, is_demo, title_en, title_zh, method_en, method_zh, l3, l4, l5, analysis_engine, blind_spots_en, blind_spots_zh, news, fetched_at`

**DROP（flows 有、scenario 無意義，絕不可加空殼）**：
`money_map`、`reservoirs`、任何 flows 專屬資金地圖欄位。**不要**因為「保留所有 flows key」就塞一個空的 `money_map`。

**ADD（本平台特有頂層 key）**：
`headline`（portal 卡片讀這裡）、`disclaimer_en`、`disclaimer_zh`、`prior_scenarios`。

語義：
- `source`：`"live"` 只要有任一 driver 真讀到 live（見 §L3-1 偵測表），否則 `"seed"`。
- `is_demo`：`siblings is None`（即 `load_snapshot` 的 cold path）→ `True`；`refresh()` 帶 siblings → 由是否有 live driver 決定（無任何 live 仍可 `is_demo=False` 但 `source="seed"`，比照 flows）。

---

## 1. 命名 / 路由 / kicker / CEO 一句問題

| 欄位 | 值 |
|---|---|
| route / url_prefix | `/scenario` |
| blueprint | `scenario_bp` |
| kicker（卡片 UPPERCASE id） | `SCENARIO` |
| name_en / name_zh | **Scenario Radar** / **情境雷達** |
| 副標 | Capital-Market State Machine ·「資本市場狀態機」 |
| Python 套件 | `scenario/`（mirror `flows/`） |
| refresh 密碼 env | `SCENARIO_REFRESH_PASSWORD`，default `scenario2026` |
| Claude model | `claude-opus-4-8` |

**它回答的一句 CEO 問題：**
> 「把下面所有平台的即時狀態合起來看，接下來資本市場最可能進入哪一種狀態？每種狀態機率多少、是被哪些訊號推上去的、而當這些平台彼此打架（divergence）時又代表什麼？」

> **誠實定位（必須出現在 UI 與 KB）**：這是 **model-assisted scenario probabilities，不是 market-implied probabilities**（不是選擇權市場隱含機率）。是「把金字塔多個平台的訊號，依規則 + Claude 合成出來的觀點機率」。

---

## 2. 金字塔擺位（Tier I，與 econ 並排）+ portal 接線

### 2a. Tier I 從 1 張變 2 張卡 → 用既有 `.row.n2`，零新增 CSS

**已驗證**（`templates/portal.html`）：Tier I 目前是 `<div class="row n1">`（line 223，`max-width:300px` 單欄）。`.row.n2`（line 134，`max-width:588px` 兩欄）已存在，且 line 178 / 181 的兩個 `@media` block **以名稱列舉** `.row.n1, .row.n2, .row.n3, .row.n4`，所以 `n2` 在 980px / 560px 斷點都已被涵蓋。

**指定做法（最高 fidelity、零風險）**：把 Tier I 的 `<div class="row n1">`（line 223）改成 **`<div class="row n2">`**，並在 **既有 econ `<a class="card">`（line 224）之前**插入 scenario 卡（**scenario 左、econ 右**——scenario 是「下一步的合成結論」，放左邊讀起來像冠頂結論；econ 是被給定的背景）。silhouette 變成 2→2→3→4，由 588→588→876→1164，仍是金字塔由窄而寬。

> **builder 注意（容易做反）**：必須是「改 `n1→n2` **且** 把新 `<a class="card">` 插在 econ 那個 `<a>` **之前**」。append 在 econ 之後（變成 econ 左、scenario 右）是**錯的 build**，與本規格的 scenario 左/econ 右順序相反。

> **嚴禁**新增任何自訂 apex/napex class（例如 `.row.napex`、`.row.n2.apex`）。理由：line 178/181 的 `@media` 只列舉 `.n1..n4`，任何**新 class 不會在 980px/560px 收合 → mobile 破版**。直接重用 `n2` 是唯一既優雅又已接好 RWD 的選擇。
>
> Tier I 的 `tier-head`（tier-no「I」、`tier1` / `tier1_sub`）以及金字塔的 `spine`/`axis`/`thesis` 全部**保持不動**——既有的中央 spine、頂端金色 axis chip、`.tier-no` 圓圈「I」三個視覺收斂裝置已足以把雙卡頂層錨定成尖端，不需改文案。

scenario 卡片 markup（插在 econ 卡之前，沿用既有 card 結構）：
```html
<a class="card" href="/scenario/">
  <div class="kicker">SCENARIO</div>
  <h2>{{ t('scenario_name') }}</h2>
  <div class="desc">{{ t('scenario_desc') }}{% if scenario_divergences %} · ⚠ {{ scenario_divergences }} {{ t('scenario_div_lbl') }}{% endif %}</div>
  <div class="foot">
    <div class="stat">
      {% if scenario_prob is not none %}<span class="score">{{ scenario_prob }}%<small>{{ scenario_base_label }}</small></span>{% endif %}
      {% if scenario_updated %}<span class="meta">{{ t('updated') }} · {{ scenario_updated }}</span>{% endif %}
    </div>
    <span class="enter">{{ t('enter') }} <span class="arr">→</span></span>
  </div>
</a>
```

### 2b. 卡片 headline stat（單一數字 + verdict + 鉤子）

卡片 `.score` 顯示**最高機率情境（base case）的機率 + 它的短名**，例如 `34% 後段見頂`：
- `scenario_prob` = `headline.base_prob`（整數）
- `scenario_base_label` = `headline.base_label.{zh|en}`（依 `ui_lang()`，截短 ≤6 字）
- desc 末尾掛 **`⚠ N 項背離`**（`headline.divergence_count`）——讓高層**還沒點進去**就感覺到「有事」。N=0 時不顯示。

> **committed seed 必須讓卡片冷啟動有值**：`data/scenario/snapshot.json` 種子**必須**已含一份自洽的 `headline.base_prob`（整數）、`headline.base_label.{en,zh}`、`headline.divergence_count`，這樣 fresh deploy（無 key、未 refresh）時 portal 卡片就能顯示 `34% 後段見頂`，不是空白。驗收清單會檢查此點。

### 2c. portal() 只讀 cached scenario snapshot，不 fan-out

`app.py portal()` 的 `scenario_load_snapshot()` 呼叫**只讀 `data/scenario/snapshot.json` 這一份 cached 檔**（§0a：`load_snapshot()` 不 gather siblings）。**絕不可**在 portal render 時觸發對 10 個 siblings 的 fan-out 讀取（econ snapshot 約 770KB，若每次首頁 render 都連鎖讀全家，會把 econ 的大檔讀放大 N 倍）。siblings 只在使用者按 refresh 時才被讀。

### 2d. `app.py` 接線（五處，精確到位）

**已驗證 app.py 觸點**：import 區 line 41–66、register line 74–83、before_request tuple line 205、portal() 讀 snapshot line 251–308、STRINGS line ~145。

1. **import**（接在 `from payback import ...` line 66 之後）：
```python
from scenario import scenario_bp
from scenario import load_snapshot as scenario_load_snapshot
# scenario refresh is manual-only (password-gated button), like flows/pricing/payback —
# NOT wired into the weekly scheduler (keep Opus cost on-demand).
```
2. **register**（接在 `app.register_blueprint(payback_bp)` line 83 之後）：
```python
app.register_blueprint(scenario_bp)
```
3. **before_request 白名單**：在 line 205 那串 `request.path.startswith((...))` tuple 末尾加 `"/scenario/api/"`。
4. **portal()**：仿其它 sibling 用 try/except 包起來（接在 payback 區塊後），並在 `render_template("portal.html", ...)` kwargs 加：
```python
try:
    scenario_snap = scenario_load_snapshot()
except Exception:
    scenario_snap = None
...
scenario_updated=(scenario_snap.get("as_of") if scenario_snap else None),
scenario_prob=((scenario_snap.get("headline") or {}).get("base_prob")) if scenario_snap else None,
scenario_base_label=(((scenario_snap.get("headline") or {}).get("base_label") or {}).get("zh" if ui_lang()=="zh" else "en")) if scenario_snap else None,
scenario_divergences=((scenario_snap.get("headline") or {}).get("divergence_count")) if scenario_snap else None,
```
5. **STRINGS dict**（放在 payback 那組附近，逐字比照雙語風格）：
```python
"scenario_name":{"en": "Scenario Radar", "zh": "情境雷達"},
"scenario_desc":{"en": "Synthesizes every platform below into the capital-market states that could come next — each with a model-assisted probability and the driver signals that move it.",
                 "zh": "把下方所有平台合成成接下來可能的資本市場狀態 — 每個狀態附 model-assisted 機率,以及推動它的 driver 訊號。"},
"scenario_div_lbl":{"en": "divergences", "zh": "項背離"},
```
> label 由 snapshot 帶 zh/en（不需額外 `scenario_lbl` STRINGS）。`tier1`/`tier1_sub`/`axis_top`/`thesis` **不改**（保持 flows-style minimalism）。

6. **`.env.example` 與 `render.yaml`**：各加一行 `SCENARIO_REFRESH_PASSWORD`，**完全比照** 既有 `CAPFLOW_REFRESH_PASSWORD` 的寫法（`.env.example` line 31 / `render.yaml` line 27 區塊）。**兩個檔都要加，不可只加一個。**
> ⚠ **已知前例破窗**：`render.yaml` 目前**漏了** `PAYBACK_REFRESH_PASSWORD`（只有 CAPFLOW/CWENGINE/EARNINGS/PRICING）。**不要**參照 payback 來抄 render.yaml（會複製這個遺漏）。請以 `CAPFLOW_REFRESH_PASSWORD` 為樣板，確認 `SCENARIO_REFRESH_PASSWORD` 同時出現在 `.env.example` 與 `render.yaml` 兩處。

### 2e. SDK 版本前提（影響本平台甚鉅）

本平台是金字塔中**最依賴大型 structured output** 的一張卡。`requirements.txt` 目前釘 `anthropic>=0.28`，該版本**早於** `output_config` / structured-outputs / `thinking={"type":"adaptive"}` API；若部署環境真的裝到 0.28，每次 refresh 都會**靜默 fallback 到 rules**。builder 須**確認 deployed env 的 anthropic SDK 版本支援這些參數**（flows/payback 已在用，代表線上版本夠新）；若不確定，把 floor 提到與 flows/payback 線上一致的版本（不要降版）。此為前置確認，不要默默假設。

---

## 3. 五層設計（FIVE-LAYER，改造成 meta / synthesis 平台）

flows 的精神：**L1+L2 = 策展 `knowledge_base.json`；L3 = `model.py` 純量化；L4+L5 = `analysis.py`（Claude Opus 4.8，structured output + prompt-cached system + thinking adaptive，一次 call）+ 永遠有 `_rules()` deterministic 後備。** 全部照搬，差別只在「L3 的原始輸入不是 yfinance，而是其它平台的 snapshot」。

### L1 — 策展情境空間（`knowledge_base.json` 的 `scenarios_seed`）

定義一組**盡可能正交、互斥、跨 near/mid/structural horizon** 的資本市場狀態，**固定 6 個、id 不變**（這樣 prior/affinity/attribution/drift/divergence 才能跨 refresh 對齊）。每個 seed 物件：

```json
{
  "id": "soft_landing_broadening",
  "horizon": "mid",                          // "near" | "mid" | "structural"
  "name_en": "Soft landing, AI broadens", "name_zh": "軟著陸 · AI 擴散",
  "prior": 18,                               // 整數;六個 prior 加總 = 100(static base rate);每個 >= 1
  "narrative_en": "...", "narrative_zh": "...",
  "market_path": {                           // 各資產類別會怎麼走(短語,雙語)
    "equities_en": "...", "equities_zh": "...",
    "rates_en": "...",    "rates_zh": "...",
    "credit_en": "...",   "credit_zh": "...",
    "ai_semis_en": "...", "ai_semis_zh": "..."
  },
  "triggers_en": ["..."], "triggers_zh": ["..."],
  "falsifiers_en": ["..."], "falsifiers_zh": ["..."],
  "foundry_read_en": "...", "foundry_read_zh": "...",  // 對 foundry/先進製程/TSMC 的意涵(中性、不具名)
  "affinity": {                              // L3 用:此情境偏好哪種 state-vector 方向,每個 ∈ [-1,+1]
    "macro_cycle": +1, "inflation_rates": -0.3, "bubble_heat": -1, "flow_direction": +1,
    "retail_inst": -0.4, "payback_health": +1, "pricing_power": +0.5,
    "compute_demand": +0.5, "wafer_demand": +0.5, "rival_pressure": -0.2
  }
}
```

**6 個 canonical 情境（id 固定，文字自撰，誠實、具體、中性、不具名；務必盡量 MECE，避免語意重疊）：**
1. `soft_landing_broadening`（mid）— 軟著陸、AI 受益面擴散、回本改善（risk-on, healthy）。
2. `goldilocks_melt_up`（near）— 流動性寬鬆 + 估值衝高的噴出行情（risk-on 但泡沫熱、晚期）。
3. `late_cycle_topping`（mid）— **AI 需求仍強，但 payback BURNING/INVESTING + bubble HOT + flows DRAINING** 的典型後段見頂格局。**這是最有價值的背離情境，務必明確點名**。
4. `growth_scare_rotation`（near）— 成長疑慮 / 高檔輪動，未崩但 risk-off 升溫。
5. `credit_liquidity_break`（structural/tail）— 信用利差走擴 / 流動性退潮 / regime break（risk-off）。
6. `ai_capex_air_pocket`（structural）— AI capex 主動收手（payback 太差、pricing 受擠壓、compute/wafer 動能轉弱）造成的產業向下重定價（對 foundry 最直接）。

`prior` 六個加總 = 100，是「沒有任何即時訊號時的 base rate」。**所有 `prior` 必須 ≥ 1**（L3 用 `ln(prior)`，不可為 0）。`affinity` 的 key 必須 ⊆ driver id 集合。

### L1b — KB `config` 區塊（所有可調 scalar 住 KB，不可 hardcode 在 model.py）

`knowledge_base.json` **必須**有一個頂層 `config` 物件，集中所有 L3 數學常數與 baseline（house style：living assumptions in KB，可調可追溯）。builder **不可**把這些寫死在 `model.py`——公式裡引用的是 `kb["config"][...]`：

```json
"config": {
  "evidence_gain": 1.5,            // softmax 的 k
  "coverage_power": 1.0,           // shrink = coverage ** coverage_power
  "compute_baseline_bn": 591,      // compute grand_total_end_year_usd_bn 的對照基準(MEMORY base-case)
  "compute_gain_k": 2.0,           // tanh((v/baseline-1)*k)
  "wafer_baseline_wpm": 22000,     // cwengine wafers_per_month 對照基準
  "wafer_gain_k": 2.0,
  "value_clamp": 1.0,              // driver value clamp 到 [-1,+1]
  "sensitivity_delta": 0.5         // sensitivity 一階擾動量 ±Δ
}
```
> 若 KB 缺 `config`，formula 會 crash；務必先建好這塊並給上述 seed 值。

### L2 — driver dictionary + sibling→driver 接線表（`knowledge_base.json` 的 `drivers`）

定義 **10 個主 driver**（state-vector 維度）+ 數個 context driver。每個 driver 明確記載它從**哪個 sibling 的 snapshot、哪個 JSON path** 取數、以及**如何 normalize 到 `[-1, +1]`**（`-1` = 對風險/AI 最不利，`+1` = 最有利）。每個 driver 帶 `seed`（離線預設 normalized 值 + 方向），任一 sibling 缺席時仍能 render。

```json
{
  "id": "macro_cycle",
  "name_en": "Macro cycle", "name_zh": "總體景氣循環",
  "source_platform": "econ",
  "note_en": "...", "note_zh": "...",
  "seed": { "value": 0.1, "available": true },
  "weight": 1.0
}
```

**接線表（builder 照此實作 `_extract_drivers()`，每一條都 try/except + `.get()` 鏈 + `if snap is None` 前置判斷，缺平台/缺欄位/None → `available:false`、`source:"unavailable"`、`value=seed.value`，並記到 `coverage.unavailable[]`）。下列 JSON path 全部已對真實 snapshot 驗證：**

| driver id | sibling | snapshot 來源（已在 `_gather_siblings` 取好,model 只讀 dict） | JSON path（已驗證） | normalize → [-1,+1]（+1 = risk-on/AI 利多） |
|---|---|---|---|---|
| `macro_cycle` | econ | `siblings["econ"]`（可能 None） | `indicators[]` 找 `id=="cfnai"` 讀 `latest_value`（活動指數）+ `id=="yield_curve_3m_10y"` 讀 `latest_value` | `tanh(cfnai)`；曲線轉正陡峭往正、倒掛往負；再讀**頂層** `tsmc_negative_count`>0 額外往負拉。clamp 到 `[-value_clamp,+value_clamp]`。 |
| `inflation_rates` | econ | 同上 | `indicators[]` 找 `id=="cpi"` 讀 **`changes["1y"]["pct"]`（YoY,非 1m）** + `id=="yield_curve_3m_10y"` | 通膨 YoY 再加速 / 利率壓力 → 負；`-tanh(...)`。**務必用 1y（YoY）那格,不是 1m（噪音）。** |
| `bubble_heat` | aibubble | `siblings["aibubble"]` | `scores.composite`（0–100）+ `scores.zone.key`（`alert`/`warm`/…，**注意 zone 只有 `key`/`zh`/`desc_zh`，無 `en`**） | `-(composite-50)/50`：越熱越負。`zone.key=="alert"` 再 −0.2。clamp。**英文 label 從 KB driver dict 取，不從 sibling 取（zone 無 en）。** |
| `flow_direction` | flows | `siblings["flows"]` | `l3.marginal_direction.score`（-100..100） | `score/100`。 |
| `retail_inst` | flows | 同上 | `l3.retail_vs_inst.divergence` + `l3.retail_vs_inst.warning`(bool) | `warning==true`（散戶過熱、機構撤）→ topping 訊號 → −0.4；否則 `-clamp(divergence/50,-1,1)` 輕微。 |
| `payback_health` | payback | `siblings["payback"]` | `headline.coverage`(0..1) + `headline.verdict_key`(`monetizing`/`investing`/`burning`) | `monetizing→+0.6`、`investing→0`、`burning→-0.6`，再 `+(coverage-0.3)*1.5`，clamp。 |
| `pricing_power` | pricing | `siblings["pricing"]` | `pricing_power.score`(0..100) + `pricing_power.verdict_key`(`defensible`/`neutral`/`squeezed`) | `(score-50)/50`；`squeezed`/`defensible` 各微調 ∓0.15。 |
| `compute_demand` | compute | `siblings["compute"]` | `headline.grand_total_end_year_usd_bn`（**canonical key，與 portal() 一致**）vs `config.compute_baseline_bn` | `tanh((v/baseline - 1)*compute_gain_k)`。無值可比 → `seed.value`(+0.2)。 |
| `wafer_demand` | cwengine | `siblings["cwengine"]` | `inference.wafers_per_month`（已驗證存在）vs `config.wafer_baseline_wpm` | `tanh((v/baseline - 1)*wafer_gain_k)`。 |
| `rival_pressure` | rival | `siblings["rival"]`（來自 `rival.load_kb()`） | `len(events)` + `research_date` 新鮮度 | 競爭/客戶流出壓力上升 → 負（對 foundry 不利）。無方向資訊時 seed=0、weight 低。 |

**每平台 live-vs-seed 偵測表（coverage 是 headline 誠實度指標,口徑必須可重現；7 個 sibling 沒有 `is_demo` flag,逐一定死規則,builder 不可自由臆測）：**

| platform | live 判定（否則 seed；snap 為 None → unavailable） |
|---|---|
| flows / payback / pricing | `snap.get("source")=="live"` 或 `snap.get("is_demo") is False` → `live`；否則 `seed` |
| compute / cwengine / racks | `snap.get("live_present") is True` → `live`；否則 `seed` |
| earnings | `snap.get("source")=="live"`（earnings 有 `source` 欄）→ `live`；否則 `seed` |
| econ | 無 demo flag → **snapshot 存在（非 None）且有 `date` 即視為 `live`**（present=live） |
| rival | `load_kb()` 有回傳且 `events` 非空 → `live`；否則 `seed` |
| bottleneck | OPTIONAL；讀到 → `live`；ImportError/None → `unavailable` |

> **builder 易錯點（務必避免）**：不要因為「多數 sibling 沒有 is_demo」就一律當 live → coverage 永遠 ~100% → coverage-shrink 永不啟動 → 整個「證據稀薄時收斂回 prior」的誠實機制**靜默失效**。一定要照上表逐平台判定。

**Context driver（低 weight，不主導機率，但進 narrative / early_warning）：**
- `catalyst_density`（from earnings `event_count`）：`min(event_count/20, 1)` 當作 0..1 catalyst-load，**不進 state-vector 加權**，只在 L4/L5 用來排「何時會驗證」。
- `supply_structure`（from racks `summary.n_systems`）：純 context，weight ≤ 0.2。

**`flows_self_scenarios`（reflexive 對賬,非 driver）**：見 L3 第 5 步。**映射不靠 flows 的 `l5.scenarios` 自由文字 name**（那是 Claude 改寫過、不穩定）：改讀 **flows `knowledge_base.json` 的 `scenarios_seed` 穩定 id（`continuation` / `rotation` / `blowoff` / `regime_break`）**，按 flows KB 的情境順序對位到 flows `l5.scenarios[]`（同序），再用穩定 id 映射到本平台情境（見 L3-5）。

**`bottleneck`（OPTIONAL）**：repo 有 `bottleneck/` 且其 `load_snapshot()` 存在（`bottleneck/__init__.py`），但**未在 app.py 註冊**，live 部署可能不存在。→ best-effort，`_gather_siblings` 已 `_safe` 包好，缺則 None → unavailable。**絕不可** import 失敗就 crash。

### L3 — 純量化 aggregator（`model.py`，deterministic，這是 rigor 核心）

**全部 sibling 的讀取已在 `_gather_siblings()`（§0a）完成並 lazy import；`model.py` 只消費傳入的 `siblings` dict，本身不 import siblings、不在 import-time 或 cold-start 讀 siblings**——避免 `app.py import scenario → scenario import 全家` 的 import-time 連鎖 boot 失敗（repo 已被 payback/pandas 撞名坑過）。

`build_snapshot(kb, siblings=None, generated_at="", today="")` 步驟：

**1. `_extract_drivers(kb, siblings)`** → `drivers[]`：依 L2 表，每個 `{id, name_en/zh, source_platform, value(-1..1), raw, available(bool), source("live"|"seed"|"unavailable"), label_en/zh}`。
- `siblings is None`（seed 模式）→ 所有 driver 直接用 `kb.drivers[].seed`，`source="seed"`、`available=seed.available`。
- `siblings` 為 dict → 對每個 driver：取 `snap = siblings.get(platform)`；**先 `if snap is None`** → `unavailable`、`value=seed.value`、記 `coverage.unavailable`；否則用 `.get()` 鏈抽欄位、normalize；live/seed 由 §L2 偵測表判定。任一抽取例外 → 同 unavailable 處理。

**2. state vector**：把主 driver 的 `value` 蒐成 dict `state = {driver_id: value}`。

**3. coverage（誠實性核心）**：
```
live_count    = # drivers with source=="live"
scoring_count = # 主 driver(排除 catalyst/context)        # 固定 = 10
coverage      = live_count / scoring_count               # ∈ [0,1]
```

**4. baseline 分布（決定論機率公式，這套就是無 key 時的最終答案，也是餵 Claude 的 seed）**：

把 prior 先正規化成和為 1（`prior_s ≥ 1` 已由 KB 保證）。對每個情境算：
```
evidence_s = Σ_d  affinity[s][d] * state[d] * weight[d]          # driver 證據(affinity·state 內積)
shrink     = coverage ** config.coverage_power                   # coverage_power default 1.0
logit_s    = ln(prior_s) + shrink * config.evidence_gain * evidence_s
w_s        = exp(logit_s)
p_s        = w_s / Σ w_s                                         # 唯一一次 softmax → 合法正分布
```
- `unavailable` driver 用 seed 值仍計入，但其 `evidence` 貢獻 ×0.5（記在 attribution）。
- **coverage-shrink 的意義（必做）**：coverage→0 時分布退回純 prior；coverage→1 時全用證據。**這就是「證據稀薄時不擺出假精度」的數學落地**，不是只貼標籤。
- 取整：把 `p_s*100` 過 **`renormalize_to_100()`**（見下），保證整數且加總 = 100。

**`renormalize_to_100(prob_floats: list[float]) -> list[int]`（單一共用 helper，rules 與 Claude 兩路都呼叫）**：
- **前置條件**：先把所有輸入 **clamp 到 ≥ 0**（softmax 恆正,但 sensitivity 擾動與 Claude 失序輸出可能給出 0/負/sum≠100；負數會破壞 largest-remainder 的加總不變式）。
- **全零輸入** edge case：若 clamp 後總和為 0 → 回傳 **prior 正規化後的整數分布**（不可除以 0）。
- 一般情況：依輸入比例 scale 到總和 100，`floor` 後餘額按小數部分由大到小逐一 +1；**平手 tie-break 按情境固定 canonical 順序（索引序）**——決定論，測試才穩。
- 保證：回傳 list 長度不變、每個 ≥ 0、`sum == 100`。

**5. flows 反身對賬（reflexive reconcile，避免重複計數）**：flows 的 `marginal_direction`/`retail_vs_inst` 已經是 `flow_direction`/`retail_inst` 兩個 driver 的來源。因此 **flows 自己的情境機率分布絕不平均進加權**（否則 double-count 資金訊號）。做法：
- 讀 flows KB `scenarios_seed` 的穩定 id（`continuation`/`rotation`/`blowoff`/`regime_break`），按序對位 flows live snapshot 的 `l5.scenarios[]`（同序取 prob），**不靠 l5 的自由文字 name**。
- 用穩定 id 映射到本平台情境：`continuation→soft_landing_broadening`、`blowoff→goldilocks_melt_up`、`regime_break→credit_liquidity_break`、`rotation→growth_scare_rotation`。
- 存進 `l3.cross_checks.flows_scenarios`，**當「外部對賬/鄰居意見」並排顯示**；若本平台 base case 與 flows 最高機率情境方向矛盾 → push 一筆 `reflexive_split` divergence。
- flows 缺席（None）時 `cross_checks.flows_scenarios = []`、note 標 unavailable。

**6. probability attribution（可稽核，非黑箱，必出）**：對每個情境輸出 `attribution[]` = 推升/拉低它的 top driver：`{driver_id, name_en/zh, source, contribution(signed_points), direction("up"|"down"), reason_en/zh}`，由步驟 4 每項 `affinity[s][d]*shrink*config.evidence_gain*state[d]*weight[d]` 排序取絕對值最大的 3–4 個。**`signed_points` 的口徑**：以「logit 貢獻換算的近似百分點」呈現（proxy），並在 `method_*` 與 UI 明說這是**近似歸因（proxy）非逐點精確**——誠實標明口徑，不假裝精確。

**7. divergence / coherence engine（招牌賣點）**：偵測「方向應一致卻矛盾」的跨平台背離，命中就 push `{key, severity("high"|"medium"|"low"), en, zh, platforms[]}`。至少實作：
- `late_cycle_topping`：`compute_demand>=0`（需求不弱）**且** `payback_health<0`（燒錢）**且** `bubble_heat<-0.2`（很熱）**且** `flow_direction<0`（撤離）→ severity high，文案點名「典型後段見頂」，並讓 `regime_key` 落在 `topping`。
- `demand_vs_payback_gap`：`compute_demand>0` 且 `payback_health<0`（量增但回本惡化）。
- `demand_vs_pricing`：`compute_demand>0` 但 `pricing_power<-0.3`（量增價殺）。
- `macro_vs_market`：`macro_cycle<0` 但 `flow_direction>0.3`（經濟轉弱但資金靠流動性硬撐）。
- `retail_euphoria`：flows `retail_vs_inst.warning==true`。
- 另算 `coherence_score`（0–100）= `100 − 正規化的可用 driver tilt 離散度`。低 = 平台彼此打架 = 值得注意（給高層的「盤面有多自洽」溫度計）。

**8. headline + drift**：
- `headline = {base_id, base_prob(int), base_label:{en,zh}, base_narrative:{en,zh}, regime_key(risk_on|mixed|risk_off|topping|melt_up|air_pocket), divergence_count, coherence_score, coverage, coverage_label("8/10 live"), confidence}`。`base_id` = 機率最高的情境。`confidence`：`high` if `coverage>=0.7 and coherence_score>=60`；`low` if `coverage<0.5 or coherence_score<40`；否則 `medium`。
- **probability drift**：`refresh()` 在重算前先讀現有 `snapshot.json` 的 base 分布，傳入並存進新 snapshot 的 `prior_scenarios`；每情境輸出 `prior_prob` 與 `drift = now_prob - prior_prob`。首次/無前值 → `prior_scenarios` = 各情境 `prior`、`drift = null`。

**9. sensitivity（what-would-move-probabilities）的一階近似（rules 版,務必在 state-vector 層擾動）**：對每個主 driver，**在 pre-softmax 的 state vector 上**加 ±`config.sensitivity_delta`（clamp），**重跑整個步驟 4（含 renormalize）**，量 base scenario 的 `base_prob` 變化量；挑影響最大的 3–5 個。**絕不可**在 renormalize 後的整數機率上直接加減（那是錯的；擾動必須發生在 softmax 之前）。

**10.** 呼叫 `analysis.analyze(kb, l3)` 拿 L4/L5（Claude 或 rules），組進 envelope（§0b）回傳。

### L4 — Claude 精修情境集與機率（`analysis.py`）

**比照 `flows/analysis.py` 的 call 機制**（`MODEL`、prompt-cached `SYSTEM`、`_fmt_l3()`、`_claude()`、`_rules()`、`analyze()`：有 key 試 Claude，except/無 key → rules）。`client.messages.create(...)` 參數照 flows，但 **`max_tokens=8000`**，且 `SCHEMA` 用本文件 §4 給的（**不是** flows 的 schema 內文）：

```python
msg = client.messages.create(
    model=MODEL,
    max_tokens=8000,                      # scenario 輸出大;flows 用 4000、payback 因 4000 截斷而升 8000。
    thinking={"type": "adaptive"},        # 8000 是 sibling 收斂值;若仍嫌不足,改用 SYSTEM 約束 narrative 長度,而非無限加 cap。
    system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
    output_config={"format": {"type": "json_schema", "schema": SCHEMA}, "effort": "medium"},
    messages=[{"role": "user", "content": user}],
)
text = next((b.text for b in msg.content if b.type == "text"), "")
data = json.loads(text)                   # 解析失敗(截斷)即靜默 fallback 到 _rules()
```

`_fmt_l3()` 把 L3 的 driver state vector（value/label/available/source）、baseline scenario 分布（id+prob+attribution）、divergences、coherence、coverage（live vs seed）、cross_checks.flows_scenarios、KB 的 narrative/triggers 全餵進去。

SYSTEM（prompt-cached，繁中 + English，無 hedging boilerplate）要點：
- 你是 cross-platform 情境綜合策略師；使用者已完成 L1–L3 並給你**決定性 baseline 分布 + driver state vector + divergences + attribution**；你的工作是 L4/L5。
- **以 baseline 分布為錨做有限校準**，保留同一組 6 個情境 id，機率**整數且加總 100**；每個情境附 `rationale_en/zh` 說明為何調高/調低（引用實際 driver 數字）。
- **divergence 不可被平均掉**：偵測到 `late_cycle_topping` 等要在 narrative 與 regime 明確點名其意義。
- 每個情境 `foundry_read_*` 必填（對 foundry/TSMC 的意涵）。
- **不可新增/刪除情境 id**（越界 id 會被 model 端丟棄）。
- 明確標示這是 **model-assisted、非 market-implied** 機率；coverage 低時語氣要保守。
- Chinese 用繁體;每段 ≤ ~110 words / 150 字;只輸出 structured object。

L4 輸出：校準後 6 情境機率（sum 100，附 rationale）、`base_case`（id + thesis 雙語 + confidence）、`tail_risk`（**偏低機率高衝擊**的尾部情境，通常 `credit_liquidity_break`/`ai_capex_air_pocket`，非單純 argmin）、`expected_market_path`（base case 下 equities/rates/credit/ai_semis 路徑短語雙語）。

**Claude 回來後的後處理（`_claude()` 必做,這是對 flows 的刻意 DEVIATION——flows 不 sanitize,本平台一定要；同時也是測試項。不可只靠 SYSTEM prompt 喊「加總 100」就當保證）**：
1. **id 白名單**：丟棄不在 KB canonical 6-id 集合的情境；缺的 canonical 情境用 L3 baseline 機率補回。
2. 過 `renormalize_to_100()` → 強制整數加總 100（即使 Claude 已自稱 100 也要過一次，因 LLM 非決定論）。
3. **baseline join（補 drift/delta UI 的資料來源,Claude schema 不含這兩欄,必須後處理注入）**：每個 `l4.scenarios` 依 `id` join 回 L3 baseline 的 prob，注入 `baseline_prob` 與 `delta_vs_baseline = prob - baseline_prob`。
4. **argmax 校驗**：`headline.base_id` 必須 == `argmax(l4.scenarios prob)`；若 Claude 的 `base_case.id` 與其自己機率的 argmax 矛盾，改寫 headline（與 base_case.id）對齊 argmax。

### L5 — 決策層（同一個 Claude call，與 flows 一樣 L4/L5 同 call）

`l5 = {watch[], falsification[], early_warning[], sensitivity[]}`：
- `watch[]`：要盯什麼（雙語 `{en,zh}`）。
- `falsification[]`：2–3 條會推翻 base case 的條件（雙語）。
- `early_warning[]`：5–7 條訊號，**依「誰最先亮」排序**（daily→weekly→monthly），各帶 `freq`（daily/weekly/monthly）與 `source_platform`（指出去哪張卡看）。
- `sensitivity[]`（what-would-move-probabilities）：3–5 條，`{driver_id, if_en/zh, moves_en/zh}`。rules 版用 §L3-9 的 state-vector 一階近似生成（pre-softmax 擾動、重跑分布、報 base_prob 差值）。

### `_rules()`（deterministic 後備，永遠先於 Claude 存在且可跑）

**直接用 L3 已算好的 `baseline_distribution`（含 attribution、prior、drift）+ divergences 組 L4/L5**，比照 flows `_rules()`：
- `base_case` = L3 headline top scenario；confidence 由 §L3-8 公式決定。
- `tail_risk` = divergence 觸發時指向對應負向情境（late_cycle/credit_break）；否則取機率不高但 affinity 偏負者。
- `falsification` / `early_warning` / `sensitivity` / `watch`：從 KB `triggers`/`falsifiers` + driver→platform 對應**模板化**生成（每筆雙語 + freq + source_platform）。
- `l4.scenarios` 直接取 L3 baseline（已含 `baseline_prob`==`prob`、`delta_vs_baseline`==0），過同一個 `renormalize_to_100()`。
- 回傳 `{"engine":"rules", "l4":{...}, "l5":{...}}`，與 Claude 同 schema。

---

## 4. 要建立的檔案（package layout 鏡像 flows/ 的 4-file shape）

```
scenario/
  __init__.py            # blueprint + load_snapshot/refresh/_compute/_gather_siblings + 3 routes + 密碼閘(複製 flows 改名)
  knowledge_base.json    # config + L1 scenarios_seed[6](含 affinity) + L2 drivers[10+] + title/method/disclaimer/blind_spots
  model.py               # L3: _extract_drivers(只讀傳入 siblings dict,含 None-check) + build_snapshot + renormalize_to_100 + coverage-shrink + softmax + attribution + divergence + drift + sensitivity
  analysis.py            # L4/L5: MODEL/SCHEMA(本文件 §4)/SYSTEM/_fmt_l3/_claude(max_tokens=8000 + 後處理 sanitize/renormalize/baseline-join/argmax)/_rules/analyze
  test_scenario.py       # stdlib unittest, seed-based, no network/no key
templates/
  scenario.html          # 單一 snapshot JSON blob 注入 + client-side LANG toggle(比照 flows.html)
data/scenario/
  snapshot.json          # committed seed(含自洽 headline.base_prob/base_label,離線可渲染、portal 卡片冷啟有值)
```

> **嚴格維持 flows 的 4-file shape**：L3 全部折進 `model.py`，**不要**另開 `aggregator.py` 或 `collectors.py`（那會讀起來像不同作者）。`_gather_siblings()` 放 `__init__.py`（屬 refresh 路徑）。

**改動既有檔**：`app.py`（§2d 五處 + STRINGS）、`templates/portal.html`（Tier I `n1→n2` + 插入 scenario 卡，§2a）、`.env.example` + `render.yaml`（§2d-6）、必要時 `requirements.txt` anchor 確認（§2e）。

### Claude json_schema 形狀（`analysis.py` 的 `SCHEMA`，`additionalProperties:False`、機率整數）

> ⚠ **此 SCHEMA 與 flows 的 SCHEMA 不同**。「copy flows」只複製 call 機制，**SCHEMA 內文用下面這份**。本平台相對 flows 的 divergence 欄位：`l4.scenarios` 的 `id`/`rationale` 形狀、`base_case`、`tail_risk`、`expected_market_path`、`l5.early_warning.source_platform`、`l5.sensitivity` 全是 scenario-only。**不要**因「copy flows schema」而漏掉 `early_warning.source_platform`。

```jsonc
{
  "type":"object","additionalProperties":false,
  "properties":{
    "l4":{"type":"object","additionalProperties":false,"properties":{
      "scenarios":{"type":"array","items":{"type":"object","additionalProperties":false,"properties":{
        "id":{"type":"string"},
        "name_en":{"type":"string"},"name_zh":{"type":"string"},
        "prob":{"type":"integer"},
        "rationale_en":{"type":"string"},"rationale_zh":{"type":"string"}
      },"required":["id","name_en","name_zh","prob","rationale_en","rationale_zh"]}},
      "base_case":{"type":"object","additionalProperties":false,"properties":{
        "id":{"type":"string"},
        "thesis_en":{"type":"string"},"thesis_zh":{"type":"string"},
        "confidence":{"type":"string","enum":["high","medium","low"]}
      },"required":["id","thesis_en","thesis_zh","confidence"]},
      "tail_risk":{"type":"object","additionalProperties":false,"properties":{
        "id":{"type":"string"},"why_en":{"type":"string"},"why_zh":{"type":"string"}
      },"required":["id","why_en","why_zh"]},
      "expected_market_path":{"type":"object","additionalProperties":false,"properties":{
        "equities_en":{"type":"string"},"equities_zh":{"type":"string"},
        "rates_en":{"type":"string"},"rates_zh":{"type":"string"},
        "credit_en":{"type":"string"},"credit_zh":{"type":"string"},
        "ai_semis_en":{"type":"string"},"ai_semis_zh":{"type":"string"}
      },"required":["equities_en","equities_zh","rates_en","rates_zh","credit_en","credit_zh","ai_semis_en","ai_semis_zh"]}
    },"required":["scenarios","base_case","tail_risk","expected_market_path"]},
    "l5":{"type":"object","additionalProperties":false,"properties":{
      "watch":{"type":"array","items":{"type":"object","additionalProperties":false,
        "properties":{"en":{"type":"string"},"zh":{"type":"string"}},"required":["en","zh"]}},
      "falsification":{"type":"array","items":{"type":"object","additionalProperties":false,
        "properties":{"en":{"type":"string"},"zh":{"type":"string"}},"required":["en","zh"]}},
      "early_warning":{"type":"array","items":{"type":"object","additionalProperties":false,
        "properties":{"en":{"type":"string"},"zh":{"type":"string"},"freq":{"type":"string"},"source_platform":{"type":"string"}},
        "required":["en","zh","freq","source_platform"]}},
      "sensitivity":{"type":"array","items":{"type":"object","additionalProperties":false,
        "properties":{"driver_id":{"type":"string"},"if_en":{"type":"string"},"if_zh":{"type":"string"},"moves_en":{"type":"string"},"moves_zh":{"type":"string"}},
        "required":["driver_id","if_en","if_zh","moves_en","moves_zh"]}}
    },"required":["watch","falsification","early_warning","sensitivity"]}
  },"required":["l4","l5"]
}
```

> Claude 回的 `l4.scenarios` 是調整後機率;**attribution、prior、affinity、drift、baseline_prob/delta_vs_baseline 由 L3 + `_claude()` 後處理計算並掛在 snapshot;Claude 本身不產這些欄位**;前端把 `l4.scenarios.prob` 與 L3 的 attribution 以 `id` join。

### snapshot JSON shape（`build_snapshot` 回傳;committed seed 同形）

```jsonc
{
  "generated_at":"2026-06-23 12:00 UTC", "as_of":"2026-06-23",
  "source":"seed", "is_demo":true,                  // "live"|"seed"
  "analysis_engine":"rules",                        // "claude"|"rules"
  "title_en":"Scenario Radar","title_zh":"情境雷達",
  "method_en":"Model-assisted scenario probabilities synthesized from the platforms below — NOT market-implied. Probabilities are attributed to pyramid drivers (proxy attribution).",
  "method_zh":"由下方各平台合成的 model-assisted 情境機率,非市場隱含機率。每個機率歸因到金字塔 driver(近似歸因)。",
  "disclaimer_en":"Model-assisted scenario probabilities — NOT market-implied.",
  "disclaimer_zh":"模型輔助情境機率 — 非市場隱含機率。",
  "headline":{
    "base_id":"late_cycle_topping","base_prob":34,
    "base_label":{"en":"Late-cycle topping","zh":"後段見頂"},
    "base_narrative":{"en":"...","zh":"..."},
    "regime_key":"topping",                         // risk_on|mixed|risk_off|topping|melt_up|air_pocket
    "divergence_count":2, "coherence_score":58,
    "coverage":0.8, "coverage_label":"8/10 live", "confidence":"medium"
  },
  "l3":{
    "drivers":[ {"id":"macro_cycle","name_en":"...","name_zh":"...","source_platform":"econ",
                 "value":0.1,"raw":"...","label_en":"...","label_zh":"...","available":true,"source":"seed"}, ... ],
    "state_vector":{ "macro_cycle":0.1, ... },
    "coverage":{"live_count":0,"seed_count":10,"unavailable":[],"coverage":0.0},
    "coherence_score":58,
    "scenarios":[ {"id":"late_cycle_topping","name_en":"...","name_zh":"...","horizon":"mid",
                   "prob":34,"prior":17,"prior_prob":30,"drift":4,
                   "attribution":[ {"driver_id":"payback_health","name_en":"...","name_zh":"...","source":"live","contribution":-6,"direction":"down","reason_en":"...","reason_zh":"..."}, ... ],
                   "narrative":{"en":"...","zh":"..."},"market_path":{...},
                   "triggers":{"en":[...],"zh":[...]},"falsifiers":{"en":[...],"zh":[...]},
                   "foundry_read":{"en":"...","zh":"..."} }, ... ],   // 6 個, prob 加總 100
    "divergences":[ {"key":"late_cycle_topping","severity":"high","en":"...","zh":"...","platforms":["compute","payback","aibubble","flows"]}, ... ],
    "cross_checks":{ "flows_scenarios":[ {"mapped_id":"...","name_en":"...","name_zh":"...","prob":48} ], "note_en":"flows' own distribution — same-source cross-check, not weighted in.","note_zh":"flows 自身分布 — 同源對賬,未計入加權。" }
  },
  "l4":{ "scenarios":[ {"id":"...","prob":34,"baseline_prob":34,"delta_vs_baseline":0, "rationale_en":"...","rationale_zh":"..."} ], "base_case":{...}, "tail_risk":{...}, "expected_market_path":{...} },
  "l5":{ "watch":[...], "falsification":[...], "early_warning":[...], "sensitivity":[...] },
  "prior_scenarios":[ {"id":"...","prob":30}, ... ],
  "blind_spots_en":[...],"blind_spots_zh":[...],
  "news":[], "fetched_at":null
}
```

### 前端 UX（`templates/scenario.html`）— 比照 flows.html，整頁由單一 `snapshot` JSON render + client-side LANG toggle（不重打 API）

沿用 portal 的 dark-luxe gold 設計變數。由上而下：

1. **頂部 disclaimer 橫條（永遠可見、不可摺疊）**：`disclaimer_*`「model-assisted, not market-implied」。右上 `analysis_engine` 徽章（Claude/Rules）+ `source` 徽章（LIVE/SEED）。
2. **Hero verdict**：大字一句 base case（`base_narrative`）+ 機率環（donut）顯示 `base_prob%` + regime 燈號（risk_on 金 / mixed 中性 / risk_off 藍紫，用 `--gold`/`--accent`）+ **confidence 徽章** + **coverage 徽章**（`8/10 live`）。**coverage < 0.5 時整條變黃並加一句**「證據覆蓋偏低,分布已向先驗收斂,請降低對精確機率的信任」。
3. **背離面板（招牌賣點,放顯眼處,緊接 Hero）**：`l3.divergences` 列表 + `coherence_score` 溫度計（越低越多內部矛盾）。每條小卡 severity 著色（high 紅/medium 琥珀/low 灰）+ 涉及 `platforms[]` chips（可點連到對應卡）。`late_cycle_topping` 命中時最醒目。頂部一句總結「N 項跨平台背離 — 最值得注意:{最高 severity}」。
4. **情境機率排行（probability bars,centerpiece）**：6 情境由高到低,水平長條（寬=prob%）+ 機率數字 + **drift chip**（`+5pt`/`-3pt` vs 上次,綠/紅,首次「new」）+ horizon tag（near/mid/structural）。base case 金色描邊。
5. **情境 drill-down**（點開）：narrative、market_path（equities/rates/credit/ai_semis 四列）、triggers、falsifiers、**`foundry_read`（金色框強調「干我何事」）**、以及 **probability attribution 小卡**（哪些 driver 把它推上/拉下,雙向 +/− 條,標來源平台 chip + `source` 徽章）。標題「為什麼是這個機率」。
6. **Driver / live-vs-seed 透明表**：10+ driver 一覽,每列 value（-1..+1 小條）+ 來自哪張卡（可點 `/econ/`、`/flows/`…）+ **live/seed/unavailable badge**（🟢/🟡/⚪）。底部「live N / seed M / unavailable K, coverage = X%」。
7. **Cross-check（reflexive）**：把 `l3.cross_checks.flows_scenarios` 並排,標「flows 自身分布(外部對賬,未計入加權)」+ `note`。
8. **L5 決策層**：watch / falsification / early_warning（依 freq 排序、附 source_platform 連結）/ sensitivity（what-moves-probabilities）。
9. **footer + Refresh**：`method_*` 全句、`generated_at`、密碼輸入框 + POST `/scenario/api/refresh`（比照 flows.html refresh widget;成功 reload;錯密碼顯示錯誤）。

> RWD：機率條、歸因條、背離卡在窄螢幕全部 stack;donut 縮小置中。

---

## 5. 測試計畫（`test_scenario.py`，stdlib `unittest`，seed-based，無 network / 無 key，目標 14–18 個）

比照 `flows/test_flows.py` 風格：`from scenario import _compute, _kb, model, analysis`;`setUp`/`tearDown` 用 `os.environ.pop("ANTHROPIC_API_KEY", None)` 確保 rules 路徑。**用注入假 driver dict / 假 siblings dict 測 model,不真讀 siblings。** 必含：

- **KB well-formed**：`scenarios_seed` 恰 6 個、id 唯一、`prior` 加總==100、每個 `prior>=1`、`name/narrative/triggers/falsifiers/foundry_read/market_path/affinity` 齊備且雙語非空;`affinity` 的 key ⊆ driver id 集合;`drivers` 每個有 `seed.value`、`source_platform` 合法;**`config` 區塊存在且含 `evidence_gain/coverage_power/compute_baseline_bn/wafer_baseline_wpm/sensitivity_delta`**。
- **bilingual parity**：每個 scenario/driver/divergence 模板字串 en 與 zh 同時非空;`blind_spots_en` 與 `blind_spots_zh` 等長。
- **`renormalize_to_100`**：多組浮點輸入恆 `sum==100`、長度不變、tie-break 決定論（含 `[33.33,33.33,33.34]` edge、**含一個含 0 的 list、一個 sum>100 的 list、一個全 0 的 list（回 prior 正規化）、一個含負值的 list（先 clamp）**,同輸入兩次結果一致）。
- **`_compute()` seed shape**：無參數 `_compute()` 含 `headline,l3,l4,l5,analysis_engine,as_of,disclaimer_en`;`is_demo==True`、`source=="seed"`、`analysis_engine=="rules"`;**envelope 不含 `money_map`/`reservoirs`**。
- **probabilities sum to 100（硬性,兩處）**：`sum(s["prob"] for s in snap["l3"]["scenarios"])==100`;且 `analysis.analyze()` 回的 `l4.scenarios` 經後處理後加總==100。
- **base case 一致**：`headline.base_id == argmax(l3.scenarios prob)`;`headline.base_prob` 相符。
- **attribution 可稽核**：每個 scenario `attribution` 非空、`driver_id` 都屬於 driver 集合、`contribution` 為數值、`direction ∈ {up,down}`。
- **coverage-shrink**：高 coverage（多 live）vs 低 coverage（多 seed/unavailable）下,低 coverage 的分布**更接近 prior**（用與 prior 的 L1 距離斷言）。**這條若失敗通常代表 live-detection 把所有 sibling 誤判成 live → coverage 永遠 1。**
- **live-vs-seed 偵測**：餵一個 `is_demo=True` 的 flows snap → 該 driver `source=="seed"`;餵 `source=="live"` 的 → `source=="live"`;餵 `econ` 有 `date` 的 dict → `live`;餵 `compute` `live_present=False` → `seed`。coverage 數字隨之正確。
- **None sibling 防呆**：`siblings={"econ":None, ...}`（模擬 `econ._load_snapshot()` 回 None）→ 該 driver `unavailable`,不 crash。
- **missing sibling degrades gracefully（硬性）**：傳入只給部分平台、其餘 key 缺或 None 的 `siblings` dict（或某 loader 丟例外）→ `build_snapshot` **不得 crash**;該 driver `available==False`、`source=="unavailable"`、進 `coverage.unavailable`;scenarios 仍加總 100。
- **all siblings missing**：`siblings=None`（純 seed）→ `live_count==0`、分布≈prior 正規化、`confidence=="low"`、仍加總 100。
- **rules contract**：無 key 時 `analyze()` 回 `engine=="rules"`,`base_case.confidence ∈ {high,medium,low}`,`l5.watch/falsification/early_warning/sensitivity` 皆非空且雙語,`early_warning` 每筆 `freq ∈ {daily,weekly,monthly}` 且 `source_platform` 非空。
- **divergence rule**：構造 `compute_demand>=0 & payback_health<0 & bubble_heat<-0.2 & flow_direction<0` state → `late_cycle_topping` divergence（severity high）被 push、`regime_key` 落在 `topping`。
- **reflexive reconcile**：餵假 flows snap（KB id `regime_break` 對位的 `l5.scenarios` 機率最高）→ flows 分布進 `cross_checks.flows_scenarios`、**未直接平均進加權**、機率仍加總 100。
- **Claude sanitize（不打網路,直接餵假 Claude dict 給 `_claude` 後處理函式）**：餵一個 **sum≠100 且含未知 id** 的假 Claude `data` → 未知 id 被丟棄、缺的 canonical 情境用 baseline 補回、過 `renormalize_to_100` 後 `sum==100`、每個 `l4.scenario` 有 `baseline_prob`/`delta_vs_baseline`、`headline.base_id == argmax(l4 prob)`。**此測試是 Claude 路徑唯一會被驗到的地方（seed 測試走 rules,永遠 100,驗不出 sanitize），務必涵蓋。**
- **sensitivity 一階**：sensitivity 條目的擾動在 state-vector 層、重跑分布;斷言 `moves_*` 數值方向合理（如把 `payback_health` 往 + 擾動會降低 `late_cycle_topping` 機率）。
- **drift**：給定 `prior_scenarios` → 每情境 `drift` 計算正確;無 prior → `drift is None`。

執行：`python3 -m unittest scenario.test_scenario -v`。**全部離線、零 network、零 API key。**

---

## 6. 誠實 / 嚴謹要求（必做,逐項落地）

1. **model-assisted, not market-implied**：`disclaimer_*` 橫條永遠可見;`method_*`、Hero 角落、KB、Claude SYSTEM 皆標明。**不要**暗示是選擇權市場隱含機率。
2. **機率加總 100 可證**：rules 與 Claude 兩路都過同一個 `renormalize_to_100()`（largest-remainder + 決定論 tie-break + clamp≥0 + 全零回 prior）;兩處（l3.scenarios、l4.scenarios）測試硬性驗證。**不可只靠 SYSTEM prompt 喊 100。**
3. **機率可稽核（非黑箱）**：每情境帶 `attribution[]`,明列哪個 driver 推上/拉下幾 pt、來自哪個平台、live/seed/unavailable;`signed_points` 口徑（proxy）在 method/UI 誠實標明。
4. **live-vs-seed 證據分級 + coverage-shrink**：每 driver 依 §L2 偵測表標 `available`/`source`（不可一律當 live）;coverage 低 → 分布自動向 prior 收斂（數學落地,非僅標籤）+ confidence 降級 + UI 黃色 banner。**避免 false precision**。
5. **背離是最有價值訊號**：`divergences[]` 明確點名跨平台矛盾,尤其 late-cycle topping(需求強+燒錢+泡沫熱+資金撤離)醒目呈現;`coherence_score` 給溫度計。
6. **deterministic rules 後備自洽**：無 key 時 `_rules()` + L3 分布即完整可用產品,同一份分布就是餵 Claude 的 seed（flows 雙用法）。
7. **reflexive 不重複計數**：flows 的 driver(marginal_direction/divergence)只計入一次;flows 自身情境分布當外部對賬(`cross_checks`,經穩定 KB id 映射、非自由文字 name)、**絕不平均進加權**;矛盾記為 divergence。
8. **degrade gracefully**：每個 sibling 讀取 lazy import + `_safe` + try/except + None-check（含 OPTIONAL bottleneck、含 econ 回 None）;任一平台缺失只讓對應 driver 變 unavailable,**絕不 crash**——完全比照 `app.py portal()` 容錯。

---

## 7. 驗收清單（builder 自查）

- [ ] `python3 -m unittest scenario.test_scenario -v` 全綠（含「機率加總 100」「missing sibling 優雅降級」「Claude sanitize 後處理」「coverage-shrink」四條硬性測試,離線無 key）。
- [ ] `python3 app.py` 能 boot（無 import-time 連鎖失敗;scenario 的 sibling import 全部 lazy）;`/` 首頁 Tier I 出現 **scenario(左) + econ(右) 兩張卡並排**,silhouette 仍是金字塔(588→588→876→1164),mobile 980/560 斷點不破版（沿用既有 n2 規則）。
- [ ] portal 卡片在 **fresh deploy（無 key、未 refresh、純 committed seed）** 時即顯示 `34% 後段見頂`（或 seed 對應值）+ `⚠ N 項背離`（若有）——卡片 stat 來自 committed seed 的 `headline`,非空白。
- [ ] portal() render 時**只讀** `data/scenario/snapshot.json` 一份檔,**不**對 10 個 siblings fan-out。
- [ ] `/scenario/` 在**無 ANTHROPIC_API_KEY、且故意讓所有 sibling 讀取失敗/回 None**時仍能 render（純 seed）,徽章顯示 RULES + SEED。
- [ ] 正常情況（按 refresh）讀到各 sibling 即時狀態,driver panel live badge、divergence/attribution/drift/coherence 都有資料;coverage 數字隨 live-detection 正確變動（非永遠 100%）。
- [ ] `POST /scenario/api/refresh`（正確密碼 `scenario2026`）成功重算落檔;錯密碼回 403;refresh 例外回 500。
- [ ] snapshot `l3.scenarios` 與 `l4.scenarios` 機率皆加總 100;`headline.base_id == argmax`;`l4.scenarios` 每筆有 `baseline_prob`/`delta_vs_baseline`。
- [ ] 全站雙語可切(繁體),無公司名,入口低調。
- [ ] **未**接進 weekly APScheduler（`_refresh_all_bg` 不含 scenario）;`SCENARIO_REFRESH_PASSWORD` 已加入 `.env.example` **與** `render.yaml`**兩處**（注意 render.yaml 既有漏 PAYBACK 的前例,別跟著漏）。
- [ ] package 維持 flows 4-file shape（`__init__.py`/`knowledge_base.json`/`model.py`/`analysis.py`/`test_scenario.py`）;`knowledge_base.json` 含 `config` 區塊;所有 sibling import lazy 且 `_safe` 包覆;envelope 不含空殼 `money_map`。
- [ ] anthropic SDK 版本確認支援 `output_config`/`thinking=adaptive`（否則 refresh 靜默退 rules）。