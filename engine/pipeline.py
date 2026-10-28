"""Node placement: split one ComfyUI workflow (API format) across the controller and the workers.

A rule says "run nodes of this type on that machine" and, optionally, "the image feeding it comes from
that machine". build_plan() turns a workflow plus rules into stages (one machine, one slice of the graph);
execute() runs the stages in order and carries IMAGE results from one machine to the next through
ComfyUI's own /view and /upload/image endpoints.

Only IMAGE values travel between machines. Light nodes (loaders, prompt encoders, ...) are simply
copied to every machine that needs them. Anything else that would have to cross (a LATENT, a MODEL)
is refused with a message that says which two nodes to put on the same machine.
"""
import copy
import os
import random
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import requests

CTL = "controller"


class PlanError(ValueError):
    """The workflow and the rules cannot be turned into a plan. The text is meant for the person."""


def _link(v, wf):
    return isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and v[0] in wf and isinstance(v[1], int)


# ----------------------------------------------------------------------------- planning
def _check_wf(wf):
    if not isinstance(wf, dict) or not wf:
        raise PlanError("The workflow is empty.")
    if isinstance(wf.get("nodes"), list):
        raise PlanError("This is the regular workflow file. In ComfyUI use Workflow > Export (API) and paste that one.")
    for nid, n in wf.items():
        if not isinstance(n, dict) or "class_type" not in n or not isinstance(n.get("inputs", {}), dict):
            raise PlanError("This is not an API-format workflow. In ComfyUI use Workflow > Export (API).")


