"""Worker: runs a headless ComfyUI behind a Cloudflare tunnel and prints a pairing code.

    python engine/worker.py --name worker1 --models models.json
"""
import argparse
import os
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
    ap = argparse.ArgumentParser(description="ComfyCluster worker")
    ap.add_argument("--name", default="worker1")
    ap.add_argument("--root", default="/content")
    ap.add_argument("--cache-dir", default=os.path.join(ROOT, ".cache"))
    ap.add_argument("--models", default=os.path.join(ROOT, "models.json"))
    ap.add_argument("--port", type=int, default=8188)
    ap.add_argument("--code-file", required=True, help="the pairing code is written here once the worker is ready")
    args = ap.parse_args()

    global log
    log = comfy.make_logger(os.path.join(args.root, "comfycluster_engine.log"))

    log(f"ComfyCluster worker '{args.name}'")
    comfy_dir = comfy.install(args.root, log)
    comfy.download_models(comfy_dir, args.models, log)

    log("Starting ComfyUI...")
    proc = comfy.launch(comfy_dir, args.port, os.path.join(args.root, "comfyui.log"))
    comfy.wait_ready(args.port, proc)
    log("ComfyUI is up")

    got, url = threading.Event(), []
    tunnel.start(args.port, args.cache_dir, lambda u: (url.append(u), got.set()), on_error=log,
                 on_line=lambda l: log("[cloudflared] " + l))
    if not got.wait(120):
        raise SystemExit("The Cloudflare tunnel did not start.")

    code = pairing.make_code(args.name, url[0])
    tmp = args.code_file + ".part"
    with open(tmp, "w") as f:
        f.write(code)
    os.replace(tmp, args.code_file)
    log("Ready. Pairing code written.")

    while proc.poll() is None:
        time.sleep(5)
    log("ComfyUI exited.")


if __name__ == "__main__":
    main()
