# Pal Memory Contract

## Current Implementation Notes

The current code treats memory as a prompt and routing surface, not as an executor.

Prompt ownership:

- `pal.memory.prompt.MemoryPromptFragmentProvider` always contributes `Memory Guide` when the memory module is registered.
- Dynamic memory context is appended only when a `MemoryPack` is present.
- Memory rules are no longer owned by `pal.behavior`.

Current system prompt rules:

- If memory has been recalled or is present in the prompt, Pal MUST use it as reference before deciding, writing, retrying, debugging, or taking external action.
- If a tool/capability call fails and memory recall is available, Pal MUST call `memory_recall` for relevant experience before debugging, retrying, or asking the user.
- If the user challenges Pal's memory, says Pal already knows/remembers something, or corrects a recalled/stored fact, Pal MUST recall relevant memory before writing, patching, or insisting.
- `memory_query_hints` from behavior advice do not trigger recall by themselves; when recall is required, use them as query seeds.

Current prompt projection:

- Recent L1 context is projected as `Recent Context`.
- Current summary is projected as `<conversation_summary>`.
- Recalled durable facts, preferences, project context, and case knowledge are projected as `<recalled_memories view="summary">`.
- Behavior-owned advisor hints are projected separately by the behavior module as `<advisor_hints>`; they do not live in memory L2.
- The old single `Working Memory` prompt label is no longer the current projection.
- Recalled memories render as `[mem_ref]: text`; `mem_ref` is operational metadata for `memory_update` and `memory_delete`.
- L3 recall render suffixes such as `[L3 summary; origin available]` are not shown in prompt output.

L1 tool-result compaction:

- Old tool protocol messages are cleared by complete turn groups.
- A single tool-heavy turn is not partially cleared inside itself.

Storage boundary:

- Stable fact or preference -> memory commit/correction.
- Future behavior route -> affordance.
- Reusable procedure -> skill candidate.
- Repair lesson or reusable task experience -> propose memory or skill candidate first unless explicitly approved.

> 目标：把 `L1 / L2 / L3`、memory lifecycle 和 memory action contract 一次写死。

## 目标

`Memory` 不是聊天缓存，也不是随手塞数据库的事实堆。

`Memory` 是 `Pal` 的分层记忆系统：

- `L1` 保存压缩后的近端对话
- `L2` 保存当前 runtime 真正需要的 hot context
- `L3` 保存长期可检索的 durable memory

## 三层结构

```mermaid
flowchart LR
    TURN["Raw Turn"] --> L1["L1\ncompressed turns + summaries"]
    L1 -->|compact| L2["L2\nhot working set"]
    L2 -->|retire| L3["L3\nsearchable durable store"]
    L3 -->|recall| L2
```

## L1

### 定义

`L1` 是对话本身，但不是 raw transcript append-only log。

每轮结束后，需要做一次极低温、近无损的语义压缩，将压缩后的 turn transcript 放入 `L1`。

这里的目标不是抽象总结，而是保留尽可能完整的角色顺序与语义内容，同时削去明显噪音和冗余 payload。

### 内容

- compressed turns
- conversation summaries

### 边界

- `L1` 属于 runtime memory
- `L1` 只存在 RAM
- 进程重启后 `L1` 丢失
- `L1` 不属于 Core durable state
- `L1` 不是 summary bucket

### Runtime Shape

`L1` 更接近 recent turn stream / bounded transcript buffer，而不是业务队列。

它的职责是：

- 承接每轮结束后的压缩 turn
- 为后续 `compact` 提供近端上下文源

它不应被建模成：

- task queue
- durable log
- 独立业务对象仓库

## L2

### 定义

`L2` 是 hot runtime working set。

它保存当前推理和工作真正需要的提炼上下文，而不是长期真相源。

`L2` 的真相结构以 entry set 为主，而不是一组 durable buckets。

也就是说：

- 决定 memory 行为的是 entry 内部 metadata
- bucket 主要服务于 runtime 组织和 prompt 投影
- HOT prompt projection 仅包含当前处于 HOT 状态的 memory 条目，并渲染为 `<recalled_memories>`

### 内容