def build_plan(wf, rules, machines, info):
    """wf: API-format workflow {id: {"class_type", "inputs"}}.  rules: [{"node_type", "worker", "source"}].
    machines: names that can be used (controller + online workers).  info(cls): ComfyUI object_info entry or None."""
    _check_wf(wf)
    valid = set(machines)
    rules = [r for r in rules if r.get("enabled", True) and (r.get("node_type") or "").strip()]

    cache = {}

    def meta(nid):
        cls = wf[nid]["class_type"]
        if cls not in cache:
            m = info(cls)
            if m is None:
                raise PlanError(f"The node type \u201c{cls}\u201d is not installed on the controller or on any connected worker.")
            cache[cls] = m
        return cache[cls]

    def otypes(nid):
        return [t if isinstance(t, str) else "COMBO" for t in meta(nid).get("output", [])]

    def deps(nid):
        return [(k, v[0], v[1]) for k, v in wf[nid].get("inputs", {}).items() if _link(v, wf)]

    def label(nid):
        return f"{wf[nid]['class_type']} #{nid}"

    order, state = [], {}                                   # dependency order, and loop detection

    def visit(n):
        if state.get(n) == 2:
            return
        if state.get(n) == 1:
            raise PlanError("The workflow contains a loop.")
        state[n] = 1
        for _, d, _ in deps(n):
            visit(d)
        state[n] = 2
        order.append(n)

    for n in wf:
        visit(n)
    for n in order:                                         # a bad slot number would crash the plan later
        for k, s, slot in deps(n):
            if slot >= len(otypes(s)):
                raise PlanError(f"{label(n)} reads output {slot} of {label(s)}, which does not exist.")

    # "light" nodes are copied wherever they are needed; the rest are placed on one machine each
    light = {}
    for n in order:
        t, ds = otypes(n), deps(n)
        if meta(n).get("output_node") or any(x in ("IMAGE", "MASK") for x in t):
            light[n] = False
        elif not ds:
            light[n] = True
        elif "LATENT" in t:
            light[n] = False
        else:
            light[n] = all(light[s] for _, s, _ in ds)

    owner, pinned, notes, hits_by_rule = {}, set(), [], []
    for r in rules:
        w, want = r.get("worker"), (r["node_type"] or "").strip()
        if w not in valid:
            raise PlanError(f"Rule \u201c{want}\u201d runs on {w}, which is not connected.")
        hits = [n for n in order if wf[n]["class_type"].lower() == want.lower()]
        if not hits:
            notes.append(f"No \u201c{want}\u201d node in this workflow.")
        elif all(light[n] for n in hits):
            notes.append(f"\u201c{want}\u201d is a light node: it is loaded on every machine that needs it, so the rule has no effect.")
        hits = [n for n in hits if not light[n]]
        for n in hits:
            owner[n] = w
            pinned.add(n)
        hits_by_rule.append(hits)

    for r, hits in zip(rules, hits_by_rule):                # "image input from": move the whole chain that makes the image
        src = r.get("source") or "auto"
        if src == "auto":
            continue
        if src not in valid:
            raise PlanError(f"Rule \u201c{r['node_type']}\u201d takes its image from {src}, which is not connected.")
        for n in hits:
            for _, s, slot in deps(n):
                if otypes(s)[slot] not in ("IMAGE", "MASK"):
                    continue
                stack = [s]
                while stack:
                    x = stack.pop()
                    if x in pinned or light[x] or x in owner:
                        continue
                    owner[x] = src
                    stack += [d for _, d, _ in deps(x)]

    for n in order:
        if not light[n]:
            owner.setdefault(n, CTL)

    level = {}                                              # a hop to another machine always means a later step
    for n in order:
        if light[n]:
            continue
        lv = 0
        for _, s, _ in deps(n):
            if not light[s]:
                lv = max(lv, level[s] + (1 if owner[s] != owner[n] else 0))
        level[n] = lv

    groups = {}
    for n in order:
        if not light[n]:
            groups.setdefault((owner[n], level[n]), []).append(n)

    stages = {}
    for (machine, lv), members in groups.items():
        mset, sub, loads, dups = set(members), {}, {}, []

        def add(n):
            if n in sub:
                return
            node = sub[n] = copy.deepcopy(wf[n])
            for k, v in list(node.get("inputs", {}).items()):
                if not _link(v, wf):
                    continue
                s, slot = v
                if s in mset or light[s]:
                    add(s)
                    if light[s] and s not in dups:
                        dups.append(s)
                    continue
                typ = otypes(s)[slot]
                if typ != "IMAGE":
                    raise PlanError(
                        f"{label(s)} (on {owner[s]}) feeds {label(n)} (on {machine}) with a {typ}. "
                        f"Only images can move between machines: put these two nodes on the same machine.")
                lid = f"cc_in_{s}_{slot}"
                loads.setdefault(lid, dict(id=lid, src=s, slot=slot, node=n, machine=owner[s], level=level[s]))
                node["inputs"][k] = [lid, 0]

        for m in members:
            add(m)
        for ld in loads.values():
            sub[ld["id"]] = {"class_type": "LoadImage", "inputs": {"image": ""}}
        stages[(machine, lv)] = dict(machine=machine, level=lv, prompt=sub, members=members, dups=dups,
                                     loads=list(loads.values()), exports=[])

    for st in stages.values():                              # every image another stage needs is saved to temp on its machine
        for ld in st["loads"]:
            src_stage = stages[(ld["machine"], ld["level"])]
            eid = f"cc_out_{ld['src']}_{ld['slot']}"
            if eid not in src_stage["prompt"]:
                src_stage["prompt"][eid] = {"class_type": "PreviewImage", "inputs": {"images": [ld["src"], ld["slot"]]}}
                src_stage["exports"].append(dict(id=eid, src=ld["src"], slot=ld["slot"]))

    out = []
    for key in sorted(stages, key=lambda k: (k[1], k[0] != CTL, k[0])):
        st = stages[key]
        st["outputs"] = [n for n in st["members"] if meta(n).get("output_node")]
        st["runs"] = bool(st["outputs"] or st["exports"])
        st["nodes"] = [dict(id=n, cls=wf[n]["class_type"]) for n in st["members"]]
        st["dup_nodes"] = [dict(id=n, cls=wf[n]["class_type"]) for n in st["dups"]]
        st["inputs"] = [dict(machine=ld["machine"], node=ld["src"], cls=wf[ld["src"]]["class_type"],
                             slot=ld["slot"], to=ld["node"], to_cls=wf[ld["node"]]["class_type"]) for ld in st["loads"]]
        st["id"] = f"{st['machine']}@{st['level']}"
        out.append(st)
    if not out:
        raise PlanError("Nothing to run: the workflow has no nodes that produce output.")
    return dict(stages=out, notes=notes)


