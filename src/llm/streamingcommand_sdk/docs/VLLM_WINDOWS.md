# vLLM 与 Windows

vLLM 仅负责 ASR 模型执行；流式分段、语种证据、定稿、候选解析和 H1 会话由 StreamingCommand 管理。
LLM 继续使用训练时的一次前向候选解析器，不能用普通聊天 completion 替换它的决策头。

## Linux：已验证的旧 SDK 环境

当前原 Demo 使用 qwen-asr 0.0.6 + vLLM 0.14.0，和 LLM 的 Transformers5 环境分开。
在单独环境安装匹配版本，再指定该环境的 Python：

```bash
python -m venv .asr-vllm
.asr-vllm/bin/python -m pip install -r requirements/asr-vllm-linux.txt
```

固定组合为 qwen-asr0.0.6、vLLM0.14.0、Torch2.9.1、Transformers4.57.6。
这记录的是已执行版本；CUDA wheel与驱动仍需符合目标平台。
参阅 [vLLM 官方安装文档](https://docs.vllm.ai/en/stable/getting_started/installation/gpu/)。
不要把当前机器借用依赖、CUDA 库符号链接或固定 GPU UUID 当成可移植环境。

```python
ASRConfig(backend="vllm", python="/path/to/asr-env/bin/python", device="cuda:0")
```

旧版 vLLM 要求数字 `CUDA_VISIBLE_DEVICES`。包会从调用方的可见列表选出配置指定的设备。旧版 vLLM 收到 UUID 形式的选择时会报错。原生 PyTorch 和内置 Qwen3-ASR 的 vLLM 适配路径还可通过 `expected_gpu_uuid` 核验实际设备。

## 原生 Windows vLLM 在哪里

之前迁移文档使用 [SystemPanic/vllm-windows](https://github.com/SystemPanic/vllm-windows)。
历史审查版本 [v0.29.0](https://github.com/SystemPanic/vllm-windows/releases/tag/v0.29.0) 提供：

* Python 3.12 win_amd64 wheel；
* CUDA13、PyTorch2.11；
* Ampere / Ada / Blackwell 显卡。

这些限制来自该 release；它不是适用于所有 Windows 机器的通用 wheel。
已保存的下载 URL、SHA256、字节数和历史元数据在包内 `assets/windows_vllm_wheel.json`。
该记录是 2026-10-25 的审查结果，不表示 Windows 实机已验证或应无条件安装。

```powershell
py -3.12 -m venv .asr-vllm
.asr-vllm\Scripts\python -m pip install "下载的匹配版本.whl" --extra-index-url https://download.pytorch.org/whl/cu130
```

根据该 wheel 的实际依赖和现有驱动选择环境；安装前核对 Python ABI、Torch/CUDA 和显卡架构。
现代 wheel 使用 vLLM 内置 Qwen3-ASR，**无需安装旧 qwen-asr SDK**。
不要为了 ASR 更改已有 LLM 环境；在配置中指定独立解释器：

```python
ASRConfig(backend="vllm-builtin", python=r"C:\app\.asr-vllm\Scripts\python.exe", device="cuda:0")
```

该解释器需有 numpy、safetensors 和对应 vLLM 所需的 Transformers。
StreamingCommand 把自带 worker 文件交给解释器运行；仅启动一个 ASR 引擎，权重银行不是第二个模型实例。
执行前先运行该解释器的 CUDA/vLLM 安装检查，再用同一份 PCM 对照原生后端；记录版本、设备、文本、定稿和释放行为。
本仓库不宣称已在 Windows 执行 `.pyd`、麦克风或 GPU 验收。默认 native 不要求安装 vLLM。
