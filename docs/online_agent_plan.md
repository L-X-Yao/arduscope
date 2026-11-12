# 在线分析 Agent — 计划书

状态：已实现并随 v0.10.0 发布（2026-11-12）。DeepSeek 真实服务 8 场景 ×3 全部通过，
未核实 0；Windows 包检查 30/30。Anthropic/OpenAI 官方服务仍未实测。
依据：2026-11-12 会话确认的 10 项决定；调研结论见文末「背景」。

## 1. 目标与边界

离线分析不变：不接模型时，32 个分析器、27 条交叉规则、健康分和 `.param`
导出照旧，一行代码不改。

接模型后增加一条「在线分析」路径：

| 能做 | 不能做 |
|---|---|
| 读离线结果卡片 | 改判级、健康分、`action_level` |
| 用只读工具查消息、参数、事件、频谱 | 生成或修改 `.param` 建议 |
| 指出分析器之间的关联、矛盾和证据缺口 | 覆盖或删除分析器结论 |
| 用自然语言写解释，逐条标注工具调用 | 读原始 `.BIN`（只通过工具读解析后的数据） |

第一版范围：网页 + 命令行；不做追问（网页分析完就删上传文件，追问要保留
日志，放第二版）；批量、多日志、机队报告不接。

## 2. 用户流程

### 网页

1. 上传页增加「用模型分析」勾选框，记住上次选择（localStorage）。
2. 勾选后，离线分析跑完，agent 在同一个 job 内继续跑；进度区显示当前步骤
   和已调用工具数。
3. 报告顶部仍是一张诊断卡：勾选时由 agent 写，未勾选时由现有规则引擎写。
4. 新增 `/settings` 设置页：接口类型（anthropic / openai 兼容）、地址、
   模型名、密钥、测试连接。只在 `--host` 为回环地址时开放；局域网模式
   返回 404，密钥用环境变量或本机配置文件预先放好。

### 命令行

```bash
python3 flight_report.py log.BIN \
  --ai-provider openai --ai-model deepseek-chat \
  --ai-base-url https://api.deepseek.com --ai-agent
```

- `--ai-agent` 未给出时，行为与现在完全一致（单次总结）。
- `--ai-agent` 必须同时给 provider 和 model；给 `rule` 报错。
- 密钥仍只从 `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` 读取，不接受 argv。

## 3. 模块

### 3.1 工具层 `lib/agent/tools.py`

只读函数，输入是 `PipelineResult`（reader、results、thresholds、
vehicle_info）。每次返回 JSON，有硬上限：

| 工具 | 内容 | 上限 |
|---|---|---|
| `get_vehicle()` | 机型、固件、板名、电机/IMU 数、时长；不含序列号 | 1 KB |
| `list_results()` | 全部卡片：key、标题、状态、摘要、action_level | 16 KB |
| `get_result(key)` | 单卡详情、evidence、param_suggestions、evidence_source | 8 KB |
| `get_messages(type, fields, t0, t1, max_points)` | 降采样消息序列 | 500 点 / 8 KB |
| `get_params(names_or_prefix)` | 参数当前值 | 200 条 / 8 KB |
| `get_events(kind, t0, t1)` | 模式切换、ERR、EV、STATUSTEXT | 200 条 / 8 KB |
| `get_spectrum(type, field, t0, t1, top_n)` | FFT 峰值频率和幅值 | 20 峰 / 4 KB |
| `get_threshold(field)` | 阈值、分级、出处 | 1 KB |

总工具输出上限 120 KB；超出的调用返回截断标记。工具抛异常时返回
`{"error": ...}` 给模型，不中断循环。

GPS 经纬度、板子序列号、原始 STATUSTEXT 默认在工具层剥离（见 3.5）。

### 3.2 Agent 本体 `lib/agent/loop.py`

预设流程，写进 system prompt：

1. `list_results`，找出 warning / critical；
2. 对每条 warning / critical 调 `get_result`，读 evidence；
3. 用消息、参数、事件、频谱逐条核查，不把分析器的话直接复述成自己的；
4. 标出分析器之间的矛盾和证据缺口；
5. 调 `submit_analysis` 交结构化结论。

