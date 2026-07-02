"""AI Bubble Monitor — 監測宇宙、評分權重與靜態內容設定。

六大訊號維度：
  1. 巨頭資本支出強度（hyperscaler capex）
  2. AI 核心股估值熱度
  3. 價格動能過熱
  4. 基礎設施溢價（電力 + 資料中心 REIT）
  5. 信用與投機溫度
  6. 市場集中度
全部以免費資料源計算：yfinance（行情/基本面/現金流量表）、
Google News RSS（繁中）、FRED（選配，HY OAS 利差）。
"""

# ── 監測分組（儀表板卡片區，依序顯示） ──
GROUPS = [
    {
        "key": "mega",
        "zh": "雲端巨頭（CapEx 引擎）",
        "en": "Hyperscalers",
        "desc_zh": "AI 基建投資的金主——資本支出趨勢是本平台核心訊號",
        "tickers": [
            {"ticker": "MSFT",  "zh": "微軟",     "tag_zh": "Azure / OpenAI 最大金主"},
            {"ticker": "GOOGL", "zh": "Alphabet", "tag_zh": "GCP / Gemini / TPU 自研"},
            {"ticker": "AMZN",  "zh": "亞馬遜",   "tag_zh": "AWS / Trainium 自研"},
            {"ticker": "META",  "zh": "Meta",     "tag_zh": "開源 Llama / 自用算力"},
            {"ticker": "ORCL",  "zh": "甲骨文",   "tag_zh": "OCI / 舉債擴建疑慮焦點"},
        ],
    },
    {
        "key": "core",
        "zh": "AI 算力核心股",
        "en": "AI Compute Core",
        "desc_zh": "晶片與算力供應鏈——估值與動能是泡沫溫度的直接讀數",
        "tickers": [
            {"ticker": "NVDA", "zh": "輝達",       "tag_zh": "AI 晶片霸主 / 泡沫論主角"},
            {"ticker": "TSM",  "zh": "台積電 ADR", "tag_zh": "先進製程獨家代工"},
            {"ticker": "AVGO", "zh": "博通",       "tag_zh": "ASIC / 網通晶片"},
            {"ticker": "AMD",  "zh": "超微",       "tag_zh": "GPU 第二供應商"},
            {"ticker": "MU",   "zh": "美光",       "tag_zh": "HBM 記憶體"},
            {"ticker": "ASML", "zh": "艾司摩爾",   "tag_zh": "EUV 微影設備"},
            {"ticker": "ANET", "zh": "Arista",     "tag_zh": "AI 資料中心網路"},
            {"ticker": "SMCI", "zh": "美超微",     "tag_zh": "AI 伺服器 / 高波動"},
            {"ticker": "ARM",  "zh": "安謀",       "tag_zh": "IP 授權 / 高估值指標"},
            {"ticker": "VRT",  "zh": "Vertiv",     "tag_zh": "資料中心電力散熱設備"},
        ],
    },
    {
        "key": "neocloud",
        "zh": "新雲與 AI 應用",
        "en": "Neoclouds & AI Apps",
        "desc_zh": "舉債買 GPU 的新雲 + 高估值 AI 應用——投機溫度最敏感的一群",
        "tickers": [
            {"ticker": "CRWV", "zh": "CoreWeave", "tag_zh": "GPU 租賃 / 債務融資指標"},
            {"ticker": "NBIS", "zh": "Nebius",    "tag_zh": "歐系新雲"},
            {"ticker": "PLTR", "zh": "Palantir",  "tag_zh": "AI 應用估值風向標"},
            {"ticker": "AI",   "zh": "C3.ai",     "tag_zh": "企業 AI / 上一輪炒作殘留"},
        ],
    },
    {
        "key": "power",
        "zh": "電力與能源鏈",
        "en": "Power & Energy",
        "desc_zh": "AI 資料中心吃電——電力股相對公用事業的超額漲幅 = 基礎設施溢價",
        "tickers": [
            {"ticker": "VST",  "zh": "Vistra",        "tag_zh": "獨立發電 / AI 電力首選"},
            {"ticker": "CEG",  "zh": "Constellation", "tag_zh": "核電 / 微軟三哩島合約"},
            {"ticker": "NRG",  "zh": "NRG Energy",    "tag_zh": "獨立發電"},
            {"ticker": "TLN",  "zh": "Talen",         "tag_zh": "核電直供 AWS"},
            {"ticker": "GEV",  "zh": "GE Vernova",    "tag_zh": "燃氣輪機 / 電網設備"},
            {"ticker": "OKLO", "zh": "Oklo",          "tag_zh": "SMR 小型核電 / 無營收投機標的"},
            {"ticker": "SMR",  "zh": "NuScale",       "tag_zh": "SMR 概念"},
            {"ticker": "CCJ",  "zh": "Cameco",        "tag_zh": "鈾礦 / 核燃料"},
            {"ticker": "XLU",  "zh": "公用事業 ETF",  "tag_zh": "電力板塊基準（對照組）"},
        ],
    },
    {
        "key": "reit",
        "zh": "資料中心 REIT",
        "en": "Data-Center REITs",
        "desc_zh": "資料中心地產——租金與估值反映算力空間供需，落後則暗示過建",
        "tickers": [
            {"ticker": "DLR",  "zh": "Digital Realty", "tag_zh": "全球資料中心 REIT 龍頭"},
            {"ticker": "EQIX", "zh": "Equinix",        "tag_zh": "互聯資料中心龍頭"},
            {"ticker": "IRM",  "zh": "Iron Mountain",  "tag_zh": "文件倉儲轉型資料中心"},
            {"ticker": "SRVR", "zh": "資料基建 ETF",   "tag_zh": "Data & Infrastructure REIT ETF"},
            {"ticker": "VNQ",  "zh": "REIT 總指數",    "tag_zh": "地產板塊基準（對照組）"},
        ],
    },
    {
        "key": "credit",
        "zh": "信用與投機溫度",
        "en": "Credit & Speculation",
        "desc_zh": "泡沫靠便宜資金餵養——高收益債利差、比特幣、投機成長股、VIX",
        "tickers": [
            {"ticker": "HYG",     "zh": "高收益債 ETF", "tag_zh": "垃圾債價格（利差反向）"},
            {"ticker": "IEF",     "zh": "7-10 年美債",  "tag_zh": "無風險利率對照"},
            {"ticker": "BTC-USD", "zh": "比特幣",       "tag_zh": "全市場風險偏好溫度計"},
            {"ticker": "ARKK",    "zh": "ARK 創新 ETF", "tag_zh": "投機成長股代理"},
            {"ticker": "^VIX",    "zh": "VIX 恐慌指數", "tag_zh": "過低 = 自滿訊號"},
        ],
    },
    {
        "key": "bench",
        "zh": "市場基準",
        "en": "Benchmarks",
        "desc_zh": "大盤對照——等權重 vs 市值加權的分歧即是集中度訊號",
        "tickers": [
            {"ticker": "SPY",  "zh": "標普500",        "tag_zh": "市值加權大盤"},
            {"ticker": "RSP",  "zh": "標普500 等權重", "tag_zh": "等權重對照（集中度訊號）"},
            {"ticker": "QQQ",  "zh": "那斯達克100",    "tag_zh": "科技權值"},
            {"ticker": "^TNX", "zh": "美債 10 年殖利率", "tag_zh": "資金成本錨"},
        ],
    },
]

