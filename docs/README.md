# Pal Architecture V1

这是 `Pal` 的新架构文档目录。

旧 `design/` 目录中的版本不再继续收口，这里是后续重构实现的唯一新基线。

但旧文档里的稳定语义并没有被丢掉。

当前新文档已经直接吸收并重写了旧稿中值得保留的主线，包括：

- `process_model.md` 中的 `supervisor -> pal -> minions` 进程语义
- `ipc_model.md` 中的 `supervisor-pal` 控制面与 `pal-minions` 执行面的分离
- `provider_and_channel.md` 中的 canonical provider shape 与 `Pal`-owned channel 边界
- `skill_system_v1.md` 中的 `skill = manual`、`tool = 唯一执行原语`
- `runtime_invariants.md` 中已经稳定下来的 runtime 边界
- `pal_v1.md` 中已证明有效的近无损 `L1` transcript、discovery-first execution、native tool calling 边界

因此，旧 `design/` 现在主要作为历史参考和细节追溯来源，而不是继续演化的主文档。

## 阅读顺序

1. [pal_architecture_v1.md](/Users/nathan/Desktop/coding/Pal/design/architecture_v1/pal_architecture_v1.md)
2. [pal_runtime_stack.md](/Users/nathan/Desktop/coding/Pal/design/architecture_v1/pal_runtime_stack.md)
3. [pal_bootstrap_and_process.md](/Users/nathan/Desktop/coding/Pal/design/architecture_v1/pal_bootstrap_and_process.md)
4. [pal_channel_contract.md](/Users/nathan/Desktop/coding/Pal/design/architecture_v1/pal_channel_contract.md)
5. [pal_llm_contract.md](/Users/nathan/Desktop/coding/Pal/design/architecture_v1/pal_llm_contract.md)
6. [pal_execution_contract.md](/Users/nathan/Desktop/coding/Pal/design/architecture_v1/pal_execution_contract.md)
7. [pal_control_plane.md](/Users/nathan/Desktop/coding/Pal/design/architecture_v1/pal_control_plane.md)
8. [pal_introspection_contract.md](/Users/nathan/Desktop/coding/Pal/design/architecture_v1/pal_introspection_contract.md)
9. [pal_tasking_contract.md](/Users/nathan/Desktop/coding/Pal/design/architecture_v1/pal_tasking_contract.md)
10. [pal_service_contract.md](/Users/nathan/Desktop/coding/Pal/design/architecture_v1/pal_service_contract.md)
11. [pal_memory_contract.md](/Users/nathan/Desktop/coding/Pal/design/architecture_v1/pal_memory_contract.md)
12. [pal_failure_reporting_contract.md](/Users/nathan/Desktop/coding/Pal/design/architecture_v1/pal_failure_reporting_contract.md)
13. [pal_migration_map.md](/Users/nathan/Desktop/coding/Pal/design/architecture_v1/pal_migration_map.md)

## 文档定位

- `pal_architecture_v1.md`
  总纲和全局不变量
- `pal_runtime_stack.md`
  模块骨架、owning boundary、公开接口
- `pal_bootstrap_and_process.md`
  wizard、supervisor、Pal、minions 的启动关系与初始化流程
- `pal_channel_contract.md`
  channel I/O、normalize、reply routing、IM acknowledgement UX
- `pal_llm_contract.md`
  canonical shape、LiteLLM transport、streaming、模型路由注册表
- `pal_execution_contract.md`
  capability / tool / plugin / execution 宪法
- `pal_control_plane.md`
  显式控制与审批治理
- `pal_introspection_contract.md`
  自观察、自诊断、自维护、自扩展元能力层
- `pal_tasking_contract.md`
  tasking、minions、checkpoint、ledger、workspace 治理
- `pal_service_contract.md`
  service、schedule、service run、output channel 约束
- `pal_memory_contract.md`
  `L1/L2/L3` 和记忆生命周期
- `pal_failure_reporting_contract.md`
  自修失败后的开发者升级报告契约
- `pal_migration_map.md`
  当前代码迁移归类
