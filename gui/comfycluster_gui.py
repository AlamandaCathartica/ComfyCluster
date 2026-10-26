"""ComfyCluster controller GUI: a title and one big frame that holds ComfyUI.

    python gui/comfycluster_gui.py --demo            look around, no ComfyUI needed
    python gui/comfycluster_gui.py --tunnel ...      Colab: public Cloudflare tunnel protected by a key
"""
import argparse
import hmac
import json
import os
import secrets
import sys
import threading
import time
import webbrowser

import requests
from flask import Flask, Response, g, jsonify, request, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WEB = os.path.join(HERE, "web")
ACCESS_KEY = os.environ.get("COMFYCLUSTER_KEY", "")
try:
    VERSION = open(os.path.join(ROOT, "VERSION"), encoding="utf-8").read().strip()
except OSError:
    VERSION = "dev"

STATE = {"demo": False, "comfy_url": "", "comfy_local": "", "comfy_ready": False, "workers": []}
_LOCK = threading.Lock()
app = Flask(__name__, static_folder=None)


@app.before_request
def guard():
    if not ACCESS_KEY:
        return None
    if hmac.compare_digest(request.cookies.get("comfycluster_key", ""), ACCESS_KEY):
        return None
    qk = request.args.get("key", "")
    if qk and hmac.compare_digest(qk, ACCESS_KEY):
        g.set_cookie = True
        return None
    return Response("Forbidden. Open the full link printed by the notebook (it ends with ?key=...).", 403)


@app.after_request
def after(resp):
    if g.get("set_cookie"):
        secure = request.headers.get("X-Forwarded-Proto", request.scheme) == "https"
        resp.set_cookie("comfycluster_key", ACCESS_KEY, httponly=True, samesite="Lax", secure=secure, max_age=86400)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/")
def index():
    return send_from_directory(WEB, "index.html")


@app.route("/api/state")
def state():
    with _LOCK:
        workers = [{"name": w["name"], "online": w.get("online", False)} for w in STATE["workers"]]
        return jsonify(version=VERSION, demo=STATE["demo"], comfy_url=STATE["comfy_url"],
                       comfy_ready=STATE["comfy_ready"], workers=workers)


def _ok(url):
    try:
        return requests.get(url.rstrip("/") + "/system_stats", timeout=6).ok
    except requests.RequestException:
        return False


def poller():
    """Every few seconds: is ComfyUI up, and which workers answer."""
    while True:
        ready = bool(STATE["comfy_local"]) and _ok(STATE["comfy_local"])
        online = {w["name"]: _ok(w["url"]) for w in STATE["workers"]}
        with _LOCK:
            STATE["comfy_ready"] = ready
            for w in STATE["workers"]:
                w["online"] = online.get(w["name"], False)
        time.sleep(8)


def main():
    global ACCESS_KEY
    ap = argparse.ArgumentParser(description="ComfyCluster controller GUI")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 7860)))
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--tunnel", action="store_true", help="expose the GUI through a Cloudflare quick tunnel (Colab)")
    ap.add_argument("--url-file", help="write the public link here once the tunnel is up")
    ap.add_argument("--cache-dir", default=os.path.join(ROOT, ".cache"))
    ap.add_argument("--key", default="", help="access key required in the link / cookie (generated with --tunnel)")
    ap.add_argument("--comfy-url", default="", help="public address of ComfyUI, loaded inside the frame")
    ap.add_argument("--comfy-local", default="", help="local address of ComfyUI, used to check that it is up")
    ap.add_argument("--workers-file", help="JSON list of {name, url} written by the controller")
    ap.add_argument("--demo", action="store_true", help="no ComfyUI, shows the empty frame")
    args = ap.parse_args()

    if args.tunnel and not args.key:
        args.key = secrets.token_urlsafe(16)
    ACCESS_KEY = args.key or ACCESS_KEY
    STATE.update(demo=args.demo, comfy_url=args.comfy_url, comfy_local=args.comfy_local)
    if args.workers_file and os.path.isfile(args.workers_file):
        with open(args.workers_file, encoding="utf-8") as f:
            STATE["workers"] = json.load(f)
    threading.Thread(target=poller, daemon=True).start()

    if args.tunnel:
        sys.path.insert(0, HERE)
        import tunnel

        def on_url(url):
            link = url + "/" + (f"?key={ACCESS_KEY}" if ACCESS_KEY else "")
            print("GUI link:", link, flush=True)
            if args.url_file:
                with open(args.url_file, "w") as f:
                    f.write(link)

        tunnel.start(args.port, args.cache_dir, on_url, on_error=lambda m: print(m, flush=True))
    elif not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{args.host}:{args.port}/")).start()

    print(f"ComfyCluster GUI v{VERSION} on http://{args.host}:{args.port}", flush=True)
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