# 抓取季度財報（資本支出）的公司 — 雲端巨頭五家。
CAPEX_TICKERS = ["MSFT", "GOOGL", "AMZN", "META", "ORCL"]

# 需要抓 .info 基本面（PE/PS/市值）的個股（排除 ETF / 指數 / 加密貨幣）。
FUNDAMENTAL_GROUP_KEYS = ("mega", "core", "neocloud", "power", "reit")
NO_FUNDAMENTALS = {"XLU", "SRVR", "VNQ", "HYG", "IEF", "ARKK", "SPY", "RSP", "QQQ",
                   "BTC-USD", "^VIX", "^TNX"}

# 估值/動能籃子定義（評分用）。
VALUATION_BASKET = [t["ticker"] for g in GROUPS if g["key"] == "core" for t in g["tickers"]]
MOMENTUM_BASKET = VALUATION_BASKET + ["CRWV", "NBIS", "PLTR"]
POWER_BASKET = ["VST", "CEG", "NRG", "TLN", "GEV"]
SMR_SPECULATIVE = ["OKLO", "SMR"]
REIT_BASKET = ["DLR", "EQIX", "IRM", "SRVR"]

# 綜合泡沫溫度權重（合計 1.0）。
SCORE_WEIGHTS = {
    "capex": 0.20,
    "valuation": 0.20,
    "momentum": 0.15,
    "infra": 0.15,
    "credit": 0.15,
    "concentration": 0.15,
}