- compacted working facts
- recalled entries (HOT)
- conversation summary
- case candidates

### 边界

- `L2` 属于 runtime memory
- `L2` 只存在 RAM
- 进程重启后 `L2` 丢失
- `L2` 不绑在 durable conversation host 上
- `L2` 的一等结构是 entry set，不是 durable conversation record

### Runtime Shape

`L2` 更接近 hot entry set + lifecycle state machine，而不是队列。

也就是说：

- 它是当前热记忆的集合
- HOT prompt projection 是 HOT memory 条目的 prompt 投影视图；当前实现不再使用一个通用 `Working Memory` 标签
- 它不应被理解成先进先出的业务队列

### Scope 原则

`task-wise memory` 是 scope，不是 bucket。

也就是说：

- 内容分类回答“这是什么”
- scope 回答“这属于当前 task 还是 system”

更进一步说：

- `kind` 只回答 `fact | case`
- `task` 不是第三种 memory kind
- `task_id` 只是把同一种 memory 放到 task scope 下

因此 memory 的最小心智模型是：

- `system-scoped fact`
- `task-scoped fact`
- `system-scoped case`
- `task-scoped case`

### Entry Metadata 原则

决定 `L2` 条目行为的关键信息应放在 entry 内部，而不是塞进 bucket 名字里。

至少包括：

- `kind`
- `scope`
- `task_id`
- `source_kind`
- `candidate_state`
- `memory_id`
- `title`
- `summary`
- `rendered`

其中：

- `source_kind` 决定这条内容来自 `L1 compact`、`L3 recall` 还是显式 `commit`
- `candidate_state` 决定它是否还是候选态
- `retire` 是否回写 `L3` 应主要看 entry metadata，而不是看 bucket 名字

因此：

- `recalled_cases` 不应成为一等 bucket
- `case_candidate` 更适合作为 entry 的状态标记

### `L2Entry` Unified Runtime Shape

虽然 `L3` 的 durable truth model 是异构的，但 `L2` 应保持同构。

也就是说：

- `L3` 可以拆成 `memory_facts` 和 `memory_cases`
- `L2` 不照搬 truth table 形状
- `L2` 保存统一的 runtime projection entry

推荐最小结构：

- `entry_id`
- `kind`
- `scope`
- `task_id`
- `source_kind`
- `source_ref`
- `candidate_state`
- `touched_at`
- `title`
- `summary`
- `rendered`
- `payload`

字段语义：

- `kind = fact | case`
- `scope = system | task`
- `source_kind = l1_compaction | l3_recall | explicit_commit | correction`
- `source_ref` 在 `l3_recall` 时指向 durable truth row
- `candidate_state = candidate | stable`
- `rendered` 是面向 prompt 的投影文本
- `payload` 是 runtime 使用的结构化补充信息

对 `payload` 的解释由 `kind` 决定：

#### `fact` entry payload

- `fact_text`
- optional extra fields

#### `case` entry payload

- `situation`
- `task`
- `action`
- `result`
- optional extra fields

因此：

- `L3` 异构
- `L2` 同构
- `retire` 时再根据 `kind` 决定落到 `memory_facts` 或 `memory_cases`

### L2 Lifecycle Contract: HOT / GHOST / DORMANT

`L2` 条目有明确的生命周期状态，不再是只进不出的 LRU 缓存。

```
DORMANT ──recall/commit──→ HOT       (renewal_count = 0)
HOT      ──hot_ttl到期──→ GHOST       (renewal_count 不变)
GHOST    ──再次recall──→ HOT          (renewal_count += 1)
GHOST    ──ghost_ttl到期──→ DORMANT
HOT      ──再次recall──→ HOT          (renewal_count 不变，只刷新 ttl)
```

**HOT 代表”当前被真正用上的东西”**，不是”刚被写出来的东西”。

状态转换规则：

- DORMANT → HOT: renewal_count = 0，hot_ttl = 5
- HOT refresh（recall 命中已在 HOT 的条目）: renewal_count 不变，只重置 hot_ttl
- HOT → GHOST: renewal_count 不变，ghost_ttl = 3
- GHOST → HOT: renewal_count += 1
- renewal_count >= MAX_RENEWAL_COUNT (3) 时，HOT 过期直接 DORMANT，跳过 GHOST

