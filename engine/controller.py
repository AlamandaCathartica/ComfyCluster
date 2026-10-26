"""Controller: ComfyUI + ComfyUI-Distributed, the paired workers, and the GUI page.

    python engine/controller.py --codes "CW1-... CW1-..." --key SECRET --url-file link.txt
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "gui"))

import comfy  # noqa: E402
import pairing  # noqa: E402
import tunnel  # noqa: E402


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="ComfyCluster controller")
    ap.add_argument("--root", default="/content")
    ap.add_argument("--cache-dir", default=os.path.join(ROOT, ".cache"))
    ap.add_argument("--models", default=os.path.join(ROOT, "models.json"))
    ap.add_argument("--codes", default="", help="worker pairing codes, separated by spaces or new lines")
    ap.add_argument("--comfy-port", type=int, default=8188)
    ap.add_argument("--gui-port", type=int, default=7860)
    ap.add_argument("--key", default="", help="access key for the GUI link")
    ap.add_argument("--url-file", required=True, help="the GUI link is written here once it is up")
    args = ap.parse_args()

    log("ComfyCluster controller")
    workers = []
    for w in pairing.parse_codes(args.codes):
        ok = comfy.probe(w["url"], 10)
        log(f"{'ONLINE ' if ok else 'OFFLINE'} {w['name']}")
        if ok:
            workers.append(w)
    log(f"{len(workers)} worker(s) online")

    comfy_dir = comfy.install(args.root, log)
    comfy.download_models(comfy_dir, args.models, log)
    comfy.write_worker_config(comfy_dir, workers, log)

    log("Starting ComfyUI...")
    proc = comfy.launch(comfy_dir, args.comfy_port, os.path.join(args.root, "comfyui.log"))
    comfy.wait_ready(args.comfy_port, proc)
    log("ComfyUI is up")

    got, url = threading.Event(), []
    tunnel.start(args.comfy_port, args.cache_dir, lambda u: (url.append(u), got.set()), on_error=log,
                 on_line=lambda l: None)
    if not got.wait(120):
        raise SystemExit("The ComfyUI tunnel did not start.")
    log("ComfyUI tunnel is up")

    workers_file = os.path.join(args.root, "comfycluster_workers.json")
    with open(workers_file, "w") as f:
        json.dump(workers, f)

    log("Starting the GUI...")
    gui = subprocess.Popen([
        sys.executable, os.path.join(ROOT, "gui", "comfycluster_gui.py"),
        "--port", str(args.gui_port), "--no-browser", "--tunnel", "--url-file", args.url_file,
        "--cache-dir", args.cache_dir, "--key", args.key,
        "--comfy-url", url[0], "--comfy-local", f"http://127.0.0.1:{args.comfy_port}",
        "--workers-file", workers_file,
    ])
    while proc.poll() is None and gui.poll() is None:
        time.sleep(5)
    log("A component exited. See comfyui.log.")


if __name__ == "__main__":
    main()