# 溫度分區（含義與配色鍵）。
ZONES = [
    {"max": 30,  "key": "calm",    "zh": "冷靜區",   "desc_zh": "市場情緒與基本面大致匹配"},
    {"max": 50,  "key": "warming", "zh": "升溫區",   "desc_zh": "資金流入加速，估值開始領先基本面"},
    {"max": 70,  "key": "hot",     "zh": "過熱區",   "desc_zh": "多項指標偏離歷史常態，需提高警覺"},
    {"max": 85,  "key": "alert",   "zh": "泡沫警戒", "desc_zh": "估值/槓桿/投機指標同步亮紅燈"},
    {"max": 101, "key": "extreme", "zh": "極端泡沫", "desc_zh": "歷史級別的過熱，任何催化劑都可能觸發回調"},
]

# FRED 選配序列（設定 FRED_API_KEY 時抓取）。
FRED_HY_OAS = "BAMLH0A0HYM2"   # ICE BofA US High Yield OAS（百分點）

# Google News RSS 主題（繁中版）。
NEWS_TOPICS = [
    {"key": "bubble", "zh": "AI 泡沫論戰",
     "query": "AI 泡沫 OR \"AI bubble\""},
    {"key": "capex", "zh": "資本支出與資料中心",
     "query": "AI 資本支出 OR 資料中心 投資 OR 資料中心 擴建"},
    {"key": "power", "zh": "電力與能源",
     "query": "資料中心 電力 OR AI 用電 OR 電網"},
    {"key": "chips", "zh": "晶片與算力",
     "query": "輝達 OR NVIDIA OR GPU 需求"},
    {"key": "credit", "zh": "融資與信用",
     "query": "CoreWeave OR 甲骨文 債券 OR AI 融資 OR AI 貸款"},
]

# ── 歷史泡沫對照（靜態整理，定性參考） ──
HISTORY = {
    "title_zh": "歷史泡沫對照：2000 網路泡沫 vs 本輪 AI 週期",
    "note_zh": "定性整理（數字為約數，供框架參考，非精確統計）。歷史不會重複，但會押韻。",
    "rows": [
        {"metric_zh": "龍頭估值",
         "then_zh": "思科前瞻 PE 約 130 倍；那斯達克整體 PE 約 70 倍",
         "now_zh": "輝達前瞻 PE 約 30–40 倍；Mag7 約 30 倍上下——估值有獲利支撐",
         "verdict": "lower"},
        {"metric_zh": "資金來源",
         "then_zh": "電信商靠垃圾債舉債鋪光纖，槓桿驅動",
         "now_zh": "巨頭前期以自有現金流支應；2025 起 Meta/甲骨文/CoreWeave 轉向債券、SPV、私募信貸",
         "verdict": "watch"},
        {"metric_zh": "基礎設施過建",
         "then_zh": "光纖鋪完點亮率不到 10%，暗光纖過剩消化十年",
         "now_zh": "GPU 折舊僅 3–6 年、技術迭代快——若過建，貶值速度遠快於光纖",
         "verdict": "worse"},
        {"metric_zh": "收入兌現",
         "then_zh": ".com 公司普遍無營收、燒錢換眼球",
         "now_zh": "AI 應用收入成長中，但體量仍遠小於每年數千億美元的基建投資，缺口待補",
         "verdict": "watch"},
        {"metric_zh": "循環交易",
         "then_zh": "思科供應商融資（vendor financing）撐起客戶採購",
         "now_zh": "輝達投資 OpenAI/CoreWeave，被投方再回頭採購晶片與算力——結構神似",
         "verdict": "similar"},
        {"metric_zh": "實體瓶頸",
         "then_zh": "頻寬過剩、無實體限制",
         "now_zh": "電網互聯排隊 5–7 年、燃氣輪機缺貨、電價上行——瓶頸既是護城河也是過熱證據",
         "verdict": "new"},
    ],
    "lessons_zh": [
        "2000 年泡沫破裂後，基礎設施留存並孕育了寬頻時代——即使泡沫破裂，算力與電力資產不會消失，但「誰在最高點買單」決定誰受傷。",
        "亞馬遜在 2000–2001 下跌 94% 後才成為巨頭——泡沫判斷與長期價值判斷是兩回事。",
        "本輪與 2000 最大差異：金主是全球現金流最強的公司；最大相似：資本開支成長率遠超收入成長率，且融資結構正在轉向槓桿。",
        "監測重點不是「會不會破」，而是訊號的邊際變化：資本支出指引下修、GPU 租賃價格鬆動、信用利差走闊、電力合約毀約——任何兩項同時出現即應提高現金比重。",
    ],
}

