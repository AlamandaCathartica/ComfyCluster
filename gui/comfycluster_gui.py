"""ComfyCluster controller GUI: a title, one big frame that holds ComfyUI, and a worker monitor window.

    python gui/comfycluster_gui.py --demo            look around, no ComfyUI needed
    python gui/comfycluster_gui.py --tunnel ...      Colab: public Cloudflare tunnel protected by a key
"""
import argparse
import hashlib
import hmac
import io
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import webbrowser
import zipfile
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote, urljoin, urlparse

import requests
from flask import Flask, Response, g, jsonify, request, send_file, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "engine"))
from logtail import tail_log  # noqa: E402
from pairing import PREFIX, parse_code  # noqa: E402
from comfy import worker_entry  # noqa: E402
import pipeline  # noqa: E402
import modelpush  # noqa: E402
WEB = os.path.join(HERE, "web")
GIB = 1024 ** 3
ACCESS_KEY = os.environ.get("COMFYCLUSTER_KEY", "")
try:
    VERSION = open(os.path.join(ROOT, "VERSION"), encoding="utf-8").read().strip()
except OSError:
    VERSION = "dev"

STATE = {"demo": False, "comfy_url": "", "comfy_local": "", "comfy_ready": False,
         "workers": [], "workers_file": "", "config_path": "", "comfy_dir": "", "stage_dir": "", "logs": {},
         "assign_file": "", "rules": [], "sync": None}
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
_INDEX = {"mtime": 0.0, "html": ""}


@app.route("/")
def index():
    """The page, with the ComfyUI address already inside it: the frame can start loading as soon as the page
    arrives, instead of after a second request, and the browser can open the connection to ComfyUI early."""
    path = os.path.join(WEB, "index.html")
    mtime = os.path.getmtime(path)
    if _INDEX["mtime"] != mtime:
        with open(path, encoding="utf-8") as f:
            _INDEX.update(mtime=mtime, html=f.read())
    url = STATE["comfy_url"] if re.fullmatch(r"https?://[\w.\-:]+/?", STATE["comfy_url"] or "") else ""
    boot = ""
    if url:
        boot = (f'<link rel="preconnect" href="{url}" crossorigin>'
                f'<script>window.BOOT={json.dumps({"url": url, "ready": bool(STATE["comfy_ready"])})};</script>')
    return Response(_INDEX["html"].replace("<!--BOOT-->", boot, 1), mimetype="text/html")


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
                same_name["key"] = w.get("key", "")             # a restarted worker has a new key (or none: an old worker)
                same_name.update(probe)
                entry, status, text = same_name, "updated", f"Updated {name} with its new address."
            else:
                entry = dict(name=name, url=w["url"], key=w.get("key", ""), enabled=True)
                entry.update(probe)
                STATE["workers"].append(entry)
                status, text = "added", f"Added {name}."
            changed.append((name, w["url"], entry["enabled"]))
        results.append(dict(ok=True, code=tok, name=name, status=status, message=text))

    if changed:
        _save_workers_file()
        _register_workers(changed)
        WAKE.set()
        _sync_wake()
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
    if STATE["sync"]:
        STATE["sync"].forget(name)
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
                if kind == "model":
                    _sync_wake()
        except (OSError, ValueError, zipfile.BadZipFile) as e:
            if os.path.isfile(part):
                os.remove(part)
            return jsonify(error=f"could not install it: {e}"), 422
    return jsonify(ok=True, done=True, message=msg, restart=kind == "node")



