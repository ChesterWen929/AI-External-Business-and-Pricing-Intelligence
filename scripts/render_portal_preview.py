"""Render portal.html to a static file for visual preview (no Flask boot, no auth).

Pulls the real STRINGS dict out of app.py via AST (so labels stay in sync) and
feeds the template representative snapshot values for every card.
"""
import ast
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parent.parent

# ── extract STRINGS literal from app.py without importing the blueprints ──
tree = ast.parse((ROOT / "app.py").read_text())
STRINGS = None
for node in ast.walk(tree):
    if isinstance(node, ast.Assign):
        for tgt in node.targets:
            if isinstance(tgt, ast.Name) and tgt.id == "STRINGS":
                STRINGS = ast.literal_eval(node.value)
assert STRINGS, "STRINGS not found in app.py"


env = Environment(loader=FileSystemLoader(str(ROOT / "templates")))
out_dir = ROOT / "scripts" / "_preview"
out_dir.mkdir(exist_ok=True)


def render(lang):
    def t(key):
        return STRINGS.get(key, {}).get(lang, key)

    return env.get_template("portal.html").render(
        lang=lang,
        t=t,
        # tier I
        econ_count=11, econ_updated="2026-06-13",
        # tier II
        flows_score=-3, flows_updated="2026-06-15",
        aib_score=64.5, aib_zone="過熱" if lang == "zh" else "Overheated", aib_updated="2026-06-10",
        # tier III
        compute_2030=591.0, compute_updated="2026-06-14",
        payback_coverage=0.41, payback_verdict="investing", payback_updated="2026-06-20",
        cwe_wpm=185000, cwe_updated="2026-06-16",
        # tier IV
        racks_count=19, racks_updated="2026-06-14",
        earnings_count=12, earnings_updated="2026-06-17",
        pricing_score=58, pricing_verdict="neutral", pricing_updated="2026-06-18",
        rival_events=23, rival_updated="2026-06-11",
    )


(out_dir / "index.html").write_text(render("zh"))  # default = 中文
(out_dir / "en.html").write_text(render("en"))
print(out_dir / "index.html")
