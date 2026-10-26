# ComfyCluster

[![Open the controller in Colab](assets/colab-badge.svg)](https://colab.research.google.com/github/AlamandaCathartica/ComfyCluster/blob/main/Controller.ipynb)

Run **ComfyUI** on several Google Colab accounts at once. One **controller** gives you the full ComfyUI interface in a single clean page. Four **workers** only render. Together they make five GPUs that share your generations.

## What it does

- **One page.** The controller GUI shows a title and a large rounded frame with ComfyUI inside. Light and dark theme.
- **Workers pair by code.** Each worker prints a short pairing code. Paste the codes into the controller and it registers the workers with [ComfyUI-Distributed](https://github.com/robertvoy/ComfyUI-Distributed).
- **Different accounts are fine.** Nothing is shared through Google Drive, so every tab can use its own account.
- **No uploads.** The notebooks download the code from this repository each time they run, and the GUI link is protected by a private key.

## How it works

Distributed generation runs whole images in parallel: the same workflow goes to every machine with a different seed, and the collector node brings the results back to the controller. A single image is never split across GPUs, except in tiled upscaling.

## How to use it

1. Open `Worker.ipynb` in Colab on each worker account (choose a **GPU runtime**). Give each a different `WORKER_NAME`, run the cell and copy the pairing code it prints (starts with `CW1-`).
2. Open `Controller.ipynb` on the controller account. Paste all the codes into `WORKER_CODES`, separated by spaces, and run the cell.
3. Open the private link it prints. Build your workflow, add the Distributed nodes (for example Distributed Seed and Distributed Collector) and queue.

Keep every notebook cell running. If a worker restarts it gets a new address, so paste its new code and run the controller cell again.

## Models

`models.json` lists what every machine downloads. Edit it in your fork so the controller and workers always match. Gated models need a Colab secret named `HF_TOKEN`.

## Notes

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
| `Controller.ipynb` | Notebook for the controller account: clones this repo, starts ComfyUI, pairs the workers, opens the GUI |
| `Worker.ipynb` | Notebook for each worker account: starts a headless ComfyUI and prints the pairing code |
| `models.json` | Models downloaded by every machine |
| `engine/controller.py` | Starts ComfyUI, registers workers, starts the GUI and its tunnel |
| `engine/worker.py` | Starts ComfyUI and its tunnel, writes the pairing code |
| `engine/comfy.py` | Installs and launches ComfyUI and ComfyUI-Distributed, downloads models |
| `engine/pairing.py` | Makes and reads pairing codes |
| `gui/comfycluster_gui.py` | The GUI server |
| `gui/web/index.html` | The web page |
| `gui/tunnel.py` | Cloudflare quick tunnel helper |
| `VERSION` | Shown in the notebook output and the server log |
