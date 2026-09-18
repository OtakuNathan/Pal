# LLM Projection Refactor v2 — Baseline Record

**日期：2026-09-18。执行者：Pal。**
**基线 commit：`7e0b1f74cbbedc3214ba362c9b1413ae058aebe4`（= origin/main，已核对一致）。**
**分支：`refactor/llm-projection-session-v2`。Worktree：`~/Documents/coding/Pal-projection-v2`。**
**保护分支：`safety/llm-projection-before-v2-7e0b1f7`（本地）。**
**方案包：`~/Documents/coding/upgrade/pal_projection_plan/`，sha256sum -c MANIFEST.sha256 全部 OK。**

## 环境

- 主机：Raspberry Pi（本机），内存 3.7Gi
- Python 3.13（`~/.local/lib/python3.13`），pytest 9.0.3
- Java OpenJDK 21.0.10；`tla2tools.jar` 已存在于 `~/Documents/coding/Pal/tla2tools.jar` 与 `~/tla2tools.jar`
  → **与方案包作者环境不同：本机可执行 SANY/TLC，P1 gate 无工具障碍**
- ⚠️ Worktree 导入陷阱：系统 editable 安装把 `import pal` 解析到主 checkout
  （`~/Documents/coding/Pal/src`）。**在 worktree 内的一切测试/脚本必须
  `PYTHONPATH=$PWD/src` 前置**，否则静默测到旧代码。已实测验证两种解析路径。

## 方案三项预检分歧的确认结论（HANDOFF.md 要求）

### 1. 既有 durable commit 如何原子关联 IR 与 required-native

现状：**没有按轮的 joint commit**。事实链：

- `MemoryRuntimeStatePort.snapshot_state()`（`src/pal/memory/runtime_state.py:34`）把
  L1 turns 整体序列化；每条消息经 `message_to_payload()` **连同其 ReplayEnvelope（即 v1 native）**一起进 payload；L2 条目、heat、top-of-mind 同快照。
- `prepare_restore_state()` 全量校验后 `install_prepared_state()` 一次性替换——**原子性粒度 = 整个 runtime state 快照**。
- `MemoryService._close_l1_turn_transactionally`（`src/pal/memory/service.py:494`）是**内存内**事务（replace + 失败回滚 `restore_turn`），不产生持久化凭证。

推论：PLAN §4 的 `HistoryCommitReceipt`（semantic/native 同凭证提交）是**新建物**，
不存在可直接复用的按轮原语；P4 应复用的边界是 snapshot/install 这一对全量边界。

### 2. context-view 正常 turn 边界是否有早期内容重写

结论：**正常路径未发现早期历史改写；存在两个有界的 turn 边界投影变化通道**：

- `projected_messages()`（`src/pal/memory/context_view.py:15`）只做过滤不改写内容：
  1. settle 后从**已结算轮**的投影中剔除 pal 自著的 `pal_prompt_context` developer 消息与 summary 投影（`settled=True` 分支，调用点 `turn_executor.py:1122/2091`、`turn_ir.py:487`）——这些块位于轮次尾部，冻结前缀不受影响（`test_prompt_prefix_stability_e2e` 已钉）；
  2. `turn.metadata["prompt_context_state"]["excluded"]` 集合可显式剔除消息——一个**有界重写通道**。
- 对 PLAN §5.3 的回应：宿主确实存在"已发送内容在后续请求中消失"的机制，但作用域限于轮次尾部瞬态块与显式排除集；新设计必须把这两个通道升级为显式 receipt/generation 事件，不得静默走 append。

### 3. 按实际 endpoint 配置核对 provider continuation 约束