# ----------------------------------------------------------------------------- models and nodes from a link
# The controller downloads the file itself (the browser only starts it and shows progress).
LINKS = {}
_PRIVATE_OK = os.environ.get("COMFYCLUSTER_ALLOW_PRIVATE_LINKS") == "1"      # for tests only
_CD_NAME = re.compile(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", re.I)


def _public_url(u):
    """Refuse links that point into this machine or its network (a download link has no business there)."""
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("Enter a link that starts with http:// or https://")
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except OSError:
        raise ValueError("Could not find that address.") from None
    if not _PRIVATE_OK and any(not ipaddress.ip_address(i[4][0]).is_global for i in infos):
        raise ValueError("That address is not on the public internet.")


def _link_name(given, resp, url):
    cands = [given]
    m = _CD_NAME.search(resp.headers.get("Content-Disposition", ""))
    if m:
        cands.append(unquote(m.group(1).strip()))
    cands.append(unquote(urlparse(url).path))
    for c in cands:
        n = _safe_name(c or "")
        if n:
            return n
    return ""


def _link_download(job, url, token):
    part = os.path.join(STATE["stage_dir"], "link-" + job["id"] + ".part")
    try:
        job["state"] = "connecting"
        host0, cur = urlparse(url).hostname, url
        for _ in range(6):
            _public_url(cur)
            hdr = {"User-Agent": "ComfyCluster"}
            if token and urlparse(cur).hostname == host0:         # the token never follows a redirect to another site
                hdr["Authorization"] = "Bearer " + token
            r = requests.get(cur, headers=hdr, stream=True, allow_redirects=False, timeout=(15, 60))
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                cur = urljoin(cur, r.headers["Location"])
                r.close()
                continue
            break
        else:
            raise ValueError("Too many redirects.")
        if r.status_code in (401, 403):
            raise ValueError(f"The site refused the download ({r.status_code}). It may need an access token.")
        if not r.ok:
            raise ValueError(f"The site answered with error {r.status_code}.")
        if "text/html" in r.headers.get("Content-Type", "").lower():
            raise ValueError("This link opens a web page, not a file. Use the direct download link (on Hugging Face, the one under \u201cresolve\u201d).")
        name = _link_name(job["name"], r, cur)
        exts = MODEL_EXT if job["kind"] == "model" else (".zip",)
        if not name or not name.lower().endswith(exts):
            raise ValueError("Could not tell the file name or type. Type the file name (for example model.safetensors) and try again.")
        job["name"] = name
        dest_dir = os.path.join(STATE["comfy_dir"], "models", job["folder"]) if job["kind"] == "model" else os.path.join(STATE["comfy_dir"], "custom_nodes")
        os.makedirs(dest_dir, exist_ok=True)
        os.makedirs(STATE["stage_dir"], exist_ok=True)
        job["total"] = int(r.headers.get("Content-Length") or 0)
        if job["total"] and job["total"] + GIB > shutil.disk_usage(dest_dir).free:
            raise ValueError("Not enough free disk space for this file.")
        job["state"] = "downloading"
        with open(part, "wb") as f:
            for blk in r.iter_content(1 << 20):
                if job["cancel"]:
                    raise InterruptedError
                f.write(blk)
                job["done"] += len(blk)
        if job["total"] and job["done"] != job["total"]:
            raise ValueError("The download ended early. Try again.")
        if job["kind"] == "node":
            job["msg"] = "Installed as " + _install_node_zip(part, dest_dir, name[:-4])
            os.remove(part)
        else:
            os.replace(part, os.path.join(dest_dir, name))
            job["msg"] = "Saved in models/" + job["folder"]
            _sync_wake()
        job["state"] = "done"
    except InterruptedError:
        job.update(state="cancelled", msg="Cancelled")
    except (ValueError, OSError, requests.RequestException, zipfile.BadZipFile) as e:
        job.update(state="error", msg=str(e) if isinstance(e, ValueError) else f"Download failed: {e}"[:300])
    finally:
        if os.path.isfile(part):
            try:
                os.remove(part)
            except OSError:
                pass


def _link_demo(job):
    job.update(state="downloading", total=1_800_000_000)
    while job["done"] < job["total"] and not job["cancel"]:
        time.sleep(0.25)
        job["done"] += 90_000_000
    job.update(state="cancelled" if job["cancel"] else "done", msg="Cancelled" if job["cancel"] else "Demo: nothing was downloaded")


@app.post("/api/upload/link")
def upload_link():
    if not STATE["comfy_dir"]:
        return jsonify(error="downloads are not available here"), 503
    b = request.get_json(silent=True) or {}
    url, kind, folder = str(b.get("url", "")).strip(), b.get("kind", "model"), b.get("folder", "")
    name, token = str(b.get("name", "")).strip(), str(b.get("token", "")).strip()
    if not url or len(url) > 2000:
        return jsonify(error="Paste a link first."), 400
    if urlparse(url).scheme not in ("http", "https"):
        return jsonify(error="The link must start with http:// or https://"), 400
    if kind not in ("model", "node") or (kind == "model" and folder not in MODEL_FOLDERS):
        return jsonify(error="Choose where to save it."), 400
    if name and not _safe_name(name):
        return jsonify(error="That file name has characters that are not allowed."), 400
    job = dict(id=secrets.token_hex(5), url=url.split("?")[0][:200], kind=kind, folder=folder if kind == "model" else "",
               name=name, state="starting", done=0, total=0, msg="", cancel=False, t0=time.time())
    for old in sorted(LINKS.values(), key=lambda j: j["t0"])[:-19]:
        LINKS.pop(old["id"], None)
    LINKS[job["id"]] = job
    target = (lambda: _link_demo(job)) if STATE["demo"] else (lambda: _link_download(job, url, token))
    threading.Thread(target=target, daemon=True).start()
    return jsonify(ok=True, job=job["id"])


@app.get("/api/upload/link/<jid>")
def upload_link_status(jid):
    j = LINKS.get(jid)
    return jsonify({k: v for k, v in j.items() if k != "cancel"}) if j else (jsonify(error="unknown download"), 404)


@app.post("/api/upload/link/<jid>/cancel")
def upload_link_cancel(jid):
    j = LINKS.get(jid)
    if not j:
        return jsonify(error="unknown download"), 404
    j["cancel"] = True
    return jsonify(ok=True)


# ----------------------------------------------------------------------------- model sync (controller -> every worker)
# A model enters the cluster here, on the controller (upload, link, models.json). modelpush.SyncManager then sends
# every model the controller has to every connected worker. Workers never download and never talk to each other.
def _engine_log(msg):
    """Model sync messages go to the engine log, so they show in the Log window next to what the notebook does."""
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    path = STATE["logs"].get("engine", "")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def _sync_workers():
    """What the sync needs to know about each worker. The key stays on the server: it never goes to the browser."""
    with _LOCK:
        return [dict(name=w["name"], url=w["url"], key=w.get("key", ""), online=bool(w.get("online")),
                     enabled=w.get("enabled", True)) for w in STATE["workers"]]


def _sync_wake():
    if STATE["sync"]:
        STATE["sync"].wake()


_DEMO_SYNC = {"auto": True, "enabled": True, "controller": {"state": "idle", "models": 4, "bytes": 14_200_000_000, "skipped": [], "waiting": 0},
              "workers": {"worker1": dict(state="synced", models=4, synced=4, queue=0, file="", done=0, total=0, error="", note=""),
                          "worker2": dict(state="syncing", models=4, synced=2, queue=2, file="checkpoints/sdxl/base.safetensors",
                                          done=2_300_000_000, total=6_900_000_000, error="", note=""),
                          "worker3": dict(state="error", models=4, synced=3, queue=1, file="", done=0, total=0,
                                          error="loras/detail.safetensors: not enough free disk space on the worker", note="")}}


@app.get("/api/sync")
def sync_status():
    if STATE["demo"]:
        return jsonify(_DEMO_SYNC)
    if not STATE["sync"]:
        return jsonify(enabled=False)
    return jsonify(enabled=True, **STATE["sync"].snapshot())


@app.post("/api/sync/run")
def sync_run():
    """Look at every worker now instead of at the next scheduled pass."""
    if not STATE["sync"]:
        return jsonify(error="Model sync is not available here."), 503
    STATE["sync"].run_now()
    return jsonify(ok=True)


@app.post("/api/sync/auto")
def sync_auto():
    """Turn the automatic pass on or off. Off: models are only sent when Sync now is pressed."""
    if not STATE["sync"]:
        return jsonify(error="Model sync is not available here."), 503
    STATE["sync"].set_auto(bool((request.get_json(silent=True) or {}).get("on")))
    return jsonify(ok=True, auto=STATE["sync"].auto)


# ----------------------------------------------------------------------------- files (download from the controller)
FILE_ROOTS = {                       # id: (label, folder inside ComfyUI, newest first?)
    "output": ("Outputs", "output", True), "input": ("Inputs", "input", True),
    "workflows": ("Workflows", os.path.join("user", "default", "workflows"), True),
    "models": ("Models", "models", False), "custom_nodes": ("Custom nodes", "custom_nodes", False),
}
MAX_ENTRIES = 1000
MAX_ZIP_FILES = 20000
SKIP_DIRS = {"__pycache__", ".git", ".ipynb_checkpoints"}


class _Sink(io.RawIOBase):
    """Collects what zipfile writes, so a zip can be sent while it is still being made."""
    def __init__(self):
        self.parts, self.pos = [], 0

    def writable(self):
        return True

    def write(self, b):
        self.parts.append(bytes(b))
        self.pos += len(b)
        return len(b)

    def tell(self):
        return self.pos

    def drain(self):
        out, self.parts = b"".join(self.parts), []
        return out


def _file_target(root, rel):
    """(root folder, full path) for a root id and a relative path, or None when it would leave the root."""
    if root not in FILE_ROOTS or not STATE["comfy_dir"]:
        return None
    base = os.path.realpath(os.path.join(STATE["comfy_dir"], FILE_ROOTS[root][1]))
    parts = [p for p in (rel or "").replace("\\", "/").split("/") if p]
    if any(p in (".", "..") or ":" in p for p in parts):
        return None
    full = os.path.realpath(os.path.join(base, *parts))
    return (base, full) if full == base or full.startswith(base + os.sep) else None


@app.get("/api/files/roots")
def files_roots():
    out = []
    for rid, (label, sub, _) in FILE_ROOTS.items():
        if STATE["comfy_dir"] and os.path.isdir(os.path.join(STATE["comfy_dir"], sub)):
            out.append(dict(id=rid, label=label))
    return jsonify(enabled=bool(STATE["comfy_dir"]), roots=out)


@app.get("/api/files/list")
def files_list():
    root, rel = request.args.get("root", ""), request.args.get("path", "")
    t = _file_target(root, rel)
    if not t or not os.path.isdir(t[1]):
        return jsonify(error="That folder is not available."), 404
    items = []
    try:
        with os.scandir(t[1]) as it:
            for e in it:
                if e.name.startswith(".") or e.name in SKIP_DIRS:
                    continue
                try:
                    st, isdir = e.stat(), e.is_dir()
                except OSError:
                    continue
                items.append(dict(name=e.name, dir=isdir, size=0 if isdir else st.st_size, mtime=int(st.st_mtime)))
    except OSError:
        return jsonify(error="Could not read that folder."), 500
    newest = FILE_ROOTS[root][2]
    items.sort(key=lambda x: (not x["dir"], -x["mtime"] if newest else x["name"].lower()))
    return jsonify(root=root, path="/".join(p for p in rel.replace("\\", "/").split("/") if p),
                   entries=items[:MAX_ENTRIES], truncated=len(items) > MAX_ENTRIES)


@app.get("/api/files/get")
def files_get():
    t = _file_target(request.args.get("root", ""), request.args.get("path", ""))
    if not t or not os.path.isfile(t[1]) or os.path.basename(t[1]).startswith("."):
        return jsonify(error="File not found."), 404
    return send_file(t[1], as_attachment=True, download_name=os.path.basename(t[1]), conditional=True)


@app.get("/api/files/zip")
def files_zip():
    root = request.args.get("root", "")
    t = _file_target(root, request.args.get("path", ""))
    if not t or not os.path.isdir(t[1]):
        return jsonify(error="Folder not found."), 404
    files, total = [], 0
    for d, dirs, names in os.walk(t[1]):
        dirs[:] = [x for x in dirs if not x.startswith(".") and x not in SKIP_DIRS]
        for n in names:
            full = os.path.join(d, n)
            if n.startswith(".") or os.path.islink(full):
                continue
            try:
                total += os.path.getsize(full)
            except OSError:
                continue
            files.append((full, os.path.relpath(full, t[1]).replace(os.sep, "/")))
            if len(files) > MAX_ZIP_FILES or total > MAX_UNZIPPED:
                return jsonify(error="This folder is too big to zip. Open it and download files one at a time."), 413
    if not files:
        return jsonify(error="This folder is empty."), 404

    def stream():
        sink = _Sink()
        with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED) as z:
            for full, arc in files:
                try:
                    with open(full, "rb") as src, z.open(arc, "w", force_zip64=True) as dst:
                        for blk in iter(lambda: src.read(1 << 20), b""):
                            dst.write(blk)
                            data = sink.drain()
                            if data:
                                yield data
                except OSError:
                    continue
        yield sink.drain()
    name = (os.path.basename(t[1].rstrip(os.sep)) or root) + ".zip"
    resp = Response(stream(), mimetype="application/zip")
    resp.headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return resp