# ════════════════════════ 前瞻訊號層 FRONTIER ════════════════════════
# 設計原則：監測「實體算力經濟」的即時讀數——領先股價/利差 6–12 個月。
# 2000 年最早的訊號不是那斯達克下跌，而是光纖點亮率不到 10%；
# 本輪的等價物是 GPU 租賃現貨價、算力供需剪刀差、資料中心退租事件。

# vast.ai 現貨市場追蹤的 GPU 型號（on-demand、單卡報價的中位數）。
GPU_SPOT_MODELS = [
    {"key": "h100", "vast_name": "H100 SXM",  "zh": "H100 SXM",  "main": True},
    {"key": "h200", "vast_name": "H200",      "zh": "H200",      "main": False},
    {"key": "a100", "vast_name": "A100 SXM4", "zh": "A100 SXM4", "main": False},
]

# H100 全成本回本租金帶（$/hr）：採購 $25–30k、5 年折舊、含電力/機房、
# 利用率 70% 假設下的粗估。現貨價跌入此帶 = 租賃商無利可圖 = 過建實證。
GPU_BREAKEVEN_BAND = (1.3, 1.9)

# GPU 現貨價歷史錨點（公開報導約數，演示基線；即時點每次更新自動累加）。
GPU_PRICE_SEED_HISTORY = [
    {"date": "2023-06", "h100": 8.0,  "a100": 2.8},
    {"date": "2023-12", "h100": 6.0,  "a100": 2.2},
    {"date": "2024-06", "h100": 4.5,  "a100": 1.8},
    {"date": "2024-12", "h100": 3.4,  "a100": 1.5},
    {"date": "2025-06", "h100": 2.8,  "a100": 1.3},
    {"date": "2025-12", "h100": 2.6,  "a100": 1.1},
]

# 開發者採用脈搏：npm 套件（api.npmjs.org 提供 18 個月日頻歷史，免金鑰）。
NPM_PACKAGES = [
    {"pkg": "openai",            "zh": "OpenAI SDK (npm)"},
    {"pkg": "@anthropic-ai/sdk", "zh": "Anthropic SDK (npm)"},
]
# PyPI（pypistats.org，180 天，常限流——抓不到就安靜跳過）。
PYPI_PACKAGES = ["openai", "anthropic"]

