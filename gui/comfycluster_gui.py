"""ComfyCluster controller GUI: a title, one big frame that holds ComfyUI, and a worker monitor window.

    python gui/comfycluster_gui.py --demo            look around, no ComfyUI needed
    python gui/comfycluster_gui.py --tunnel ...      Colab: public Cloudflare tunnel protected by a key
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
import webbrowser
import zipfile
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Flask, Response, g, jsonify, request, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "engine"))
from logtail import tail_log  # noqa: E402
from pairing import PREFIX, parse_code  # noqa: E402
from comfy import worker_entry  # noqa: E402
WEB = os.path.join(HERE, "web")
GIB = 1024 ** 3
ACCESS_KEY = os.environ.get("COMFYCLUSTER_KEY", "")
try:
    VERSION = open(os.path.join(ROOT, "VERSION"), encoding="utf-8").read().strip()
except OSError:
    VERSION = "dev"

STATE = {"demo": False, "comfy_url": "", "comfy_local": "", "comfy_ready": False,
         "workers": [], "workers_file": "", "config_path": "", "comfy_dir": "", "stage_dir": "", "logs": {}}
_LOCK = threading.Lock()
_CFG_LOCK = threading.Lock()
WAKE = threading.Event()          # set to refresh the worker status right away
app = Flask(__name__, static_folder=None)


# ----------------------------------------------------------------------------- access key
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


# ----------------------------------------------------------------------------- routes
@app.route("/")
def index():
    return send_from_directory(WEB, "index.html")


PUBLIC_KEYS = ("name", "enabled", "online", "gpu", "vram_used", "vram_total", "running", "pending",
               "gpu_util", "gpu_temp", "gpu_power", "cpu_name", "cpu_cores", "cpu_percent",
               "ram_used", "ram_total", "disk_used", "disk_total", "comfy_version", "torch_version", "python_version")


def _public(w):
    return {k: w.get(k) for k in PUBLIC_KEYS}


@app.route("/api/state")
def state():
    with _LOCK:
        return jsonify(version=VERSION, demo=STATE["demo"], comfy_url=STATE["comfy_url"],
                       comfy_ready=STATE["comfy_ready"], workers=[_public(w) for w in STATE["workers"]])


RESERVED_NAMES = {"all", "controller"}          # used by the connect / log routes
MAX_CODES = 20


@app.post("/api/workers/add")
def add_workers():
    """Pair workers from pasted pairing codes (one or several). A worker that is already paired is updated."""
    body = request.get_json(silent=True) or {}
    tokens = [t for t in re.split(r"[\s,]+", str(body.get("codes", ""))[:20000]) if t]
    if not tokens:
        return jsonify(ok=False, results=[], error="Paste a pairing code first."), 400
    results, found = [], []
    if len(tokens) > MAX_CODES:
        results.append(dict(ok=False, code="", message=f"Only the first {MAX_CODES} codes were used."))
    for tok in tokens[:MAX_CODES]:
        head = tok[:12] + ("\u2026" if len(tok) > 12 else "")
        if not tok.startswith(PREFIX):
            results.append(dict(ok=False, code=tok, message=f"\u201c{head}\u201d is not a pairing code. A code starts with {PREFIX}"))
            continue
        try:
            found.append((tok, parse_code(tok)))
        except ValueError:
            results.append(dict(ok=False, code=tok, message=f"The code {head} is damaged or incomplete. Copy it again from the worker."))

    with ThreadPoolExecutor(max_workers=8) as pool:            # check that each worker answers, and read its stats
        probes = list(pool.map(lambda f: _demo_probe(f[1]["name"]) if STATE["demo"] else _probe_worker(f[1]), found))
    changed = []
    for (tok, w), probe in zip(found, probes):
        name = w["name"]
        if name.lower() in RESERVED_NAMES:
            results.append(dict(ok=False, code=tok, message=f"The name \u201c{name}\u201d is reserved. Change WORKER_NAME on that worker and use its new code."))
            continue
        if not probe.get("online"):
            results.append(dict(ok=False, code=tok, message=f"{name} is not answering. Check that its Worker notebook is still running, then copy a fresh code."))
            continue
        with _LOCK:
            same_name = next((x for x in STATE["workers"] if x["name"] == name), None)
            same_url = next((x for x in STATE["workers"] if x["url"] == w["url"] and x is not same_name), None)
            if same_url:
                results.append(dict(ok=False, code=tok, message=f"That address is already paired as {same_url['name']}."))
                continue
            if same_name:
                same_name["url"] = w["url"]
                same_name.update(probe)
                entry, status, text = same_name, "updated", f"Updated {name} with its new address."
            else:
                entry = dict(name=name, url=w["url"], enabled=True)
                entry.update(probe)
                STATE["workers"].append(entry)
                status, text = "added", f"Added {name}."
            changed.append((name, w["url"], entry["enabled"]))
        results.append(dict(ok=True, code=tok, name=name, status=status, message=text))

    if changed:
        _save_workers_file()
        _register_workers(changed)
        WAKE.set()
    return jsonify(ok=any(r["ok"] for r in results), results=results)


@app.delete("/api/workers/<name>")
def delete_worker(name):
    """Remove a worker from the cluster. Only a disconnected or an offline worker can be removed."""
    with _LOCK:
        w = next((x for x in STATE["workers"] if x["name"] == name), None)
        if not w:
            return jsonify(error="unknown worker"), 404
        if w["enabled"] and w.get("online"):
            return jsonify(error=f"{name} is connected and online. Disconnect it first."), 409
        STATE["workers"].remove(w)
    _save_workers_file()
    _unregister_worker(name)
    WAKE.set()
    return jsonify(ok=True, name=name)


@app.post("/api/workers/<name>/<action>")
def set_worker(name, action):
    """Connect or disconnect one worker (or "all"). A disconnected worker is not used for new jobs."""
    if action not in ("connect", "disconnect"):
        return jsonify(error="unknown action"), 404
    enabled = action == "connect"
    with _LOCK:
        targets = [w for w in STATE["workers"] if name == "all" or w["name"] == name]
        if not targets:
            return jsonify(error="unknown worker"), 404
        for w in targets:
            w["enabled"] = enabled
        names = [w["name"] for w in targets]
    saved = _save_enabled(names, enabled)
    WAKE.set()
    return jsonify(ok=True, enabled=enabled, workers=names, saved=saved)


# ----------------------------------------------------------------------------- logs
def _demo_log(source, which):
    """Demo mode only: sample log text, so the Log window can be looked at without a cluster."""
    ctl = source == "controller"
    if which == "engine":
        rows = ([("09:09:11", "ComfyCluster controller"), ("09:09:12", "ONLINE  worker1"), ("09:09:13", "ONLINE  worker2"),
                 ("09:09:14", "ONLINE  worker3"), ("09:09:24", "OFFLINE worker4"), ("09:09:24", "3 worker(s) online")]
                if ctl else [("09:08:03", f"ComfyCluster worker '{source}'")]) + [
            ("09:08:41", "Cloning ComfyUI..."), ("09:09:02", "Installing ComfyUI requirements..."),
            ("09:09:25", "Cloning ComfyUI-Distributed..."), ("09:09:27", "Model present: v1-5-pruned-emaonly.safetensors")] + (
            [("09:09:28", "Registered 3 worker(s) in /content/ComfyUI/custom_nodes/ComfyUI-Distributed/gpu_config.json")] if ctl else []) + [
            ("09:09:28", "Starting ComfyUI..."), ("09:11:17", "ComfyUI is up"),
            ("09:11:19", '[cloudflared] 2026-10-27T09:11:19Z INF Requesting new quick Tunnel on trycloudflare.com...'),
            ("09:11:20", '[cloudflared] 2026-10-27T09:11:20Z INF Registered tunnel connection connIndex=0 location=sin11 protocol=quic'),
            ("09:11:31", "Warning: You are sending unauthenticated requests to the HF Hub. Please set a HF_TOKEN to enable higher rate limits."),
            ("09:11:40", "Starting the GUI..." if ctl else "Ready. Pairing code written.")]
        return [f"[{t}] {m}" for t, m in sorted(rows, key=lambda r: r[0])]
    return ["Total VRAM 15095 MB, total RAM 12979 MB", "pytorch version: 2.8.0+cu126",
            "Device: cuda:0 Tesla T4 : cudaMallocAsync", "Using pytorch attention",
            "Import times for custom nodes:", "   0.1 seconds: /content/ComfyUI/custom_nodes/ComfyCluster-Stats",
            "   0.4 seconds: /content/ComfyUI/custom_nodes/ComfyUI-Distributed", "Starting server",
            "To see the GUI go to: http://0.0.0.0:8188", "got prompt",
            "WARNING: object supporting the buffer API required", "100%|##########| 20/20 [00:12<00:00,  1.62it/s]",
            "Prompt executed in 14.21 seconds", "got prompt", "Prompt executed in 13.87 seconds"]


@app.get("/api/log/<source>/<which>")
def get_log(source, which):
    """The end of one log: the controller's own files, or a worker's through its /comfycluster/log add-on."""
    if which not in ("engine", "comfy"):
        return jsonify(error="unknown log"), 404
    try:
        n = max(1, min(2000, int(request.args.get("lines", "400"))))
        since = max(0, int(request.args.get("since", "0")))
    except ValueError:
        return jsonify(error="bad number"), 400
    if source != "controller":
        with _LOCK:
            w = next((dict(x) for x in STATE["workers"] if x["name"] == source), None)
        if not w:
            return jsonify(error="unknown worker"), 404
        if not w.get("online"):
            return jsonify(error=f"{source} is offline."), 503
    if STATE["demo"]:
        lines = _demo_log(source, which)
        size = sum(len(x) + 1 for x in lines)
        return jsonify(lines=[] if since >= size else lines[-n:], size=size)
    if source == "controller":
        path = STATE["logs"].get(which, "")
        return jsonify(tail_log(path, n, since) if path else {"lines": [], "size": 0, "missing": True})
    try:
        r = requests.get(w["url"].rstrip("/") + "/comfycluster/log", params=dict(file=which, lines=n, since=since), timeout=8)
    except requests.RequestException:
        return jsonify(error=f"Could not reach {source}."), 502
    if r.status_code == 404:
        return jsonify(error=f"{source} runs an older version. Run Worker.ipynb again to enable its log."), 404
    try:
        return jsonify(r.json()) if r.ok else (jsonify(error=f"{source} answered with an error ({r.status_code})."), 502)
    except ValueError:
        return jsonify(error=f"{source} sent an unreadable answer."), 502


# ----------------------------------------------------------------------------- uploads (models and custom nodes)
MODEL_FOLDERS = ["checkpoints", "loras", "vae", "controlnet", "upscale_models", "embeddings", "clip",
                 "clip_vision", "text_encoders", "diffusion_models", "unet", "vae_approx"]
MODEL_EXT = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft", ".onnx")
NODE_EXT = (".zip", ".py")
CHUNK = 32 * 1024 * 1024          # stays under the 100 MB request limit of Cloudflare tunnels
MAX_UNZIPPED = 8 * GIB
SAFE_NAME = re.compile(r"[\w][\w .+()\-]{0,150}")
_UP_LOCK = threading.Lock()


def _safe_name(raw):
    name = os.path.basename((raw or "").replace("\\", "/")).strip()
    return name if SAFE_NAME.fullmatch(name) and ".." not in name else ""


def _install_node_zip(zip_path, custom_dir, stem):
    """Unpack a custom node zip into custom_nodes/. Returns the folder name it ended up in."""
    with zipfile.ZipFile(zip_path) as z:
        infos = [i for i in z.infolist() if i.filename and not i.filename.startswith("__MACOSX/")]
        if not infos:
            raise ValueError("the zip is empty")
        if sum(i.file_size for i in infos) > MAX_UNZIPPED:
            raise ValueError("the zip is too large when unpacked")
        for i in infos:
            parts = i.filename.replace("\\", "/").split("/")
            if i.filename.startswith(("/", "\\")) or ".." in parts or (parts[0] and ":" in parts[0]):
                raise ValueError("the zip has unsafe paths")
        tops = {i.filename.replace("\\", "/").split("/")[0] for i in infos}
        wrapped = len(tops) == 1 and all("/" in i.filename.replace("\\", "/") for i in infos)
        folder = tops.pop() if wrapped else stem
        if not _safe_name(folder):
            raise ValueError("bad folder name in the zip")
        tmp = os.path.join(STATE["stage_dir"], "unzip-" + secrets.token_hex(4))
        try:
            z.extractall(tmp, members=infos)
            src = os.path.join(tmp, folder) if wrapped else tmp
            target = os.path.join(custom_dir, folder)
            if os.path.isdir(target):
                shutil.rmtree(target)
            os.makedirs(custom_dir, exist_ok=True)
            shutil.move(src, target)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return folder


@app.get("/api/upload/config")
def upload_config():
    return jsonify(enabled=bool(STATE["comfy_dir"]), folders=MODEL_FOLDERS, chunk=CHUNK,
                   model_ext=list(MODEL_EXT), node_ext=list(NODE_EXT))


@app.post("/api/upload/chunk")
def upload_chunk():
    """One slice of a file. Slices arrive in order (offset must match what is already on disk), so a
    broken connection can resume, and no single request is bigger than CHUNK."""
    if not STATE["comfy_dir"]:
        return jsonify(error="uploads are not available here"), 503
    kind, folder = request.args.get("kind", ""), request.args.get("folder", "")
    name = _safe_name(request.args.get("name", ""))
    try:
        offset, total = int(request.args.get("offset", "")), int(request.args.get("total", ""))
    except ValueError:
        return jsonify(error="bad offset or size"), 400
    if kind == "model" and folder in MODEL_FOLDERS and name.lower().endswith(MODEL_EXT):
        dest_dir = os.path.join(STATE["comfy_dir"], "models", folder)
    elif kind == "node" and name.lower().endswith(NODE_EXT):
        dest_dir = os.path.join(STATE["comfy_dir"], "custom_nodes")
    else:
        return jsonify(error="this file type is not accepted here"), 400
    if total <= 0 or offset < 0 or offset >= total:
        return jsonify(error="bad offset or size"), 400

    os.makedirs(STATE["stage_dir"], exist_ok=True)
    key = hashlib.sha1(f"{kind}|{folder}|{name}".encode()).hexdigest()[:20]
    part = os.path.join(STATE["stage_dir"], key + ".part")
    with _UP_LOCK:
        have = os.path.getsize(part) if os.path.isfile(part) else 0
        if offset == 0 and have:
            os.remove(part)
            have = 0
        if offset != have:
            return jsonify(error="out of sync", have=have), 409
        with open(part, "ab") as f:
            for blk in iter(lambda: request.stream.read(1 << 20), b""):
                f.write(blk)
        size = os.path.getsize(part)
        if size > total:
            os.remove(part)
            return jsonify(error="file is larger than announced"), 400
        if size < total:
            return jsonify(ok=True, have=size, done=False)

        try:                                                    # last slice: put the file in place
            os.makedirs(dest_dir, exist_ok=True)
            if kind == "node" and name.lower().endswith(".zip"):
                installed = _install_node_zip(part, dest_dir, name[:-4])
                os.remove(part)
                msg = f"Installed as {installed}"
            else:
                os.replace(part, os.path.join(dest_dir, name))
                msg = "Uploaded" if kind == "model" else "Installed"
        except (OSError, ValueError, zipfile.BadZipFile) as e:
            if os.path.isfile(part):
                os.remove(part)
            return jsonify(error=f"could not install it: {e}"), 422
    return jsonify(ok=True, done=True, message=msg, restart=kind == "node")



# ----------------------------------------------------------------------------- ComfyUI-Distributed config
def _save_enabled(names, enabled):
    """Mirror the switch into ComfyUI-Distributed's gpu_config.json so the extension skips the worker."""
    path = STATE["config_path"]
    if not path:
        return False
    with _CFG_LOCK:
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
            for entry in cfg.get("workers", []):
                if entry.get("id") in names or entry.get("name") in names:
                    entry["enabled"] = enabled
            tmp = path + ".part"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            os.replace(tmp, path)
            return True
        except (OSError, ValueError):
            return False


