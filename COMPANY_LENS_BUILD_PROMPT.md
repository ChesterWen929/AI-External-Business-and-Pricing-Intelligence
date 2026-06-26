# Company Deep-Dive (Company Lens) — Build Spec (v5, post 5-round refinement)

Tier V at the base of the macro-ai-monitor pyramid: a **single-company** analysis
layer. First company = **Amazon** (`/company/amazon`). Multi-company by design.

## The 5-round prompt refinement (summary)
1. **Raw** — "add an Amazon card: how they raise compute price, sources, AI benefit, TSMC link."
2. **Structure** — four pillars (A pricing / B sources / C benefit / D silicon·TSMC); reuse the five-layer pipeline (L1 KB → L2 source dict → L3 live quant → L4 Claude → L5 scenarios); multi-company blueprint `/company/<slug>`.
3. **Sharpen** — "raise compute price" ≠ list price (falls); 6 named **realized-price levers**, each direction + observability + proxy → 0–100 score + verdict (raising/holding/eroding). AWS discloses no AI-only revenue → multi-method **labeled estimates**, never fabricate precision.
4. **Wire to platform + TSMC framing** — pillar D maps each accelerator → node → CoWoS → TSMC, tier-graded, with a TSMC-exposure %; every output framed for a TSMC CEO; deterministic alerts; cross-links to /cwengine /pricing /payback /compute; bilingual; Opus 4.8 + rules fallback; password-gated manual refresh (`companylens2026`); seed renders offline.
5. **Harden + extensible (this spec)** — per-company `company/kb/<slug>.json` + registry; snapshot cache `data/company/<slug>.json`; keyless collectors (yfinance AMZN/NVDA/TSM + Google News), failures fall back to seed; fixed Claude structured-output schema; portal Tier V card (score + AI-benefit $). Non-goals: not investment advice, no confidential AWS prices.

## Architecture (mirrors pricing/payback)
```
company/
  __init__.py          blueprint, multi-company registry, routes, password-gated refresh
  model.py             L3 quant: 4 pillars + scores + deterministic alerts + snapshot
  analysis.py          L4 four-pillar CEO read + integrated thesis, L5 scenarios — Claude Opus 4.8 or rules
  collectors.py        keyless live: yfinance (AMZN/NVDA/TSM) + Google News RSS
  kb/amazon.json       curated L1/L2 seed (levers, sources, benefit estimates, silicon chain, scenarios)
  test_company.py      18 engine tests (rules engine, no network)
templates/company.html bilingual zh-Hant/en dashboard
data/company/<slug>.json  cached snapshot (gitignored data dir)
```

## The four pillars (answer the four user questions)
- **A · Pricing** — 6 levers: custom-silicon margin capture, mix-shift up-stack, accelerator scarcity premium, commitment lock-in (RPO), value-based token pricing, ancillary attach. Weighted-mean strength → score (Amazon seed ≈ 65 → RAISING).
- **B · Sources** — 8-row data-source dictionary (10-Q/RPO, Price List API, Bedrock pricing, Vantage, GPU spot, Liftr/SemiAnalysis, TSMC IR), tier-graded with links.
- **C · Benefit** — 3 labeled estimates (AI-attributable AWS revenue run-rate ≈ $22B headline; op-income lens; Anthropic stake), confidence + tier. Consensus only within same metric (never adds op-income to revenue).
- **D · Silicon/TSMC** — chain: Trainium2/3, Inferentia2, Graviton4, rented NVIDIA GPUs → node/CoWoS → TSMC. ~100% TSMC exposure; "Amazon is a TSMC customer twice over (direct via Annapurna + indirect via NVIDIA); CoWoS is the binding constraint."

## Conventions honored
- Live equity proxies are **sentiment context only** — deliberately kept OUT of the pricing-power score (tested).
- Refresh: manual, password-gated (`COMPANY_REFRESH_PASSWORD`, default `companylens2026`); NOT in the weekly scheduler (keep Opus cost on-demand).
- Slugs come only from the on-disk registry; path-traversal rejected.

## Add the next company
Drop `company/kb/<slug>.json` (same shape as amazon.json) → it appears in the registry and at `/company/<slug>`. No code change required.
