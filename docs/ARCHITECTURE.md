# 架构与数据边界

```text
CLI / Win32 热键 / 控制面板
  -> sounddevice 录音
  -> SiliconFlow ASR -> 失败时 faster-whisper
  -> 编辑提示词 + 可选本机词典
  -> OpenAI-compatible LLM / 可选 Ollama
  -> 启发式输出检查 -> 失败时保留原始转写
  -> 剪贴板 / 可选焦点检查后的粘贴
  -> 本机 logs（最近正文、运行元数据、显式标记的坏例）
```

## 模块

- `src/win_voice_input/app.py`：配置合并、词典、ASR/LLM 路由、热键、UI、恢复和运行状态。
- `scripts/`：启动、安装、环境诊断、词典导入、Rime 导出和可选快捷方式维护。
- `benchmarks/scripts/`：ASR/CER、流式协议、文本编辑评分、使用及坏例元数据汇总。
- `tests/`：标准库离线回归；不连接 provider，不下载模型，不录音或粘贴。

## 私有数据

默认云端路径会上传音频；启用云端编辑时会上传转写正文和参与编辑的词典。
词典不会作为热词参数传给当前 SiliconFlow ASR 实现，但会进入 LLM 编辑输入。
`logs/last-result.json` 保存最近结果和原始转写；坏例快照也可包含完整正文。
usage 索引过滤顶层正文键，不保证错误消息、备注或嵌套字段无敏感信息。
这些文件不属于可公开产物，`.gitignore` 不是上传其他渠道时的隐私保护。

## 边界

焦点检查只比较录音开始和粘贴时的前台窗口，不保证应用内部光标未变化。
公开配置默认 `paste.method=clipboard`，必须手动粘贴；自动粘贴需显式启用。
提示词与启发式检查只是尽力防护，不提供语义正确性或提示注入安全保证。
模型名和 endpoint 是配置示例，不代表当前可用性、质量排名或收费承诺。
