# Pal Service Contract

> 目标：定义 `service` 子系统的职责、对象模型，以及与 channel 的绑定关系。

## 目标

`service` 负责让 `Pal` 承诺“跨时间兑现”的动作。

它不等于 minions，也不等于 reminder。

它负责：

- 定时任务
- 周期任务
- 计划性回顾
- 持续性 monitoring / recap / briefing

这一版的目标不是重写旧 service 语义，而是：

- 基本保留当前 service 语义
- 去掉多余的 `user_id / user_pal_id`
- 把 service spec 收成更清楚的行动描述
- 把 output channel 正式绑定到 channel 子系统

## Owns

- service definition
- schedule
- service run lifecycle
- due trigger materialization
- output delivery target selection

## Does Not Own

- channel transport implementation
- tool execution runtime
- minions lifecycle
- memory truth
- control constitution

## 核心对象

## Service

`Service` 是一个长期存在的“未来动作承诺”。

它至少要表达三件事：

- 你要做什么
- 你如何做
- 是否要带某个 skill/manual 来做

也就是说，service 的内部 spec 不应只是任意 blob，而应有清晰语义。

### `service_spec`

推荐最小结构：

- `goal`
- `method`
- `skill_refs`

字段语义：

- `goal`
  这次服务想完成什么
- `method`
  推荐如何完成，包括是否偏向 inline 还是 minions-prepared
- `skill_refs`
  可选 skill/manual 引用列表，用于约束或增强执行方法

这意味着 service 真正表达的不是一个死模板，而是一份：

- 目标
- 方法
- 可选 skill context

## ServiceRun

`ServiceRun` 是某次具体触发的执行记录。

它至少应表达：

- `service_run_id`
- `service_id`
- `triggered_at`
- `status`
- `preparation_mode`
- `minions_work_order_id`
- `effective_out_channel_id`
- `output_summary`
- `artifacts`
- `finished_at`

## 与 Channel 的关系

service 的输入 channel 和输出 channel 都不应再只是匿名 blob。

至少输出 channel 必须成为正式 channel 引用。

### Output Channel Rule

每个 service 都必须带一个 `out_channel_id`。

规则如下：

- 默认值：创建 service 时的输入 channel
- 所属关系：`out_channel_id` 必须引用 channel 子系统中的有效 endpoint
- 约束方式：必须有 foreign key

也就是说：

- 如果用户在 Telegram 中创建 service
- 默认输出也回 Telegram
- 除非显式改成别的 channel endpoint

### Foreign Key Rule

`service.out_channel_id` 必须对 channel endpoint 表建立外键约束。

原因是：

- service 的存在意义是未来把结果送到某个 channel
- 如果目标 output channel 已不存在
- 那么这个 service 也失去了存在意义

因此：

- 不允许 service 指向悬空 channel
- output channel 消失时，service 应被归档、删除或标记无效
- 这条约束应由数据库和 service runtime 共同维护

## 推荐 Schema

推荐最小 truth tables 为：

### `pal_services`

- `service_id`
- `title`
- `service_family`
- `schedule_blob`
- `service_spec_blob`
- `in_channel_id`
- `out_channel_id`
- `status`
- `last_run_at`
- `next_run_at_utc`
- `created_at`
- `updated_at`

其中：

- 不再保留 `user_id`
- 不再保留 `user_pal_id`
- `in_channel_id` / `out_channel_id` 都应指向 channel endpoint truth

### `pal_service_runs`

- `service_run_id`
- `service_id`
- `triggered_at`
- `status`
- `preparation_mode`
- `minions_work_order_id`
- `effective_out_channel_id`
- `output_summary`
- `artifacts_blob`
- `created_at`
- `updated_at`
- `finished_at`

其中：

- `effective_out_channel_id` 表示本次运行最终实际使用的输出 channel
- 允许它与 service 默认输出 channel 一致
- 也允许在运行时被合法覆盖

## 与旧版本的对齐

旧 schema 里的这些概念继续保留：

- `service_family`
- `schedule`
- `service_spec`
- `status`
- `last_run_at`
- `next_run_at_utc`
- `ServiceRun`
- `preparation_mode`
- `minions_work_order_id`

新版本主要改变的是：

- 去掉 `user_id / user_pal_id`
- 不再让 channel 关系主要依赖 `*_channel_blob`
- 把输出 channel 变成正式引用
- 把 service spec 收口成更清楚的行动描述

## 执行原则

service 被触发后，可以有两条主路径：

- `pal_inline`
- `minions_preparation`

选择哪条路径由：

- service 的 `method`
- 当前 capability 可见面
- `Pal` 的决策

决定。

但无论如何：

- service 本身只是 future action contract
- 真正副作用仍然经 `Execution`

## Invariants

- `service` 语义总体与旧版本保持对齐。
- `service` 必须表达“做什么 / 如何做 / 是否带 skill”。
- `out_channel_id` 默认等于创建时的输入 channel。
- `out_channel_id` 必须通过 foreign key 指向有效 channel endpoint。
- 输出 channel 不存在时，该 service 不应继续保留为有效服务。
- service run 是正式对象，不是瞬时日志。

## Non-Goals

- 不在本文件定义 scheduler 算法
- 不在本文件定义具体 service family 的 prompt
- 不在本文件定义 minions preparation 的实现细节
