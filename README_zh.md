# ArduScope

[![CI](https://github.com/L-X-Yao/arduscope/actions/workflows/ci.yml/badge.svg)](https://github.com/L-X-Yao/arduscope/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-green.svg)](https://www.python.org/)

ArduPilot `.BIN` 飞行日志诊断工具。把"飞得不好"翻译成"改哪个参数"。

[English](README.md)

## 功能

ArduScope 读取 ArduPilot `.BIN` 日志，运行 32 个分析器，生成可执行的参数调整建议，并附一份描述性飞行包络摘要。每条警告都引用来源：METHODIC（即 [ArduPilot Methodic Configurator 的调参指南](https://ardupilot.github.io/MethodicConfigurator/TUNING_GUIDE_ArduCopter.html)）、ArduPilot wiki 或 ArduPilot 源码。

**核心特性：**
- **32 个分析维度** — 自动发现，在 `analyzers/` 下新建 `.py` 文件即可扩展
- **结构化参数建议** — 具体参数名 + 建议值，可导出 `.param` 文件直接导入 Mission Planner
- **METHODIC 标准阈值** — 每条 warning 和 critical 都附来源引用。还没有出处的限值列在[已知限制](docs/known_limitations.md)里
- **交叉关联诊断** — 27 条规则检测多系统联合故障模式
- **交互式 HTML 报告** — Plotly 图表和地图代码内嵌，离线可用。地图瓦片需要联网，无网络时地图上会提示
- **多日志对比** — 跟踪调参收敛过程
- **机队历史档案** — 每架次自动按机身归档（硬件序列号识别），报告尾部趋势卡、`fleet_report.py` 查询（含 `--backfill` 回填历史日志）+ web 机队页（`/fleet`，可改别名）
- **Web + 命令行** — 浏览器拖拽上传或命令行批处理
- **中英双语** — 默认中文，`--lang en` 输出英文

## 快速开始

Windows 且没有 Python：从 [Releases](https://github.com/L-X-Yao/arduscope/releases)
下载 `arduscope-vX.Y.Z-win64.zip`，解压后双击 `start.bat`。包内自带 Python，
启动后在浏览器里打开 Web 界面。

ArduScope 不在 PyPI 上发布，PyPI 上名为 `arduscope` 或 `ardupilot-scope` 的包不是本项目。

从源码安装，需要 Python 3.10 或更高：

```bash
git clone https://github.com/L-X-Yao/arduscope.git
cd arduscope
pip install .

# 单日志分析（自动打开浏览器 + 输出 .param 参数建议）
python3 flight_report.py 00000285.BIN

# 多日志对比
python3 multi_report.py flight1.BIN flight2.BIN flight3.BIN

# 批量处理
python3 batch_report.py logs/ -o reports/

# Web 界面
pip install .[web]
python3 web_app.py
```

`pip install .` 还会装上 `arduscope`、`arduscope-multi`、`arduscope-batch`、`arduscope-fleet`、`arduscope-web` 五个命令，分别对应上面的脚本和 `fleet_report.py`。

## 用法

```bash
# 输出到指定路径
python3 flight_report.py -o report.html log.BIN

# 导出 JSON / CSV / PDF
python3 flight_report.py --format json log.BIN
python3 flight_report.py --format csv log.BIN
python3 flight_report.py --format pdf log.BIN   # 需要: pip install .[pdf]，图表还需要 Chrome

# 语言选择
python3 flight_report.py --lang en log.BIN

# 自定义阈值覆盖
python3 flight_report.py --thresholds custom.json log.BIN

# 运行 / 跳过指定分析器
python3 flight_report.py --only summary,attitude,motor log.BIN
python3 flight_report.py --skip vibration,ekf log.BIN

# 场景模板：只跑一部分分析器，有的模板阈值更严
python3 flight_report.py --list-templates
python3 flight_report.py --template crash-diagnosis log.BIN

# 这一架次不记入机队历史（报告旁的 fleet.db）
python3 flight_report.py --no-fleet-record log.BIN

# 多日志对比
python3 multi_report.py -o trend.html *.BIN

# Web 界面（开发模式）
python3 web_app.py --port 9090 --report-dir ./reports   # 默认: ~/.arduscope/reports
python3 web_app.py --keep-reports 1000                  # 超过 N 份时删除最旧的（默认全部保留）
python3 web_app.py --host 0.0.0.0 --allow-host my.host  # 无登录，只在可信网络使用

# Web 界面（生产模式）
pip install .[production]
python3 web_app.py --server waitress --workers 8

# 用 LLM 做 AI 诊断（默认是离线规则引擎）
pip install .[ai]
ANTHROPIC_API_KEY=... python3 flight_report.py --ai-provider anthropic --ai-model 模型名 log.BIN
# 任何兼容 OpenAI chat completions 接口的服务也可以用
OPENAI_API_KEY=... python3 flight_report.py --ai-provider openai --ai-base-url https://api.example.com/v1 --ai-model 模型名 log.BIN
```

## 分析器

| 分析器 | Key | 消息类型 | 说明 |
|--------|-----|----------|------|
| 飞行摘要 | `summary` | prescan | 固件版本、模式时间线、错误事件 |
| 飞行包络 | `flight_profile` | GPS, POS, XKF1, ARSP, ATT | 高度 / 速度 / 爬升率 / 航程 / 姿态包络（描述性） |
| 姿态跟踪 | `attitude` | ATT, RATE, PIDx | Roll/Pitch/Yaw 跟踪误差、PID 分析 |
| 电机输出 | `motor` | RCOU, MOTB | 饱和度、平衡性、悬停余量 |
| 振动 | `vibration` | VIBE, IMU | 振动水平、FFT 频谱 |
| 陀螺仪饱和 | `gyro_saturation` | IMU | 陀螺仪传感器饱和检测 |
| 油门塌陷 | `throttle_collapse` | CTUN, RCOU | 油门压在混控下限、极限环取证 |
| 陷波调参 | `notch_tuning` | RCOU, ISBH/ISBD, IMU | FFT 峰值检测 + 陷波参数建议 |
| PID 调参 | `pid_tuning` | ATT, RATE, PIDx | PID 质量评估 + 增益建议 |
| 舵面抖动 | `servo_dither` | RCOU | 过零率振荡检测 |
| EKF 健康 | `ekf` | XKF4/NKF4, XKF3/NKF3 | 方差监控、创新量 |
| 电池 | `battery` | BAT | 电压、电流、剩余容量 |
| 功率 | `power` | BAT, GPS | 功耗、能量 (Wh)、能效 (Wh/km) |
| 板载电源 | `board_power` | POWR, MCU | 板载电压跌落与掉电取证 |
| GPS 质量 | `gps` | GPS, GPA | 卫星数、HDop、航迹图 |
| GPS 跳变 | `gps_glitch` | GPS | 基于 haversine 距离的位置突变检测 |
| EKF 速度发散 | `ekf_vel_divergence` | GPS, XKF1 | 按通道比较 EKF 速度与 GPS |
| 罗盘 | `compass` | MAG, RCOU | 场强一致性、偏移、电机干扰 |
| 罗盘一致性 | `compass_consistency` | MAG | 多罗盘场强 RMS 对比 |
| 故障保护余量 | `failsafe` | ERR/EV, BAT, GPS, XKF4 | 距故障保护触发阈值的余量 |
| 时钟健康 | `clock_health` | ATT | 时间戳单调性、抖动、间隙检测 |
| IMU 一致性 | `imu_consistency` | IMU | 多传感器加速度计/陀螺仪对比 |
| 温度 | `temperature` | IMU | IMU 温漂、高温警告 |
| 遥控输入 | `rc_input` | RCIN | 通道监控、信号丢失检测 |
| 测距仪 | `rangefinder` | RFND, CTUN | 距离质量、气压计对比 |
| 日志健康 | `log_health` | PM, ATT | 循环时间、CPU 负载、消息时序 |
| VTOL 转换 | `vtol_transition` | QTUN | 转换时间、高度损失（QuadPlane） |
| 空速 | `airspeed` | ARSP, GPS | 双空速管一致性、皮托管堵塞 |
| TECS 调参 | `tecs_tuning` | TECS | 巡航段高度/速度跟踪 RMS（固定翼） |
| 巡航能效 | `cruise_efficiency` | BAT, GPS | 每公里能耗、巡航功率估算 |
| 航点任务 | `mission` | CMD, GPS/POS | 航点精度、完成率 |
| 风场 | `wind` | XKF2/NKF2 | 风速/风向、阵风分析 |

## 输出格式

**HTML 报告**（主要）— 执行摘要、健康仪表盘、32 个分析器卡片含交互图表、统一 `.param` 参数建议表。

**JSON** — 结构化数据，每个分析器包含 `status` / `action_level` / `param_suggestions`。

**CSV** — 表格格式，Excel 直接打开。

**PDF** — 打印版报告（需要 `pip install .[pdf]`，图表渲染还需要 Chrome）。

## 阈值来源（优先级）

1. **METHODIC 官方文档** — 基线标准，不可协商
2. **ArduPilot 源码限值** — 故障保护/预解锁触发值
3. **真实飞行日志校准** — 来自真实飞行的统计分布

工具会自动读取日志中的参数（`FS_EKF_THRESH`、`BATT_LOW_VOLT` 等）作为判断依据。

自定义阈值覆盖 `--thresholds custom.json`：
```json
{
    "vibe_warn": 20.0,
    "vibe_crit": 40.0,
    "temp_warn": 55.0
}
```

## 项目定位

本工具面向 **ArduPilot 调参工程师**。诊断严格性是特性，不是缺陷——"觉得阈值严"不构成调松依据。社区日志数据欢迎用于阈值校准，带日志的误报报告同样欢迎，降低标准的诉求不接受。功能开发已于 2026 年 11 月结束，维护者只修缺陷，不加新功能。详见 [POSITIONING_zh.md](POSITIONING_zh.md)。

## 已知限制

[docs/known_limitations.md](docs/known_limitations.md) 列出 ArduScope 做不到或做不准的地方。按报告行动前先了解三点：

- critical 很常见：语料 560 份日志里 414 份至少有一条，其中误报占多少没有统计过。
- 参数建议只在 SITL 里飞过，20 条规则中的 12 条，没有上过真机。加载 `.param` 文件前请逐项核对。
- 损坏的日志会按能读到的部分分析，报告不会提示日志已损坏。

## 项目结构

```
arduscope/
├── flight_report.py      单日志 CLI 入口
├── multi_report.py       多日志对比 CLI
├── batch_report.py       批量处理 CLI
├── fleet_report.py       机队历史 CLI
├── web_app.py            Web 界面 (Bottle + Waitress)
├── lib/                  核心库
│   ├── pipeline.py       统一分析管线
│   ├── log_reader.py     pymavlink .BIN 解析器
│   ├── html_report.py    HTML 报告生成器
│   ├── ai_diagnosis.py   交叉关联引擎 (27 条规则)
│   ├── thresholds.py     阈值配置
│   ├── i18n.py           国际化
│   └── ...
├── analyzers/            32 个自动发现的分析器
├── locales/              zh.json + en.json
├── tools/                CI、lint、release、baseline 工具
├── tests/                pytest 测试套件（fast、合成、真实日志、SITL 四层）
└── docs/                 阈值溯源、建议依据、已知限制
```

## 安全

Web 界面没有登录，默认只监听 127.0.0.1。[SECURITY.md](SECURITY.md) 说明了这意味着
什么，以及如何报告漏洞。

## 许可证

[MIT](LICENSE)
