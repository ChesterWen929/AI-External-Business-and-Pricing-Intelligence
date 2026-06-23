"""Standalone preview runner for the AI Capex Payback Radar card ONLY.

Serves just the /payback blueprint so the page can be visually verified without
booting the full app (whose other cards import yfinance→pandas at module load,
which collides with the repo's local `bottleneck/` package dir on this machine).
Not used in production — app.py mounts the same blueprint behind shared auth.
"""
import os
import sys

sys.modules["bottleneck"] = None  # neutralize the local-dir/pandas name collision (preview only)
os.environ.setdefault("APP_PASSWORD", "preview")
os.environ.setdefault("SECRET_KEY", "preview-dev")

from flask import Flask, redirect

from payback import payback_bp

app = Flask(__name__, template_folder="templates")
app.config["JSON_AS_ASCII"] = False
app.register_blueprint(payback_bp)


@app.route("/")
def home():
    return redirect("/payback/")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5266)), debug=False)
