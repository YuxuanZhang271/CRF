# StreamingCommand

在 Python 应用中实时转写语音，并把识别结果解析为命令。

## 用默认麦克风开始识别

在有麦克风的电脑上打开终端，进入本仓库根目录。用 Python 3.10–3.13 执行以下两条命令。第一条只需安装一次。

```bash
python -m pip install -e .
python example/live_recognize.py
```

脚本加载模型后，会自动打开这台电脑的系统默认麦克风。看到「正在打开系统默认麦克风」后，说一句「向上望少少」，再稍停一下。终端会边听边显示转写预览，随后打印定稿和解析出的命令。

```text
[transcript.final] 向上望少少。
[command.final] EXECUTE {'I': 'bend', 'T': 'none', 'P': 'up'}
```

按 Ctrl+C 停止。系统询问时请允许终端访问麦克风。Linux 需要 PortAudio，Debian/Ubuntu 上的包名是 `libportaudio2`。

## 接入你的 Python 应用

用 `StreamingCommand(config_path=...)` 加载你的配置，然后在 `async with` 里迭代 `recognizer.listen()`。省略 `device` 就会使用系统默认麦克风。包负责采集和分块，应用按需消费转写和命令事件。可以直接从 [麦克风样板](example/live_recognize.py) 开始，或查 [API 索引](doc/API.md)。

也可以运行 `python example/recognize.py` 回放随仓库提供的[粤语录音](example/cantonese.wav)。要换自己的文件，修改[录音样板](example/recognize.py)中的 `AUDIO_PATH`。

## 选择模型和候选

两个样板都读取 [example/settings.json](example/settings.json)。它使用 Hugging Face 发布的 V17 ASR、step 1392 Clarification 和 `endoscope-hybrid` 候选。默认初始化也从 Hugging Face 加载 step 1392。空缓存首次初始化约需下载 **11.5 GB**。模型文件默认放在 Hugging Face 共享缓存，通常是 `~/.cache/huggingface/hub`，已经缓存的固定版本可直接复用。

要换模型或候选，修改示例 JSON，或把样板的 `CONFIG_PATH` 指向自己的 JSON。已有本地权重时，可以从[本地配置范例](src/streamingcommand/assets/presets/local-latest.json)复制一份，按实际目录填写各项 `path`。需要查看远端仓库和固定版本时，参见 [Hugging Face 配置范例](src/streamingcommand/assets/presets/huggingface-release.json)。

- [模型来源与版本](doc/API.md#指定模型来源)
- [候选动作、目标和方向](doc/API.md#更换候选)
- [缓存与离线用法](doc/API.md#缓存和离线使用)
- [vLLM 与 Windows 说明](docs/VLLM_WINDOWS.md)
- [测试与实测范围](docs/VALIDATION.md)