三条铁规则：

1. TTL 续命封顶 3 次（防永久驻留）
2. GHOST 只存 entry_id + heat_level + ghost_ttl（轻量）
3. “再次召回” = 真正进入 working set，不是搜索命中

TTL 是 turn-based，不是 function-call-based。`tick_heat()` 在 turn 结束时调用，不在 `build_pack()` 里调用。

### HOT Prompt Projection

HOT prompt projection 是 L2 中所有 HOT memory 条目的 prompt 投影。当前实现渲染为 XML-style user-context blocks，而不是一个通用 `Working Memory` block。

核心原则：

- HOT prompt projection 只包含 HOT memory 条目
- 无 HOT 条目时不注入任何 L2 块
- GHOST 条目不渲染
- compact 只更新 L1，不直接触发 HOT

触发 HOT 的动作：

- `commit`（写入后自动进入 HOT）
- `recall` 命中后进入 working set

### L3 Commit 向量去重

写入前先 embed search_text，向量搜索已有记忆（top_k=1）。两阶段确认：

1. **Candidate 阶段**：cosine similarity > 0.85 → 标记为 candidate duplicate
2. **Confirm 阶段**：检查 candidate 的 canonical_key 是否匹配，或 title/topic 是否强重叠
   - 确认重叠 → merge（更新旧条目的 title/summary/search_text）
   - 不确认 → 创建新条目（相似但不同主题）

这避免了误合并——比如两条关于不同编程语言的 fact 可能向量相似但主题不同。

embedding provider 不可用时退化为原有 hash 去重。

## L3

### 定义

`L3` 是外挂、可插拔、searchable durable memory engine。

它不是 prompt 常驻层，而是长期可检索的 document/index system。

### 内容

- durable facts
- durable cases
- searchable memory documents
- related indexes

### 边界

- 只有 `L3` durable
- `L3` 才是长期真相源
- `L3` 不直接整层进入 prompt
- `L3` 通过 recall 刷新 `L2`

## L3 的搜索引擎模型

`L3` 以 search engine 思维管理，而不是“记忆桶”思维。

它至少包括：

- document store
- lexical recall
- semantic recall
- topic lookup
- ranking and fusion

`L3` 是可插拔 provider，因此其实现可以替换，但行为契约必须固定。

## L3 Truth Model

`L3` 的 durable truth model 不再建议把 `fact` 和 `case` 混在同一张大表中。

新的基线是：

- `memory_facts`
- `memory_cases`

拆分原因：

- `fact` 和 `case` 的内容结构天然不同
- `case` 的结构化字段不应和 `payload_blob` 双重存储
- `fact_kind` 在旧设计中过载过多语义
- `preference / core_policy / top_of_mind` 不应继续伪装成 `L3 fact`

这里还要强调一条：

- `task` 不是第三张 truth table
- `task` 只是 `fact` 或 `case` 的 scope
- `tasking` 拥有 task 对象本身
- `memory` 只通过 `task_id` 轻量引用 task scope

### `memory_facts`

`memory_facts` 保存 durable reusable facts。

建议最小字段：

- `fact_id`
- `task_id`
- `title`
- `summary`
- `search_text`
- `canonical_key`
- `dedupe_fingerprint`
- `payload_blob`
- `lifecycle`
- `use_count`
- `last_used_at`
- `created_at`
- `updated_at`

说明：

- `task_id` 为空表示 system scope
- `payload_blob` 只允许承载扩展属性，不应重复主列内容
- `canonical_key` 用于显式 fact identity 和幂等 upsert

### `memory_cases`

`memory_cases` 保存 durable reusable cases。

建议最小字段：

- `case_id`
- `task_id`
- `title`
- `summary`
- `situation_text`
- `task_text`
- `action_text`
- `result_text`
- `search_text`
- `dedupe_fingerprint`
- `payload_blob`
- `lifecycle`
- `use_count`
- `last_used_at`
- `created_at`
- `updated_at`

说明：

