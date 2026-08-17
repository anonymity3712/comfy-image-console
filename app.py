"""
ComfyUI Web Frontend - a tiny stdlib-only HTTP server.

Serves an HTML UI for editing prompts, tweaking parameters, generating images
via ComfyUI HTTP API (127.0.0.1:8188), and browsing generated images.

Run:
    python app.py [--port 7860] [--comfyui http://127.0.0.1:8188]

Folder layout (created next to app.py if missing):
    app.py
    model_registry.json      - production model registry
    static/                  - CSS/JS
    outputs/                 - generated PNGs copied here for the gallery
    prompts/                 - saved prompt presets (JSON)
"""
import argparse
import copy
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import pathlib
import uuid
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
OUTPUTS_DIR = ROOT / "outputs"
PROMPTS_DIR = ROOT / "prompts"

COMFYUI_DEFAULT_PORT = 8188
COMFYUI_DISCOVERY_PORTS = (8188, 8189, 8190, 8187)


def _env_path(name):
    """Read an optional absolute path from the environment."""
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else None


def _env_paths(name):
    """Read an OS path-separator-delimited list of model directories."""
    value = os.environ.get(name, "").strip()
    return [Path(part).expanduser() for part in value.split(os.pathsep) if part.strip()]


COMFYUI_INSTALL_ROOT = _env_path("COMFYUI_INSTALL_ROOT")
COMFYUI_ROOT = COMFYUI_INSTALL_ROOT / "ComfyUI" if COMFYUI_INSTALL_ROOT else None
COMFYUI_PYTHON = (
    COMFYUI_ROOT
    / ".venv"
    / (Path("Scripts") / "python.exe" if os.name == "nt" else Path("bin") / "python")
    if COMFYUI_ROOT
    else None
)
COMFYUI_INPUT_DIR = _env_path("COMFYUI_INPUT_DIR")
COMFYUI_OUTPUT_DIR = _env_path("COMFYUI_OUTPUT_DIR")
DEFAULT_MODEL_ID = "z_image_turbo"
_COMFY_START_LOCK = threading.Lock()
_ACTIVE_COMFY_CLIENT = None

for d in (STATIC_DIR, OUTPUTS_DIR, PROMPTS_DIR):
    d.mkdir(parents=True, exist_ok=True)


def _gallery_output_dirs():
    """Return local and ComfyUI output directories in precedence order."""
    dirs = [OUTPUTS_DIR]
    if (
        COMFYUI_OUTPUT_DIR
        and COMFYUI_OUTPUT_DIR != OUTPUTS_DIR
        and COMFYUI_OUTPUT_DIR.exists()
    ):
        dirs.append(COMFYUI_OUTPUT_DIR)
    return dirs


def _gallery_files():
    """Index PNG outputs from both sources, preferring the local copy."""
    by_name = {}
    for source_dir in _gallery_output_dirs():
        try:
            candidates = source_dir.glob("*.png")
        except OSError:
            continue
        for path in candidates:
            try:
                if not path.is_file():
                    continue
                stat = path.stat()
            except OSError:
                continue
            # Windows filenames are case-insensitive; keep the first source.
            key = path.name.casefold()
            if key not in by_name:
                by_name[key] = (path, stat)
    return sorted(
        by_name.values(),
        key=lambda item: (item[1].st_mtime, item[0].name.casefold()),
        reverse=True,
    )


def _valid_gallery_name(name):
    return bool(name) and pathlib.Path(name).name == name and name not in {".", ".."}


def _matching_gallery_paths(name):
    """Find all real PNG files with this basename in registered output dirs."""
    if not _valid_gallery_name(name):
        return []
    wanted = name.casefold()
    matches = []
    for source_dir in _gallery_output_dirs():
        try:
            candidates = source_dir.glob("*.png")
        except OSError:
            continue
        for path in candidates:
            try:
                if path.is_file() and path.name.casefold() == wanted:
                    matches.append(path)
            except OSError:
                continue
    return matches


def _find_gallery_file(name):
    """Resolve a basename from either gallery source without path traversal."""
    matches = _matching_gallery_paths(name)
    return matches[0] if matches else None


DEFAULT_PROMPTS = [
    {
        "name": "古装女子写真",
        "prompt": (
            "这是一张优雅年轻女子的全身写真，她身着华丽的古代汉服，仪态万方地面向镜头。"
            "她拥有精致的椭圆脸庞，肌肤如瓷般白皙，一双柔和的杏仁状棕色眼睛，笔直挺拔的眉毛，"
            "小巧的鼻子，粉嫩的嘴唇，以及一种沉静而略带疏离的神情。她乌黑的长发梳成精致的传统高髻，"
            "中分，几缕碎发垂落在脸颊两侧，点缀着精致的银色发簪、花朵饰品和流苏。她佩戴着纤细的银色长耳环。"
            "她身穿一件精美的银灰色露肩汉服，薄如蝉翼的丝绸层层叠叠，散发着淡淡的金属光泽，衣边绣着云纹图案，"
            "宽大的袖子飘逸灵动，腰间系着多层腰带，长长的褶裥裙摆自然垂落至地。面料丝滑柔顺，轻盈透亮，"
            "细节丰富，褶皱逼真，光泽柔和。她的双手优雅地置于腰间。构图居中对称，全身从头到脚清晰可见，"
            "纯净的白色无缝摄影棚背景，柔和的高调灯光，漫射的前光，微妙的轮廓光，轻柔的地面阴影，营造出空灵的氛围，"
            "展现了奢华时尚大片的精致女性之美，逼真的CGI技术，真实的肌肤纹理，高度精细的面料，电影级渲染，"
            "超清晰，8K分辨率，堪称杰作。"
        ),
        "negative": "",
        "width": 864,
        "height": 1536,
        "seed": 7777,
    },
]


# ---------- ComfyUI client -------------------------------------------------- #


