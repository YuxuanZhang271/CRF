# StreamingCommand API 索引

| 你想做什么 | 从这里开始 |
| --- | --- |
| 用系统默认麦克风实时识别 | [麦克风识别](#麦克风识别) |
| 识别已有录音，或直接解析一句文字 | [录音与文字](#录音与文字) |
| 从结果里取出转写或命令 | [识别事件](#识别事件) |
| 选择 Hugging Face 或本地模型 | [指定模型来源](#指定模型来源) |
| 修改模型能识别的动作、目标和方向 | [更换候选](#更换候选) |
| 查模型下载到哪里，或切到离线 | [缓存和离线使用](#缓存和离线使用) |
| 把应用采集的音频送给识别器 | [自有音频](#自有音频) |
| 手动启动、关闭或切换模型 | [管理识别器](#管理识别器) |
| 查配置字段、候选文件或澄清朗读 | [配置字段](#配置字段) · [候选文件](#候选文件) · [澄清朗读](#澄清朗读) |

## 麦克风识别

按 [README](../README.md#用默认麦克风开始识别) 安装后，在仓库根目录运行 `python example/live_recognize.py`。脚本会读取 `example/settings.json`，打开系统默认麦克风并持续输出结果。按 Ctrl+C 停止。

接入到自己的 Python 应用时，核心代码如下。示例只打印定稿。完整样板也打印识别中的预览与命令结果。 [麦克风样板](../example/live_recognize.py)

```python
import asyncio
from streamingcommand import StreamingCommand


async def main():
    async with StreamingCommand(config_path="example/settings.json") as recognizer:
        async for event in recognizer.listen():
            if event["type"] == "transcript.final":
                print(event["text"], flush=True)


if __name__ == "__main__":
    asyncio.run(main())
```

`recognizer.listen(*, device=None, candidates=None, history=None)` 返回事件异步迭代器。省略 `device` 使用系统默认输入，指定时可传设备编号或名称。用 `python -m sounddevice` 查看设备。只需要语音转写时，把 `StreamingCommand` 换成 `StreamingASR`。

CRF extension: `listen(..., audio_callback=callback)` calls a synchronous,
nonblocking callback with each mono 16 kHz PCM16 block before feeding ASR,
including silence. Exceptions propagate as audio-source failures. The microphone
iterator closes with the session; CRF's callback only enqueues bounded recorder
data and never performs file I/O.

## 录音与文字

| 接入方式 | Python 调用 | 返回 |
| --- | --- | --- |
| 识别已有录音 | `async for event in recognizer.transcribe_file(path)` | 连续的转写和命令事件 |
| 直接解析一句文字 | `result = await recognizer.parse(text)` | 一个包含 `status`、`slots`、`resolved` 和 `execute` 的字典 |

`transcribe_file(path, *, realtime=True, candidates=None, history=None)` 自动解码、下混并重采样。默认按录音时长输入，也可以设 `realtime=False`。在仓库根目录执行 `python example/recognize.py` 可回放随附的粤语录音。 [录音样板](../example/recognize.py)

`parse(text, *, candidates=None, history=None)` 需要启用命令模型，并在识别器空闲时调用。传入非空文字，例如 `await recognizer.parse("Bend up.")`。每次独立调用的历史由应用传入。

## 识别事件

| 事件 | 应用怎么用 |
| --- | --- |
| `transcript.partial` | 读取 `text` 显示预览，按 `segment_id` 替换同一段的旧预览。 |
| `transcript.final` | 保存本段定稿，文字在 `text`。 |
| `command.partial` | 显示命令预览。 |
| `command.final` | 读取 `status` 和 `slots`。应用需要消费动作时，先检查 `resolved` 与 `execute`。 |
| `command.error` | 读取 `message` 了解本次解析失败的原因。 |
| `command.feedback` | 读取 [澄清回答](#逐块输入音频) 的处理结果。 |
| `session.started`、`session.completed` | 识别开始和输入正常结束时的生命周期事件。 |

录音、麦克风和自有音频都支持传入初始历史 `history={"H1": "I=bend;T=none;P=left"}`。省略时从空历史开始。每段连续音频保留最近一条已确定的命令作为 H1。

## 指定模型来源

| 配置范例 | 适合谁 |
| --- | --- |
| [示例 settings.json](../example/settings.json) | 用两个 Python 样板直接运行。固定 Hugging Face 上的 V17 ASR 与 step 1392 Clarification。 |
| [本地最新测试版](../src/streamingcommand/assets/presets/local-latest.json) | 权重已在本地。复制后把各项 `path` 改成自己的实际目录。 |
| [Hugging Face 发布版](../src/streamingcommand/assets/presets/huggingface-release.json) | 从远端加载到其他项目。JSON 列出每项资源的 URL 和 40 位提交 SHA。 |
| [包的默认配置](../src/streamingcommand/assets/settings.json) | 了解省略配置路径时的默认模型和参数。 |

`asr.profile` 选择语音处理策略。`command.model` 选择命令模型和 `models` 中的同名权重项。可以为 ASR 主模型 `asr`、参考模型 `asr-base`、语种模型 `language`、语音活动检测 `speech`、命令基座 `llm-base` 和选中的命令模型分别设置来源。

| `models.<资源名>.source` | 同一项中填写的字段 |
| --- | --- |
| `huggingface` | `url` 为模型仓库链接，`revision` 为完整提交 SHA，`subfolder` 可指定仓库子目录。 |
| `local` | `path` 为模型目录，可以是绝对路径或相对配置文件的路径。 |

命令权重可加 `expected_step`，启动时检查 `experiment.json` 中的步数。每个资源选用 `models` 条目或下方的旧式路径字段之一。旧候选排序器需要根目录里的 PEFT 适配器、`head.safetensors` 和 `experiment.json`。澄清模型的 PEFT 权重位于 `adapter/`，根目录还需要 `head.safetensors`、`clarify_head.pt` 和 `experiment.json`。

| `command.model` | 权重 |
| --- | --- |
| `clarification-hybrid` | 默认和示例均用 Hugging Face 的 step 1392 |
| `clarification-new` | step 3003，需要提供自己的模型来源 |
| `clarification-latest` | step 936，需要提供自己的模型来源 |
| `phonocommand-llm-0.8b` | 旧候选排序器 |

三种澄清模型支持 H1、器械护士的空方向候选和 `semantic-pair`。发布版使用的粤语处理策略由库在推理时应用。 [V17 ASR 发布记录](https://huggingface.co/humoiww/PhonoCommand-ASR/blob/ff8daaafd4a7fd4b396c2ffcab8da230d3c17a4f/releases/20261120-hybrid/ASR_PROFILE.json)

## 更换候选

编辑 JSON 的 `command.candidates`，或用 `command.candidates_file` 指向一份只含 I、T、P 的 JSON。从包的默认候选开始时，省略这两个字段。I 是意图，T 是目标，P 是方向或位置。

器械护士场景可用以下配置。它只允许递交、停止和复位，方向结果为 `null`。

```json
{
  "command": {
    "model": "clarification-hybrid",
    "candidates": {
      "I": ["give", "stop", "reset"],
      "T": ["grasper", "gauze", "needle holder"],
      "P": []
    }
  }
}
```

I 和 T 至少各含一个值，每组里的名称应非空、唯一。P 非空时，T 和 P 都需要业务值 `"none"`。P 可在澄清模型的器械护士场景中留空，此时 I 只能取 `give`、`stop`、`reset`。内窥镜自转用 `self rotate`。示例配置的 `endoscope-hybrid` 还包含 `zoom in`、`zoom out`、`spray water` 和 `spray air`。

`listen`、`transcribe_file`、`parse`、`stream` 和 `session` 接受 `candidates=...`，只覆盖这次输入对应的候选。

## 缓存和离线使用

默认使用 Hugging Face 的共享 Hub 缓存，通常是 `~/.cache/huggingface/hub`。环境变量 `HF_HUB_CACHE` 或 `HF_HOME` 可以改变位置。要单独存放，优先设 JSON 的 `cache_dir`，也可设 `STREAMINGCOMMAND_CACHE`，Hub 快照将放在指定目录下的 `hub/`。环境变量应在启动 Python 前设置。 [Hugging Face 缓存布局](https://huggingface.co/docs/huggingface_hub/en/guides/manage-cache)

用同一份配置联网初始化一次，再将 JSON 顶层的 `offline` 改为 `true`。包会从完整共享快照或经校验的旧缓存读取固定版本，缺少所需文件时在启动阶段报错。配置为 `local` 的资源直接从目录加载。自定义提交在离线模式下也会尝试读取旧 Hub 快照。

下面的工具通过 `streamingcommand.resources` 导入。

| 调用 | 用途 |
| --- | --- |
| `ModelStore(cache_dir=None, *, offline=False)` | 手动管理模型资源。 |
| `store = ModelStore.from_config(config)` | 使用识别器的来源、缓存和离线设置。 |
| `store.hub_cache_dir` | 查看新下载的目的地。 |
| `store.source(name)` | 查看指定资源的生效来源。 |
| `store.ensure(name, override=None)` | 返回资源的实际本地路径，需要时下载。包清单内的远端文件按大小和 SHA-256 校验。`override` 可指定调用方的本地目录。 |
| `sha256(path)` | 计算一个文件的 SHA-256。 |

## 自有音频

如果应用已经有异步音频源，直接使用 `async for event in recognizer.stream(source)`。`stream(source, *, candidates=None, history=None)` 接收含静音的 16 kHz、单声道、小端 PCM16 字节块，返回与麦克风接口相同的事件。

需要原始 PCM 时，可以从 `streamingcommand` 导入下面两个函数。

| 调用 | 返回 |
| --- | --- |
| `microphone(device=None, *, sample_rate=None, block_ms=20, queue_seconds=5)` | 异步迭代录音字节。默认按输入设备采样率采集，再转换到 16 kHz。缓冲溢出会抛出 `streamingcommand.audio.AudioOverflowError`。 |
| `audio_file(path, *, realtime=True, block_ms=20)` | 异步迭代录音文件解码后的单声道 16 kHz PCM16。 |

### 逐块输入音频

需要从回调或网络接收 PCM、动态更新候选或回复澄清时，用 `session = recognizer.session(...)` 创建一次音频会话。会话负责本次输入、事件、H1 和待答澄清。识别器负责加载模型，可以顺序复用来开启下一次会话。

`session(*, candidates=None, history=None, vad=None, interval_ms=None, endpoint_ms=None, max_segment_ms=None, recognition_logic=None)` 中的参数覆盖本次会话。自定义 VAD 需要实现 `is_speech(frame, rate)`。每个识别器支持一个活动会话，发送音频和读取事件在两个 asyncio 任务里并行进行。

| 调用 | 什么时候用 |
| --- | --- |
| `async with session` 或 `await session.start()` | 开始处理本次输入。推荐用上下文管理器自动关闭。 |
| `await session.feed(pcm)` | 送入一个 PCM 块。字节数必须为偶数，缓冲超限时抛出 `BufferError`。 |
| `async for event in session.events()` | 读取本次事件，支持一个消费者。 |
| `await session.finish()` | 输入结束后调用。保持消费事件，直到末尾定稿处理完、收到 `session.completed`。 |
| `await session.close()` | 结束会话并释放识别器，也可用于提前中断。 |
| `await session.fail(exc)` | 把音频源异常传给事件消费者，再关闭会话。 |
| `session.configure_candidates(candidates)` | 更新本次候选，返回候选和版本。更新后，之前的待答澄清失效。 |
| `await session.feedback(text, clarification_id)` | 使用先前事件的澄清 ID 提交回答。结果从 `command.feedback` 读取。 |
| `await session.clear_memory()` | 清除待答澄清与模型临时状态，后续处理仍可使用 H1。 |

## 管理识别器

| 调用 | 用途 |
| --- | --- |
| `StreamingCommand(config=None, *, config_path=None, asr_backend=None, command_backend=None)` | 创建识别器。可以传配置对象、位置参数 JSON 路径，或 `config_path`。注入自定义 ASR 后端时，也由应用提供需要的命令后端。 |
| `StreamingASR(config=None, **kwargs)` | 只加载转写所需模型，音频接口相同。 |
| `async with StreamingCommand(...) as recognizer` | 进入时启动，退出时关闭。`StreamingASR` 也支持。 |
| `await StreamingCommand.initialize(config_path=None, *, config=None)` | 创建、启动并返回识别器，由调用方关闭。`StreamingASR.initialize(...)` 同样可用。 |
| `await recognizer.start()` | 启动已有实例，返回实例自身，可重复调用。 |
| `recognizer.info()` | 查看就绪状态、实际模型和候选。ASR 的 `model_sources` 记录资源路径，命令后端的 `model_id`、`checkpoint_source`、`checkpoint_step` 和 `default_candidates` 记录权重和候选。 |
| `await recognizer.switch_model(model)` | 在空闲时切换命令模型并返回新状态，继续复用 ASR。 |
| `await recognizer.close()` | 关闭活动音频和已加载的后端，可重复调用。关闭后可创建新的识别器。 |

直接管理后端的应用可以使用 `await streamingcommand.backends.build_backends(config)` 获取待启动的 `(asr, command)`。禁用命令时，第二项为 `None`。

## 配置字段

`StreamingConfig.load(path)` 把 UTF-8 JSON 读成配置对象。也可以用 `StreamingConfig(asr=ASRConfig(...), command=CommandConfig(...), ...)` 构造配置。JSON 内的相对路径从该文件所在目录解析。

| 字段 | 默认值及作用 |
| --- | --- |
| `models` | 为每项资源选择远端仓库、本地目录或打包权重，见[模型来源](#指定模型来源)。 |
| `asr.backend`、`asr.device` | 默认 `native`、`auto`。可选 `cpu` 或 `cuda:N`，其中 N 是当前进程看到的设备编号。也支持需要兼容 CUDA 环境的 `vllm`、`vllm-builtin` 和用于协议测试的 `fake`。 [Windows 与 vLLM](../docs/VLLM_WINDOWS.md) |
| `asr.profile` | 默认 `v15-canto-commit`。可选 `v17-yue-precision` 和历史单引擎策略 `v15`、`v15-realtime`、`v15-continuity`、`v15-temporal`、`v15-temporal-stream`、`v15-speech-aware`。 |
| `asr.python`、`asr.expected_gpu_uuid`、`asr.warmup` | 可指定 ASR worker 的解释器和预期 GPU，默认在启动时预热。 |
| `asr.num_workers`、`command.num_workers` | CRF extension: 1–32 weight-loading threads per native model worker; SDK default 4, CRF settings 8. Pinned Transformers 5.23 worker-local compatibility setting. |
| `parallel_start` | CRF extension: start ASR and command workers concurrently; SDK default `false`, CRF settings `true`. Startup failure cancels/joins sibling startup before closing both backends. |
| `asr.model_dir`、`asr.alternate_model_dir`、`asr.language_model_dir`、`asr.speech_model_dir` | 直接指定 ASR 主模型、参考基座、语种模型和 VAD 的本地目录。 |
| `asr.compatibility_lock_file` | 与已有 POSIX 部署共用的额外 ASR 锁路径，可选。 |
| `command.enabled`、`command.model`、`command.device` | 默认 `true`、`clarification-hybrid`、`auto`。设备选择用与 ASR 相同的规则。 |
| `command.candidates`、`command.candidates_file`、`command.candidate_profiles_dir` | 内嵌候选、候选文件、或候选配置目录，见[更换候选](#更换候选)。 |
| `command.recognition_logic` | 默认 `original`。澄清模型可选 `semantic-pair`，比较本段与相邻未执行片段拼接后的命令。 |
| `command.python`、`command.base_model_dir`、`command.checkpoint` | 可指定命令 worker 解释器、模型基座和 checkpoint 目录。 |
| `command.use_cuda_graphs`、`command.expected_gpu_uuid` | CUDA 图默认启用，预期 GPU 身份可选。 |
| `interval_ms`、`endpoint_ms`、`max_segment_ms` | 默认 800、240、16000 毫秒，分别是流式更新间隔、定稿静音阈值和单段最长时长。 |
| `event_queue_size`、`max_pending_audio_ms` | 默认 256 个事件、30000 毫秒音频，对积压设置上限。 |
| `cache_dir`、`logs_dir`、`offline` | 用于改变缓存位置、日志目录和离线模式，`offline` 默认为 `false`。 |

## 候选文件

用 `CandidateProfiles(directory=..., registry=None)` 管理多份具名 I/T/P 配置。省略目录时使用包的用户配置目录。

| 调用 | 用途 |
| --- | --- |
| `CandidateProfiles.filename(name)` | 校验名称，返回带 `.json` 的文件名。 |
| `profiles.list()` | 列出文件、校验错误和模型默认映射，键为 `configs`、`errors`、`defaults`。 |
| `profiles.read(name)` | 返回 `name` 和 `candidates`。 |
| `profiles.save(name, candidates, *, create=False)` | 保存候选。用 `create=True` 新建或另存为。 |
| `profiles.rename(name, new_name)` | 重命名并更新模型默认映射。 |
| `profiles.default_path(model, fallback)` | 返回模型的默认候选路径，缺少映射时使用备用路径。 |

## 澄清朗读

需要朗读模型的澄清提问时，额外安装 `python -m pip install -e ".[speech]"`。从 `streamingcommand.speech` 导入 `ClarificationSpeech`。

| 调用 | 用途 |
| --- | --- |
| `ClarificationSpeech(logs_root=None, *, offline=False)` | 创建澄清朗读器。 |
| `await speech.speak(text)` | 将文字合成为 WAV 字节。 |
| `await speech.start()` | 提前启动 CPU worker。 |
| `await speech.close()` | 释放朗读 worker。 |