- `case` 的核心真相字段是 `situation/task/action/result`
- `payload_blob` 只允许保存非核心扩展信息
- 不允许在 `payload_blob` 里重复存一份 `situation/task/action/result`

## L3 Index Model

`L3` 的 truth tables 分开，但 retrieval/index layer 仍应统一。

推荐至少保留这几层：

- `memory_document_projection`
- `memories_fts`
- `memory_topics`
- `memory_embeddings`

### `memory_document_projection`

`memory_document_projection` 是统一检索投影层。

它不是真相源，而是把 `memory_facts` / `memory_cases` 投影成统一 document shape，供 recall 使用。

建议最小字段：

- `document_id`
- `owner_kind`
- `owner_id`
- `task_id`
- `title`
- `summary`
- `search_text`
- `lifecycle`
- `use_count`
- `last_used_at`
- `created_at`
- `updated_at`

其中：

- `owner_kind = fact | case`
- `owner_id` 指回对应 truth row

如果实现更偏向 view 或 materialized projection，也允许不是物理表，但统一 document projection 这个契约应保留。

### `memories_fts`

`memories_fts` 是 lexical index。

它应该面向统一 document projection，而不是直接耦合某一张 truth table。

### `memory_topics`

旧的 `tags` / `memory_tags` / `topic_tags_blob` 统一收敛为：

- `memory_topics`

建议最小字段：

- `document_id`
- `topic`
- `normalized_topic`
- `created_at`

语义：

- 它不是抽象 tag system
- 它是 `topic -> memory document` 的反向索引

### `memory_embeddings`

`memory_embeddings` 是 detachable derived index，不是真相源。

建议最小字段：

- `embedding_id`
- `document_id`
- `embedding_kind`
- `model_name`
- `model_revision`
- `source_text_hash`
- `embedding_blob`
- `embedding_norm`
- `created_at`
- `updated_at`

约束：

- embedding model 变更时，不做“自动同步”
- 正确路径是 reindex / migration script
- recall 必须允许在 embedding 不可用时退化到 FTS + topics

## Proposed Table Shapes

下面这版是当前推荐直接落到 markdown 里的 memory schema baseline。

先强调两条：

- `L1` 和 `L2` 是 runtime structures，不是 durable tables
- 需要正式落表的主要是 `L3` 和它的 index layer

以下表结构定义在本版本中视为推荐基线，而不是随意举例。

### `memory_facts`

| Column | Type | Required | Meaning |
| --- | --- | --- | --- |
| `fact_id` | `TEXT` | yes | fact 主键 |
| `task_id` | `TEXT` | no | 为空表示 system scope；不为空表示 task scope |
| `title` | `TEXT` | yes | 面向检索和展示的短标题 |
| `summary` | `TEXT` | yes | 主摘要 |
| `search_text` | `TEXT` | yes | lexical search 的归一化检索文本 |
| `canonical_key` | `TEXT` | no | 显式 fact identity，用于幂等 upsert |
| `dedupe_fingerprint` | `TEXT` | no | 退火/归档时的去重指纹 |
| `payload_blob` | `JSON TEXT` | no | 扩展属性；不得重复主列信息 |
| `lifecycle` | `TEXT` | yes | `active | archived` |
| `use_count` | `INTEGER` | yes | 被 recall / 使用的次数 |
| `last_used_at` | `TEXT` | no | 最近一次被使用时间 |
| `created_at` | `TEXT` | yes | 创建时间 |
| `updated_at` | `TEXT` | yes | 更新时间 |

推荐约束：

- `PRIMARY KEY (fact_id)`
- `FOREIGN KEY (task_id) REFERENCES tasks(task_id)`，如果 tasking schema 保持外键
- `CHECK (lifecycle IN ('active', 'archived'))`

推荐索引：

- `(task_id, updated_at DESC)`
- `(canonical_key)`，带 `WHERE canonical_key IS NOT NULL`
- `(dedupe_fingerprint)`，带 `WHERE dedupe_fingerprint IS NOT NULL`

### `memory_cases`

