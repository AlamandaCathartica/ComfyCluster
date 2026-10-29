# ComfyCluster
For Controller Instances:
[![Open the controller in Colab](assets/colab-badge.svg)](https://colab.research.google.com/github/AlamandaCathartica/ComfyCluster/blob/main/Controller.ipynb)
For Worker Instances:
[![Open the worker in Colab](assets/colab-badge.svg)](https://colab.research.google.com/github/AlamandaCathartica/ComfyCluster/blob/main/Worker.ipynb)

Run **ComfyUI** on several Google Colab accounts at once. One **controller** gives you the full ComfyUI interface in a single clean page. Four **workers** only render. Together they make five GPUs that share your generations.

![ComfyCluster GUI](assets/gui-light.png)

## What it does

- **One page.** The controller GUI shows a title and a large rounded frame with ComfyUI inside. Light and dark theme.
- **Worker monitor.** The **Workers** button next to the theme toggle opens a floating window. It shows each worker's status, GPU, VRAM, and running and pending jobs, and lets you **Connect** or **Disconnect** any worker (or all at once).
- **Worker details.** The Workers window is a large dialog with one card per worker: GPU (model, utilization, VRAM, temperature, power), CPU (model, cores, usage), RAM, disk, running and pending jobs, and the ComfyUI, PyTorch and Python versions. CPU and GPU load come from a small add-on (`engine/comfycluster_stats.py`) that each machine installs when its notebook runs; a worker started before this update still shows GPU memory, RAM and jobs, but not CPU or GPU load until you run its notebook again.
- **Server log.** The icon-only button left of the theme toggle opens the log window. Pick the machine (Controller or any worker) and the log (Engine = what the notebook does, ComfyUI = what ComfyUI prints). Worker logs are read from the worker over its tunnel through the same add-on, so a worker started before this update needs its notebook run again. **Clear** hides what is shown now and keeps only new lines.
- **Upload.** The **Upload** button next to Workers sends models (into the chosen `models/` folder) or custom nodes (`.zip` or `.py`) to the controller. Large files go up in 32 MB slices, so they pass through the tunnel and a dropped connection resumes. Press **R** in ComfyUI to see a new model; restart ComfyUI to load a new custom node. The **From link** tab downloads a model (or a custom node `.zip`) straight to the controller from a direct link, with a progress bar and Cancel. Hugging Face and Civitai links work; for private or gated files paste an access token, which is used for that download only and is not stored. Links to private or local addresses are refused.
- **Files.** The **Files** button lists the controller's `output`, `input`, workflow and model folders. Download a single file, or a whole folder as a zip (streamed, so large folders work through the tunnel).
- **Delete a worker.** A worker that is disconnected or offline gets a **Delete** button next to Connect. It asks for a second click, then removes the worker from the monitor, from ComfyUI-Distributed's config and from the saved list. A connected, online worker cannot be deleted: disconnect it first. To bring it back later, paste its pairing code again.
- **Add workers in the page.** Each worker prints a short pairing code. Open the **Workers** window, paste the code (or several, one per line or separated by spaces) into **Add a worker** and press **Add worker**. The controller checks that the worker answers, registers it with [ComfyUI-Distributed](https://github.com/robertvoy/ComfyUI-Distributed) and remembers it if the controller restarts. Pasting the new code of a restarted worker replaces its old address.
- **Node assignment.** The **Assign** button opens a window where you decide which machine runs which node. Add a rule per node type (for example `FaceDetailer`), pick the machine to **Run on**, and optionally where its **Image input from**. Example: FaceDetailer on `worker2` with its image from `worker1` makes worker1 render the image, worker2 refine the faces, and the controller save the result. Open the **Run a workflow** tab, paste a workflow exported with **Workflow > Export (API)**, press **Preview plan** to see the steps, then **Run**. Rules are saved on the controller.
- **Mirror mode.** In the Assign window, switch from **Manual mode** to **Mirror mode** to run the whole workflow on every machine you pick, so one Run gives one image per machine. Workers get their own random seed, and you can add settings that change one input (a prompt, a size, a sampler) on one machine or on all of them. Input images the workflow loads are copied to the workers for you.
- **Model sync.** Every worker gets the same models as the controller, automatically. A model enters the cluster in one place, the controller (an upload, a link, or `models.json`), and the controller sends it to every connected worker. Workers never download a model and never talk to each other. The **Workers** window shows how far each worker is (`3 / 4 models`, the file being sent with its progress, or what went wrong), a **Send automatically** switch and a **Sync now** button. See [Model sync](#model-sync).
- **Different accounts are fine.** Nothing is shared through Google Drive, so every tab can use its own account.
- **No uploads.** The notebooks download the code from this repository each time they run, and the GUI link is protected by a private key.

## How it works

Distributed generation runs whole images in parallel: the same workflow goes to every machine with a different seed, and the collector node brings the results back to the controller. A single image is never split across GPUs, except in tiled upscaling.

## How to use it

1. Open `Worker.ipynb` in Colab on each worker account (choose a **GPU runtime**). Give each a different `WORKER_NAME`, run the cell and copy the pairing code it prints (starts with `CW1-`).
2. Open `Controller.ipynb` on the controller account and run the cell.
3. Open the private link it prints. Click **Workers**, paste each pairing code into **Add a worker** and press **Add worker**.
4. Build your workflow, add the Distributed nodes (for example Distributed Seed and Distributed Collector) and queue.

Keep every notebook cell running. If a worker restarts it gets a new address: paste its new code in the Workers window. If you run the controller cell again, workers that still answer are kept.

## Node assignment: how it works and its limits

The controller splits your workflow into steps, one slice per machine, and runs them in order through each machine's ComfyUI API. Between steps it copies the image from one machine to the next (`/view` then `/upload/image`), so a step starts when the one before it has finished. Steps that do not depend on each other run side by side.

- **Only images move between machines.** A LATENT, MODEL or CONDITIONING cannot. The plan tells you which two nodes to put on the same machine.
- **Light nodes are copied, not moved.** Loaders, prompt encoders and detector providers are loaded on every machine that needs them.
- **Every machine needs the same custom nodes and models** (models are copied by model sync; custom nodes are not) for the nodes it runs (FaceDetailer needs the Impact Pack and its detector model on that worker). The plan warns when a node type is missing on a machine.
- **The Run tab runs the exported workflow itself**, not the graph open in the ComfyUI frame, because the page cannot read another site's graph. Results appear in the window.
- A worker must be connected and online to receive nodes.

### Mirror mode notes

- Every machine needs the models (model sync takes care of this) and custom nodes the whole workflow uses.
- The controller keeps the workflow's seed; each worker gets a random one (switch this off to leave seeds alone). A seed you set yourself is never replaced.
- A setting can only change a value typed into a node, not an input wired to another node.
- Results show in the Assign window, one image per machine. For results inside the ComfyUI frame, use the Distributed Seed and Distributed Collector nodes instead.

## Why a refresh can be slow, and what is done about it

ComfyUI sends `Cache-Control: no-cache` for every `.js` and `.css` file. After a refresh (Ctrl+R) the browser therefore asks again for every file of the interface, often hundreds, and each question goes through the Cloudflare tunnel. The ComfyCluster add-on (`engine/comfycluster_stats.py`, installed on every machine) now lets the browser keep the interface files that carry a hash in their name (`/assets/index-AbC123.js`), which never change. Files from custom nodes (`/extensions/`, `/scripts/`) are kept for five minutes. The API, `/view`, `/prompt`, the websocket and the page itself are untouched. The GUI page also starts loading the ComfyUI frame at once and opens the connection to ComfyUI early.

- **The first visit of a session is not faster.** Each session gets a new tunnel address, so the browser starts with an empty cache. Refreshes within a session are what improve.
- **To check it**, open the browser's developer tools, Network tab, and refresh. Interface files should show `(disk cache)` instead of `304`.
- A custom node whose script changed inside the same five minutes may show the old script until you hard refresh (Ctrl+Shift+R).
- Still outside this fix: what ComfyUI-Distributed and other custom nodes load on start, and `object_info`, which ComfyUI builds fresh on every load.

## Models

`models.json` lists what the **controller** downloads at start. Edit it in your fork. Gated models need a Colab secret named `HF_TOKEN` on the controller. The workers get their models from the controller (see below).

## Model sync

The controller is the source of truth: every model file it has (`checkpoints`, `loras`, `vae`, `controlnet`, ... under `models/`) is sent to every connected, online worker.

```
your PC --upload--> controller --push--> worker1 .. workerN
web link --download-> controller
```

- **How it works.** The controller checks each file's SHA-256 (hashed once and remembered) and asks each worker what it has. It sends what is missing or different, smallest files first, in 32 MB slices through the tunnel, so a dropped connection resumes. A worker checks the SHA-256 of every file before it makes it visible, so a model is never half written or damaged.
- **When.** A few seconds after a model lands on the controller, and then every 20 seconds, so a worker that joins or restarts catches up by itself. A file still being written is left alone until it stops changing.
- **Nothing is deleted.** A file that exists only on a worker stays. A worker file with the same path but different content is replaced by the controller's version.
- **Safety.** Each worker makes a secret key when it starts and puts it in its pairing code. The controller sends the key with every request and the worker refuses anything without it. The key is never sent to the browser. Paths are checked on both sides (a model folder, a file name and a model extension only), and a worker keeps 1 GiB free.
- **Old workers.** A worker started before this update has no key and shows *Run Worker.ipynb again and paste its new code to turn on model sync*. After you restart it, paste its new code in **Add a worker**: it replaces the old address and key.
- **Disconnected workers** are skipped until you connect them. **Send automatically** off means models are only sent when you press **Sync now**.
- **Limits.** Only single model files are synced (`.safetensors`, `.ckpt`, `.pt`, `.pth`, `.bin`, `.gguf`, `.sft`, `.onnx`), not whole repository folders such as `diffusers/`. Files with unusual names are skipped and listed in the Workers window. Colab disks are small: a worker that is full shows the file it could not take.

## Notes

- **Pairing codes are checked when you add them.** A code is refused if it is damaged, if the worker does not answer (start or restart its notebook and copy a fresh code), or if its address is already paired under another name. The names `all` and `controller` are reserved, so do not use them as `WORKER_NAME`. A worker that goes away stays in the list as Offline until you delete it.
- If a worker you added does not appear in ComfyUI-Distributed's own panel, reload the ComfyUI page. The extension reads the worker list from its config file, and may only do so when it loads.
- Anyone who has a worker's address can read its logs and load figures through the add-on, like they can use its ComfyUI. Keep the pairing codes private.
- **Uploads, link downloads and Files all use the controller only.** Models you add there are sent to the workers by model sync. Custom nodes uploaded here are not installed on workers, and their `requirements.txt` is not installed automatically.
- **Connect / Disconnect** switches a worker on or off in ComfyUI-Distributed's config (the `enabled` flag) and in the monitor. A disconnected worker keeps running but is not meant to receive new jobs. Whether the extension re-reads the file immediately or only on its next reload depends on its version.
- ComfyUI generally needs a paid Colab plan. Use each account within its own plan limits.
- The controller's ComfyUI address and each worker's address are public Cloudflare quick-tunnel URLs. Only the GUI link is protected by a key, so keep the codes and links private.
- The worker entries written for ComfyUI-Distributed follow its config format as understood at the time of writing. If workers do not appear in its panel, add them there (Add Worker > Remote); the addresses are in the worker logs.

## Run the GUI locally

```
pip install -r requirements.txt
python gui/comfycluster_gui.py --demo
```

Then open `http://127.0.0.1:7860`. With a ComfyUI already running on this machine, add `--comfy-url http://127.0.0.1:8188 --comfy-local http://127.0.0.1:8188`.

## Repository layout

| Path | What it is |
|---|---|
| `Controller.ipynb` | Notebook for the controller account: clones this repo, starts ComfyUI and opens the GUI (workers are added in the page) |
| `Worker.ipynb` | Notebook for each worker account: starts a headless ComfyUI and prints the pairing code |
| `models.json` | Models the controller downloads (model sync sends them to the workers) |
| `engine/controller.py` | Starts ComfyUI, registers workers, starts the GUI and its tunnel |
| `engine/worker.py` | Starts ComfyUI and its tunnel, makes the sync key, writes the pairing code |
| `engine/comfy.py` | Installs and launches ComfyUI and ComfyUI-Distributed, downloads the controller's models |
| `engine/comfycluster_stats.py` | ComfyUI add-on that serves CPU, RAM, disk and GPU load to the monitor, and receives models on workers |
| `engine/modelsync.py` | Model sync protocol (CMS1): path rules, hashing, inventory. Shared by both sides |
| `engine/modelpush.py` | Controller side of model sync: compares inventories and sends what is missing |
| `engine/modelsync_worker.py` | Worker side of model sync: receives, verifies and installs files |
| `engine/logtail.py` | Reads the end of a log file (used by the GUI and the worker add-on) |
| `engine/pipeline.py` | Splits a workflow across machines by node type and runs the steps (node assignment) |
| `engine/pairing.py` | Makes and reads pairing codes (name, address and sync key) |
| `gui/comfycluster_gui.py` | The GUI server (worker monitor, adding workers by code, logs, uploads) |
| `gui/web/index.html` | The web page |
| `gui/tunnel.py` | Cloudflare quick tunnel helper |
| `VERSION` | Shown in the notebook output and the server log |
