# ArduScope 产品定位

[English](POSITIONING.md)

**最后更新**：2026-11-11
**维护者**：L-X-Yao
**本文档目的**：功能讨论、取舍、争议以本文件为准。要改定位，就改本文件。

---

## 一、一句话定位

**给 ArduPilot 调参工程师用的专业诊断工具，把"飞得不好"翻译成"改哪个参数"。**

---

## 二、用户

**目标用户：ArduPilot 调参工程师**（有参数调整经验，能读懂 METHODIC 术语。METHODIC 即 [ArduPilot Methodic Configurator 的调参指南](https://ardupilot.github.io/MethodicConfigurator/TUNING_GUIDE_ArduCopter.html)）

- 设计决策由项目维护者（L-X-Yao）主导
- 接受社区用户的**日志数据**作为阈值校准来源
- 误报是缺陷：带日志的误报报告是欢迎的
- 不接受"降低诊断标准"类的反馈——工具严格是特性，不是缺陷
- 不做简化模式、不做降低门槛的设计

---

## 三、解决什么问题

**唯一核心痛点：诊断能力 —— 从"飞得不好"到"改哪个参数"**

行业现状：
- 飞手能感觉飞机有问题，但**不知道改哪个参数**
- 大部分工具只输出原始数据，不给诊断结论
- ArduPilot METHODIC 文档知识门槛高，查找困难

本工具定位：
- 读日志 → 自动检测问题 → 给出依据和参数建议
- 把 METHODIC 的知识编码成 32 个分析器的判定规则
- 每条 warning 和 critical 都附判定依据
- 参数建议写明参数名。数值要有出处和一次飞行验证，两样都登记在 `docs/suggestion_evidence.md`。没有飞过的规则，要么只指方向不给数值，要么带豁免：理由写在 `tools/check_suggestions.py` 里，名单列在 `docs/known_limitations.md`。只改日志记录内容的设置不靠飞行验证

**不做的事**（当前阶段）：
- 不做"认证 / 评分"（证明飞机好不是本工具目标）。报告里的健康分只用于排序和分流，不是认证：含 critical 时封顶 49，落在红档（2026-11-05 确认）
- 不做"教学"（教学是写文档的事，不是工具的事）
- 不做"陪伴 / 安抚"（工具不和稀泥）

---

## 四、工具的立场

### 4.1 诊断严格性
**阈值按 METHODIC 标准严格执行**，不为"让用户觉得飞得好"而调松。

- 用户主观"飞得好" ≠ 工具应该全绿
- 工具发现差距就直接说
- 用户不同意可以忽略建议，但**不能要求工具改变判断**

### 4.2 阈值来源（按优先级）
1. **METHODIC 官方文档**（最高权威，baseline 不可违背）
2. **ArduPilot 源码硬限**（failsafe/pre-arm 触发条件，critical 级别依据）
3. **真实日志数据校准**（用真实飞行日志统计分布，修正 warning/optimize 边界）
4. ❌ **不采用**：工程师个人经验拍脑袋定值

代码还没有做到这一条的地方，逐项列在 [docs/known_limitations.md](docs/known_limitations.md)。`lib/thresholds.py` 里标 `E` 的字段没有权威出处，属于豁免项：名单由 `tests/test_threshold_authority.py` 锁定，新增要写明理由，其中仍会影响判级的列在已知限制里。

### 4.3 权威性表达
报告里每条建议都要**标注依据**：
- "来源：METHODIC IMU_Temperature.md §3.2"
- "来源：ArduPilot wiki EKF Failsafe 默认 FS_EKF_THRESH=1.0"
- "来源：21 份实飞日志 P75 分位"

用户看到来源才愿意相信工具。

---

## 五、输出形态

当前形态：

- CLI：`flight_report.py log.BIN` → HTML 报告 + `.param` 建议
- Web GUI：`web_app.py` → 浏览器拖拽上传
- 多日志对比：`multi_report.py`、`batch_report.py`
- 机队历史：`fleet_report.py`，以及 Web 界面的 `/fleet` 页面
- 离线可用的 HTML 报告（地图瓦片除外）

优先级始终是**报告内容的深度和准确性**，形态其次。

---

## 六、维护状态

**功能开发已于 2026-11-10 结束。在线分析 agent 于 2026-11-12 重开并随 v0.10.0 发布。维护者只修缺陷，不加新功能。**

合并请求按 [CONTRIBUTING.md](CONTRIBUTING.md) 的门禁处理。改分析器逻辑或阈值的，要等维护者在不公开的日志上跑完快照和语料两步。

**不做以下事情**：
- ❌ 为"让报告看起来专业"做的美化
- ❌ 为"覆盖更多场景"做的超前设计
- ❌ 为单点反馈做的策略性调整（不因个别用户降低标准）

**只做以下事情**：
- ✅ 提升诊断准确率（减少误报、减少漏报）
- ✅ 把权威依据显性化（让建议可信）
- ✅ 用真实日志数据校准阈值

---

## 七、现状

### 已完成
- 离线诊断引擎：32 个分析器覆盖主要诊断维度；METHODIC 阈值来源标注（`docs/convergence_criteria.md`）；`param_suggestions` 结构化输出 + `.param` 导出；代码审计 + 测试覆盖
- 在线分析 agent：可选的模型分析，走只读工具并带引用；随 v0.10.0 发布。离线引擎仍是权威
- 开源：2026-11-11 公开。`pip install .` 可安装（包名 `ardupilot-scope`，只发 GitHub Release，不发 PyPI）；英文 README；报告中英双语

### 没有排期的设想
列在这里只为说明当初的方向，都不是承诺：
- 多次飞行趋势追踪 → 调参收敛助手
- 社区匿名日志数据集 → 统计驱动的阈值校准
- Mission Planner / MAVProxy 集成
- 阈值分层：warning/optimize 二档

---

## 八、变更规则

- 本文件由维护者修改，中英两份同步
- 社区反馈中**日志数据**可用于阈值校准，**降低标准的诉求**不构成变更依据
- 重大改动前先对照开头的一句话定位，确认方向一致

---

**本文件结束。**