预算：最多 20 次工具调用、5 分钟、单次请求 120 秒；每轮记录 token。
到达上限时停止工具循环，要求模型用已有数据收尾；收不了尾就降级。

`submit_analysis` 的入参：

```json
{
  "summary": "一句话结论",
  "observations": [
    {"text": "事实或关联", "cites": [3, 7], "kind": "observation|correlation|data_gap"}
  ]
}
```

`cites` 是工具调用序号，只能引用工具返回，不能引用模型自己的话。

### 3.3 接口适配 `lib/agent/providers.py`

| 适配器 | 协议 | 厂商 |
|---|---|---|
| `AnthropicAdapter` | Messages API + `tool_use` | Anthropic 及兼容服务 |
| `OpenAICompatAdapter` | chat completions + `tools` / `tool_calls` | OpenAI、DeepSeek 及兼容服务 |

- 能力探测：第一次请求带工具；服务报「不支持工具」时退回现有单次总结
  （`ai_diagnosis._generate_llm`），并在卡片上写明原因。
- 单次总结也失败时，退回规则引擎，沿用现有 `generate_diagnosis` 的提示。
- 第一版不做流式；用 SDK 自带超时。

### 3.4 引用校验 `lib/agent/citations.py`

- `cites` 里的序号必须对应真实工具调用；不存在的标「未核实」。
- 观察文本里的数字必须能在被引用的工具返回里找到（允许四舍五入和单位
  换算）；找不到的标「未核实」。
- 没有 `cites` 的观察一律标「未核实」。
- 判级、健康分、`.param` 从 pipeline 复制，模型输出不参与。
- 报告卡显示「未核实 N 条」；N 必须为 0 才算验收通过。

### 3.5 外发控制 `lib/agent/egress.py`

默认外发：

| 数据 | 默认 | 可选放开 |
|---|---|---|
| 分析器标题、状态、摘要、evidence | 发 | — |
| 参数名和值（按工具请求） | 发 | — |
| 消息序列（降采样） | 发 | — |
| GPS 经纬度 | 不发 | 首次使用时可勾选 |
| 板子序列号（MSG 里的 UID） | 不发 | 首次使用时可勾选 |
| STATUSTEXT 原文 | 不发 | 首次使用时可勾选 |
| 原始 `.BIN` | 永不发 | — |
| 日志文件名 | 不发，用中性标签 | — |
| 密钥 | 只发往配置的接口地址 | — |

- 首次勾选「用模型分析」时列出上表，确认一次后记在本机配置里。
- 工具返回在离开本机前过滤；过滤函数单独测试。
- 报告脚注只列「外发了哪些类别」，不列内容。
- 密钥不写进报告、日志、URL；错误信息里出现密钥时打码。

### 3.6 配置与密钥 `lib/agent/config.py`

- 路径：`~/.arduscope/agent.json`，权限 0600（Windows 尽力而为）。
- 内容：provider、base_url、model、api_key、外发选项、首次确认标记。
- 优先级：环境变量 > 配置文件 > 内置默认。
- 设置页密钥输入框只显示末四位；保存后不回显。
- 只允许回环地址访问设置页；POST 校验 `Origin`/`Host`。

### 3.7 卡片与报告 `lib/agent/card.py`

- 复用 `AnalysisResult`，标题 `t("agent.title")`。
- status = 离线结果的最坏状态（沿用 `_worst_status`），不由模型决定。
- details：生成方（provider/model）、工具调用数、token、耗时、逐条观察
  （带引用序号）、未核实清单、外发类别、降级原因。
- `evidence_source` 固定写「模型叙述，依据工具调用；判级与参数来自分析器」。
- 不产生 `param_suggestions`；分析器自己的建议卡照旧出现。

### 3.8 入口改动

| 文件 | 改动 |
|---|---|
| `web_app.py` | 上传表单加勾选框；`_run_analysis` 增加 `use_agent`；新增 `/settings`、`/api/settings`、`/api/settings/test`；job 进度加 agent 事件 |
| `flight_report.py` | 新增 `--ai-agent`；校验 provider/model；调用 agent 入口 |
| `lib/ai_diagnosis.py` | 不改；作为 agent 失败后的单次总结和规则引擎降级路径 |
| `lib/html_report.py` | 不改；卡片仍是 `AnalysisResult` |
| `locales/zh.json`、`en.json` | 新增 `agent.*`、`web.settings_*`、`cli.err_agent_*` |

