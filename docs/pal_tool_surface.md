# Pal Tool Surface Configuration

> 目标：定义哪些 capability 暴露给 LLM 作为 function-calling 工具，以及如何配置。

## 目标

Pal 注册了 100+ 个 capability，但 LLM 的 function-calling 窗口有限。不需要把所有能力都塞进 prompt。

Tool Surface 负责从全部 capability 中筛选出"常用工具集"暴露给 LLM。其余能力通过 `op_exec_disc_search` 按需发现，通过 `op_exec_run` 执行。

## 设计原则

### Discovery-First

LLM 不需要一次看到所有工具。它只需要：

1. 常用工具直接可用
2. 发现机制按需查找更多
3. 执行机制调用任意已注册能力

### 数据驱动

工具暴露列表不硬编码在 Python 里。而是通过 `src/pal/core/tool_surface.toml` 配置文件管理。

好处：

- 加减工具只改 TOML，不改代码
- 配置与逻辑分离
- 易于审计和版本控制

## 配置文件格式

`src/pal/core/tool_surface.toml`

### `[singletons]` — 静态能力

```toml
[singletons]
capabilities = [
    "op_exec_disc_read",
    "op_exec_disc_search",
    "op_exec_run",
    "intro_module_llm_active",
    "op_web_search_query",
    "op_web_fetch_read",
    # ...
]
```

这些 capability 的 `target_id` 为 `SINGLETON_TARGET`，始终暴露。

### `[[dynamic]]` — 动态能力

```toml
[[dynamic]]
canonical_path = "op_l3_recall_query"
provider_setting = "memory"

[[dynamic]]
canonical_path = "intro_provider_web_search_health"
provider_setting = "active_web_search_provider_id"
```

这些 capability 跟随"当前活跃 provider"动态解析。

`provider_setting` 的取值：

- `"memory"` — 从 `MemoryService.l3_selector.active_provider_id` 读取
- 其他字符串 — 从 `RuntimeSettingRepository` 按 key 读取

运行时解析流程：

1. 读取 setting 获取 active provider ID
2. 在 `compiled_capability_index` 中查找该 `canonical_path + target_id == active_provider_id` 的 descriptor
3. 匹配到则暴露给 LLM

## 当前暴露的工具分类

### 核心（始终暴露）

| 工具 | 作用 |
|------|------|
| `op_exec_disc_read` | 查看能力目录 |
| `op_exec_disc_search` | 搜索能力 |
| `op_exec_run` | 执行任意能力 |

### 自省（模块级）

所有 `intro_module_*` 能力。覆盖 channel、control、core、execution、failure、identity、llm、memory、plugins、service、web_search、web_fetch。

### 操作（模块级）

常用管理操作：channel 管理、control 生命周期、LLM 切换、memory provider 切换、plugin 管理、service CRUD、web 搜索/抓取。

### 动态（跟随 provider）

- L3 memory 操作：recall、commit、correct
- web_search provider health
- web_fetch provider health

## 代码入口

`src/pal/core/tool_surface.py` 中的 `ToolSurface` 类：

- `__init__` 读取 `tool_surface.toml`
- `select_llm_descriptors()` 根据 TOML 配置筛选 descriptor
- `build_llm_tool_contracts()` 将 descriptor 转为 LLM function-calling 格式

## 修改工具列表

1. 编辑 `src/pal/core/tool_surface.toml`
2. 在 `[singletons]` 中加减 canonical_path
3. 或在 `[[dynamic]]` 中添加跟随 provider 的能力
4. 重启 Pal 生效

## Invariants

- `discovery_search` 和 `exec_run` 必须始终暴露，保证 LLM 能发现和执行任意能力。
- 单例能力列表从 TOML 文件读取，不在 Python 中硬编码。
- 动态能力必须在运行时解析 active provider，不能静态配置 target_id。
- 未暴露的能力仍然在 capability index 中，可通过 discovery 找到。
