# Comfy Image Console 操作手册

## 1. 功能边界

本系统是本地 ComfyUI 文生图控制台，不负责视频生成、模型下载或实验环境管理。

正式能力：

1. Z-Image Turbo 与 Flux.1-dev 双模型工作流。
2. 按模型归属过滤 LoRA，槽位上限由注册表控制。
3. 异步生成、进度轮询、取消、失败提示和批量顺序出图。
4. PNG 元数据写入与一键参数回填。
5. 历史图库、大图查看、真实文件删除和 A/B 对比。
6. 中文提示词本地翻译、随机电影质感后缀和预设管理。

## 2. 启动与停止

1. 启动 ComfyUI，并确认 `http://127.0.0.1:8188` 可访问。
2. 在项目目录执行 `start.bat`，或运行 `python app.py`。
3. 打开控制台输出的 Web 地址，默认为 `http://127.0.0.1:7860`。
4. 停止 Web 服务时在控制台按 `Ctrl+C`；ComfyUI 按自己的安装方式退出。

如需让本系统索引 ComfyUI 输出目录、扫描模型目录或自动拉起 ComfyUI，先设置 `COMFYUI_URL`、`COMFYUI_INSTALL_ROOT`、`COMFYUI_INPUT_DIR`、`COMFYUI_OUTPUT_DIR`、`COMFYUI_LORA_DIRS`、`COMFYUI_UNET_DIRS` 等环境变量。具体示例见 `README.md`。

## 3. 模型注册表

正式模型配置位于 `model_registry.json`。

| 模型 ID | 工作流 | 默认采样 | LoRA 上限 |
|---|---|---|---:|
| `z_image_turbo` | `workflow_zimage.json` | 8 步、CFG 1、`res_multistep`、`simple` | 6 |
| `flux1-dev` | `workflow_flux.json` | 25 步、CFG 1、`euler`、`simple` | 3 |

模型文件名必须与 ComfyUI 实际可见文件一致。修改注册表后重启 Web 服务。

## 4. 工作流规则

生成时后端按以下顺序处理：

1. 读取 `model_registry.json`，按 `model_id` 找到工作流。
2. 覆写 UNet、CLIP、VAE 文件名。
3. 按节点类型定位采样器、latent、保存节点和 LoRA 链。
4. 注入正向提示词、负面提示词、尺寸、采样参数、种子、LoRA 与 TeaCache。
5. 校验必需节点、引用、字段和数值范围后提交 ComfyUI。
6. 轮询历史结果，拉回 PNG，写入元数据并展示。

Z-Image 留空负面提示词时使用 `ConditioningZeroOut`。填写负面提示词后，后端动态生成独立 `CLIPTextEncode` 负面条件。Flux 工作流始终有独立负面文本节点。

## 5. LoRA 管理

LoRA 目录来自 `COMFYUI_LORA_DIRS`，多个目录使用当前系统路径分隔符。

归属识别顺序：

1. `model_registry.json` 的 `lora_overrides`。
2. safetensors 元数据。
3. 文件名关键词。

新增或替换 LoRA 后点击“扫描 LoRA”。识别不到的 LoRA 显示为 unknown，不会混入当前模型可选列表。中文名维护在 `lora_chinese_names.json`；`lora_scan_cache.json` 是运行时缓存，不提交。

## 6. 日常使用

1. 选择主模型；页面会应用默认采样参数和 LoRA 上限。
2. 输入正向提示词，可点击“翻译”转换为英文，或点击“随机”追加电影质感描述。
3. 按需填写负面提示词、尺寸、步数、CFG、采样器、调度器、种子和 LoRA。
4. 点击“生成图片”。批量模式会顺序提交，每张使用不同随机种子。
5. 点击历史图片可回填参数；双击查看大图；A/B 按钮可加入对比槽。

生成结果保存到 `outputs/`。如配置了 `COMFYUI_OUTPUT_DIR`，页面会同时索引该目录。页面删除是真实删除，会删除两个登记目录中的同名 PNG。

## 7. 故障处理

| 现象 | 处理 |
|---|---|
| 顶部显示 ComfyUI 未运行 | 先启动 ComfyUI；仍失败时查看 ComfyUI 控制台 |
| 自动启动失败 | 核对 `COMFYUI_INSTALL_ROOT` 是否指向包含 `ComfyUI/main.py` 的安装根目录 |
| 历史缺少 ComfyUI 输出 | 核对 `COMFYUI_OUTPUT_DIR` 是否存在且可读 |
| LoRA 列表为空 | 核对 `COMFYUI_LORA_DIRS`，点击“扫描 LoRA” |
| 生成超时 | 默认单任务 900 秒；检查 GPU、显存和 ComfyUI 队列 |
| 模型或 LoRA 找不到 | 核对注册表文件名和实际模型目录 |
| 工作流校验失败 | 检查导出的 JSON 是否为 API 格式，且包含必需节点 |
| 显存不足 | 降低分辨率或步数，取消未用 LoRA，必要时清缓存 |
| 翻译不可用 | argostranslate 中英包缺失，不影响生图主流程 |

## 8. 发布前检查

每次修改代码或配置后执行：

```powershell
python -m py_compile .\app.py
python -m unittest discover -s tests
```

启动服务后检查：

```powershell
Invoke-RestMethod http://127.0.0.1:7860/api/status
Invoke-RestMethod http://127.0.0.1:7860/api/models
```

验收标准：

- `/api/status` 返回 `ok=true`。
- `/api/models` 返回 2 个模型。
- 页面能加载模型与兼容 LoRA。
- 生成任务能返回 `job_id`，完成后 PNG 出现在 `outputs/`。

真实生图会占用 GPU；如本轮只做结构验证，必须在交付说明中明确未做推理压测。
