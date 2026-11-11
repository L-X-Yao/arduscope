# 机队历史维度 — 设计记录（v1）

状态：已实现。归档、报告里的趋势卡、`fleet_report.py` 和 Web 的 `/fleet` 页面随 v0.6.0 发布，`--backfill` 和别名编辑随 v0.7.0 发布。本文是 2026-07-16 动手前的设计稿，文中的三处「决策点」当时待定，现以代码和 `CHANGELOG.md` 为准。

## 目标

把 ArduScope 从"单日志诊断"扩展到"按机身的健康档案"：每次分析自动
归档关键指标，回答"这台机器在变坏吗"——振动漂移、电池衰减、板级供电
恶化、姿态控制退化。适合同一批机身长期积累日志的用法。

## 1. 机身识别（已实证，无需拍板）

开机 MSG 消息自带 STM32 96-bit 硬件序列号，格式为板名加三段 UID。三份实飞
日志各带一条，下面的序列号是示例值：

```
CubeOrange 001A002B 3030510C 31323334
CubeOrange 003E004F 30305110 39303132
CubeOrange 002C003D 3030510E 35363738
```

- 主键 = 三段 UID 拼接（`001A002B-3030510C-31323334`）。
- 别名表允许人工命名（"3 号原型机"/客户编号），序列号缺失的旧日志
  （无 MSG 或被截断）落入 `unknown`，不猜。
- 换飞控板 = 新机身档案，符合"档案跟板不跟机架"的硬件取证语义；
  需要跟机架时靠别名表手工合并视图（v1 不做自动合并）。

## 2. 存储（已定：报告目录旁）

- 引擎：**sqlite（Python 标准库，零新依赖）**。
- 模式：
  ```sql
  airframes(serial TEXT PRIMARY KEY, alias TEXT, board TEXT,
            first_seen TEXT, last_seen TEXT)
  flights(id INTEGER PRIMARY KEY, serial TEXT REFERENCES airframes,
          log_sha1 TEXT UNIQUE,      -- 同一日志重分析不重复入库
          log_name TEXT, flight_date TEXT,   -- 取自 GPS GWk/GMS
          duration_s REAL, health_score REAL,
          fw_version TEXT, fw_hash TEXT,
          metrics_json TEXT)         -- 指标明细，见 §3
  ```
- 位置（已定）：**A2 报告目录旁**。其余候选留档：
  - A1 `~/.arduscope/fleet.db`（跨仓库、跨工作目录稳定）
  - A2 报告目录旁 `<report-dir>/fleet.db`（跟着 web 部署走）
  - A3 CLI 参数显式指定，无默认（最不魔法，最多敲字）

## 3. 每架次入库指标（v1 固定集）

| 组 | 指标 |
|----|------|
| 总体 | health_score、每分析器 status 快照 |
| 振动 | 主 IMU VibeX/Y/Z 峰值与 RMS、clip 计数 |
| 电池 | 起飞满电压、最低电压、载荷电压跌落、消耗 mAh |
| 板级供电 | Vcc 中位/解锁期波动、MCU 电压极值（温度通道坏，不入库） |
| 动力 | 悬停输出均值、电机不平衡度 % |
| 姿控 | 旋翼相 RATE R/P/Y RMS（pid_tuning 权威指标） |
| 飞行 | 时长、里程、最大高度（flight_profile 已算好） |

原则：只存"已由分析器算出的数"，档案层不做新判定；趋势展示为
info 级（无阈值判定，POSITIONING 免罪），除非未来找到有出处的退化
判据再升级告警。

## 4. 采集路径（已定：默认开启）

pipeline 分析完成后的 post-hook（`lib/fleet_history.py`）：
- B1 **默认开启**，`--no-fleet-record` 关闭（档案完整性优先）
- B2 默认关闭，`--fleet-record` 显式开启（保守）

web 上传与 batch_report 走同一 hook；失败（db 锁/坏日志）只警告
不影响报告生成。

## 5. 呈现（已定：Phase 1 只做报告节+CLI）

- Phase 1（已实现）：报告尾部新增「本机历史」节——该序列号最近 N=20
  架次的核心指标 sparkline（Plotly，复用现有图表基建）+ 架次表。
  CLI `fleet_report.py <serial|alias>` 输出独立机队页。
- Phase 2（已实现，2026-07-16）：web 机队页——`/fleet` 全部机身列表
  （序列号/别名/板型/架次数/末次飞行/最新评分），`/fleet/<key>` 单机
  趋势图 + 架次表；key 支持序列号/唯一前缀/别名（UTF-8 别名可进 URL）。
- 补充（2026-07-17）：`fleet_report.py --backfill <paths...>` 用历史
  日志建档（只分析+归档不出报告；sha1 去重在分析前判断，重跑秒级）；
  `/fleet/<key>` 页内置别名编辑表单（留空清除）。

## 6. 边界

- 纯本地文件，不上云；db 不进 git。
- 多进程并发写：sqlite WAL 模式 + 重试，web 线程池场景够用。
- 测试：SyntheticReader 不落库（无 serial→unknown 且测试默认关闭
  record）；用 tmp_path db 做单元测试。

## 工作量估算

lib/fleet_history.py + pipeline hook + 报告节 + fleet_report.py +
测试 ≈ 1 个工作日；web 机队页再 +半天。