# 資料中心壓力雷達：Google News 兩種語言各抓一批，逐標題分類。
DC_NEWS_QUERIES = [
    {"lang": "en", "hl": "en-US", "gl": "US", "ceid": "US:en",
     "query": '"data center" (cancel OR cancelled OR pause OR paused OR delay OR shelve OR scrapped OR "walk away" OR "pull back" OR lease)'},
    {"lang": "zh", "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant",
     "query": "資料中心 OR 數據中心 (取消 OR 暫停 OR 退租 OR 延後 OR 縮減 OR 擴建 OR 動工 OR 簽約)"},
]
DC_STRESS_KEYWORDS = [
    "取消", "暫停", "退租", "延後", "縮減", "喊停", "砍單", "放緩", "中止", "叫停", "減速",
    "cancel", "pause", "halt", "delay", "scrap", "walk away", "walks away", "pull back",
    "pulls back", "shelve", "abandon", "cut back", "cuts", "slow", "retreat", "exit",
]
DC_EXPANSION_KEYWORDS = [
    "動工", "擴建", "簽約", "啟用", "上線", "興建", "開工", "投資", "落腳", "進駐", "加碼",
    "expand", "build", "launch", "sign", "break ground", "breaks ground", "invest",
    "new data center", "megawatt", "gigawatt", "opens", "construction",
]

# 循環交易追蹤：已揭露的大型循環結構（供應商融資 / 股權換算力）種子台帳。
# 與 /payback 的 circularity_edges 共用 3 條邊，值需保持一致（避免兩卡漂移）：
#   輝達→OpenAI $100B、甲骨文↔OpenAI $300B（Stargate）、微軟→OpenAI $13B。
# 兩卡「總額」不同（aibubble ~$529B vs payback $424B）是刻意的：各自納入不同的額外邊，
# 故為「異質、不可相加」——兩卡皆已如此註明，不要合併兩本台帳。
CIRCULAR_DEALS_SEED = [
    {"date": "2025-09", "parties_zh": "輝達 → OpenAI",
     "structure_zh": "最高 $100B 分階段投資，與算力部署里程碑掛鉤——被投方再採購輝達晶片",
     "value_b": 100},
    {"date": "2025-10", "parties_zh": "AMD ↔ OpenAI",
     "structure_zh": "6GW 算力採購合約 + 最多 1.6 億股認股權證（約 10% 股權）——客戶變股東",
     "value_b": 90},
    {"date": "2025", "parties_zh": "甲骨文 ↔ OpenAI",
     "structure_zh": "報導約 $300B / 5 年算力合約（Stargate）——甲骨文舉債建設、收入依賴單一客戶",
     "value_b": 300},
    {"date": "2023–2025", "parties_zh": "輝達 → CoreWeave",
     "structure_zh": "持股 + $6.3B 算力回購保底（backstop）——供應商為客戶的需求兜底",
     "value_b": 6.3},
    {"date": "2019–2023", "parties_zh": "微軟 → OpenAI",
     "structure_zh": "累計約 $13B 投資，多數以 Azure 算力抵扣——投資即營收",
     "value_b": 13},
    {"date": "2025", "parties_zh": "輝達 → xAI / Nebius / Humain 等",
     "structure_zh": "多起「入股 + 供貨」組合，金額數十億美元級",
     "value_b": 20},
]
CIRC_NEWS_QUERIES = [
    {"lang": "en", "hl": "en-US", "gl": "US", "ceid": "US:en",
     "query": '"vendor financing" AI OR "Nvidia invests" OR "equity for compute" OR "circular deal" AI'},
    {"lang": "zh", "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant",
     "query": "輝達 投資 OR 循環交易 OR 算力 入股"},
]
CIRC_KEYWORDS = [
    "invest", "stake", "equity", "warrant", "financing", "backstop", "circular",
    "投資", "入股", "持股", "認股", "融資", "回購", "循環", "兜底",
]

# HN「Who is hiring」徵才成熟度分類關鍵詞。
HN_AI_KEYWORDS = [
    "llm", " ai ", "ai-", "genai", "gen-ai", "machine learning", "ml engineer",
    "gpt", "rag", "agent", "inference", "foundation model", "fine-tun", "openai",
    "anthropic", "claude", "transformer", "pytorch", "deep learning",
]
HN_PROD_KEYWORDS = [
    "production", "inference", "serving", "latency", "scale", "scaling", "deployed",
    "in prod", "optimization", "gpu cluster", "throughput", "reliability", "observability",
    "cost optimization", "evals",
]