现役 11 个 enabled 端点横跨全部三 shape（详见
`continuation_contract_matrix.md`）。关键事实：**活动端点 `deepseek-v4.1-flash`
走 anthropic_messages**，[W2]/[W3] 类 thinking/signature/reasoning_content 契约
直接约束生产主路径。GLM（openai_completion）的续接要求未核对，标 TBD；
openrouter GPT-5.6 三端点与 gpt-6-astra 走 openai_response，[W1] encrypted_content
契约适用（astra 的 cache 路线决策另有 Nathan 2026-09-17 搁置决定，不在本轮回滚）。

## 本机验证记录（只列实际执行过的）

| 检查 | 结果 | 时间 |
|---|---|---|
| 方案包 sha256 校验 | 全 OK | 2026-09-18 |
| 仓库基线（fetch 后 HEAD=origin/main=7e0b1f74，工作树干净） | 一致 | 2026-09-18 |
| 方案包 examples 14 项构造测试 | 14 passed（本地复跑，与包内证据一致） | 2026-09-18 |
| 全量套件 pytest tests/（2823 tests / 145 files） | 见下节 | 2026-09-18 |

### 全量套件结果

**跑法变更（P0 发现）：单进程跑不完全量，改为分文件隔离跑。**事实链：

1. 前台单进程尝试：~30 分钟未完成，人工终止（无进度可见性，流程失误）；
2. 后台单进程（systemd 瞬态服务 + 日志）：前 395 项 81 秒完成，随后进入
   `test_bootstrap_and_repositories.py`（136 tests）慢区，~15s/测试，且 pytest
   RSS 线性增长（219MB → 426MB，可用内存 1.8Gi → 406Mi）；
3. 在危及主机前主动终止，已跑部分 **454 passed / 0 failed**（存
   `/tmp/pal_baseline_partial_450.log`）；
4. 由此推断今晨 CI 崩溃机理：套件慢区长时间占用 + ollama 加载模型叠加 → 内存耗尽硬挂（非单一 OOM kill，日志未及落盘）。

**最终策略**：`/tmp/pal_baseline_chunked.sh` —— 每个测试文件独立 pytest 进程
+ 1500s 单文件超时，summary 落 `/tmp/pal_baseline_chunks/summary.tsv`。
RSS 增长被限制在单文件内，慢/重文件单独暴露。

**分块结果（141 个 test 文件逐一隔离跑完，无超时）：**

| 项 | 数值 |
|---|---|
| rc=0 文件 | 139 / 141 |
| 通过测试数 | 2557（不含 subtests 计数；另有多文件 subtests 全过） |
| 失败 | **1**：`test_package_installation.py::test_conflicting_dependencies_and_repeated_preparation_are_isolated` |
| 跳过 | **1**（模块级）：`test_tool_schema_properties.py`——缺 `hypothesis_jsonschema` 依赖 |
| 超时（rc=124） | 0 |

**唯一失败的定性：预先存在的环境性失败，与本次改造无关。**证据：在主
checkout（~/Documents/coding/Pal，同样 PYTHONPATH=src）单独复跑同样失败；
断言内容为 `sys.path == before_path`，被主机 `/usr/lib/python3/dist-packages/
setuptools/_vendor` 的导入期注入污染（jieba → pkg_resources 链）。修复它
不属于本项目范围，记入基线后不再追。

结论：**基线可动工**——除上述 1 个环境性失败 + 1 个依赖缺失 skip 外，
全部通过。后续 gate 的对照基准即本表。

## 与 PLAN 的差异记录（增量，不改变计划）

1. 本机具备 tla2tools.jar + Java 21 → PLAN §9 "当前验证状态：TLC 尚未执行" 在本机可解除；P1 gate 照常执行 SANY/TLC + mutant。
2. Worktree 需 PYTHONPATH 前置（见环境节）。
3. 全量套件在本机耗时量级如上；后续 gate 建议一律后台 + 日志。

## 约束遵守声明

未修改生产数据库/配置（`~/.pal/pal.sqlite3` 仅只读 SELECT endpoint 元数据，未含密钥）；
未重启线上 Pal；未使用真实密钥；未发起付费调用；未 force push；main 未动。