| Column | Type | Required | Meaning |
| --- | --- | --- | --- |
| `case_id` | `TEXT` | yes | case 主键 |
| `task_id` | `TEXT` | no | 为空表示 system scope；不为空表示 task scope |
| `title` | `TEXT` | yes | case 标题 |
| `summary` | `TEXT` | yes | case 摘要 |
| `situation_text` | `TEXT` | yes | 情境 |
| `task_text` | `TEXT` | yes | 任务 |
| `action_text` | `TEXT` | yes | 动作 |
| `result_text` | `TEXT` | yes | 结果 |
| `search_text` | `TEXT` | yes | 聚合后的检索文本 |
| `dedupe_fingerprint` | `TEXT` | no | case 去重指纹 |
| `payload_blob` | `JSON TEXT` | no | 非核心扩展属性；不得重复 STAR 主字段 |
| `lifecycle` | `TEXT` | yes | `active | archived` |
| `use_count` | `INTEGER` | yes | 被 recall / 使用的次数 |
| `last_used_at` | `TEXT` | no | 最近一次被使用时间 |
| `created_at` | `TEXT` | yes | 创建时间 |
| `updated_at` | `TEXT` | yes | 更新时间 |

推荐约束：

- `PRIMARY KEY (case_id)`
- `FOREIGN KEY (task_id) REFERENCES tasks(task_id)`，如果 tasking schema 保持外键
- `CHECK (lifecycle IN ('active', 'archived'))`

推荐索引：

- `(task_id, updated_at DESC)`
- `(dedupe_fingerprint)`，带 `WHERE dedupe_fingerprint IS NOT NULL`

### `memory_document_projection`

`memory_document_projection` 更推荐作为 `VIEW` 或 materialized projection，而不是新的 truth table。

| Column | Type | Required | Meaning |
| --- | --- | --- | --- |
| `document_id` | `TEXT` | yes | 统一 document 主键 |
| `owner_kind` | `TEXT` | yes | `fact | case` |
| `owner_id` | `TEXT` | yes | 指向 `fact_id` 或 `case_id` |
| `task_id` | `TEXT` | no | scope 维度 |
| `title` | `TEXT` | yes | document 标题 |
| `summary` | `TEXT` | yes | document 摘要 |
| `search_text` | `TEXT` | yes | document 检索文本 |
| `lifecycle` | `TEXT` | yes | 当前生命周期 |
| `use_count` | `INTEGER` | yes | 用于 ranking |
| `last_used_at` | `TEXT` | no | 最近使用时间 |
| `created_at` | `TEXT` | yes | 创建时间 |
| `updated_at` | `TEXT` | yes | 更新时间 |

说明：

- fact 和 case 都先投影成统一 document，再接 FTS / topics / embeddings
- recall 排序主要面对 document，而不是直接面对 truth row

### `memories_fts`

`memories_fts` 是挂在 `memory_document_projection` 上的 lexical index。

建议索引字段：

| Column | Type | Meaning |
| --- | --- | --- |
| `title` | FTS | 高权重标题 |
| `summary` | FTS | 中高权重摘要 |
| `search_text` | FTS | 主检索文本 |

如果 case 检索效果需要更强，也可以在 projection 层提前把：

- `situation_text`
- `task_text`
- `action_text`
- `result_text`

拼入 `search_text`，而不是再让 FTS 表额外持有一套 case 专属列。

### `memory_topics`

| Column | Type | Required | Meaning |
| --- | --- | --- | --- |
| `document_id` | `TEXT` | yes | 指向统一 document |
| `topic` | `TEXT` | yes | 原始话题文本 |
| `normalized_topic` | `TEXT` | yes | 归一化话题文本 |
| `created_at` | `TEXT` | yes | 创建时间 |

推荐约束：

- `PRIMARY KEY (document_id, normalized_topic)`

推荐索引：

- `(normalized_topic, document_id)`

### `memory_embeddings`