def _coerce(old, val):
    """Turn what was typed into the type the input already has (number, switch or text)."""
    if isinstance(old, bool):
        return val if isinstance(val, bool) else str(val).strip().lower() in ("1", "true", "yes", "on")
    try:
        if isinstance(old, int):
            return val if isinstance(val, int) and not isinstance(val, bool) else int(str(val).strip())
        if isinstance(old, float):
            return float(str(val).strip())
    except ValueError:
        raise PlanError(f"\u201c{val}\u201d is not a number.") from None
    return str(val)


def build_mirror(wf, machines, overrides, info, randomize=True):
    """Mirror mode: every chosen machine runs the whole workflow.
    overrides: [{"machine": name or "all", "node": id, "input": name, "value": text}] change one input on one machine.
    With randomize, every machine except the controller gets its own seed, so the images differ."""
    _check_wf(wf)
    machines = list(dict.fromkeys(machines))
    if not machines:
        raise PlanError("Pick at least one machine to mirror to.")
    cache, outputs, files = {}, [], []
    for nid, n in wf.items():
        cls = n["class_type"]
        if cls not in cache:
            cache[cls] = info(cls)
            if cache[cls] is None:
                raise PlanError(f"The node type \u201c{cls}\u201d is not installed on the controller or on any connected worker.")
        if cache[cls].get("output_node"):
            outputs.append(nid)
        img = n.get("inputs", {}).get("image")
        if cls.startswith("LoadImage") and isinstance(img, str) and img:
            name = img.split(" [")[0]
            sub, _, fn = name.rpartition("/")
            files.append(dict(filename=fn, subfolder=sub, type="input"))
    if not outputs:
        raise PlanError("Nothing to run: the workflow has no node that produces output.")

    clean = []
    for o in overrides or []:
        m, nid, k = o.get("machine", "all"), str(o.get("node", "")), str(o.get("input", ""))
        if m != "all" and m not in machines:
            continue                                        # that machine is not part of this run
        if nid not in wf or k not in wf[nid].get("inputs", {}):
            raise PlanError(f"Node #{nid} has no input \u201c{k}\u201d.")
        if _link(wf[nid]["inputs"][k], wf):
            raise PlanError(f"{wf[nid]['class_type']} #{nid}: \u201c{k}\u201d is connected to another node, so it has no value to change.")
        clean.append((m, nid, k, o.get("value", "")))

    stages = []
    for m in machines:
        prompt, changes, seeds = copy.deepcopy(wf), [], []
        if randomize and m != CTL:
            for nid, n in prompt.items():
                for k in ("seed", "noise_seed"):
                    v = n["inputs"].get(k)
                    if isinstance(v, int) and not isinstance(v, bool):
                        n["inputs"][k] = random.randrange(2 ** 48)
                        seeds.append(n["inputs"][k])
        for scope in ("all", m):                            # settings for one machine win over "all machines"
            for who, nid, k, val in clean:
                if who != scope:
                    continue
                new = _coerce(wf[nid]["inputs"][k], val)
                prompt[nid]["inputs"][k] = new
                if new != wf[nid]["inputs"][k]:
                    changes.append(f"{wf[nid]['class_type']} #{nid}: {k} = {new}")
        nodes = [dict(id=n, cls=wf[n]["class_type"]) for n in wf]
        stages.append(dict(id=f"{m}@0", machine=m, level=0, prompt=prompt, members=list(wf), dups=[], loads=[], exports=[],
                           outputs=outputs, runs=True, nodes=nodes, dup_nodes=[], inputs=[], mirror=True, files=files,
                           changes=changes, note=("Done \u00B7 seed " + ", ".join(map(str, seeds[:3]))) if seeds else "Done"))
    return dict(stages=stages, notes=([] if randomize else ["Seeds are not changed, so machines with identical settings give identical images."]))