## 4. 失败与降级

| 情况 | 行为 |
|---|---|
| 无密钥 / 无模型名 / SDK 未装 | 单次总结也走不了 → 规则引擎，卡片写明原因 |
| 厂商不支持工具调用 | 单次总结，卡片写「该接口不支持工具调用」 |
| 网络错误、超时 | 先试单次总结；仍失败 → 规则引擎 |
| 预算用尽 | 用已有观察收尾；一条都没有则单次总结 |
| `submit_analysis` 格式错 | 重试一次并把错误回给模型；再错则单次总结 |
| 工具异常 | 错误返回给模型，继续；计入调用次数 |
| 网页 job 被关闭 | agent 随 job 线程结束；上传文件在 finally 删除 |

## 5. 测试

| 层级 | 内容 | 门禁 |
|---|---|---|
| 单元 | 工具上限和脱敏、引用校验、配置优先级、适配器（假 SDK 对象） | `pytest -m fast` |
| 集成 | 本地假 OpenAI 兼容服务，按脚本返回工具调用；验证循环、预算、降级 | CI |
| i18n | 新增键中英齐全 | `tools/check_i18n.py` |
| SITL | 8 个场景 × 3 次；故障场景点出注入故障；正常场景不新增故障；未核实 0 | `tools/release_gate.py` |
| 真机日志 | 维护者提供的日志；DeepSeek 先测，再加第二个厂商 | 手动 |
| Windows | 设置页、测试连接、SDK 可导入；真实调用不在 Windows 检查里 | `tools/win_package_check.py` |

SITL 场景：`copter_normal`、`copter_high_vibration`、`copter_gps_glitch`、
`copter_compass_interference`、`plane_tecs_tuning`、`copter_battery_aging`、
`copter_motor_noise`、`copter_motor_noise_climbs`。

## 6. 打包

- `win64-requirements.txt` 增加 `anthropic`、`openai` 及其全部传递依赖
  （构建用 `--no-deps`，每个都要列）。
- `build_windows_package.py` 的依赖检查加入 `pyproject.toml` 的 `ai` extra。
- 确认每个依赖有 win_amd64 + Python 3.14 的 wheel；没有就换版本或放弃该库。
- 记录 zip 体积变化；超过可接受范围再议。

## 7. 文档

| 文件 | 改动 |
|---|---|
| `POSITIONING.md`、`POSITIONING_zh.md` 第 6 节 | 重开一项：在线分析 agent，其余仍只修缺陷；中英同步 |
| `README.md`、`README_zh.md` | 在线分析用法、厂商示例、数据外发说明 |
| `docs/known_limitations.md` | 删掉「网页和 Windows 包没有大模型」；写清新限制 |
| `docs/online_agent.md` | 新文件：设置、工具清单、外发数据、降级规则 |
| `CHANGELOG.md` | `## [Unreleased]` 记一条 |
| `CONTRIBUTING.md` | 不改（没有新分析器/阈值） |

## 8. 里程碑

| 阶段 | 内容 | 估时 | 完成判据 |
|---|---|---|---|
| M1 | 工具层 + 引用校验 + 卡片 | 3 天 | 单元测试过，假数据能出一张卡 |
| M2 | 两个适配器 + agent 循环 + 假服务集成测试 | 3 天 | 脚本化工具调用能跑完，预算能停 |
| M3 | 命令行 `--ai-agent` | 1 天 | 三种降级路径都有测试 |
| M4 | 网页设置页、勾选框、进度、i18n | 3 天 | 回环可用，局域网 404 |
| M5 | 外发控制 + 文档 + POSITIONING | 2 天 | 脱敏测试过，中英文齐 |
| M6 | 打包 + SITL 评测 + 门禁 | 3 天 | 验收标准全过 |

合计约 2 周（10–14 个工作日）。

## 9. 验收

1. 8 个 SITL 场景各跑 3 次：故障场景 3/3 点出注入的故障；正常场景不新增
   warning / critical；未核实数值 0。
