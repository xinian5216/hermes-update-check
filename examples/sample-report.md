# Hermes Update Advisor

## 本地安装状态

- **Reported Version**: v0.21.2
- **Channel**: MAIN
- **Branch**: main
- **Commit**: 5eb99eb2
- **Nearest Release**: v2026.9.11
- **Behind Latest**: 125 commits
- **Working Tree**: dirty

## 最新正式版本

- **Release**: v0.21.3 (v2026.9.14)
- **Published**: 2026-09-14T16:04:14Z (15.3 小时前)
- **Commits**: 1039
- **Merged PRs**: 338

## 风险

- **Change Risk**: 86 / 100
- **Regression Signal**: 88 / 100
- **Data Confidence**: 100 / 100
- **Environment Risk**: 67 / 100
- **Overall Risk**: 99/100
- **Risk Level**: VERY HIGH
- **Stability**: 1/100
- **Issue Signal Confidence**: 80/100

## 硬门禁

- **PASS** 观测数据是否充分
- **PASS** 本地环境状态
- **BLOCK** Release 年龄 >= 48h — Release 仅发布 15.3 小时，低于最小观察期 48 小时
- **BLOCK** 禁止在 main 开发分支上执行更新 — 当前安装跟踪 main 开发分支（代码可能领先正式 Release）
- **BLOCK** 工作区必须干净 — 工作区有 4 个未提交修改
- **PASS** 禁止预发布版本
- **BLOCK** 禁止存在活跃的数据库回归 — 数据库 / state.db 存在 2 个报告（2 位报告人，2 个 open），严重程度 CRITICAL，可信度 MEDIUM
- **PASS** 禁止存在活跃的Session/数据丢失回归
- **BLOCK** 禁止存在活跃的升级失败回归 — 升级 / 回滚 存在 2 个报告（2 位报告人，1 个 open），严重程度 HIGH，可信度 MEDIUM
- **WARN** Channel 与 preferred_channel 不一致 — 当前跟踪的是 MAIN，而 preferred_channel 是 stable：建议备份后切换到正式 Release（本工具不会自动降级）

## 回归信号

| cluster | severity | confidence | reports | reporters | open | confirmed |
|---|---|---|---:|---:|---:|---:|
| Gateway | CRITICAL | HIGH | 7 | 6 | 6 | 0 |
| Provider / 模型接入 | CRITICAL | HIGH | 7 | 7 | 6 | 0 |
| 崩溃 / 进程异常 | CRITICAL | MEDIUM | 8 | 8 | 5 | 0 |
| 数据库 / state.db | CRITICAL | MEDIUM | 2 | 2 | 2 | 0 |
| Session / 会话数据 | CRITICAL | LOW | 1 | 1 | 1 | 0 |
| 认证 / 凭证 | HIGH | MEDIUM | 3 | 3 | 3 | 0 |
| 升级 / 回滚 | HIGH | MEDIUM | 2 | 2 | 1 | 0 |
| 配置迁移 | HIGH | LOW | 1 | 1 | 1 | 0 |
| MCP | MEDIUM | LOW | 1 | 1 | 1 | 0 |

## 风险因子明细

| factor | pts | detail |
|---|---:|---|
| 变更内容风险（Release Notes / commit） | +31.3 | 数据库 / 迁移 / schema(14), Session / 状态存储(19), 认证 / 凭证 / Token(22), 大规模重写 / 重构(16), Gateway(27), 配置 / 配置迁移(2), 存储 / 序列化 / 后端(5), Provider / SDK / MCP / 工具系统(16) |
| 发布年龄 | +18.0 | less than 24 h old |
| 改动规模与版本类型 | +16.0 | very large diff；patch release bundling a large number of merged PRs |
| 发布后 Issue 信号 | +19.0 | 基线窗口 Issue 数量过大（10044），速率对比不可靠；以严重问题报告为准；多个独立报告：Gateway 无法启动 / 异常（3 个）；多个独立报告：数据损坏 / Session 丢失（2 个）；单个报告：配置迁移异常；发布后 72 小时内有仍处于 open 的严重问题 |
| 安装方式与本地上下文 | +8.0 | install tracks the main branch；git working tree has uncommitted changes |
| 稳定性加分（负分） | +0.0 | 无 |
| 回归聚类可信度（Regression Signal） | +26.4 | Gateway：CRITICAL × 可信度 HIGH（7 报告 / 6 报告人）；Provider / 模型接入：CRITICAL × 可信度 HIGH（7 报告 / 7 报告人）；崩溃 / 进程异常：CRITICAL × 可信度 MEDIUM（8 报告 / 8 报告人）；数据库 / state.db：CRITICAL × 可信度 MEDIUM（2 报告 / 2 报告人）；… (+7) |

## 建议

**WAIT**

HARD GATE TRIGGERED —— WAIT

- Release 仅发布 15.3 小时，低于最小观察期 48 小时
- 当前安装跟踪 main 开发分支（代码可能领先正式 Release）
- 工作区有 4 个未提交修改
- 数据库 / state.db 存在 2 个报告（2 位报告人，2 个 open），严重程度 CRITICAL，可信度 MEDIUM
- 升级 / 回滚 存在 2 个报告（2 位报告人，1 个 open），严重程度 HIGH，可信度 MEDIUM
- 涉及 数据库 / 迁移 / schema（命中 14 次）
- 涉及 Session / 状态存储（命中 19 次）
- 涉及 认证 / 凭证 / Token（命中 22 次）

建议复查时间：2026-09-15T19:25:02Z（约 12 小时后）