def public_plan(plan):
    """The plan without the prompts, for the page."""
    keep = ("id", "machine", "level", "nodes", "dup_nodes", "inputs", "runs", "missing", "mirror", "changes")
    return dict(notes=plan["notes"], warnings=plan.get("warnings", []),
                stages=[{k: st.get(k) for k in keep} for st in plan["stages"]])


# ----------------------------------------------------------------------------- talking to the machines
class Cluster:
    """The ComfyUI servers a plan can use: {"controller": url, "worker1": url, ...} (controller first)."""

    def __init__(self, bases):
        self.bases = {k: v.rstrip("/") for k, v in bases.items()}
        self._info = {}

    def object_info(self, machine, cls):
        key = (machine, cls)
        if key not in self._info:
            val = None
            try:
                r = requests.get(f"{self.bases[machine]}/object_info/{quote(cls, safe='')}", timeout=20)
                if r.ok:
                    d = r.json()
                    val = d.get(cls) if isinstance(d, dict) else None
            except (requests.RequestException, ValueError):
                val = None
            self._info[key] = val
        return self._info[key]

    def info_any(self, cls):
        for m in self.bases:
            v = self.object_info(m, cls)
            if v:
                return v
        return None

    def missing(self, plan):
        """Add plan["warnings"]: node types a stage needs that its machine does not have."""
        jobs = []
        for st in plan["stages"]:
            for cls in sorted({n["class_type"] for n in st["prompt"].values()}):
                jobs.append((st, cls))
        with ThreadPoolExecutor(max_workers=8) as pool:
            found = list(pool.map(lambda j: self.object_info(j[0]["machine"], j[1]) is not None, jobs))
        warn = []
        for st in plan["stages"]:
            st["missing"] = []
        for (st, cls), ok in zip(jobs, found):
            if not ok:
                st["missing"].append(cls)
        for st in plan["stages"]:
            if st["missing"]:
                warn.append(f"{st['machine']} does not have: {', '.join(st['missing'])}. Install the custom node there, then run its notebook again.")
        plan["warnings"] = warn
        return plan

    def transfer(self, f, to_machine, stem, keep=False):
        """Copy one image from the machine that made it to the input folder of `to_machine`. Returns the LoadImage value."""
        r = requests.get(self.bases[f["machine"]] + "/view", timeout=120,
                         params=dict(filename=f["filename"], subfolder=f.get("subfolder", ""), type=f.get("type", "temp")))
        r.raise_for_status()
        name = f["filename"] if keep else stem + (os.path.splitext(f["filename"])[1] or ".png")
        data = {"overwrite": "true", "type": "input"}
        if keep and f.get("subfolder"):
            data["subfolder"] = f["subfolder"]
        up = requests.post(self.bases[to_machine] + "/upload/image", timeout=120, files={"image": (name, r.content, "image/png")},
                           data=data)
        up.raise_for_status()
        d = up.json()
        return f"{d['subfolder']}/{d['name']}" if d.get("subfolder") else d["name"]

    def run_prompt(self, machine, prompt, timeout=3600):
        base = self.bases[machine]
        r = requests.post(base + "/prompt", json={"prompt": prompt, "client_id": uuid.uuid4().hex}, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"{machine}: {_why(r)}")
        pid = r.json()["prompt_id"]
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                h = requests.get(f"{base}/history/{pid}", timeout=15).json()
            except (requests.RequestException, ValueError):
                h = {}
            if pid in h:
                status = h[pid].get("status") or {}
                if status.get("status_str") == "error":
                    raise RuntimeError(f"{machine}: {_hist_error(status)}")
                return h[pid].get("outputs", {})
            time.sleep(1)
        raise RuntimeError(f"{machine} took too long.")


def _why(r):
    try:
        d = r.json()
    except ValueError:
        return f"answered with an error ({r.status_code})."
    err = d.get("error")
    parts = [(err.get("message") if isinstance(err, dict) else str(err or "")) or f"error {r.status_code}"]
    for nid, ne in (d.get("node_errors") or {}).items():
        for e in (ne.get("errors") or [])[:2]:
            parts.append(f"{ne.get('class_type', 'node')} #{nid}: {e.get('message', '')} {e.get('details', '')}".strip())
    return " \u2013 ".join(p for p in parts if p)[:600]