# ----------------------------------------------------------------------------- node assignment
# Rules say which machine runs which node type. A workflow (API format) is split by them and run step by step.
JOBS = {}
MAX_RULES = 50
_T = lambda out, node=False: {"output": out, "output_node": node}          # noqa: E731
DEMO_INFO = {                          # demo mode only: enough node types for the example workflow
    "CheckpointLoaderSimple": _T(["MODEL", "CLIP", "VAE"]), "CLIPTextEncode": _T(["CONDITIONING"]),
    "EmptyLatentImage": _T(["LATENT"]), "KSampler": _T(["LATENT"]), "VAEDecode": _T(["IMAGE"]),
    "UltralyticsDetectorProvider": _T(["BBOX_DETECTOR", "SEGM_DETECTOR"]),
    "FaceDetailer": _T(["IMAGE", "IMAGE", "MASK", "DETAILER_PIPE", "IMAGE"]), "LoadImage": _T(["IMAGE", "MASK"]),
    "ImageScale": _T(["IMAGE"]), "SaveImage": _T([], True), "PreviewImage": _T([], True),
}
_NODES = {"t": 0.0, "types": []}


def _machine_bases():
    """Machines a plan may use right now: the controller and every connected, online worker."""
    with _LOCK:
        ws = [dict(w) for w in STATE["workers"]]
    out = {pipeline.CTL: STATE["comfy_local"]}
    for w in ws:
        if w.get("enabled") and w.get("online"):
            out[w["name"]] = w["url"]
    return out


