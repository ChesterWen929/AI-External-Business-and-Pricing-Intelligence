"""Regenerate every card's snapshot seed locally, then they get committed + pushed.

Why this exists: the app can't boot locally because the repo's own `bottleneck/`
directory shadows pandas' optional `bottleneck` dependency. This runner
temporarily renames `bottleneck/` out of the way (it is NOT a registered
blueprint, so nothing depends on it), refreshes each module independently, and
restores the directory in a finally block.

Needs (from .env): FRED_API_KEY (econ), ANTHROPIC_API_KEY (Claude L4/L5 on
aibubble/cwengine/flows/pricing/payback/econ-genai), FINNHUB_API_KEY (earnings).
yfinance is keyless. Each module is wrapped so one failure never aborts the rest.
"""
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BN = ROOT / "bottleneck"
BN_OFF = ROOT / "_bottleneck_off"

sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass


def keyflag(k):
    return "set" if os.environ.get(k) else "MISSING"


print("keys:",
      "FRED=" + keyflag("FRED_API_KEY"),
      "ANTHROPIC=" + keyflag("ANTHROPIC_API_KEY"),
      "FINNHUB=" + keyflag("FINNHUB_API_KEY"))
print("-" * 60)

results = []


def run(name, fn):
    t0 = time.time()
    try:
        fn()
        dt = round(time.time() - t0, 1)
        results.append((name, "OK", dt, ""))
        print(f"[OK]   {name:9} {dt}s")
    except Exception as e:  # noqa: BLE001 — one bad module must not abort the run
        dt = round(time.time() - t0, 1)
        results.append((name, "FAIL", dt, repr(e)[:200]))
        print(f"[FAIL] {name:9} {dt}s  {repr(e)[:200]}")
        traceback.print_exc()


def _compute():
    from compute import refresh
    return refresh()


def _racks():
    from racks import refresh
    return refresh()


def _rival():
    from rival import refresh_live
    return refresh_live()


def _earnings():
    from earnings import refresh
    return refresh()


def _aibubble():
    from aibubble import fetcher
    return fetcher.refresh()


def _cwengine():
    from cwengine import refresh
    return refresh()


def _flows():
    from flows import refresh
    return refresh()


def _pricing():
    from pricing import refresh
    return refresh()


def _payback():
    from payback import refresh
    return refresh()


def _econ():
    from econ import refresh_job
    return refresh_job.run_weekly_refresh_sync(gen_ai=True, force=True)


# Order: quant/cheap first, Claude cards next, econ last (slowest).
PLAN = [
    ("compute", _compute), ("racks", _racks), ("rival", _rival), ("earnings", _earnings),
    ("aibubble", _aibubble), ("cwengine", _cwengine),
    ("flows", _flows), ("pricing", _pricing), ("payback", _payback),
    ("econ", _econ),
]


def main():
    only = set(sys.argv[1:])  # optional: refresh only named modules
    renamed = False
    if BN.exists():
        BN.rename(BN_OFF)
        renamed = True
        print("temporarily moved bottleneck/ -> _bottleneck_off")
    try:
        for name, fn in PLAN:
            if only and name not in only:
                continue
            run(name, fn)
    finally:
        if renamed and BN_OFF.exists():
            BN_OFF.rename(BN)
            print("restored bottleneck/")

    print("\n=== SUMMARY ===")
    ok = sum(1 for _, s, _, _ in results if s == "OK")
    for n, s, t, e in results:
        print(f"{s:4} {n:9} {t}s {e}")
    print(f"{ok}/{len(results)} refreshed OK")


if __name__ == "__main__":
    main()