def _load_enabled():
    path = STATE["config_path"]
    if not path or not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return
    flags = {e.get("id"): bool(e.get("enabled", True)) for e in cfg.get("workers", [])}
    for w in STATE["workers"]:
        w["enabled"] = flags.get(w["name"], True)


def _register_workers(items):
    """Put new or re-addressed workers into ComfyUI-Distributed's gpu_config.json (keeps its other settings)."""
    path = STATE["config_path"]
    if not path:
        return False
    with _CFG_LOCK:
        try:
            cfg = {}
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as f:
                    cfg = json.load(f)
            cfg.setdefault("master", {})
            listed = cfg.setdefault("workers", [])
            for name, url, enabled in items:
                new = worker_entry(name, url, enabled)
                old = next((e for e in listed if e.get("id") == name or e.get("name") == name), None)
                if old:
                    old.update({k: new[k] for k in ("host", "port", "type")})
                else:
                    listed.append(new)
            tmp = path + ".part"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            os.replace(tmp, path)
            return True
        except (OSError, ValueError):
            return False


def _unregister_worker(name):
    """Take a worker out of ComfyUI-Distributed's gpu_config.json (its other settings stay)."""
    path = STATE["config_path"]
    if not path or not os.path.isfile(path):
        return False
    with _CFG_LOCK:
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
            cfg["workers"] = [e for e in cfg.get("workers", []) if name not in (e.get("id"), e.get("name"))]
            tmp = path + ".part"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            os.replace(tmp, path)
            return True
        except (OSError, ValueError):
            return False


