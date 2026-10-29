"""ComfyUI add-on (adds no nodes): serves GET /comfycluster/stats (CPU, RAM, disk, GPU load),
GET /comfycluster/log?file=engine|comfy (the end of this machine's two log files) and, on workers, the model sync
endpoints GET /comfycluster/sync/inventory and POST /comfycluster/sync/chunk (the controller sends its models here).

ComfyUI's own /system_stats has no CPU or GPU usage. engine/comfy.py copies this file into
custom_nodes/ComfyCluster-Stats/ on every machine, and the controller GUI reads the endpoint.
"""
import asyncio
import os
import re
import shutil
import subprocess

try:
    import psutil
except ImportError:  # ComfyUI installs psutil, this is only a safety net
    psutil = None

try:
    from .logtail import tail_log
    from . import modelsync as ms
    from .modelsync_worker import Receiver
except ImportError:      # run outside the custom_nodes package (tests)
    from logtail import tail_log
    import modelsync as ms
    from modelsync_worker import Receiver

# custom_nodes/ComfyCluster-Stats/__init__.py -> ComfyUI/custom_nodes/ComfyCluster-Stats -> three levels up is the root
_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
LOGS = {"engine": os.path.join(_ROOT, "comfycluster_engine.log"), "comfy": os.path.join(_ROOT, "comfyui.log")}
SYNC_KEY = os.environ.get("COMFYCLUSTER_SYNC_KEY", "")      # set by engine/worker.py; the controller's machine has none


def _models_dir():
    try:
        import folder_paths
        return folder_paths.models_dir
    except Exception:  # noqa: BLE001  (outside ComfyUI)
        return os.path.join(_ROOT, "ComfyUI", "models")


def _int(text):
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


# ---- faster page loads --------------------------------------------------------------------------
# ComfyUI sends "Cache-Control: no-cache" for every .js and .css file, so after each refresh the browser asks again
# for every file of the interface (often hundreds), and each question travels through the tunnel. The files of the
# interface have a hash in their name and never change, so they can be kept. Only static files are touched: never
# /api, /view, /prompt or the websocket, and never the page itself.
_HASHED = re.compile(r"[-.][A-Za-z0-9_-]{8,}\.[A-Za-z0-9]+$")
KEEP_FOREVER = "public, max-age=31536000, immutable"


def cache_policy(path):
    """Cache-Control value for a static file path, or None to leave ComfyUI's own header alone."""
    if path.startswith("/assets/"):
        return KEEP_FOREVER if _HASHED.search(path) else "public, max-age=86400"
    if path.startswith(("/extensions/", "/scripts/", "/lib/")):
        return "public, max-age=300"      # custom node files can change when a node is updated, so only five minutes
    return None


async def _cache_mw(request, handler):
    response = await handler(request)
    try:
        if request.method in ("GET", "HEAD") and response.status in (200, 304):
            value = cache_policy(request.path)
            if value:
                response.headers["Cache-Control"] = value
    except Exception:  # noqa: BLE001  (a header must never break a page)
        pass
    return response


def _cpu_name():
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


def _num(text):
    try:
        return float(text)
    except ValueError:      # "[N/A]" and friends
        return None


def _gpu():
    """GPU 0 through nvidia-smi: utilization %, temperature in C, power in W."""
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,temperature.gpu,power.draw",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3)
        if r.returncode != 0 or not r.stdout.strip():
            return {}
        util, temp, power = [_num(x.strip()) for x in r.stdout.strip().splitlines()[0].split(",")[:3]]
        return {"gpu_util": util, "gpu_temp": temp, "gpu_power": power}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def collect():
    out = {"cpu_name": _cpu_name()}
    if psutil:
        vm = psutil.virtual_memory()
        out.update(cpu_percent=psutil.cpu_percent(interval=None), cpu_cores=psutil.cpu_count(),
                   ram_total=vm.total, ram_used=vm.total - vm.available)
    try:
        d = shutil.disk_usage("/content" if os.path.isdir("/content") else "/")
        out.update(disk_total=d.total, disk_used=d.used)
    except OSError:
        pass
    out.update(_gpu())
    return out


try:
    from aiohttp import web
    from server import PromptServer

    if psutil:
        psutil.cpu_percent(interval=None)   # the first reading is always 0, so prime it

    try:                                     # first in the list = outermost, so it runs after ComfyUI's own cache rule
        PromptServer.instance.app.middlewares.insert(0, web.middleware(_cache_mw))
    except Exception as _e:  # noqa: BLE001  (an older or newer ComfyUI: just keep its default)
        print(f"[ComfyCluster] cache rule not installed: {_e}")

    @PromptServer.instance.routes.get("/comfycluster/stats")
    async def _stats(request):
        return web.json_response(await asyncio.get_running_loop().run_in_executor(None, collect))

    @PromptServer.instance.routes.get("/comfycluster/log")
    async def _log(request):
        path = LOGS.get(request.query.get("file", "engine"))
        if not path:
            return web.json_response({"error": "unknown log"}, status=404)
        try:
            n = max(1, min(2000, int(request.query.get("lines", "400"))))
            since = max(0, int(request.query.get("since", "0")))
        except ValueError:
            return web.json_response({"error": "bad number"}, status=400)
        return web.json_response(await asyncio.get_running_loop().run_in_executor(None, tail_log, path, n, since))

    # ---- model sync (worker only): the controller pushes its models here, see engine/modelsync.py ----
    if ms.KEY_RE.fullmatch(SYNC_KEY):
        _rx = Receiver(_models_dir(), SYNC_KEY, log=print)
        _rx.start()

        @PromptServer.instance.routes.get("/comfycluster/sync/inventory")
        async def _sync_inventory(request):
            if not _rx.authed(request.headers.get(ms.KEY_HEADER)):
                return web.json_response({"error": "bad key"}, status=401)
            return web.json_response(await asyncio.get_running_loop().run_in_executor(None, _rx.inventory))

        @PromptServer.instance.routes.post("/comfycluster/sync/chunk")
        async def _sync_chunk(request):
            if not _rx.authed(request.headers.get(ms.KEY_HEADER)):      # checked before the body is read
                return web.json_response({"error": "bad key"}, status=401)
            q = request.query
            offset, total = _int(q.get("offset")), _int(q.get("total"))
            if offset is None or total is None:
                return web.json_response({"error": "bad offset or size"}, status=400)
            data = await request.read()
            status, body = await asyncio.get_running_loop().run_in_executor(
                None, _rx.receive, q.get("path"), offset, total, q.get("sha256"), data)
            return web.json_response(body, status=status)
    elif SYNC_KEY:
        print("[ComfyCluster] model sync is off: the sync key is not valid")
except ImportError:                          # not running inside ComfyUI
    pass

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