def _clean_rules(raw):
    names = {pipeline.CTL} | {w["name"] for w in STATE["workers"]}
    out = []
    for r in (raw if isinstance(raw, list) else [])[:MAX_RULES]:
        if not isinstance(r, dict):
            continue
        t, w, src = str(r.get("node_type", "")).strip()[:120], str(r.get("worker", "")), str(r.get("source") or "auto")
        if not t or w not in names:
            continue
        out.append(dict(node_type=t, worker=w, source=src if src in names else "auto", enabled=bool(r.get("enabled", True))))
    return out


def _load_rules():
    path = STATE["assign_file"]
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                STATE["rules"] = _clean_rules(json.load(f))
        except (OSError, ValueError):
            STATE["rules"] = []


def _save_rules():
    path = STATE["assign_file"]
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(STATE["rules"], f)
        os.replace(tmp, path)
    except OSError:
        pass


@app.get("/api/assign/rules")
def assign_rules():
    with _LOCK:
        names = [pipeline.CTL] + [w["name"] for w in STATE["workers"]]
    return jsonify(rules=STATE["rules"], machines=names, demo=STATE["demo"])


@app.put("/api/assign/rules")
def assign_save_rules():
    body = request.get_json(silent=True) or {}
    STATE["rules"] = _clean_rules(body.get("rules"))
    _save_rules()
    return jsonify(ok=True, rules=STATE["rules"])