def _hist_error(status):
    for name, data in status.get("messages") or []:
        if name == "execution_error" and isinstance(data, dict):
            return f"{data.get('node_type', 'node')} #{data.get('node_id', '?')} failed: {data.get('exception_message', '').strip()}"[:600]
    return "the run failed."


# ----------------------------------------------------------------------------- running
def _fill_load(prompt, lid, names):
    """Point the LoadImage placeholder `lid` at the arrived file(s). Several images are joined with ImageBatch."""
    if len(names) == 1:
        prompt[lid]["inputs"]["image"] = names[0]
        return
    prev = None
    for i, nm in enumerate(names):
        key = f"{lid}_{i}"
        prompt[key] = {"class_type": "LoadImage", "inputs": {"image": nm}}
        if prev is None:
            prev = [key, 0]
            continue
        nid = lid if i == len(names) - 1 else f"{lid}_c{i}"
        prompt[nid] = {"class_type": "ImageBatch", "inputs": {"image1": prev, "image2": [key, 0]}}
        prev = [nid, 0]


def _run_stage(cluster, st, store, job, idx):
    js = job["stages"][idx]
    if not st["runs"]:
        js.update(state="skipped", msg="Nothing to run here")
        return
    prompt = copy.deepcopy(st["prompt"])
    js.update(state="running", msg="Moving images\u2026" if st["loads"] else "Running\u2026")
    if st["machine"] != CTL:                                # mirror mode: input images the controller has, the worker may not
        for f in st.get("files", []):
            cluster.transfer(dict(f, machine=CTL), st["machine"], "", keep=True)
    for ld in st["loads"]:
        files = store.get((ld["src"], ld["slot"])) or []
        if not files:
            raise RuntimeError(f"No image arrived from {ld['machine']}.")
        stem = f"cc_{job['id']}_{ld['src']}_{ld['slot']}"
        _fill_load(prompt, ld["id"], [cluster.transfer(f, st["machine"], f"{stem}_{i}") for i, f in enumerate(files)])
    js["msg"] = "Running\u2026"
    outs = cluster.run_prompt(st["machine"], prompt)
    for ex in st["exports"]:
        store[(ex["src"], ex["slot"])] = [dict(f, machine=st["machine"]) for f in outs.get(ex["id"], {}).get("images", [])]
    for nid in st["outputs"]:
        for f in outs.get(nid, {}).get("images", []):
            job["results"].append(dict(machine=st["machine"], filename=f["filename"], subfolder=f.get("subfolder", ""),
                                       type=f.get("type", "output")))
    js.update(state="done", msg=st.get("note") or "Done")


def new_job(plan):
    return dict(id=uuid.uuid4().hex[:10], state="running", error="", results=[], t0=time.time(),
                stages=[dict(id=st["id"], machine=st["machine"], level=st["level"], state="queued", msg="") for st in plan["stages"]])


def execute(cluster, plan, job):
    """Run every stage, step by step (stages of the same step run side by side). Updates `job` as it goes."""
    store = {}
    try:
        levels = sorted({st["level"] for st in plan["stages"]})
        for lv in levels:
            idxs = [i for i, st in enumerate(plan["stages"]) if st["level"] == lv]
            with ThreadPoolExecutor(max_workers=len(idxs)) as pool:
                futs = [(i, pool.submit(_run_stage, cluster, plan["stages"][i], store, job, i)) for i in idxs]
                err = None
                for i, f in futs:
                    try:
                        f.result()
                    except Exception as e:  # noqa: BLE001
                        job["stages"][i].update(state="error", msg=str(e))
                        err = err or e
            if err:
                raise err
        job["state"] = "done"
    except Exception as e:  # noqa: BLE001
        job.update(state="error", error=str(e))
        for s in job["stages"]:
            if s["state"] in ("queued", "running"):
                s.update(state="skipped" if s["state"] == "queued" else "error", msg=s["msg"] or "Not run")
