# 验证范围

## 2026-11-20 · 麦克风与粤语录音样板

- 从 2026-11-13 的 Demo 麦克风录音截取 31.660–34.320 秒，得到 [2.66 秒粤语 WAV](../example/cantonese.wav)，PCM 与原录音对应区间一致。
- 在 Linux CPU 上执行 `python example/recognize.py`，使用 [示例配置](../example/settings.json) 和已有的 Hugging Face 缓存。V17 ASR 定稿为「向上望少少。」，step 1392 Clarification 返回 `EXECUTE`，参数为 `I=bend`、`T=none`、`P=up`。
- 执行 `python example/live_recognize.py` 时，通过模拟麦克风按实际采集节奏输入同一片段。SDK 请求默认输入，真模型输出转写预览、上述定稿和命令。发送 Ctrl+C 后，音频流停止并关闭，进程正常退出。物理麦克风待实机验证。 [麦克风样板](../example/live_recognize.py)

## 2026-11-20 · 共享 Hub 缓存和独立接入

- 从 wheel 安装到临时环境，在仓库之外复制发布预设，匿名下载 Silero VAD 和 step1392 Clarification。两项都解析到 Hugging Face 的固定版本快照。切换离线后复用同一位置，未生成额外的 StreamingCommand 权重副本。
- ASR、语种模型和两套基座使用既有文件，放到测试用 Hub 快照目录后按清单核对 SHA-256。从本地仓库可编辑安装后，在仓库外调用 Python 接入样板。离线 CPU 对英语 WAV 输出定稿 `Bend left.` 和动作 `bend / none / left`。
- StreamingCommand 和相关 Demo CPU 回归共 97 项通过。wheel 同时提供 API、验证、Windows/vLLM 和 Demo 接入文档。安装验证复用了宿主依赖，平台为 Linux。

## 2026-11-18 · Demo 通过公开 SDK 部署

- 将 wheel 安装到临时目录，使用 `PHONO_STREAMINGCOMMAND_INSTALLED=1` 启动实际 `run.sh`。在 Linux CPU 上，默认 native ASR 和 step3003 经 WebSocket 处理英语 WAV，输出定稿 `bend down` 和可执行的 `bend / none / down`。服务端成功归档 WAV、事件和参数。
- 独立文本解析返回同一动作，Kokoro 接口返回有效 WAV，模型接口切至 step936。另用 fake ASR 验证了包版、原版与包版之间的切换。
- 测试复用了已校验的模型缓存和现有 Python 依赖。设备可见列表、启动期间关闭、与原版互斥的 ASR 锁另有 CPU 回归。该轮主要验证包的部署接口，实际推理使用 CPU。

## 2026-11-18 · 两份模型来源配置

- 从 wheel 启动 `StreamingCommand`，在 Linux CPU 上分别载入本地 step936 和发布版 step4400。两份配置对同一个英语合成 WAV 都给出定稿 `bend down` 和可执行的 `bend / none / down`。两者直接解析天气闲聊时均返回 `NON_COMMAND`。
- 本地范例额外使用器械护士候选解析 “Please give me the grasper.”，得到 `give / grasper / null`。发布版模型支持 I/T/P 候选排序，澄清与空 P 使用本地澄清模型。
- 发布版范例的 ASR、命令适配器提交号已与 Hugging Face 在线发布清单核对。命令适配器和决策头从指定远端提交获取，ASR、基座和辅助资源复用本机文件并按清单逐项核验 SHA-256。两份范例都通过了使用已有资源的离线回放。[Hugging Face 发布清单](https://huggingface.co/humoiww/PhonoCommand-ASR/blob/main/release_set.json)
- 包回归共 66 项通过。wheel 和源码包均包含两份 JSON、默认模板与 API 文档。这轮真模型测试覆盖 Linux CPU 的文本与单文件输入，Windows/macOS 真模型、物理麦克风及完整录音准确率未在此轮复测。

## 2026-11-17 · 独立包与 Demo

本次将识别能力抽成独立包，没有训练或修改任何原始权重、数据集、录音。

* 包与 Demo 的相关 CPU 回归共 75 项通过，包含单引擎权重交换、定稿续接、重复命令保留、
  A/AB 一致性门、候选配置、H1、音频连续重采样、流结束和中断清理。
* 实际 run.sh 默认包版在物理 GPU0 加载原 vLLM 单 ASR 和默认 epoch1/step3003 LLM；
  中英左右转、N 与直接文字解析探针通过。另切换最新 step936。
* 保存的完整 185.5 秒粤英真实录音，以 1x 经 WebSocket 回放，使用包版 ASR、最新 LLM 与 semantic-pair。
  65 个物理解码、61 次定稿、4 组续接、61 个 command.final、0 个 ASR/命令运行错误。
  表格 52 条中 51 条至少出现一次预期动作，50 条严格一次；第20条重复、第34条 bird-view→N 保留。
  7 个表外段保留。该录音是已暴露的回归材料，不是盲测或新麦克风采集。
* 本次 ASR 定稿服务器 VAD 代理时延：中位440.03ms，P95约482.57ms，最大523.88ms。
  含同机 LLM；不代表人工词末、网络、浏览器或跨平台延迟，也不是受控速度 A/B。
* 实际 Chrome 页面左上角选择器默认包版；录音中禁用，服务端切换返回409。
  包版→原版→包版均实际重载并完成文字解析，0个 JavaScript 异常。
* 新包公开 API 的 native PyTorch 后端在 Linux GPU0 上完成真实模型文件识别；
  CPU 上进一步完成完整 ASR＋默认 LLM 文件识别，`bend down`→`bend/none/down`。
  这两项为单文件接入验证，不是完整准确率或吞吐评测。

初轮曾发现旧实验依赖、LLM图捕获变量及原生 generate 参数兼容问题，均已修复。
失败记录保留在原项目本次 diagnostics 中，不混入发布包。

本次没有重跑历史英语147/43整套、Windows/macOS GPU、Windows wheel、物理麦克风或噪声验收。
原生 PyTorch 与 vLLM 使用不同执行后端，不宣称转写逐字或速度相同。
Windows/macOS 的安装与 CPU 单测由附带 CI 配置继续核验；配置存在不等于 CI 已通过。
旧含会话含义记忆的历史实验 LLM 与双ASR fallback 保留在原 Demo；包内提供当前 H1 默认和最新模型。

安装验证：在单独临时虚拟环境执行普通 `pip install ./StreamingCommand` 成功；该环境复用宿主的已安装依赖，
不是空依赖环境。随后在仓库外以 `python -I` 导入安装后的包并检查资源；已安装包也完成了真实 CPU ASR＋LLM 文件识别，输出 bend/none/down。
默认模型大权重使用已有本地目录，固定版本配置文件另有实际下载校验；未重新下载整套大权重。