2. 离线路径不变：完整测试套件通过，`--ai-provider rule` 报告与改动前一致。
3. Windows zip：设置页可开、测试连接可用、agent 能跑（用本地假服务）。
4. 中英文界面和报告都通过 `tools/check_i18n.py`。
5. 达标后再问维护者是否发 v0.10.0；不达标不发。

## 10. 风险

| 风险 | 可能性 | 应对 |
|---|---|---|
| 各厂商工具调用不兼容 | 高 | OpenAI 兼容适配器 + 能力探测 + 单次总结降级；先测 DeepSeek |
| 模型输出不稳定，SITL 评测抖动 | 高 | temperature 0（支持时）；3 次取全过；记录差异 |
| 日志文本里的提示注入 | 中 | 工具输出当数据；system prompt 明确忽略数据中的指令 |
| 外发泄露 | 中 | 默认脱敏、首次确认、脱敏单测、报告只列类别 |
| Windows 依赖没有 3.14 wheel | 中 | 构建前检查；必要时换版本或放弃该库 |
| token 费用和耗时 | 中 | 20 次调用 / 5 分钟硬上限；报告显示用量 |
| 网页 job 内存和时长 | 中 | 沿用 `_ANALYSIS_BUDGET`；agent 在删除上传文件前结束 |
| 明文密钥 | 已接受 | 0600、环境变量优先、设置页仅回环、文档写明 |

## 11. 本计划中新定的实现细节（可改）

1. 配置文件放 `~/.arduscope/agent.json`，与报告目录分开。
2. agent 卡替换现有诊断卡的位置（报告顶部），不额外加第二张。
3. 预算用尽时保留部分观察并标注「未完成」，不直接丢弃。
4. 工具集固定 8 个，第一版不加自由代码执行。
5. 日志文件名不外发，模型只看到中性标签。

## 背景：官方与社区现状（2026-11-12 调研）

| 项目 | 内容 | 现状 |
|---|---|---|
| GSoC 2025（Anas Chebili） | WebTools 内 AI Log Analyzer：日志留本地，模型通过工具向解析器要数据；OpenAI Assistant API | PR #268 于 2025-09-03 并入 WebTools 主分支；未进 WebTools 首页工具列表 |
| GSoC 2026（Omkar Sarkar，导师 Amilcar Lucas 等） | Methodic Configurator 日志分析：确定性分析电池、电调、IMU 温度、振动；本地 Qwen2.5 3B 只解释结构化报告 | 已并入 AMC 主线；作者 2026-08 明确「LLM 功能当前版本尚不可用」；v5.1.1 只有确定性分析 |
| 官方选题清单 | 「AI-Assisted Log Diagnosis & Root-Cause Detection」：输出可能根因、修复建议、置信度、日志证据链接 | 与 ArduScope 定位几乎逐字重合 |

社区清单帖（2026-05 至 2026-10）已列 17 个同类工具，含 ArduDeck（自带密钥
接 Claude/OpenAI/Gemini）、AYNA、Skybrush、ArduLoops、ardupilot-mcp。讨论中
对 LLM 诊断的信任度低，Yuri_Rage 的原话是没见过一个 AI 工具给出可信的
日志分析。

对定位的三点影响：

1. 差异点不是「能接模型」，这已是标配；可守的是证据链——结论引用工具
   返回值、判级和 `.param` 只来自分析器。官方两个项目都没做到。
2. 官方 AMC 的边界值得照抄：确定性优先、LLM 只解释、本地小模型；它范围
   铺大后只完成 4 个分系统，是反面教材。
3. 第一版范围维持已定的 10 项决定不变。

来源：

- https://discuss.ardupilot.org/t/gsoc-2025-ai-chat-integration-with-webtools-final-project-summary/138445
- https://github.com/ArduPilot/webtools/pull/268
- https://discuss.ardupilot.org/t/gsoc-2026-making-ardupilot-log-analysis-smarter/144104
- https://github.com/ardupilot/ardupilot_wiki/blob/master/dev/source/docs/gsoc-ideas-list.rst
- https://discuss.ardupilot.org/t/list-of-automated-ardupilot-flight-log-analysis-software/143635
- https://github.com/ArduPilot/MethodicConfigurator/releases