| Column | Type | Required | Meaning |
| --- | --- | --- | --- |
| `embedding_id` | `TEXT` | yes | embedding 记录主键 |
| `document_id` | `TEXT` | yes | 指向统一 document |
| `embedding_kind` | `TEXT` | yes | 如 `primary | context | resolution` |
| `model_name` | `TEXT` | yes | embedding 模型名 |
| `model_revision` | `TEXT` | no | 模型版本或 revision |
| `source_text_hash` | `TEXT` | yes | 当前 embedding 对应的源文本 hash |
| `embedding_blob` | `JSON/BLOB` | yes | 向量本体 |
| `embedding_norm` | `REAL` | no | 向量 norm |
| `created_at` | `TEXT` | yes | 创建时间 |
| `updated_at` | `TEXT` | yes | 更新时间 |

推荐约束：

- `PRIMARY KEY (embedding_id)`
- `UNIQUE (document_id, embedding_kind, model_name, model_revision)`

推荐索引：

- `(document_id)`
- `(embedding_kind, model_name, model_revision)`

## Tables We Intentionally Do Not Keep

下面这些不再作为新基线表保留：

- `tags`
- `memory_tags`
- `pal_memories`

下面这些不再作为新基线字段保留：

- `user_pal_id`
- `fact_kind`
- `prompt_pin`
- `topic_tags_blob`

下面这些默认不进入最小基线，只在确认需要时再加回：

- `confirmed_count`
- `superseded_by`

## L3 Single-Pal Assumption

由于新架构中 `Pal` 是单主体、单用户、单实例治理系统，`L3` truth tables 不再需要 `user_pal_id` 这类 multitenancy partition key。

也就是说：

- `task_id` 仍然保留，用于表达 task scope
- `user_pal_id` 从 `L3` truth schema 中移除

## Memory And Tasking Boundary

`memory` 和 `tasking` 必然有关，但不应结构耦合。

边界应固定为：

- `tasking` 拥有 task / work order / checkpoint / ledger
- `memory` 拥有 `fact` / `case`
- `memory` 只通过 `task_id` 表达 scope

因此：

- `task` 不是 memory kind
- `memory` 不拥有 task 对象
- `tasking` 也不拥有 durable memory truth

## L3 Redundancy Cleanup

相较于当前实现，新的 `L3` 基线应去掉或迁出这些冗余语义：

- 通用 `tags`
- `memory_tags`
- `topic_tags_blob`
- `prompt_pin`
- `fact_kind`
- `user_pal_id`

对于下面这些字段，默认不进入最小基线：

- `confirmed_count`
- `superseded_by`

只有在后续确认确实需要“正式确认机制”或“immutable revision chain”时，再作为扩展能力加回。

## Memory Actions

## compact

`compact` 表示：

- 从 `L1` 提炼到 `L2`
- 去掉冗余上下文
- 保留当前工作真正需要的 distilled content

`compact` 不是 durable write。

当前实现区分两类结构化 compact：

- `pal.compaction.pal.v2`：本体会话连续性。按 committed turn 分层保留近端原文、轻压缩中端上下文、退役远端上下文；内部 payload 是结构化 dict，但 prompt 渲染为 XML 包裹 Markdown。允许提出 `memory_candidates`，但自动和手动 compact 的候选都必须 approval 后才可进入 L3。
- `pal.compaction.minion.v1`：minion 任务恢复参考包。minion compact 不使用 LLM 摘要 prompt；它是机械 compact：只收集当前 active turn 之前已经提交的 user/task inputs，并把它们渲染为 already-handled/superseded history。work order、milestone、checkpoint、repair、todo/checklist、cursor 等事实仍以 minion 账本/仓库为 source of truth，不重复写进 compact。不生成 `memory_candidates`。

## retire

`retire` 表示：

- 从 `L2` 退火到 `L3`
- 将已经足够稳定、值得长期保留的内容 durable 化

`retire` 是 `L2 -> L3` 的路径。

## recall

`recall` 表示：

- 从 `L3` 检索候选
- 经过搜索引擎策略排序
- 将结果刷新到 `L2`

`recall` 是 `L3 -> L2` 的路径。

`recall` 带 `level`。

允许值固定为：

- `seed`
- `warm`
- `deep`

### Recall 触发策略

不是所有问题都应该触发 recall。以下规则约束模型何时应主动 recall，何时直接回答。

应该触发 recall：

