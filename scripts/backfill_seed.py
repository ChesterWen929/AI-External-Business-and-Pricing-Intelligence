"""One-off: upgrade the committed econ seed snapshot in place.

  • drop discontinued series (USSLIND — frozen at 2020-02)
  • re-apply corrected display formats (ICSA / Existing Home Sales → "count")
  • compute multi-horizon change (1w/1m/3m/1y) for every indicator from its
    own observation history — works without a FRED key.

Idempotent: safe to re-run. Run from the app root:  python3 scripts/backfill_seed.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from econ.refresh_job import _fmt_value, _changes_over_horizons  # noqa: E402

SNAP = ROOT / "data" / "econ" / "snapshots" / "latest.json"

DROP_SERIES = {"USSLIND"}                       # discontinued
RECOUNT_FORMAT = {"ICSA": "count", "EXHOSLUSM495S": "count"}  # raw-count series

snap = json.loads(SNAP.read_text(encoding="utf-8"))
obs_by = snap.get("observations_by_series", {})

kept = []
for ind in snap.get("indicators", []):
    sid = ind["series_id"]
    if sid in DROP_SERIES:
        obs_by.pop(sid, None)
        continue
    if sid in RECOUNT_FORMAT:
        ind["format"] = RECOUNT_FORMAT[sid]
    # Recompute the display from the current formatter (fixes count + negative billions).
    if ind.get("latest_value") is not None:
        ind["latest_display"] = _fmt_value(ind["latest_value"], ind["format"])
    ind["changes"] = _changes_over_horizons(obs_by.get(sid, []))
    kept.append(ind)

snap["indicators"] = kept
snap["indicator_count"] = len(kept)
snap["observations_by_series"] = obs_by

SNAP.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")

# ── report ──
print(f"indicators: {len(kept)} (dropped {', '.join(DROP_SERIES)})")
print("\nsample multi-horizon change:")
for sid in ("CPIAUCSL", "ICSA", "GDPC1", "T10Y2Y", "EXHOSLUSM495S"):
    ind = next((i for i in kept if i["series_id"] == sid), None)
    if not ind:
        continue
    ch = ind.get("changes", {})
    horizons = " ".join(
        f"{k}={ch[k]['delta']:+.3g}" if ch.get(k) else f"{k}=—"
        for k in ("1w", "1m", "3m", "1y")
    )
    print(f"  {sid:14} disp={ind.get('latest_display'):>10}  {horizons}")
