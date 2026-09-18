# LLM Projection Refactor v2 — 交付报告

**分支：`refactor/llm-projection-session-v2`（worktree `~/Documents/coding/Pal-projection-v2`）。**
**基线：`7e0b1f74`。安全分支：`safety/llm-projection-before-v2-7e0b1f7`（本地）。**
**日期：2026-09-18。执行者：Pal。main / 线上 Pal / 生产配置全程未动。**

## 分支提交

| commit | 内容 |
|---|---|
| 706b1df | P0 基线：环境、三项预检确认、分文件套件基线、续接契约矩阵 |
| d55799d | P1 形式化：SANY/TLC 首次真实运行；修复方案包模型三处缺陷 |
| 90b48ad | P1 类型：projection_contracts（构造期不变式） |
| 978f95d | P2：结构化续接契约 + hook 前 native 捕获 |
| c0cbeca | P3：会话级 projection owner（receipt 驱动 chunk 冻结） |
| fa6d123 | P4：checkpoint 原子编解码 + invoker 捕获接线 |
| 3ef4c9f | P5：多 role 隔离与 worker 重启续接（receipt 台账跨重启） |
| (P6) | 收口：file_read 死代码清理、前缀摊还缓存、微基准、本报告 |

## 已实现（代码在分支上）

1. **类型层** `llm/projection_contracts.py`：identity/binding/cursor/receipt/round/
   PreparedRequest 全链构造期校验，§4.2 禁止组合不可表示。
2. **续接契约** `llm/continuation_policy.py`：三 shape 结构化验证（signature/
   encrypted_content 缺失 → Degraded；未知块/条目 → 显式 issue；坏 payload →
   Unsupported）。不按厂商名猜必需字段。
3. **native 捕获** `llm/native_capture.py`：codec 与 response hook 之间的直通
   捕获器；DeepSeek hook 剥信封的丢失点已被测试钉死并绕过。
4. **会话 owner** `llm/projection_session.py`：单写者、receipt 驱动 frontier、
   chunk 冻结（含 anthropic 尾部 user 修剪——源码验证的合并行为使 user 结尾
   不是前缀稳定边界）、幂等/冲突/迟到 receipt、切换销毁旧 lineage、
   required-native 缺失显式 ContinuationUnavailable。
5. **checkpoint** `llm/projection_checkpoint.py`：scope/binding/generation/
   frontier/native/receipt 台账同快照；拒绝缝合错代；legacy 快照走新生代。
6. **invoker 接线** `endpoint.py`：decode 经 NativeCapture（语义流不变），
   `LLMAttemptResult.native_payload_json` 附带捕获产物（新增可选字段）。

## 已运行验证（真实执行的命令与结果）

- **TLC**：single 8340/3818、isolation 92017/26244 全过（distinct 状态数与包内
  Python 模型完全一致）；mutant 以 `DraftAligned is violated` 失败（符合设计）。
  日志 `tlc/`，jar sha256 `936a2620…`，Java 21.0.10。模型修复三处（嵌套量词 ×2、
  absent 字段判别）有源码级注释。
- **定向测试**（每次改动的爆炸半径）：P1 类型 17、P2 捕获 9、P3 会话 10、
  P4 checkpoint/接线 7、P5 隔离 5，全绿；llm 全族回归 190 passed + 60 subtests
  + 6 skips（real-integration 按设计跳过，无付费调用）。
- **微基准**（本机 Pi，OPENAI_COMPLETION，800 消息 400 轮）：构建 3.82s；
  稳态增量 prepare 17.4ms（尾部 1 条）；全量 encode 14,655ms；**840 倍**；
  切换后一次性全量重建 14,775ms。残留 O(n) 为 payload 序列化（json.dumps +
  thaw_json），在 PLAN §5.4 声明的包络内（"不承诺端到端 O(tail)"）。
- **全量套件（P6 gate）**：分文件隔离跑，结果见下表。

### 全量回归（branch @ P6，分文件隔离）

| 项 | 基线（P0） | 分支（P6） |
|---|---|---|
| 文件 | 141 | 146（+5 个新测试文件） |
| rc=0 | 139 | 144 |
| 通过测试 | 2557 | **2605**（+48） |
| 非零 | 2（已知环境项） | 同 2 项，完全同款 |

非零项与基线一致：`test_package_installation` 1 failed（主 checkout 复现过的
setuptools/_vendor 环境性失败）与 `test_tool_schema_properties` 模块级 skip
（缺 hypothesis_jsonschema）。**零回归，全部新增测试通过。**

## 静态推导（有测试支撑的类型/构造保证，未做端到端运行验证）

- resident 与 Bunshin role 会话共用同一实现，隔离由 per-instance 状态 +
  scope 身份保证（S20/S13/S21/S22 会话层测试覆盖；未接 Manager proxy 全链路）。
- 断电/崩溃半写的拒绝路径（frontier 超前 L1、截断 native、错 schema）由
  构造与 checkpoint 测试覆盖；真实进程级崩溃注入未做。

## 未覆盖 / 明确未做

1. **热路径切换未执行**：resident LLMRuntime 仍走 per-message ReplayEnvelope
   编码（原路径未动，全部新组件旁路就绪）。真正切换 = P7 级变更，需你手动
   restart + canary 批准。这是有意的安全边界，不是遗漏。
2. **矩阵 TBD 未销账**：GLM 系（openai_completion）与 DeepSeek 网关 signature
   下发行为仍是 synthetic-opaque 级验证；provider-real fixture 需你授权的脱敏
   日志或 P7 canary。
3. **compact / RewriteReceipt**：context-view 的 excluded 通道升级为显式 receipt
   已在 baseline.md 记录为设计要求，本轮未实现（PLAN §5.3 审计结论：正常路径
   无早期重写，通道有界）。
4. **性能项**：未做 10/100/1000/5000 全梯度矩阵与 RSS 剖析（单一规模实测代替）；
   未引入零拷贝/手工 JSON（PLAN §11 明说最小可行版本不做）。

## 迁移与回退

- 旧快照无 projection 段 → 新生代 lineage，ReplayEnvelope 消息保持只读兼容。
- 回退 = 切回 main（`safety/llm-projection-before-v2-7e0b1f7` 为本地备份指针）；
  新 schema 只在分支上写，main 的生产数据未被任何分支代码触碰。
- 不存在旧代码读新 schema 的路径（分支未部署）。

## 停工条件核查（PLAN §13）

七条逐一核对：无必需 native 丢失后赌 API 接受（Degraded → 显式失败）；
无以关 thinking/换模型掩盖协议失败；main/线上 DB 未动、可回退；无共享
mutable projection；无未跑先报通过（TLC/测试均有日志）；无放宽断言；
frontier 推进全部走 receipt 验证。
