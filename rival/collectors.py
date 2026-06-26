"""Live-layer collectors: yfinance market snapshot + Google News RSS.

Everything here is rebuilt by /api/refresh; failures of one ticker/feed never
abort the whole refresh. No API key required.
"""
import datetime
import json
import math
import re
import urllib.request
import xml.etree.ElementTree as ET

TICKERS = {
    "TSM":      {"name_en": "TSMC",            "name_zh": "台積電"},
    "005930.KS": {"name_en": "Samsung",         "name_zh": "三星電子"},
    "INTC":     {"name_en": "Intel",            "name_zh": "英特爾"},
    "0981.HK":  {"name_en": "SMIC",             "name_zh": "中芯國際"},
    "UMC":      {"name_en": "UMC",              "name_zh": "聯電"},
    "GFS":      {"name_en": "GlobalFoundries",  "name_zh": "格羅方德"},
    "TSEM":     {"name_en": "Tower Semi",       "name_zh": "高塔半導體"},
    "1347.HK":  {"name_en": "Hua Hong",         "name_zh": "華虹半導體"},
    "5347.TWO": {"name_en": "Vanguard (VIS)",   "name_zh": "世界先進"},
    "6770.TW":  {"name_en": "PSMC",             "name_zh": "力積電"},
}

NEWS_QUERIES = [
    ("samsung_foundry", "Samsung Foundry 2nm customer"),
    ("intel_foundry",   "Intel Foundry 18A customer"),
    ("smic",            "SMIC advanced node Huawei"),
    ("rapidus",         "Rapidus 2nm"),
    ("tsmc_orders",     "TSMC orders customer foundry"),
    ("foundry_share",   "foundry market share TrendForce"),
]
NEWS_QUERIES_ZH = [
    ("zh_transfer", "台積電 轉單"),
    ("zh_samsung",  "三星 晶圓代工 客戶"),
    ("zh_intel",    "英特爾 晶圓代工"),
]

# keyword → customer-movement signal tag on a news item
SIGNAL_PATTERNS = [
    ("defection", re.compile(r"switch|defect|move[sd]? (?:order|production)|shift[s]? (?:order|production)|轉單|跳槽|改投", re.I)),
    ("win",       re.compile(r"\bwin[s]?\b|secured?|landed|拿下|奪單|贏得", re.I)),
    ("dual",      re.compile(r"dual[- ]sourc|second[- ]sourc|雙供應|第二供應", re.I)),
    ("capacity",  re.compile(r"capacity|fab construction|expansion|產能|擴產", re.I)),
    ("node",      re.compile(r"\b2nm\b|\b1\.4nm\b|18A|14A|N2\b|A16\b|SF2|良率|yield", re.I)),
]


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(v, nd=2):
    """Coerce to a finite rounded float, else None (NaN would break JSON.parse)."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return round(v, nd)


def _fast_get(info, *keys):
    """Read a value off yfinance fast_info, which exposes both snake_case and
    camelCase aliases inconsistently across versions and via either item or
    attribute access. Returns the first finite value found, else None."""
    for k in keys:
        try:
            v = info[k]
        except (KeyError, TypeError):
            v = getattr(info, k, None)
        n = _num(v)
        if n is not None:
            return n
    return None


def refresh_market(data_dir):
    import yfinance as yf

    rows, errors = [], []
    for ticker, names in TICKERS.items():
        try:
            t = yf.Ticker(ticker)
            info = t.fast_info
            price = _num(info["last_price"])
            if price is None:
                raise ValueError("no price")
            prev = _num(info["previous_close"]) or price
            # fast_info key name is unstable across yfinance versions; try the
            # known aliases, then fall back to shares-outstanding x price.
            mcap = _fast_get(info, "market_cap", "marketCap")
            if mcap is None:
                shares = _fast_get(info, "shares", "shares_outstanding", "sharesOutstanding")
                if shares is not None:
                    mcap = shares * price
            row = {
                "ticker": ticker,
                "name_en": names["name_en"],
                "name_zh": names["name_zh"],
                "price": price,
                "currency": str(getattr(info, "currency", "") or info.get("currency", "")),
                "change_pct": _num((price - prev) / prev * 100) if prev else 0.0,
                "market_cap": int(mcap) if mcap is not None else None,
                "ytd_pct": None,
            }
            try:
                hist = t.history(period="ytd")["Close"].dropna()
                if len(hist) > 1:
                    first, last = _num(hist.iloc[0]), _num(hist.iloc[-1])
                    if first and last is not None:
                        row["ytd_pct"] = _num((last / first - 1) * 100, 1)
            except Exception:
                pass
            rows.append(row)
        except Exception as exc:
            errors.append({"ticker": ticker, "error": str(exc)[:200]})

    payload = {"updated_at": _now_iso(), "quotes": rows, "errors": errors}
    (data_dir / "market_live.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return payload


def _fetch_rss(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (RivalRadar)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return ET.fromstring(resp.read())


def _parse_items(root, topic, lang, limit=8):
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        source = (item.findtext("source") or "").strip()
        if not title or not link:
            continue
        tags = [tag for tag, pat in SIGNAL_PATTERNS if pat.search(title)]
        items.append({
            "topic": topic, "lang": lang, "title": title, "link": link,
            "published": pub, "source": source, "signals": tags,
        })
        if len(items) >= limit:
            break
    return items


def refresh_news(data_dir):
    from urllib.parse import quote

    all_items, errors = [], []
    feeds = (
        [(k, q, "https://news.google.com/rss/search?q={}&hl=en-US&gl=US&ceid=US:en", "en") for k, q in NEWS_QUERIES]
        + [(k, q, "https://news.google.com/rss/search?q={}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant", "zh") for k, q in NEWS_QUERIES_ZH]
    )
    for topic, query, tmpl, lang in feeds:
        try:
            root = _fetch_rss(tmpl.format(quote(query)))
            all_items.extend(_parse_items(root, topic, lang))
        except Exception as exc:
            errors.append({"topic": topic, "error": str(exc)[:200]})

    seen, deduped = set(), []
    for it in all_items:
        key = it["title"].lower()[:80]
        if key not in seen:
            seen.add(key)
            deduped.append(it)
    # movement-signal stories first
    deduped.sort(key=lambda x: (-len(set(x["signals"]) & {"defection", "win", "dual"}), x["topic"]))

    payload = {"updated_at": _now_iso(), "items": deduped, "errors": errors}
    (data_dir / "news_live.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return payload


def refresh_all(data_dir):
    market = refresh_market(data_dir)
    news = refresh_news(data_dir)
    return {
        "refreshed_at": _now_iso(),
        "market_quotes": len(market["quotes"]),
        "market_errors": len(market["errors"]),
        "news_items": len(news["items"]),
        "news_errors": len(news["errors"]),
    }
