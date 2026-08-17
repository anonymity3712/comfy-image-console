# Comfy Image Console

A local-first web console for text-to-image generation with ComfyUI. The
Python service provides a Chinese-language UI, model switching, LoRA filtering,
asynchronous jobs, PNG metadata, history management, A/B comparison, and prompt
presets. ComfyUI remains the inference backend, so your models and generated
files stay on your own machine.

## Features

- Z-Image Turbo and Flux.1-dev workflow profiles
- Model-aware LoRA slots with ownership filtering
- Asynchronous generation, progress polling, cancellation, and retry
- Batch generation with unique seeds
- Generated-image history, full-size preview, and true file deletion
- Prompt, negative prompt, sampler, scheduler, size, seed, and TeaCache controls
- PNG metadata for one-click parameter restore
- Optional offline Chinese-to-English prompt translation

Model weights are not bundled with this repository. Install them in ComfyUI and
make sure the filenames in `model_registry.json` match your installation.

## Requirements

- Python 3.10 or newer
- A running ComfyUI server, by default `http://127.0.0.1:8188`
- Model files required by the included Z-Image or Flux workflows
- Flux GGUF workflows also need ComfyUI-GGUF

Install the required Python package:

```bash
python -m pip install -r requirements.txt
```

Optional packages improve LoRA metadata detection and offline translation:

```bash
python -m pip install -r requirements-optional.txt
```

## Configure

The web service stores prompts and local copies of generated images beside
`app.py`. Paths to your ComfyUI installation and model directories are read
from environment variables, so no machine-specific paths are stored in source.
For repeated local use, copy `.env.example` to `.env`, edit the values, and start
the service normally; `.env` is ignored by Git.

On Windows PowerShell:

```powershell
$env:COMFYUI_URL = "http://127.0.0.1:8188"
$env:COMFYUI_INSTALL_ROOT = "C:\path\to\comfyui-install"
$env:COMFYUI_INPUT_DIR = "C:\path\to\comfyui-input"
$env:COMFYUI_OUTPUT_DIR = "C:\path\to\comfyui-output"
$env:COMFYUI_LORA_DIRS = "C:\path\to\model-loras"
$env:COMFYUI_UNET_DIRS = "C:\path\to\diffusion-models"
```

On Linux or macOS:

```bash
export COMFYUI_URL="http://127.0.0.1:8188"
export COMFYUI_INSTALL_ROOT="/path/to/comfyui-install"
export COMFYUI_INPUT_DIR="/path/to/comfyui-input"
export COMFYUI_OUTPUT_DIR="/path/to/comfyui-output"
export COMFYUI_LORA_DIRS="/path/to/model-loras"
export COMFYUI_UNET_DIRS="/path/to/diffusion-models"
```

Only `COMFYUI_URL` is commonly required when ComfyUI is already running. The
other variables enable output indexing, model/LoRA scanning, and automatic
startup. Multiple model directories use the host path separator.

## Run

```bash
python app.py --host 127.0.0.1 --port 7860 --comfyui http://127.0.0.1:8188
```

On Windows, you can also run:

```bat
start.bat
```

Set `COMFY_CONSOLE_PYTHON` before running `start.bat` if you want it to use a
specific Python executable. Open `http://127.0.0.1:7860` after the service
starts.

## Project Layout

```text
app.py                    HTTP service, ComfyUI proxy, jobs, and gallery
index.html                Single-page web UI
model_registry.json       Model profiles, workflow mappings, and LoRA ownership
workflow_zimage.json      Z-Image Turbo API workflow
workflow_flux.json        Flux.1-dev API workflow
lora_chinese_names.json   Optional Chinese display names for LoRA files
prompts/                  User-created presets at runtime
outputs/                  Local generated-image copies at runtime
```

## Validation

```bash
python -m py_compile app.py
python -m unittest discover -s tests
```

After starting the service, check:

```bash
curl http://127.0.0.1:7860/api/status
curl http://127.0.0.1:7860/api/models
```

Real image-generation tests use GPU and model-dependent resources, so they are
not run automatically.

## License

Released under the [MIT License](LICENSE).