def _save_workers_file():
    """Remember the paired workers (name and address) so a controller restart still knows them."""
    path = STATE["workers_file"]
    if not path:
        return
    with _LOCK:
        data = [{"name": w["name"], "url": w["url"]} for w in STATE["workers"]]
    try:
        tmp = path + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError:
        pass


# ----------------------------------------------------------------------------- status polling
def _ok(url):
    try:
        return requests.get(url.rstrip("/") + "/system_stats", timeout=6).ok
    except requests.RequestException:
        return False


def _gpu_name(raw):
    """'cuda:0 Tesla T4 : cudaMallocAsync' -> 'Tesla T4'"""
    return re.sub(r"^cuda:\d+\s*", "", raw or "").split(" : ")[0].strip()


def _gib(v):
    return None if v is None else round(v / GIB, 1)


def _probe_worker(w):
    base = w["url"].rstrip("/")
    try:
        d = requests.get(base + "/system_stats", timeout=6).json()
        dev = (d.get("devices") or [{}])[0]
        sysd = d.get("system") or {}
        q = requests.get(base + "/queue", timeout=6).json()
    except (requests.RequestException, ValueError):
        return dict(online=False, running=0, pending=0)
    try:                       # CPU / GPU load: only workers that run comfycluster_stats.py answer this
        ex = requests.get(base + "/comfycluster/stats", timeout=6).json()
        if not isinstance(ex, dict):
            ex = {}
    except (requests.RequestException, ValueError):
        ex = {}
    total, free = dev.get("vram_total", 0), dev.get("vram_free", 0)
    ram_total = ex.get("ram_total", sysd.get("ram_total"))
    ram_used = ex.get("ram_used", None if sysd.get("ram_total") is None else sysd["ram_total"] - sysd.get("ram_free", 0))
    return dict(online=True, gpu=_gpu_name(dev.get("name")), vram_total=round(total / GIB, 1),
                vram_used=round((total - free) / GIB, 1),
                running=len(q.get("queue_running", [])), pending=len(q.get("queue_pending", [])),
                gpu_util=ex.get("gpu_util"), gpu_temp=ex.get("gpu_temp"), gpu_power=ex.get("gpu_power"),
                cpu_name=ex.get("cpu_name", ""), cpu_cores=ex.get("cpu_cores"), cpu_percent=ex.get("cpu_percent"),
                ram_total=_gib(ram_total), ram_used=_gib(ram_used),
                disk_total=_gib(ex.get("disk_total")), disk_used=_gib(ex.get("disk_used")),
                comfy_version=sysd.get("comfyui_version", ""), torch_version=sysd.get("pytorch_version", ""),
                python_version=(sysd.get("python_version") or "").split(" ")[0])


