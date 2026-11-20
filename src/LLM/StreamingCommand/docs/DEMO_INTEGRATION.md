# 原项目 Demo 接入

在 PhonoCommandDialogue 工作区中，`RealtimeTranscriptDemo/run.sh` 默认调用 `StreamingCommand.initialize()`。服务端通过公开会话接口提交 PCM、消费事件、更新候选和转交澄清回答，命令模型切换和关闭由包管理。网页、WebSocket 和录音留在 Demo。

在 Demo 使用的 Python 环境安装 `./StreamingCommand[web,speech]` 后即可运行。语音提示通过 `streamingcommand.speech.ClarificationSpeech` 在 CPU 上执行。包版默认由原生 PyTorch 在 CPU 或 CUDA 上执行，可用 `--config` 指定包的 JSON 并复用其模型来源与缓存。
WebUI 左上角可以在 StreamingCommand 包与原版之间切换；录音期间禁止切换。
切换先关闭当前拥有的 ASR/LLM，再加载所选实现，沿用同一端口、候选文件与录音界面。

```bash
RealtimeTranscriptDemo/run.sh
RealtimeTranscriptDemo/run.sh --config StreamingCommand/src/streamingcommand/assets/presets/huggingface-release.json
RealtimeTranscriptDemo/run.sh --implementation original
```

已有服务会保持原状态并披露实现不同，不会被脚本自动终止或迁移。
普通的 LLM 模型切换保持同一个 ASR；切换整个识别实现会重新加载 ASR。
包内提供当前 H1 默认/最新模型。原版保留其他历史实验模型。设置 `PHONO_STREAMINGCOMMAND_INSTALLED=1` 可把 Demo 的验证对象固定为当前环境已安装的包。
本 GitHub 仓库只发布独立包。原项目 WebUI 的修改、录音与其他工程文件不上传到这里。
