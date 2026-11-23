# Voice interaction

The dashboard preloads the speech and command models in its background thread
when `Start-FlexControl.ps1` starts the app. Recognition then runs continuously
through the server computer's system microphone, with task triggering initially
disabled. **LISTEN** or **Alt+L** toggles task triggering only. Models, microphone,
recognition session, and command memory stay active until app shutdown. Turning
LISTEN off does not interrupt an already-running task; use **INTERRUPT** to stop it.

With LISTEN enabled, the standalone English word **stop** is detected directly
in streaming ASR partials and finals, case-insensitively. It triggers Task 0
without waiting for LLM parsing. Each utterance triggers once; a Stop invalidates
older in-flight commands so delayed results cannot restart motion. Words such as
`stopwatch` and `stopping` do not match. Reaction still depends on the ASR producing
the word and on hardware stop acknowledgements. Stop remains subject to LISTEN.

Transcription previews update in place. Command utterances are highlighted and
show I/T/P slots, clarification questions, and task acceptance or rejection.
The LLM and TASK chips in the bottom status bar show concise states. Detailed
messages, input representations, worker log paths, and tracebacks appear in the
LLM console and the main console. Server diagnostics also print in the launcher
terminal. Browser messages are forwarded to the terminal even when RECORD is off.
The LLM card grows vertically with the available space, like the main console.

Each app run creates `logs/crf_YYYYMMDD_HHMMSS_microseconds_PID.log`, using Hong Kong
start time. The UTF-8 file contains warnings, errors, tracebacks, and start/end
markers independently of RECORD. Python warnings and unhandled worker-thread
errors are captured as well. Informational messages stay in the terminal and UI.

A single command-parse error marks LLM status as Error and rejects that turn,
while keeping recognition and model workers active. A subsequent successful
command result clears the error. Fatal audio/model failures still stop the stream
and report their traceback.

The `llm-interaction` thread owns the recognizer's asyncio loop. StreamingCommand
owns its ASR and LLM worker processes. Recognized commands enter the same
`TaskCoordinator` as task buttons. Partials, unresolved results, duplicates,
expired trigger generations, and commands overtaken by a task change do not start
tasks. Speech begun while triggering was disabled is never executed on re-enable.

From the repository root, with `crf` activated:

```powershell
python -m pip install -r src/llm/requirements.txt
python -m streamingcommand.cli --config src/llm/settings.json doctor
python -m streamingcommand.cli --config src/llm/settings.json download
python src/llm/streamingcommand_sdk/example/recognize.py
python src/llm/streamingcommand_sdk/example/live_recognize.py
```

The requirements pin the CUDA 13.0 PyTorch/TorchVision/TorchAudio 2.11 package
set and the installed speech stack. `environment.yaml` includes this file.
`settings.json` retains the archive's pinned model revisions, V17 Cantonese ASR,
clarification-hybrid command model, original recognition logic, and disabled
CUDA graphs. The model manifest describes approximately 11.5 GB of downloads.
The SDK validates pinned model file sizes and SHA-256 hashes.

The current detector's person/bottle/cup/banana/chair target candidates are added
alongside the original candidates. Surgical targets are explicitly rejected for
tracking by the current YOLO model. `track` with target `none` uses the category
currently selected in the dashboard.

Voice tracking selects the requested category, confirms motion has stopped, then
waits up to two seconds for fresh inference for that exact camera revision before
activating Task 16. The wait releases the motion lock and is canceled by Stop,
INTERRUPT, LISTEN being disabled, or a changed category. A fresh frame with no
visible target starts the normal Waiting for target state. Camera failures and
timeouts report their cause without starting tracking.

App options: `--llm-config PATH` selects another SDK JSON configuration and
`--microphone NAME` selects an input device by name. LISTEN starts off.

Recordings include `llm` events with the original SDK event type, segment and
revision, local receive timestamp, audio-stream origin, parsed slots, and task
dispatch results. Raw microphone audio is not saved. Camera and state recordings
continue using the existing JSONL/MJPEG format.

`streamingcommand_sdk/` is vendored from the supplied CRF-LLM archive. Its Apache
license, notices, model provenance, and documentation are retained. Model weights
stay in user caches and are not included in the repository.

The dashboard opens cached models offline. Run the download command before
starting the app; missing weights produce an actionable error. Downloads occur outside
the control session so a network outage cannot hold up robot-app shutdown.

Worker JSON pipes explicitly use UTF-8 and command requests use ASCII JSON
escapes. This avoids Windows GBK decoding corrupting Mandarin/Cantonese input
before it reaches the tokenizer, regardless of the launcher's Python UTF-8 mode.