@app.get("/api/assign/nodes")
def assign_nodes():
    """Node types the controller knows, for the autocomplete in the rule editor."""
    if STATE["demo"]:
        return jsonify(types=sorted(DEMO_INFO))
    if time.time() - _NODES["t"] > 300 and STATE["comfy_local"]:
        try:
            r = requests.get(STATE["comfy_local"].rstrip("/") + "/object_info", timeout=30)
            _NODES.update(t=time.time(), types=sorted(r.json().keys()) if r.ok else [])
        except (requests.RequestException, ValueError):
            pass
    return jsonify(types=_NODES["types"])


def _make_plan(body):
    wf = body.get("workflow")
    if isinstance(wf, str):
        try:
            wf = json.loads(wf)
        except ValueError:
            raise pipeline.PlanError("The workflow is not valid JSON.") from None
    rules = _clean_rules(body.get("rules", STATE["rules"]))
    bases = _machine_bases()
    mirror = body.get("mode") == "mirror"
    mo = body.get("mirror") if isinstance(body.get("mirror"), dict) else {}
    chosen = [m for m in (mo.get("machines") or []) if isinstance(m, str)]
    for m in chosen:
        if m not in bases:
            raise pipeline.PlanError(f"{m} is not connected.")

    def make(info):
        if mirror:
            return pipeline.build_mirror(wf, chosen, mo.get("overrides") if isinstance(mo.get("overrides"), list) else [],
                                         info, bool(mo.get("randomize", True)))
        return pipeline.build_plan(wf, rules, list(bases), info)
    if STATE["demo"]:
        plan = make(DEMO_INFO.get)
        plan["warnings"] = []
        return plan, None
    if not STATE["comfy_ready"]:
        raise pipeline.PlanError("ComfyUI on the controller is not ready yet.")
    cluster = pipeline.Cluster(bases)
    plan = make(cluster.info_any)
    return cluster.missing(plan), cluster