_DEMO_BASE = dict(online=True, gpu="Tesla T4", vram_total=15.0, cpu_name="Intel(R) Xeon(R) CPU @ 2.20GHz", cpu_cores=2,
                  ram_total=12.7, disk_total=112.6, comfy_version="0.3.60", torch_version="2.8.0+cu126", python_version="3.12.12")
DEMO_FAKE = {   # demo mode only: what the monitor window looks like with a live cluster
    "worker1": dict(_DEMO_BASE, vram_used=11.2, running=1, pending=0, gpu_util=96, gpu_temp=71, gpu_power=68.4,
                    cpu_percent=48.5, ram_used=5.9, disk_used=41.3),
    "worker2": dict(_DEMO_BASE, vram_used=3.1, running=0, pending=0, gpu_util=4, gpu_temp=43, gpu_power=27.9,
                    cpu_percent=7.2, ram_used=3.4, disk_used=39.8),
    "worker3": dict(_DEMO_BASE, vram_used=9.8, running=1, pending=2, gpu_util=88, gpu_temp=68, gpu_power=64.0,
                    cpu_percent=71.0, ram_used=9.8, disk_used=44.0),
    "worker4": dict(online=False, running=0, pending=0),
}


_DEMO_NEW = dict(_DEMO_BASE, vram_used=2.4, running=0, pending=0, gpu_util=3, gpu_temp=41, gpu_power=27.0,
                 cpu_percent=6.0, ram_used=3.1, disk_used=38.5)


