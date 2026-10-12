# Windows 中文语音输入

A Windows voice-input prototype with configurable speech recognition, transcript
editing, a private vocabulary, and clipboard-first output. Maintained by baigao417.

面向 Windows 的口述转文字原型：通过全局热键开始/结束录音，转写后整理标点
与自然段，并复制到剪贴板。用于减少长段输入时的手工打字，不是聊天助手。
公开版本不附带个人录音、词典、评测历史、平台凭证或第三方项目。

## 核心机制

```text
热键 / 控制面板 -> 录音 -> 云端 ASR（失败回退本地 Whisper）
  -> 通用编辑提示词 + 本机词典 -> 可选 LLM 编辑
  -> 输出启发式检查（失败保留原转写）
  -> 剪贴板 / 显式开启的粘贴 -> 本机私有恢复缓存与元数据
```

实现与数据流见 [架构说明](docs/ARCHITECTURE.md)。支持取消/撤销录音、最近结果
恢复、后台单实例锁、停止请求、词典管理和本机使用/坏例报告。

## 快速开始

需要 Windows、Python 3.10+ 和麦克风。控制面板需要 Python 的 Tk 支持。
在仓库根目录打开 PowerShell：

```powershell
cd .\windows-typeless-voice-input
.\scripts\install.ps1
.\scripts\run.ps1 --help
.\scripts\run.ps1 --dry-run
```

安装脚本创建本目录 `.venv`，安装依赖，并从示例生成被忽略的 `config.json`。
脚本执行策略由使用者自行管理；不要为了本项目降低系统全局安全策略。

默认 ASR 和编辑走云端服务。需要自己申请服务并评估收费及数据处理条款；
本地环境变量仅在当前终端会话设置，值不要写入公开文件：

```powershell
$env:SILICONFLOW_API_KEY = Read-Host 'API key' -MaskInput
.\scripts\run.ps1 --daily
```

`-MaskInput` 需要 PowerShell 7；Windows PowerShell 5.1 使用自己的安全凭证注入方式。
程序不会自动加载 `.env`；`.env.example` 仅说明支持的环境变量名。
按 `Ctrl+M` 切换录音，`Ctrl+Alt+M` 重新复制最近结果，`Ctrl+Alt+Q` 退出。
`--daily` 仅复制，不自动按 `Ctrl+V`；首次使用建议保持这一模式。
控制面板：`.\scripts\control-panel.ps1`。

## 配置

在 `windows-typeless-voice-input/config.json` 覆盖示例字段：

| 字段 | 含义 |
| --- | --- |
| `stt.engine` / `stt.model` | 示例为 `siliconflow` / `TeleAI/TeleSpeechASR` |
| `stt.fallback` | 本地 Whisper 回退；首次加载可能下载模型 |
| `llm.enabled` | 设为 `false` 可关闭编辑，直接保留转写 |
| `llm.provider` / `base_url` / `model` | 云端兼容接口或自行部署的 `ollama` |
| `stt.api_key_env` / `llm.api_key_env` | 从指定环境变量取 key；公开配置不使用凭证文件 |
| `paste.method` | 默认 `clipboard`；显式改为 `hotkey` 才自动粘贴 |
| `paste.focus_guard_enabled` | 自动粘贴时检查前台窗口，变化则降级为剪贴板 |
| `hotkeys` | 热键冲突时改为未占用的组合 |
| `dictionary_paths` | 通用词典及被忽略的 `dictionary.local.jsonl` |
| `history` | 本机日志、最近正文、状态与坏例目录，均不应公开 |

完全本地路径需要显式设置 `stt.engine=faster-whisper`、`stt.model`、
`stt.model_cache_dir`、`stt.device`、`stt.compute_type`、`stt.language` 等字段，
并将 `llm.provider` 改为 `ollama`、`base_url` 改为本地服务地址、`model` 改为
已安装模型；不要保留云端 fallback model。也可关闭 LLM。
首次模型下载仍需联网；离线推理必须先准备模型文件。

最小本地 ASR 配置（仍在 Windows 运行目录中）：

```powershell
$config = Get-Content .\config.example.json -Raw | ConvertFrom-Json
$config.stt = $config.stt.fallback
$config.llm.enabled = $false
$config | ConvertTo-Json -Depth 12 | Set-Content .\config.json -Encoding utf8
```

Windows PowerShell 5.1 的 UTF-8 输出带 BOM，需改用无 BOM 编码；Python 配置读取
采用 UTF-8。上面写入示例建议使用 PowerShell 7。

词典导入需要明确输入和输出文件；示例均为虚构条目。
Rime 导出默认只生成文件；`--apply-to-rime`、快捷方式安装及开机启动会修改
仓库外的本机配置，必须由使用者主动执行。本次准备未运行这些操作。

## 离线验证

在仓库根目录执行，无需安装语音依赖，也不会调用 API：

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
python -B -m unittest discover -s tests -v
$env:PYTHONPATH = (Join-Path $PWD 'windows-typeless-voice-input\src')
python -B -m win_voice_input --help
python -B -m win_voice_input --dry-run
```

测试临时目录可用 `VOICE_INPUT_TEST_TMP` 指定；CI/本地验收应指向仓库外的私有目录。
评测脚本的额外依赖在 `benchmarks/requirements.txt`，音频元数据还可能需要
`ffmpeg/ffprobe`。云端 smoke/benchmark 会上传样本并可能收费，不属于离线测试。
`examples/rewrite/` 是新建的合成文字示例；不包含真实评测结果。

## 已知限制

- 先完整录音再处理，不是实时流式输入法；流式脚本只是独立评测工具。
- 本次公开准备只验证离线 CLI 和回归逻辑，未验证真实麦克风、全局热键、
  桌面 UI、跨应用粘贴、模型推理或云端服务。不能据此宣称完整端到端可用。
- 模型示例没有质量、延迟或价格保证；依赖版本为下限范围，未锁定所有平台组合。
- 提示词和输出检查可能失效；必须检查生成结果，尤其是数字、否定词和专有名词。
- `logs/` 和坏例可能含完整正文，不加密；使用者需自行限制访问和管理留存。
- 焦点防护无法保证光标位置不变；管理员窗口和受保护应用可能拒绝热键/粘贴。

## 免责声明与致谢

软件按 MIT 许可证提供，无任何担保。不要用于未经同意的录音或处理不允许上传
的数据。云端服务、模型、依赖和可选工具遵守各自条款；MIT 不覆盖其权利。
本项目不代表 Typeless 或 VoiceInk。感谢其交互思路；未包含相关项目代码、素材
或文档。详见 [第三方说明](THIRD_PARTY_NOTICES.md)。