class ComfyClient:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.configured_base = self.base

    def _candidate_bases(self):
        """Return the configured endpoint plus local fallback ports."""
        parsed = urllib.parse.urlsplit(self.configured_base)
        if parsed.hostname not in {"127.0.0.1", "localhost"}:
            return [self.base]
        scheme = parsed.scheme or "http"
        host = parsed.hostname
        configured_port = parsed.port or COMFYUI_DEFAULT_PORT
        ports = [configured_port]
        if configured_port == COMFYUI_DEFAULT_PORT:
            ports.extend(p for p in COMFYUI_DISCOVERY_PORTS if p != configured_port)
        bases = [f"{scheme}://{host}:{p}" for p in ports]
        if self.base in bases:
            bases.remove(self.base)
        return [self.base] + bases

    @property
    def active_url(self):
        return self.base

    def _req(self, path, payload=None, timeout=30):
        last_error = None
        for base in self._candidate_bases():
            url = base + path
            if payload is None:
                req = urllib.request.Request(url)
            else:
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = json.loads(r.read().decode("utf-8"))
                if base != self.base:
                    self.base = base
                    print(f"[info] ComfyUI endpoint discovered: {self.base}")
                return data
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                last_error = e
        reason = getattr(last_error, "reason", None) or str(last_error or "unknown error")
        ports = ", ".join(str(p) for p in COMFYUI_DISCOVERY_PORTS)
        raise RuntimeError(f"comfyui_unreachable: {reason}; tried local ports {ports}")

    def _post_raw(self, path, payload=None, timeout=30):
        """POST through the same discovered endpoint, tolerating empty bodies."""
        last_error = None
        for base in self._candidate_bases():
            url = base + path
            data = None if payload is None else json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    raw = r.read()
                if base != self.base:
                    self.base = base
                    print(f"[info] ComfyUI endpoint discovered: {self.base}")
                if not raw:
                    return {}
                try:
                    return json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    return {"raw": raw.decode("utf-8", errors="replace")}
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_error = e
        reason = getattr(last_error, "reason", None) or str(last_error or "unknown error")
        ports = ", ".join(str(p) for p in COMFYUI_DISCOVERY_PORTS)
        raise RuntimeError(f"comfyui_unreachable: {reason}; tried local ports {ports}")

    def interrupt(self):
        return self._post_raw("/interrupt", timeout=5)

    def free(self):
        return self._post_raw("/free", timeout=15)

    def status(self):
        try:
            stats = self._req("/system_stats", timeout=5)
            queue = self._req("/queue", timeout=5)
            return {
                "ok": True,
                "queue_running": len(queue.get("queue_running", [])),
                "queue_pending": len(queue.get("queue_pending", [])),
                "device": (stats.get("devices") or [{}])[0].get("name"),
                "url": self.active_url,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def submit(self, workflow):
        return self._req("/prompt", {"prompt": workflow}, timeout=20)

    def history(self, prompt_id, timeout=10):
        try:
            h = self._req(f"/history/{prompt_id}", timeout=timeout)
            return h.get(prompt_id)
        except Exception:
            return None

    def fetch_image(self, filename, subfolder="", img_type="output"):
        params = urllib.parse.urlencode({
            "filename": filename,
            "subfolder": subfolder,
            "type": img_type,
        })
        last_error = None
        for base in self._candidate_bases():
            url = f"{base}/view?{params}"
            try:
                with urllib.request.urlopen(url, timeout=60) as r:
                    data = r.read()
                if base != self.base:
                    self.base = base
                    print(f"[info] ComfyUI endpoint discovered: {self.base}")
                return data
            except Exception as e:
                last_error = e
        raise RuntimeError(f"fetch_image_failed: {last_error}")


def _find_instance_model_paths():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    folder = Path(appdata) / "Comfy Desktop" / "instance-model-paths"
    configs = sorted(folder.glob("inst-*.yaml"), key=lambda p: p.stat().st_mtime, reverse=True)
    return configs[0] if configs else None


def _pick_comfyui_port():
    for port in COMFYUI_DISCOVERY_PORTS:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    return None


def _start_local_comfyui(client):
    """Start the local ComfyUI runtime when the desktop-managed process is down."""
    current = client.status()
    if current.get("ok"):
        return current
    with _COMFY_START_LOCK:
        current = client.status()
        if current.get("ok"):
            return current
        if (
            not COMFYUI_INSTALL_ROOT
            or not COMFYUI_PYTHON
            or not COMFYUI_ROOT
            or not COMFYUI_PYTHON.exists()
            or not (COMFYUI_ROOT / "main.py").exists()
        ):
            configured_root = str(COMFYUI_INSTALL_ROOT or "not configured")
            return {
                "ok": False,
                "error": (
                    "ComfyUI installation not found; set COMFYUI_INSTALL_ROOT "
                    f"to the directory containing ComfyUI/main.py (tried {configured_root})"
                ),
            }
        port = _pick_comfyui_port()
        if port is None:
            return {"ok": False, "error": "No free local ComfyUI port available"}

        args = [
            str(COMFYUI_PYTHON),
            "-s",
            "ComfyUI/main.py",
            "--listen",
            "127.0.0.1",
            "--port",
            str(port),
            "--enable-manager",
        ]
        paths_config = _find_instance_model_paths()
        if paths_config:
            args.extend(["--extra-model-paths-config", str(paths_config)])
        if COMFYUI_INPUT_DIR.exists():
            args.extend(["--input-directory", str(COMFYUI_INPUT_DIR)])
        if COMFYUI_OUTPUT_DIR.exists():
            args.extend(["--output-directory", str(COMFYUI_OUTPUT_DIR)])

        log_path = ROOT / "comfy_autostart.log"
        log = log_path.open("ab")
        try:
            proc = subprocess.Popen(
                args,
                cwd=str(COMFYUI_INSTALL_ROOT),
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        finally:
            log.close()

        # Desktop bundles can spend over a minute importing custom nodes before
        # the HTTP server becomes available.
        deadline = time.time() + 180
        while time.time() < deadline:
            time.sleep(1)
            status = client.status()
            if status.get("ok"):
                status["started_pid"] = proc.pid
                return status
            if proc.poll() is not None:
                break
        return {
            "ok": False,
            "error": f"ComfyUI failed to become ready on port {port}; see {log_path.name}",
            "port": port,
            "pid": proc.pid,
        }


# ---------- Workflow builder ------------------------------------------------ #


def _get_comfy_lora_names(client=None):
    """Query ComfyUI for currently visible LoRA filenames. Cached for 60s."""
    import time as _t
    if hasattr(_get_comfy_lora_names, "_cache") and _t.time() - _get_comfy_lora_names._cache_time < 60:
        return _get_comfy_lora_names._cache
    try:
        c = client or _ACTIVE_COMFY_CLIENT or ComfyClient("http://127.0.0.1:8188")
        info = c._req("/object_info/LoraLoader", timeout=5)
        # schema: lora_name = [[...options...], {"tooltip": "..."}]
        opts = info.get("LoraLoader", {}).get("input", {}).get("required", {}).get("lora_name", [])
        names = []
        if isinstance(opts, list) and opts:
            # First element might be the list of options
            first = opts[0]
            if isinstance(first, list):
                names = [o for o in first if isinstance(o, str)]
            elif isinstance(first, str):
                names = opts
        _get_comfy_lora_names._cache = names
        _get_comfy_lora_names._cache_time = _t.time()
        return names
    except Exception as e:
        print(f"warn: _get_comfy_lora_names failed: {e}")
        pass
    return []


def _find_node_ids(workflow, class_names):
    names = {str(x).lower() for x in class_names}
    return [
        node_id for node_id, node in workflow.items()
        if isinstance(node, dict) and str(node.get("class_type", "")).lower() in names
    ]


def _first_node(workflow, class_names):
    ids = _find_node_ids(workflow, class_names)
    return workflow.get(ids[0]) if ids else None


def apply_overrides(workflow, params):
    """Apply form params using node types rather than assuming fixed node IDs."""
    wf = copy.deepcopy(workflow)
    text_nodes = _find_node_ids(wf, {"CLIPTextEncode"})
    sampler_nodes = _find_node_ids(wf, {"KSampler", "KSamplerAdvanced"})
    latent_nodes = _find_node_ids(wf, {"EmptyLatentImage", "EmptySD3LatentImage"})
    sampling_nodes = _find_node_ids(wf, {"ModelSamplingFlux", "ModelSamplingDiscrete", "ModelSamplingAuraFlow"})
    save_nodes = _find_node_ids(wf, {"SaveImage", "PreviewImage"})

    def _ref_node_id(value):
        return value[0] if isinstance(value, list) and value and isinstance(value[0], str) else None

    positive_ids = set()
    negative_ids = set()
    for sampler_id in sampler_nodes:
        inputs = wf[sampler_id].get("inputs", {})
        positive_id = _ref_node_id(inputs.get("positive"))
        negative_id = _ref_node_id(inputs.get("negative"))
        if positive_id:
            positive_ids.add(positive_id)
        if negative_id:
            negative_ids.add(negative_id)

    for node_id in positive_ids & set(text_nodes):
        inputs = wf[node_id].setdefault("inputs", {})
        inputs["text"] = str(params.get("prompt", inputs.get("text", "")))

    if sampler_nodes:
        n = wf[sampler_nodes[0]].setdefault("inputs", {})
        for key, caster in (("seed", int), ("steps", int), ("cfg", float), ("denoise", float)):
            if key in params:
                n[key] = caster(params[key])
        for key in ("sampler_name", "scheduler"):
            if key in params:
                n[key] = params[key]
    if latent_nodes:
        n = wf[latent_nodes[0]].setdefault("inputs", {})
        if "width" in params:
            n["width"] = int(params["width"])
        if "height" in params:
            n["height"] = int(params["height"])
    for nid in sampling_nodes:
        n = wf[nid].setdefault("inputs", {})
        if "width" in n:
            n["width"] = int(params.get("width", n["width"]))
        if "height" in n:
            n["height"] = int(params.get("height", n["height"]))
    if save_nodes and "filename_prefix" in params:
        wf[save_nodes[0]].setdefault("inputs", {})["filename_prefix"] = params["filename_prefix"]
    # Only connect active LoRAs. Disconnected zero-strength nodes are skipped by ComfyUI.
    valid_names = _get_comfy_lora_names()
    lora_ids = _find_node_ids(wf, {"LoraLoader"})
    unet_ids = _find_node_ids(wf, {"UNETLoader", "UnetLoaderGGUF", "UNETLoaderGGUF"})
    clip_ids = _find_node_ids(wf, {"CLIPLoader", "DualCLIPLoader", "DualCLIPLoaderGGUF"})
    model_ref = [unet_ids[0], 0] if unet_ids else None
    clip_ref = [clip_ids[0], 0] if clip_ids else None
    for i, nid in enumerate(lora_ids, 1):
        lname = params.get(f"lora{i}_name", "none")
        skey = f"lora{i}_strength"
        try:
            strength = float(params.get(skey, 0))
        except (TypeError, ValueError):
            strength = 0
        inputs = wf[nid].setdefault("inputs", {})
        active = bool(lname and lname != "none" and strength != 0 and model_ref and clip_ref)
        if active:
            inputs["model"] = list(model_ref)
            inputs["clip"] = list(clip_ref)
            inputs["lora_name"] = lname
            inputs["strength_model"] = strength
            inputs["strength_clip"] = strength
            model_ref = [nid, 0]
            clip_ref = [nid, 1]
        else:
            # Keep the node valid for schema validation, but disconnect it from the graph.
            if valid_names:
                inputs["lora_name"] = valid_names[0]
            inputs["strength_model"] = 0.0
            inputs["strength_clip"] = 0.0
            if unet_ids:
                inputs["model"] = [unet_ids[0], 0]
            if clip_ids:
                inputs["clip"] = [clip_ids[0], 0]
    if clip_ref:
        for nid in text_nodes:
            wf[nid].setdefault("inputs", {})["clip"] = list(clip_ref)
        negative_text = str(params.get("negative", ""))
        for negative_id in list(negative_ids):
            negative_node = wf.get(negative_id)
            if not isinstance(negative_node, dict):
                continue
            negative_type = str(negative_node.get("class_type", "")).lower()
            needs_text_node = bool(negative_text) and negative_id in positive_ids
            if negative_type == "conditioningzeroout" and negative_text:
                needs_text_node = True
            if needs_text_node:
                prompt_negative_id = "__production_negative_prompt"
                wf[prompt_negative_id] = {
                    "class_type": "CLIPTextEncode",
                    "inputs": {"clip": list(clip_ref), "text": negative_text},
                }
                for sampler_id in sampler_nodes:
                    sampler_inputs = wf[sampler_id].get("inputs", {})
                    if _ref_node_id(sampler_inputs.get("negative")) == negative_id:
                        sampler_inputs["negative"] = [prompt_negative_id, 0]
            elif negative_type == "cliptextencode" and "negative" in params:
                negative_node.setdefault("inputs", {})["text"] = negative_text
    if model_ref:
        sampling_set = set(sampling_nodes)
        for nid in sampling_nodes:
            if "model" in wf[nid].get("inputs", {}):
                wf[nid]["inputs"]["model"] = list(model_ref)
        for nid in sampler_nodes:
            inputs = wf[nid].get("inputs", {})
            current = inputs.get("model")
            if "model" in inputs and not (
                isinstance(current, list) and current and current[0] in sampling_set
            ):
                inputs["model"] = list(model_ref)
    # TeaCache acceleration (node 5a). thresh=0 means disabled (passthrough).
    tea_nodes = _find_node_ids(wf, {"TeaCache"})
    if tea_nodes and "teacache_thresh" in params:
        try:
            t = float(params["teacache_thresh"])
            wf[tea_nodes[0]].setdefault("inputs", {})["rel_l1_thresh"] = max(0.0, t)
        except (TypeError, ValueError):
            pass
    return wf


def validate_workflow(workflow):
    """Validate a ComfyUI API workflow before it is submitted."""
    errors = []
    if not isinstance(workflow, dict) or not workflow:
        return {"ok": False, "errors": ["workflow must be a non-empty object"]}
    node_ids = set(workflow)
    classes = {}
    for node_id, node in workflow.items():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            errors.append(f"node {node_id}: missing inputs object")
            continue
        classes.setdefault(str(node.get("class_type", "")), []).append(node_id)
    required = [
        (("UNETLoader", "UnetLoaderGGUF", "UNETLoaderGGUF"), "UNet loader"),
        (("CLIPLoader", "DualCLIPLoader", "DualCLIPLoaderGGUF"), "CLIP loader"),
        (("VAELoader",), "VAE loader"),
        (("CLIPTextEncode",), "text encoder"),
        (("EmptyLatentImage", "EmptySD3LatentImage"), "latent image"),
        (("KSampler", "KSamplerAdvanced"), "sampler"),
        (("SaveImage", "PreviewImage"), "image output"),
    ]
    lower_classes = {k.lower(): v for k, v in classes.items()}
    for names, label in required:
        if not any(name.lower() in lower_classes for name in names):
            errors.append(f"missing required node: {label}")
    for node_id, node in workflow.items():
        inputs = node.get("inputs", {}) if isinstance(node, dict) else {}
        cls = str(node.get("class_type", ""))
        if cls.lower() == "loraloader":
            if not inputs.get("lora_name"):
                errors.append(f"node {node_id}: lora_name is empty")
            for key in ("strength_model", "strength_clip"):
                try:
                    value = float(inputs.get(key, 0))
                    if not -10 <= value <= 10:
                        errors.append(f"node {node_id}: {key} out of range")
                except (TypeError, ValueError):
                    errors.append(f"node {node_id}: {key} is not numeric")
        if cls.lower() in {"unetloader", "unetloadergguf"} and not inputs.get("unet_name"):
            errors.append(f"node {node_id}: unet_name is empty")
        if cls.lower() in {"cliploader", "dualcliploader", "dualcliploadergguf"}:
            clip_keys = [k for k in ("clip_name", "clip_name1", "clip_name2") if k in inputs]
            if not any(inputs.get(k) for k in clip_keys):
                errors.append(f"node {node_id}: CLIP model is empty")
        if cls.lower() == "vaeloader" and not inputs.get("vae_name"):
            errors.append(f"node {node_id}: vae_name is empty")
        for key, value in inputs.items():
            if isinstance(value, list) and len(value) >= 2 and isinstance(value[0], str):
                if value[0] not in node_ids:
                    errors.append(f"node {node_id}: input {key} references missing node {value[0]}")
    sampler = _first_node(workflow, {"KSampler", "KSamplerAdvanced"})
    if sampler:
        inputs = sampler.get("inputs", {})
        for key in ("seed", "steps", "cfg"):
            if key not in inputs:
                errors.append(f"sampler missing field: {key}")
        try:
            if not 1 <= int(inputs.get("steps", 0)) <= 500:
                errors.append("sampler steps must be between 1 and 500")
        except (TypeError, ValueError):
            errors.append("sampler steps is invalid")
        try:
            if not 0 <= float(inputs.get("cfg", 0)) <= 100:
                errors.append("sampler cfg must be between 0 and 100")
        except (TypeError, ValueError):
            errors.append("sampler cfg is invalid")
    latent = _first_node(workflow, {"EmptyLatentImage", "EmptySD3LatentImage"})
    if latent:
        for key in ("width", "height"):
            try:
                if not 64 <= int(latent.get("inputs", {}).get(key, 0)) <= 8192:
                    errors.append(f"latent {key} must be between 64 and 8192")
            except (TypeError, ValueError):
                errors.append(f"latent {key} is invalid")
    lora_count = len(_find_node_ids(workflow, {"LoraLoader"}))
    if lora_count > 6:
        errors.append("workflow contains more than 6 LoRA nodes")
    return {"ok": not errors, "errors": errors}


# ---------- Translation (offline argostranslate) ------------------------ #

_TRANSLATOR = None
_TRANSLATOR_READY = False

# Lora Chinese names cache (loaded from disk if available)
_LORA_CN_NAMES = {}


def _load_lora_chinese_names():
    global _LORA_CN_NAMES
    p = ROOT / "lora_chinese_names.json"
    if p.exists():
        try:
            with open(p, 'r', encoding='utf-8') as f:
                _LORA_CN_NAMES = json.load(f)
        except Exception:
            _LORA_CN_NAMES = {}


_load_lora_chinese_names()


def _init_translator():
    """Lazy-init argostranslate zh->en with monkey-patched sentencizer
    (avoids stanza needing to download from raw.githubusercontent.com)."""
    global _TRANSLATOR, _TRANSLATOR_READY
    if _TRANSLATOR_READY:
        return _TRANSLATOR
    try:
        import re as _re
        import argostranslate.sbd as _sbd
        import argostranslate.translate as _tt

        def _patched_split(self, text):
            parts = _re.split(r"(?<=[。！？.!?])\s*", text.strip())
            return [p for p in parts if p]

        class _DummyDoc:
            def __init__(self, text):
                self.text = text
                self.sentences = []

        class _DummyPipeline:
            def __call__(self, text):
                return _DummyDoc(text)

        def _patched_lazy(self):
            return _DummyPipeline()

        _sbd.StanzaSentencizer.split_sentences = _patched_split
        _sbd.StanzaSentencizer.lazy_pipeline = _patched_lazy

        _tt.load_installed_languages()
        zh = _tt.get_language_from_code("zh")
        en = _tt.get_language_from_code("en")
        if zh is None or en is None:
            raise RuntimeError("zh/en language pack not installed")
        _TRANSLATOR = zh.get_translation(en)
        try:
            _TRANSLATOR.translate("test")
        except Exception:
            pass
        _TRANSLATOR_READY = True
        return _TRANSLATOR
    except Exception as e:
        raise RuntimeError(f"translator init failed: {e}")


def translate_to_en(text):
    """Translate Chinese (or mixed) text to English using local argostranslate."""
    text = (text or "").strip()
    if not text:
        return ""
    if all(ord(c) < 128 for c in text):
        return text
    tr = _init_translator()
    try:
        out = tr.translate(text)
        return (out or "").strip()
    except Exception as e:
        raise RuntimeError(f"translate failed: {e}")







# ---------- Model registry & LoRA scanning -------------------------------- #

# Optional model directories. Separate multiple values with the OS path
# separator (`;` on Windows, `:` on Linux/macOS).
LORA_DIRS = _env_paths("COMFYUI_LORA_DIRS")
UNET_DIRS = _env_paths("COMFYUI_UNET_DIRS")

# Base model detection rules (by filename keyword / metadata)
# Order matters: more specific first
_BASE_MODEL_KEYWORDS = [
    ('z_image_turbo', ['z-image', 'z_image', 'zimage']),
    ('flux1-dev', ['flux', 'flux1']),
    ('krea2', ['krea']),
    ('sdxl', ['sdxl']),
    ('sd_1_5', ['sd_1.5', 'sd15']),
    ('qwen_image', ['qwen-image', 'qwen_image']),
]

# Registry of installed base models (manually editable)
DEFAULT_MODEL_REGISTRY = {
    "models": [
        {
            "id": "flux1-dev",
            "name": "Flux.1-dev (GGUF Q6_K)",
            "unet": "flux1-dev-Q6_K.gguf",
            "clip_name1": "t5xxl_fp8_e4m3fn.safetensors",
            "clip_name2": "clip_l.safetensors",
            "clip_type": "flux",
            "vae": "ae.safetensors",
            "weight_dtype": "default",
            "workflow": "workflow_flux.json",
            "default_settings": {"steps": 25, "cfg": 1.0, "sampler": "euler", "scheduler": "simple"},
            "default_loras": [
                {"slot": 1, "name": "lora.safetensors", "weight": 0.55},
                {"slot": 2, "name": "FLUX-dev-lora-add_details.safetensors", "weight": 0.7},
                {"slot": 3, "name": "cinematic-octane.safetensors", "weight": 0.6},
            ],
            "max_loras": 3,
        },
        {
            "id": "z_image_turbo",
            "name": "Z-Image Turbo (BF16)",
            "unet": "z_image_turbo_bf16.safetensors",
            "clip_name1": "qwen_3_4b_fp8_mixed.safetensors",
            "clip_name2": None,
            "clip_type": "lumina2",
            "vae": "ae.safetensors",
            "weight_dtype": "default",
            "workflow": "workflow_zimage.json",
            "default_settings": {"steps": 8, "cfg": 1.0, "sampler": "res_multistep", "scheduler": "simple"},
            "default_loras": [],
            "max_loras": 6,
        },
    ],
    "lora_overrides": {
            'Asian-beauty-Z-Image-Turbo-Tongyi-MAI-v1.0_20.safetensors': 'z_image_turbo',
            'StarFace1.0-Z-Image-Turbo.safetensors': 'z_image_turbo',
            'Turbomeinv38_10.safetensors': 'z_image_turbo',
            'Zmeinv_10.safetensors': 'z_image_turbo',
            'krea2-Cc风情万种-人像.safetensors': 'unknown',
            'lingyuxiu-MXJ-Z-Image-Turbo-Tongyi-MAI-v1.0_20.safetensors': 'z_image_turbo',
            'meixiong-niannian-Z-Image-Turbo-Tongyi-MAI-v1.0_20.safetensors': 'z_image_turbo',
            'niannian2-meinv-Z-Image-Turbo-Tongyi-MAI-v1.0_20.safetensors': 'z_image_turbo',
            'qingxin-yujie-Z-Image-Turbo-v1.0_20.safetensors': 'z_image_turbo',
            'ruanqing-Z-Image-Turbo-Tongyi-MAI-v1.0_20.safetensors': 'z_image_turbo',
            'tunkong_c1-st5000.safetensors': 'z_image_turbo',
            'xiaomanyao-Z-Image-Turbo-Tongyi-MAI-v1.0_20.safetensors': 'z_image_turbo',
            'xiaozhengyi-cosplay-Z-Image-Turbo-Tongyi-MAI-v1.0_20.safetensors': 'z_image_turbo',
            'yisibugua_20.safetensors': 'z_image_turbo',
            'zishiz_c1-st9000.safetensors': 'z_image_turbo',
            'zubu-youhua-Z-Image-Turbo-v1.0_c1-st8000.safetensors': 'z_image_turbo',
            'lora.safetensors': 'flux1-dev',
            'cinematic-octane.safetensors': 'flux1-dev',
            'FLUX-dev-lora-add_details.safetensors': 'flux1-dev'
        },
}


def _detect_base_model(lora_name, lora_path):
    """Return base_model_id for a LoRA using multi-strategy detection."""
    # 1. Manual override
    overrides = _load_model_registry().get("lora_overrides", {})
    if lora_name in overrides:
        return overrides[lora_name], "manual"

    # 2. Try safetensors metadata
    try:
        from safetensors import safe_open
        with safe_open(lora_path, framework="pt") as f:
            meta = f.metadata() or {}
        ss_ver = (meta.get("ss_base_model_version") or "").lower()
        for model_id, kws in _BASE_MODEL_KEYWORDS:
            if any(kw in ss_ver for kw in kws):
                return model_id, f"meta:{ss_ver}"
    except Exception:
        pass

    # 3. Filename heuristics
    name_lower = lora_name.lower()
    for model_id, kws in _BASE_MODEL_KEYWORDS:
        if any(kw in name_lower for kw in kws):
            return model_id, "filename"

    return "unknown", "no_match"


def _load_model_registry():
    """Load model registry from disk, or use defaults if missing."""
    reg_path = ROOT / "model_registry.json"
    if reg_path.exists():
        try:
            with open(reg_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"warn: failed to read model_registry.json: {e}, using defaults")
    return json.loads(json.dumps(DEFAULT_MODEL_REGISTRY))


def _save_model_registry(reg):
    """Save model registry back to disk."""
    reg_path = ROOT / "model_registry.json"
    with open(reg_path, 'w', encoding='utf-8') as f:
        json.dump(reg, f, ensure_ascii=False, indent=2)


def _scan_loras(force_rescan=False):
    """Scan LORA_DIRS, return list of dicts with name, path, base_model, size_mb, source_dir."""
    cache_path = ROOT / "lora_scan_cache.json"
    signature_items = []
    for directory in LORA_DIRS:
        path = pathlib.Path(directory)
        try:
            dir_mtime = path.stat().st_mtime_ns if path.is_dir() else 0
        except OSError:
            dir_mtime = 0
        files = []
        if path.is_dir():
            try:
                for item in sorted(path.iterdir(), key=lambda p: p.name.lower()):
                    if item.is_file() and item.suffix.lower() == ".safetensors":
                        try:
                            stat = item.stat()
                            files.append((item.name, stat.st_size, stat.st_mtime_ns))
                        except OSError:
                            continue
            except OSError:
                pass
        signature_items.append((str(path), dir_mtime, files))
    signature = hashlib.sha256(
        json.dumps(signature_items, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if not force_rescan and cache_path.exists():
        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                cached = json.load(f)
            if isinstance(cached, dict) and cached.get("signature") == signature:
                return cached.get("items", [])
            if isinstance(cached, list) and cached:
                # Legacy cache format: rebuild once so changes become observable.
                pass
        except Exception:
            pass

    result = []
    seen = set()
    for d in LORA_DIRS:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".safetensors"):
                continue
            if "minimax_h3" in fn.lower():
                continue
            if fn in seen:
                continue
            seen.add(fn)
            full = os.path.join(d, fn)
            try:
                sz = os.path.getsize(full) / 1024 / 1024
            except OSError:
                continue
            base_model, source = _detect_base_model(fn, full)
            result.append({
                "name": fn,
                "path": full,
                "size_mb": round(sz, 1),
                "base_model": base_model,
                "detect_source": source,
                "source_dir": d,
            })

    # save cache
    with open(cache_path, 'w', encoding='utf-8') as f:
        json.dump({"signature": signature, "items": result}, f, ensure_ascii=False, indent=2)
    return result


def _get_loras_for_model(model_id, all_loras=None):
    """Return LoRAs compatible with a given model_id, plus 'none' option."""
    if all_loras is None:
        all_loras = _scan_loras()
    compatible = [l for l in all_loras if l["base_model"] == model_id]
    return [{"name": "none", "base_model": "none", "size_mb": 0, "display": "（无）"}] + [
        {**l, "display": l["name"] + (f" [{l['size_mb']}MB]" if l['size_mb'] > 0 else "")}
        for l in compatible
    ]


# ---------- Async generation jobs ------------------------------------------- #


class WorkflowValidationError(RuntimeError):
    def __init__(self, details):
        self.details = details
        super().__init__("workflow_validation_failed")


class JobCancelled(RuntimeError):
    pass


def _prepare_generation(body, client):
    ready = client.status()
    if not ready.get("ok"):
        ready = _start_local_comfyui(client)
    if not ready.get("ok"):
        raise RuntimeError(ready.get("error", "ComfyUI is not available"))

    model_id = body.get("model_id", "")
    reg = _load_model_registry()
    model_cfg = next((m for m in reg.get("models", []) if m.get("id") == model_id), None)
    if model_cfg is None:
        model_cfg = next(
            (m for m in reg.get("models", []) if m.get("id") == DEFAULT_MODEL_ID),
            None,
        ) or next(iter(reg.get("models", [])), None)
        model_id = model_cfg.get("id", "") if model_cfg else ""
    if model_cfg is None:
        raise RuntimeError("no models configured")
    wf_path = ROOT / model_cfg.get("workflow", "workflow_flux.json")
    if not wf_path.exists():
        raise RuntimeError(f"workflow file not found: {wf_path}")
    try:
        wf_tpl = json.loads(wf_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(f"workflow_load: {e}")

    unet_id = next(iter(_find_node_ids(wf_tpl, {"UNETLoader", "UnetLoaderGGUF", "UNETLoaderGGUF"})), None)
    if unet_id:
        wf_tpl[unet_id].setdefault("inputs", {})["unet_name"] = model_cfg.get("unet", "")
        if "weight_dtype" in wf_tpl[unet_id].get("inputs", {}):
            wf_tpl[unet_id]["inputs"]["weight_dtype"] = model_cfg.get("weight_dtype", "default")
    clip_id = next(iter(_find_node_ids(wf_tpl, {"DualCLIPLoader", "DualCLIPLoaderGGUF", "CLIPLoader"})), None)
    if clip_id:
        inputs = wf_tpl[clip_id].setdefault("inputs", {})
        clip1, clip2 = model_cfg.get("clip_name1"), model_cfg.get("clip_name2")
        if "clip_name" in inputs:
            inputs["clip_name"] = clip1
        else:
            inputs["clip_name1"] = clip1
            if clip2 is not None:
                inputs["clip_name2"] = clip2
        if "type" in inputs:
            inputs["type"] = model_cfg.get("clip_type", "flux")
    vae_id = next(iter(_find_node_ids(wf_tpl, {"VAELoader"})), None)
    if vae_id:
        wf_tpl[vae_id].setdefault("inputs", {})["vae_name"] = model_cfg.get("vae", "ae.safetensors")
    wf = apply_overrides(wf_tpl, body)
    validation = validate_workflow(wf)
    if not validation["ok"]:
        raise WorkflowValidationError(validation)
    return wf, model_id, model_cfg


def _write_generation_metadata(dest, body, model_id, model_cfg):
    try:
        from PIL import Image, PngImagePlugin
        metadata = {
            "prompt": str(body.get("prompt", "")),
            "negative": str(body.get("negative", "")),
            "seed": str(body.get("seed", "")),
            "model_id": str(model_id),
            "model_name": str(model_cfg.get("name", "")),
            "steps": str(body.get("steps", "")),
            "cfg": str(body.get("cfg", "")),
            "sampler": str(body.get("sampler_name", "")),
            "scheduler": str(body.get("scheduler", "")),
            "width": str(body.get("width", "")),
            "height": str(body.get("height", "")),
            "workflow": str(model_cfg.get("workflow", "")),
            "teacache_thresh": str(body.get("teacache_thresh", "")),
        }
        for i in range(1, 7):
            metadata[f"lora{i}_name"] = str(body.get(f"lora{i}_name", ""))
            metadata[f"lora{i}_weight"] = str(body.get(f"lora{i}_strength", body.get(f"lora{i}_weight", "")))
        with Image.open(dest) as im:
            info = PngImagePlugin.PngInfo()
            for key, value in metadata.items():
                info.add_text(key, value)
            im.save(dest, "PNG", pnginfo=info)
    except Exception as e:
        print(f"warn: PNG metadata write failed: {e}")


def _execute_generation(body, client, update, cancel_event):
    started = time.time()
    update(status="running", message="检查 ComfyUI…", progress=0)
    wf, model_id, model_cfg = _prepare_generation(body, client)
    if cancel_event.is_set():
        raise JobCancelled("cancelled before submit")
    update(message="提交工作流…", progress=5)
    try:
        resp = client.submit(wf)
    except Exception as e:
        raise RuntimeError(str(e))
    prompt_id = resp.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"no prompt_id: {resp}")
    update(prompt_id=prompt_id, message="已提交，等待 ComfyUI…", progress=8)

    deadline = time.time() + 900
    entry = None
    while time.time() < deadline:
        if cancel_event.is_set():
            try:
                client.interrupt()
            except Exception:
                pass
            raise JobCancelled("cancelled")
        entry = client.history(prompt_id)
        status_info = (entry or {}).get("status", {}) if entry else {}
        status_str = status_info.get("status_str") or "queued"
        if status_str in {"error", "failed"}:
            raise RuntimeError(f"comfyui_job_failed: {status_info}")
        if entry and entry.get("outputs"):
            update(message="正在保存输出…", progress=95)
            break
        if status_str in {"success", "completed"} and entry is not None:
            break
        queue_pos = status_info.get("queue_position")
        message = f"ComfyUI: {status_str}"
        if queue_pos is not None:
            message += f"（队列 {queue_pos}）"
        update(message=message, progress=min(90, 10 + int((time.time() - started) / 900 * 80)))
        time.sleep(1)
    else:
        raise TimeoutError(f"generation timeout after 900 seconds, prompt_id={prompt_id}")

    outputs = (entry or {}).get("outputs", {})
    saved = []
    for node_id, out in outputs.items():
        for img in out.get("images", []):
            if cancel_event.is_set():
                raise JobCancelled("cancelled while saving")
            filename = pathlib.Path(str(img.get("filename", ""))).name
            if not filename:
                continue
            data = client.fetch_image(filename, img.get("subfolder", ""), img.get("type", "output"))
            dest = OUTPUTS_DIR / filename
            dest.write_bytes(data)
            _write_generation_metadata(dest, body, model_id, model_cfg)
            saved.append({
                "filename": filename,
                "size": len(data),
                "url": f"/api/outputs/{urllib.parse.quote(filename)}",
                "node": node_id,
            })
    if not saved:
        raise RuntimeError(f"comfyui_completed_without_images: {entry}")
    return {"prompt_id": prompt_id, "saved": saved, "node_errors": resp.get("node_errors")}


class GenerationJobManager:
    def __init__(self, client):
        self.client = client
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="image-job")
        self.lock = threading.RLock()
        self.jobs = {}
        self.request_index = {}

    def submit(self, body):
        body = copy.deepcopy(body or {})
        request_id = str(body.get("request_id", "")).strip()
        with self.lock:
            if request_id:
                old_id = self.request_index.get(request_id)
                old = self.jobs.get(old_id)
                if old and time.time() - old["created_at"] < 900:
                    return self._public(old)
            job_id = uuid.uuid4().hex
            record = {
                "job_id": job_id,
                "status": "queued",
                "created_at": time.time(),
                "updated_at": time.time(),
                "message": "排队中…",
                "progress": 0,
                "body": body,
                "prompt_id": None,
                "result": None,
                "error": None,
                "_cancel": threading.Event(),
            }
            self.jobs[job_id] = record
            if request_id:
                self.request_index[request_id] = job_id
            self.executor.submit(self._run, job_id)
            return self._public(record)

    def _update(self, job_id, **changes):
        with self.lock:
            record = self.jobs.get(job_id)
            if not record:
                return
            record.update(changes)
            record["updated_at"] = time.time()

    def _run(self, job_id):
        with self.lock:
            record = self.jobs.get(job_id)
        if not record:
            return
        try:
            result = _execute_generation(
                record["body"],
                self.client,
                lambda **changes: self._update(job_id, **changes),
                record["_cancel"],
            )
            if record["_cancel"].is_set():
                self._update(job_id, status="cancelled", message="已取消", progress=0)
            else:
                self._update(job_id, status="completed", message="已完成", progress=100, result=result)
        except JobCancelled as e:
            self._update(job_id, status="cancelled", message=str(e), error=str(e))
        except WorkflowValidationError as e:
            self._update(job_id, status="failed", message="工作流校验失败", error=str(e), validation=e.details)
        except Exception as e:
            self._update(job_id, status="failed", message="生成失败", error=str(e))

    def _public(self, record):
        data = {k: v for k, v in record.items() if not k.startswith("_") and k != "body"}
        data["elapsed_seconds"] = round(max(0, time.time() - record["created_at"]), 1)
        return data

    def get(self, job_id):
        with self.lock:
            record = self.jobs.get(job_id)
            return self._public(record) if record else None

    def cancel(self, job_id):
        with self.lock:
            record = self.jobs.get(job_id)
            if not record:
                return None
            if record["status"] in {"completed", "failed", "cancelled"}:
                return self._public(record)
            record["_cancel"].set()
            record["message"] = "正在取消…"
            record["updated_at"] = time.time()
            prompt_id = record.get("prompt_id")
        if prompt_id:
            try:
                self.client.interrupt()
            except Exception:
                pass
        return self.get(job_id)

    def retry(self, job_id):
        with self.lock:
            record = self.jobs.get(job_id)
            if not record:
                return None
            if record["status"] not in {"failed", "cancelled"}:
                return self._public(record)
            body = copy.deepcopy(record["body"])
        body["request_id"] = uuid.uuid4().hex
        return self.submit(body)


# ---------- HTTP handler ---------------------------------------------------- #


def make_handler(client: ComfyClient):
    jobs = GenerationJobManager(client)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}\n")

        def _send_json(self, obj, status=200):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_file(self, path, content_type):
            if not path.exists():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(data)

        def _read_json_body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            raw = self.rfile.read(n).decode("utf-8")
            try:
                return json.loads(raw)
            except json.JSONDecodeError as e:
                raise ValueError(f"invalid_json: {e}")

        # ---- GET routes ----
        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            path = url.path
            if path == "/" or path == "/index.html":
                self._send_file(ROOT / "index.html", "text/html; charset=utf-8")
            elif path.startswith("/static/"):
                self._send_file(ROOT / path.lstrip("/"), self._ctype(path))
            elif path == "/api/status":
                self._send_json(client.status())
            elif path.startswith("/api/jobs/"):
                job_id = path[len("/api/jobs/"):].strip("/")
                if not job_id:
                    self.send_error(HTTPStatus.BAD_REQUEST)
                    return
                job = jobs.get(job_id)
                if not job:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self._send_json(job)
            elif path == "/api/outputs":
                files = []
                for p, st in _gallery_files():
                    files.append({
                        "name": p.name,
                        "size": st.st_size,
                        "mtime": int(st.st_mtime),
                        "url": f"/api/outputs/{urllib.parse.quote(p.name)}",
                    })
                self._send_json({"files": files[:200]})
            elif path.startswith("/api/outputs/"):
                name = urllib.parse.unquote(path[len("/api/outputs/"):])
                if not _valid_gallery_name(name):
                    self.send_error(HTTPStatus.BAD_REQUEST)
                    return
                f = _find_gallery_file(name)
                if f is None:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self._send_file(f, "image/png")
            elif path == "/api/prompts":
                presets = self._load_prompts()
                self._send_json({"presets": presets, "defaults": DEFAULT_PROMPTS})
            elif path == "/api/models":
                reg = _load_model_registry()
                all_loras = _scan_loras()
                loras_by_model = {}
                seen_per_model = {}
                for l in all_loras:
                    bm = l["base_model"]
                    seen_per_model.setdefault(bm, set())
                    if l["name"] not in seen_per_model[bm]:
                        loras_by_model.setdefault(bm, []).append(l)
                        seen_per_model[bm].add(l["name"])
                overrides = reg.get("lora_overrides", {})
                for lname, lmodel in overrides.items():
                    for l in all_loras:
                        if l["name"] == lname:
                            seen_per_model.setdefault(lmodel, set())
                            if l["name"] not in seen_per_model[lmodel]:
                                loras_by_model.setdefault(lmodel, []).append(l)
                                seen_per_model[lmodel].add(l["name"])
                models_out = []
                for m in reg.get("models", []):
                    mid = m["id"]
                    compatible = loras_by_model.get(mid, [])
                    lora_options = [{"name": "none", "display": "（无）", "base_model": "none"}]
                    for l in compatible:
                        cn = _LORA_CN_NAMES.get(l["name"], {}).get("chinese_short") or _LORA_CN_NAMES.get(l["name"], {}).get("chinese_name") or ""
                        disp = l["name"]
                        if cn:
                            disp = f"{cn} ({l['name']})"
                        if l.get("size_mb", 0) > 0:
                            disp += f" [{l.get('size_mb', 0):.0f}MB]"
                        lora_options.append({
                            "name": l["name"],
                            "display": disp,
                            "chinese_name": _LORA_CN_NAMES.get(l["name"], {}).get("chinese_name", ""),
                            "chinese_short": cn,
                            "size_mb": l.get("size_mb", 0),
                            "base_model": l.get("base_model", mid),
                        })
                    models_out.append({**m, "compatible_loras": lora_options})
                self._send_json({
                    "models": models_out,
                    "all_loras": all_loras,
                    "loras_by_model": {bm: [{"name": l["name"], "size_mb": l["size_mb"], "base_model": l["base_model"]} for l in loras] for bm, loras in loras_by_model.items()},
                })
            elif path == "/api/lora-rescan":
                loras = _scan_loras(force_rescan=True)
                _load_lora_chinese_names()  # refresh cache
                self._send_json({"ok": True, "count": len(loras), "loras": loras})
            elif path == "/api/lora-names":
                # Return the Chinese names mapping
                self._send_json({"names": _LORA_CN_NAMES})
            elif path == "/api/restart-comfyui":
                # Kill the ComfyUI backend process. The user can restart it manually.
                killed = []
                try:
                    query = (
                        "Get-CimInstance Win32_Process -Filter \"Name = 'python.exe'\" "
                        "| Where-Object { $_.CommandLine -like '*main.py*' } "
                        "| Select-Object -ExpandProperty ProcessId"
                    )
                    result = subprocess.run(
                        ["powershell", "-NoProfile", "-Command", query],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                    pids = []
                    for value in result.stdout.splitlines():
                        try:
                            pids.append(int(value.strip()))
                        except ValueError:
                            continue
                    for pid in pids:
                        stop = subprocess.run(
                            ["taskkill", "/PID", str(pid), "/F"],
                            capture_output=True,
                            text=True,
                            timeout=10,
                            check=False,
                        )
                        if stop.returncode == 0:
                            killed.append(pid)
                except (OSError, subprocess.SubprocessError):
                    pass
                self._send_json({
                    "ok": True,
                    "killed_pids": killed,
                    "note": "ComfyUI backend stopped. Restart it from your ComfyUI installation.",
                })
            elif path == "/api/empty-cache":
                # Try ComfyUI /free endpoint, with /interrupt first to cancel running tasks
                results = {"tried": [], "succeeded": [], "failed": []}
                # Step 1: interrupt any running task
                try:
                    client.interrupt()
                    results["tried"].append("interrupt")
                    results["succeeded"].append("interrupt")
                except Exception as e:
                    results["tried"].append("interrupt")
                    results["failed"].append({"step": "interrupt", "error": str(e)})
                # Step 2: try /free (ComfyUI unload models)
                try:
                    results["comfyui_response"] = client.free()
                    results["tried"].append("free")
                    results["succeeded"].append("free")
                except Exception as e:
                    results["tried"].append("free")
                    results["failed"].append({"step": "free", "error": str(e), "note": "if 500/error, just restart ComfyUI for true release"})
                self._send_json({"ok": len(results["succeeded"]) > 0, "url": client.active_url, "details": results})
            elif path.startswith("/api/prompts/"):
                name = urllib.parse.unquote(path[len("/api/prompts/"):])
                if "/" in name or ".." in name:
                    self.send_error(HTTPStatus.BAD_REQUEST)
                    return
                presets = self._load_prompts()
                p = next((x for x in presets if x.get("name") == name), None)
                if not p:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self._send_json(p)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def _ctype(self, path):
            if path.endswith(".css"):
                return "text/css; charset=utf-8"
            if path.endswith(".js"):
                return "application/javascript; charset=utf-8"
            return "application/octet-stream"

        def _load_prompts(self):
            presets = []
            for f in PROMPTS_DIR.glob("*.json"):
                try:
                    presets.append(json.loads(f.read_text(encoding="utf-8")))
                except Exception:
                    pass
            presets.sort(key=lambda x: x.get("name", ""))
            return presets

        # ---- POST routes ----
        def do_POST(self):
            url = urllib.parse.urlparse(self.path)
            path = url.path
            try:
                body = self._read_json_body()
            except ValueError as e:
                self._send_json({"error": str(e)}, status=400)
                return

            if path == "/api/comfyui-start":
                status = _start_local_comfyui(client)
                self._send_json(status, status=200 if status.get("ok") else 503)
            elif path == "/api/generate":
                self._handle_generate(body)
            elif path.startswith("/api/jobs/") and path.endswith("/cancel"):
                job_id = path[len("/api/jobs/"):-len("/cancel")].strip("/")
                job = jobs.cancel(job_id)
                if not job:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self._send_json(job)
            elif path.startswith("/api/jobs/") and path.endswith("/retry"):
                job_id = path[len("/api/jobs/"):-len("/retry")].strip("/")
                job = jobs.retry(job_id)
                if not job:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self._send_json(job, status=202 if job.get("status") in {"queued", "running"} else 409)
            elif path == "/api/translate":
                text = (body.get("text") or "").strip()
                if not text:
                    self._send_json({"error": "empty text"}, status=400)
                    return
                try:
                    translated = translate_to_en(text)
                except Exception as e:
                    self._send_json({"error": str(e)}, status=502)
                    return
                self._send_json({"translated": translated, "source_chars": len(text)})
            elif path == "/api/prompts":
                p = body.get("preset")
                if not isinstance(p, dict) or "name" not in p:
                    self._send_json({"error": "preset.name required"}, status=400)
                    return
                safe = "".join(c for c in p["name"] if c.isalnum() or c in "-_")
                if not safe:
                    self._send_json({"error": "invalid name"}, status=400)
                    return
                (PROMPTS_DIR / f"{safe}.json").write_text(
                    json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                self._send_json({"ok": True, "name": safe})
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def _handle_generate(self, body):
            if not isinstance(body, dict):
                self._send_json({"error": "request body must be an object"}, status=400)
                return
            job = jobs.submit(body)
            self._send_json(job, status=202)

        def do_DELETE(self):
            url = urllib.parse.urlparse(self.path)
            path = url.path
            if path.startswith("/api/outputs/"):
                name = urllib.parse.unquote(path[len("/api/outputs/"):])
                if not _valid_gallery_name(name):
                    self.send_error(HTTPStatus.BAD_REQUEST)
                    return
                matches = _matching_gallery_paths(name)
                if not matches:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                deleted = []
                errors = []
                for f in matches:
                    try:
                        f.unlink()
                        deleted.append(str(f))
                    except OSError as e:
                        errors.append({"path": str(f), "error": str(e)})
                if errors:
                    self._send_json({
                        "ok": False,
                        "name": name,
                        "deleted": deleted,
                        "errors": errors,
                    }, status=500)
                    return
                self._send_json({"ok": True, "name": name, "deleted": deleted})
            elif path.startswith("/api/prompts/"):
                name = urllib.parse.unquote(path[len("/api/prompts/"):])
                if "/" in name or ".." in name:
                    self.send_error(HTTPStatus.BAD_REQUEST)
                    return
                f = PROMPTS_DIR / f"{name}.json"
                if f.exists():
                    f.unlink()
                    self._send_json({"ok": True})
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

    return Handler


def pick_port(preferred):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", preferred))
        port = s.getsockname()[1]
    finally:
        s.close()
    return port


def main():
    global _ACTIVE_COMFY_CLIENT
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument(
        "--comfyui",
        default=os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188"),
    )
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    client = ComfyClient(args.comfyui)
    _ACTIVE_COMFY_CLIENT = client
    handler = make_handler(client)
    port = pick_port(args.port)
    httpd = ThreadingHTTPServer((args.host, port), handler)

    print("=" * 60)
    print(f"ComfyUI Web Frontend")
    print(f"  url        : http://{args.host}:{port}")
    print(f"  comfyui    : {args.comfyui}")
    print(f"  registry   : {ROOT / 'model_registry.json'}")
    print(f"  outputs    : {OUTPUTS_DIR}")
    print(f"  prompts    : {PROMPTS_DIR}")
    s = client.status()
    print(f"  comfyui_ok : {s.get('ok')} ({s.get('error', '')})")
    try:
        _init_translator()
        print("  translator  : argostranslate zh->en ready")
    except Exception as e:
        print(f"  translator  : NOT READY ({e})")
    print("=" * 60)
    print("Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down...")
        httpd.shutdown()


if __name__ == "__main__":
    main()