# $6000 億問題（Sequoia 框架）即時版：
# 隱含必要收入 = 年化 CapEx × 2（資料中心總成本 + 50% 毛利結構的粗估），
# 對照「目前 AI 終端收入」估計值（公開報導約數，可隨時調整）。
# OpenAI/Anthropic 年化營收 run-rate 與 /payback 對齊（單一口徑、避免兩卡漂移）：
# OpenAI $25B（IPO 申報，2026-07-02 使用者核准 payback KB v2）、Anthropic $12B（NEWS，2026Q1）。
# 逐家詳情見 /payback。
AI_REVENUE_ESTIMATES = [
    {"zh": "OpenAI（年化營收 run-rate，對齊 /payback）",    "value_b": 25},
    {"zh": "Anthropic（年化營收 run-rate，對齊 /payback）", "value_b": 12},
    {"zh": "微軟/谷歌/Meta AI 歸屬收入（估）",     "value_b": 40},
    {"zh": "其他 AI 應用與 API（估）",             "value_b": 15},
]

# 前瞻綜合溫度權重（合計 1.0；缺資料的項目自動重新歸一）。
FRONTIER_WEIGHTS = {
    "gpu_spot": 0.25,
    "scissors": 0.20,
    "dc_stress": 0.20,
    "circularity": 0.15,
    "hn_maturity": 0.10,
    "revenue_gap": 0.10,
}

FRONTIER_META = {
    "gpu_spot": {"zh": "GPU 現貨租金", "en": "GPU Spot Price",
                 "note_zh": "vast.ai H100 on-demand 中位數——本輪的「暗光纖點亮率」。租金越接近回本帶，過建證據越強"},
    "scissors": {"zh": "算力供需剪刀差", "en": "Supply-Demand Scissors",
                 "note_zh": "CapEx 年增率（供給）vs 開發者採用年增率（npm SDK 下載，需求）——供給跑贏需求即泡沫指紋"},
    "dc_stress": {"zh": "資料中心壓力事件", "en": "DC Stress Events",
                  "note_zh": "新聞雷達自動分類：取消/退租/延後 vs 動工/簽約/擴建——微軟 2025 退租事件的泛化監測"},
    "circularity": {"zh": "循環交易強度", "en": "Circularity",
                    "note_zh": "供應商融資/股權換算力的累計規模與新增事件——思科 vendor financing 的本輪版本"},
    "hn_maturity": {"zh": "徵才部署成熟度", "en": "Hiring Maturity",
                    "note_zh": "HN Who-is-hiring 月帖：AI 職缺中「生產期」vs「探索期」比例——需求是否真的落地"},
    "revenue_gap": {"zh": "收入缺口倍數", "en": "Revenue Gap",
                    "note_zh": "年化 CapEx×2 的隱含必要收入 vs 目前 AI 終端收入估計——$6000 億問題的即時讀數"},
}

# 各子訊號的展示說明（方法論）。
SIGNAL_META = {
    "capex": {"zh": "巨頭資本支出強度", "en": "Hyperscaler CapEx",
              "note_zh": "五大雲端巨頭最新季資本支出年增率、CapEx/營運現金流、CapEx/營收——燒錢越兇、離現金流極限越近，泡沫溫度越高"},
    "valuation": {"zh": "AI 核心股估值熱度", "en": "Valuation Heat",
                  "note_zh": "AI 算力核心股 PE/PS 中位數與輝達 PS——估值越貴溫度越高"},
    "momentum": {"zh": "價格動能過熱", "en": "Momentum",
                 "note_zh": "AI 核心 + 新雲籃子近 6 月/1 年漲幅與相對大盤超額——漲得越急溫度越高"},
    "infra": {"zh": "基礎設施溢價", "en": "Infrastructure Premium",
              "note_zh": "電力股相對公用事業、資料中心 REIT 相對地產大盤的超額漲幅，及 SMR 無營收投機股漲幅"},
    "credit": {"zh": "信用與投機溫度", "en": "Credit & Speculation",
               "note_zh": "高收益債利差（越窄越自滿）、比特幣動能、ARKK 超額、VIX 水位（越低越自滿）"},
    "concentration": {"zh": "市場集中度", "en": "Concentration",
                      "note_zh": "標普市值加權對等權重、那斯達克對標普的 1 年領先幅度——少數巨頭撐起大盤即是集中度風險"},
}