def _demo_probe(name):
    return dict(DEMO_FAKE.get(name, _DEMO_NEW))


def poller():
    """Every few seconds: is ComfyUI up, and what are the workers doing."""
    pool = ThreadPoolExecutor(max_workers=8)
    while True:
        snap = list(STATE["workers"])
        if STATE["demo"]:
            results = [_demo_probe(w["name"]) for w in snap]
            ready = False
        else:
            ready = bool(STATE["comfy_local"]) and _ok(STATE["comfy_local"])
            results = list(pool.map(_probe_worker, snap))
        with _LOCK:
            STATE["comfy_ready"] = ready
            for w, r in zip(snap, results):
                w.update(r)
        WAKE.wait(5)
        WAKE.clear()


# ----------------------------------------------------------------------------- main
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
    ap.add_argument("--workers-file", help="JSON list of {name, url}; read at start, rewritten when workers are added")
    ap.add_argument("--distributed-config", default="", help="ComfyUI-Distributed gpu_config.json (connect / disconnect)")
    ap.add_argument("--engine-log", default="", help="the controller engine's log file (shown in the Log window)")
    ap.add_argument("--comfy-log", default="", help="ComfyUI's log file (shown in the Log window)")
    ap.add_argument("--comfy-dir", default="", help="ComfyUI folder; enables the Upload window (models, custom nodes)")
    ap.add_argument("--demo", action="store_true", help="no ComfyUI, shows the empty frame and four sample workers")
    args = ap.parse_args()

    if args.tunnel and not args.key:
        args.key = secrets.token_urlsafe(16)
    ACCESS_KEY = args.key or ACCESS_KEY
    comfy_dir = args.comfy_dir or (os.path.join(args.cache_dir, "demo-comfy") if args.demo else "")
    STATE.update(demo=args.demo, comfy_url=args.comfy_url, comfy_local=args.comfy_local,
                 config_path=args.distributed_config, comfy_dir=comfy_dir, workers_file=args.workers_file or "",
                 logs={"engine": args.engine_log, "comfy": args.comfy_log},
                 stage_dir=os.path.join(args.cache_dir, "uploads"))
    if args.workers_file and os.path.isfile(args.workers_file):
        with open(args.workers_file, encoding="utf-8") as f:
            STATE["workers"] = json.load(f)
    elif args.demo:
        STATE["workers"] = [{"name": n, "url": ""} for n in DEMO_FAKE]
    for w in STATE["workers"]:
        w.update(enabled=True, online=False, gpu="", vram_used=0, vram_total=0, running=0, pending=0)
    _load_enabled()
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
