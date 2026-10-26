"""Install, configure and launch ComfyUI. Shared by the controller and the workers."""
import json
import os
import subprocess
import sys
import time
import urllib.request

COMFY_REPO = "https://github.com/comfyanonymous/ComfyUI.git"
DIST_REPO = "https://github.com/robertvoy/ComfyUI-Distributed.git"


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])} failed:\n{(r.stdout + r.stderr)[-1500:]}")


def install(root, log=print):
    """Clone ComfyUI and the ComfyUI-Distributed extension and install their requirements."""
    comfy = os.path.join(root, "ComfyUI")
    if not os.path.isdir(os.path.join(comfy, ".git")):
        log("Cloning ComfyUI...")
        _run(["git", "clone", "--depth", "1", COMFY_REPO, comfy])
    log("Installing ComfyUI requirements...")
    _run([sys.executable, "-m", "pip", "install", "-q", "-r", os.path.join(comfy, "requirements.txt")])
    dist = os.path.join(comfy, "custom_nodes", "ComfyUI-Distributed")
    if not os.path.isdir(dist):
        log("Cloning ComfyUI-Distributed...")
        _run(["git", "clone", "--depth", "1", DIST_REPO, dist])
    req = os.path.join(dist, "requirements.txt")
    if os.path.isfile(req):
        _run([sys.executable, "-m", "pip", "install", "-q", "-r", req])
    return comfy


def download_models(comfy_dir, models_file, log=print):
    """Download every entry of models.json ({"url", "folder", "file"}) unless it is already there."""
    with open(models_file, encoding="utf-8") as f:
        models = json.load(f)
    token = os.environ.get("HF_TOKEN", "")
    for m in models:
        dest_dir = os.path.join(comfy_dir, "models", m["folder"])
        dest = os.path.join(dest_dir, m["file"])
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            log(f"Model present: {m['file']}")
            continue
        os.makedirs(dest_dir, exist_ok=True)
        headers = {"Authorization": f"Bearer {token}"} if token and "huggingface.co" in m["url"] else {}
        tmp, last = dest + ".part", -10
        with urllib.request.urlopen(urllib.request.Request(m["url"], headers=headers)) as r, open(tmp, "wb") as out:
            total, done = int(r.headers.get("Content-Length") or 0), 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if total and done * 100 // total >= last + 10:
                    last = done * 100 // total
                    log(f"Downloading {m['file']}: {last}%")
        os.replace(tmp, dest)
        log(f"Model ready: {m['file']}")


def write_worker_config(comfy_dir, workers, log=print):
    """Register remote workers with ComfyUI-Distributed (gpu_config.json).

    The file format follows the extension's own config. If a future version of the extension
    changes it, workers can still be added by hand in its panel (Add Worker > Remote).
    """
    path = os.path.join(comfy_dir, "custom_nodes", "ComfyUI-Distributed", "gpu_config.json")
    cfg = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            cfg = {}
    cfg.setdefault("master", {})
    cfg["workers"] = [{
        "id": w["name"], "name": w["name"],
        "host": w["url"].split("://", 1)[1], "port": 443,
        "type": "remote", "enabled": True, "extra_args": "",
    } for w in workers]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    log(f"Registered {len(workers)} worker(s) in {path}")


def launch(comfy_dir, port, log_path):
    log = open(log_path, "a")
    return subprocess.Popen(
        [sys.executable, "main.py", "--listen", "0.0.0.0", "--port", str(port),
         "--enable-cors-header", "*", "--disable-auto-launch"],
        cwd=comfy_dir, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def probe(base_url, timeout=5):
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/system_stats", timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def wait_ready(port, proc, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if proc.poll() is not None:
            raise RuntimeError("ComfyUI exited while starting. See comfyui.log")
        if probe(f"http://127.0.0.1:{port}", 3):
            return
        time.sleep(2)
    raise RuntimeError("ComfyUI did not become ready in time")