@app.post("/api/assign/plan")
def assign_plan():
    try:
        plan, _ = _make_plan(request.get_json(silent=True) or {})
    except pipeline.PlanError as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True, plan=pipeline.public_plan(plan))


def _demo_execute(job, plan):
    """Demo mode: no ComfyUI behind it, so the steps are only pretended."""
    try:
        for i, st in enumerate(plan["stages"]):
            job["stages"][i].update(state="running", msg="Running\u2026")
            time.sleep(1.4)
            job["stages"][i].update(state="done" if st["runs"] else "skipped", msg="Done" if st["runs"] else "Nothing to run here")
        job["state"] = "done"
    except Exception as e:  # noqa: BLE001
        job.update(state="error", error=str(e))


@app.post("/api/assign/run")
def assign_run():
    try:
        plan, cluster = _make_plan(request.get_json(silent=True) or {})
    except pipeline.PlanError as e:
        return jsonify(ok=False, error=str(e)), 400
    if plan.get("warnings"):
        return jsonify(ok=False, error=plan["warnings"][0]), 400
    job = pipeline.new_job(plan)
    for old in sorted(JOBS.values(), key=lambda j: j["t0"])[:-19]:
        JOBS.pop(old["id"], None)
    JOBS[job["id"]] = job
    target = (lambda: _demo_execute(job, plan)) if STATE["demo"] else (lambda: pipeline.execute(cluster, plan, job))
    threading.Thread(target=target, daemon=True).start()
    return jsonify(ok=True, job=job["id"])


@app.get("/api/assign/job/<jid>")
def assign_job(jid):
    job = JOBS.get(jid)
    return jsonify(job) if job else (jsonify(error="unknown job"), 404)


@app.get("/api/assign/view/<machine>")
def assign_view(machine):
    """Show an image a run produced (the page cannot reach the workers itself)."""
    base = _machine_bases().get(machine)
    typ, name, sub = request.args.get("type", ""), request.args.get("filename", ""), request.args.get("subfolder", "")
    if not base or STATE["demo"] or typ not in ("output", "temp", "input") or name != _safe_name(name) \
            or ".." in sub or not re.fullmatch(r"[\w .\-/]*", sub):
        return jsonify(error="not available"), 404
    try:
        r = requests.get(base.rstrip("/") + "/view", params=dict(filename=name, subfolder=sub, type=typ), timeout=30)
    except requests.RequestException:
        return jsonify(error="could not reach the machine"), 502
    if not r.ok:
        return jsonify(error="image not found"), 404
    return Response(r.content, mimetype=r.headers.get("Content-Type", "image/png"))


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
        data = [{"name": w["name"], "url": w["url"], "key": w.get("key", "")} for w in STATE["workers"]]
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
    if args.demo:
        wf_dir = os.path.join(comfy_dir, "user", "default", "workflows")
        os.makedirs(wf_dir, exist_ok=True)
        os.makedirs(os.path.join(comfy_dir, "output"), exist_ok=True)
        if not os.listdir(wf_dir):
            with open(os.path.join(wf_dir, "demo-workflow.json"), "w", encoding="utf-8") as f:
                json.dump({"note": "Demo file"}, f)
    if args.workers_file and os.path.isfile(args.workers_file):
        with open(args.workers_file, encoding="utf-8") as f:
            STATE["workers"] = json.load(f)
    elif args.demo:
        STATE["workers"] = [{"name": n, "url": ""} for n in DEMO_FAKE]
    for w in STATE["workers"]:
        w.update(enabled=True, online=False, gpu="", vram_used=0, vram_total=0, running=0, pending=0)
    _load_enabled()
    STATE["assign_file"] = os.path.join(os.path.dirname(os.path.abspath(args.workers_file)) if args.workers_file else args.cache_dir,
                                       "comfycluster_assign.json")
    _load_rules()
    threading.Thread(target=poller, daemon=True).start()
    if comfy_dir and not args.demo:
        STATE["sync"] = modelpush.SyncManager(os.path.join(comfy_dir, "models"), os.path.join(args.cache_dir, "model-hashes.json"),
                                              _sync_workers, log=_engine_log)
        STATE["sync"].start()

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
