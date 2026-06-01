# Pal Change Admission

> 目标：给 Pal 的人类与 AI 贡献者一条进入代码库前的最低审查线，防止大体量、跨边界、不可维护的改动直接落地。

## Principle

Pal 可以使用 AI 快速落地实现，但不能把“能生成很多代码”当成设计质量。

一个改动能进入主线，至少要回答：

- 它改的是哪个 contract？
- 它属于哪个 module owner？
- 它有没有跨过明确的 Does Not Own 边界？
- 它有没有验证当前 runtime 能活着工作？
- 它失败时如何退回或停用？

## Change Classes

### Small Local Fix

适用场景：

- bug fix
- 单模块行为修正
- 文档同步
- 小型测试补充

要求：

- 说明受影响文件
- 有 focused test 或明确说明为何不需要
- 不顺手重构无关代码

### Contract Change

适用场景：

- 新 capability
- 新 provider contract
- database schema or migration
- prompt/tool surface change
- core/runtime lifecycle change

要求：

- 先更新或新增对应 `docs/pal_*_contract.md`
- 说明 Owns / Does Not Own 是否变化
- 加 architecture or integration test
- dogfood 一次真实 runtime 或说明无法 dogfood 的原因

### Large Generated Change

适用场景：

- AI 生成或批量迁移
- 跨多个 subsystem
- 新 runtime manager / provider family
- 大量新增代码

要求：

- 拆成可审的阶段
- 每阶段必须可运行、可测试、可回滚
- 每阶段说明代码生成范围
- 禁止混入无关格式化、重命名、搬目录
- 如果 diff 过大到无法人工审查，应先改成 plan / draft / minion work order，而不是直接合入

## Admission Checklist

提交前至少检查：

- Contract: 改动是否对应现有 contract？如果不是，是否新增了 contract？
- Ownership: 代码是否留在正确 module owner 内？
- Boundary: 是否违反 `Does Not Own`？
- Persistence: 是否改 DB？如果改，是否有迁移或明确允许不兼容？
- Runtime: 是否影响 bootstrap、service loop、provider reload、sidecar lifecycle？
- Tool Surface: 是否新增或改变 LLM-visible capability/tool？名称是否稳定、namespace-first？
- Prompt Surface: 是否改变 prompt context？是否避免把 operational metadata 当作用户内容？
- Channel: 是否把 interaction realization 留给 provider，而不是写进 core/manager？
- Memory: 是否避免把 channel/conversation route 当作 memory identity？
- Tests: 是否有 focused test？跨模块时是否有 architecture/integration test？
- Dogfood: 是否用真实 runtime、socket client、cap-call 或等价路径验证？
- Rollback: 是否可以 disable provider/plugin/endpoint 或 revert 单个 commit？

## AI Contributor Rule

AI 贡献代码时应被当作高级打字员加快速实现器，而不是架构授权来源。

AI 可以：

- 根据既有 contract 实现代码
- 补测试
- 同步 docs
- 提出风险
- 帮助拆阶段

AI 不可以在没有明确 contract 的情况下：

- 发明新的 ownership boundary
- 把平台特化写进 core
- 把工具执行绕过 execution
- 把 memory 当成 routing state
- 批量生成无法审查的大型 diff

## Dogfood Rule

Pal 的 runtime 改动默认需要 dogfood。

优先验证：

- `pal run --runtime-root <runtime>`
- `pal client --runtime-root <runtime> --message "..."`
- `pal cap-call --runtime-root <runtime> --name <capability>`
- provider reload / rescan / attach / detach
- service log 中没有重复 crash 或 event-loop lifecycle error

如果真实 runtime 不能运行，提交说明必须写清：

- 缺少什么 secret/config/service
- 已经跑过哪些替代测试
- 剩余风险在哪里

## Review Shape

评审时先看风险，不先看代码量。

优先顺序：

1. 是否破坏架构边界
2. 是否破坏 runtime lifecycle
3. 是否破坏数据持久化或迁移
4. 是否破坏 LLM-visible tool/capability contract
5. 是否缺少 dogfood 或测试
6. 是否存在无关 churn

代码可以多，但必须能被解释、审查、测试和回滚。