- 关于用户本人的事实、偏好、历史
- 关于 Pal 自身的来源、设定、过去事件
- 用户显式引用过去（之前/上次/记得/回忆/以前/那次）
- 影响后续行为的承诺或约定

不应触发 recall：

- 不依赖用户或 Pal 特定存储事实的通用知识问题
- 不涉及特定过去事件或存储事实的闲聊

模糊时偏向 recall——当答案可能依赖存储的个人或系统事实时，优先 recall。

### Recall Promotion 阈值

recall 返回的所有 hit 都会投影到 L2，但只有 final score >= `RECALL_PROMOTION_THRESHOLD`（当前 0.3）的条目才进入 HOT 状态。低于阈值的条目存入 L2 但不进 HOT prompt projection，避免低相关性噪音污染 prompt。

## commit

`commit` 表示：

- 用户显式要求 `Pal` 记住某件事
- 这是一条显式写入路径
- 它不是 compact，也不是被动 retire

语义约束：

- `commit` 必然影响当前 runtime 的 memory view
- `commit` 的 durable 目标是 `L3`
- `commit` 不带 `level`
- `commit` 成功后立即进入 L2 HOT 状态
- `commit` 写入前先做向量去重检查，相似度 > 0.85 且 canonical_key/title/topic 重叠时 merge 已有条目

## correct

`correct` 表示：

- 用户或系统对已有 memory 做修正
- 这是对已有 memory truth 的显式更正

## Memory Service Contract

`MemoryService` 至少暴露：

- `prepare_turn_context`
- `record_turn`
- `compact`
- `recall`
- `commit`
- `correct`
- `retire`

## L3 Provider Contract

`L3Provider` 至少提供：

- read
- search
- commit
- correct
- archive
- health
- introspection surface

## Page Fault 废弃

`page_fault` 机制在新架构中不再保留。

原因是：

- 它依赖“模型静默决定该回忆什么”的旧哲学
- 缺少用户可理解的治理边界
- 已被 `L1/L2/L3` 分级 memory contract 取代

新架构中：

- 浅层上下文依赖 `L1/L2`
- 深度 recall 通过 `recall(level=...)`
- memory 控制由 `commit / correct / recall / retire / compact` 明确定义

## Prompt Projection Rule

LLM 不应逐字段消费 `L2` 内部 schema。

运行时应把 `L2` entry 投影成更适合 prompt 的 memory pack，再交给模型。

默认推荐顺序：

1. `Recent Context`
2. `<conversation_summary>`
3. `<recalled_memories view="summary">`
4. Behavior-owned `<advisor_hints>` may appear as a separate runtime-reminder block, but it is not an L2 memory projection.

这意味着：

- recalled memories 只包含当前真正被用上的记忆
- 无 HOT 条目时不注入任何 L2 块
- `source_kind / candidate_state / scope` 等字段主要服务于 runtime 逻辑，而不是逐字段暴露给 LLM

## Invariants

- `L1/L2` 只存在 RAM。
- `L3` 是唯一 durable memory layer。
- `L1` 是近无损压缩 transcript，不是 summary。
- `L2` 的真相结构以 entry metadata + lifecycle state 为主。
- `L1 -> L2` 叫 compact。
- `L2 -> L3` 叫 retire。
- `L3 -> L2` 叫 recall。
- `commit` 是显式写入，不带 `level`。
- HOT prompt projection 仅包含 HOT memory 条目，不是 durable bucket。
- L2 条目有 HOT / GHOST / DORMANT 生命周期，TTL 续命封顶 3 次。
- `task-wise memory` 是 scope，不是 bucket。
- `page_fault` 正式废弃。
- `commit` 写入前做向量去重，避免重复记忆。
- recall 只在涉及用户/Pal 特定事实、过去事件引用、行为承诺时触发，不用于通用知识问题。
- recall 结果中 final score < 0.3 的条目不进入 HOT，避免低相关性噪音污染 prompt。

## Non-Goals

- 不在本文件定义 L3 的具体数据库 schema
- 不在本文件定义具体 embedding 模型
- 不在本文件定义 UI 如何展示 memory
- 不把 `profile / preference / identity contract` 混入 `L3` 作为默认规则
