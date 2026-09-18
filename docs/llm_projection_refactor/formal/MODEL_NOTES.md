# 模型说明与验证边界

## 文件

- `EndpointProjection.tla`：abstract safety specification。
- `single.cfg`：1 scope、2个endpoint、2个tool call ID、最多2次attempt/2个历史block。
- `isolation.cfg`：2 scopes、2个endpoint、1个tool call ID、每scope最多1次attempt/1个block。
- `stale_replay_mutant.cfg`：故意在Repair中只改semantic calls而不改native inventory；预期违反DraftAligned。
- `explore_model.py`：独立Python有限状态实现；不是TLC，不验证TLA语法。

## 当前已经执行

Python 3 单scope模型穷举：3,818个状态、8,339条transition，无列出的不变量失败。

Python 3 双scope模型穷举：26,244个状态、92,016条transition，无列出的不变量失败。

故意引入stale-native修复缺陷后，Python模型在42个已发现状态内找到反例：

```text
Init
Begin(0, ('a',))
Deliver((0, 0, 0, 'A', 1))
Repair(0)
=> DraftAligned violated
```

这证明本包的抽象断言能拒绝该缺陷，不证明现有/未来Pal产品实现正确，也不证明TLA与Python语义完全一致。

## 还没有执行

SANY/TLC未运行。本环境的GitHub下载不可用，不能取得`tla2tools.jar`。执行者必须把真正的SANY/TLC运行作为P1 gate。不要将这些Python通过数引用成TLC状态数。

在有Java和可信`tla2tools.jar`的机器上：

```bash
export TLA2TOOLS_JAR=/absolute/path/to/tla2tools.jar
./run_models.sh
```

脚本先解析，再跑两个正例配置，再验证mutant确实以DraftAligned反例失败。使用哪个工具版本、jar hash和Java版本都应写进发布证据。脚本不自动联网，不替用户安装工具。

## 抽象与实现映射

| 模型 | 实现义务 |
|---|---|
| slots[s] | 每logical session独占owner，不能按endpoint合并 |
| generation | 投影谱系换代；实现需单独保留history_epoch与projection_generation |
| fence | 当前worker/writer授权；迟到消息检查，不使旧合法native失效 |
| history + native 原子Commit | 同一durable acceptance/checkpoint generation；不是跨库“最终会同步” |
| cache是Project前缀 | 只消费可信append receipt；同长度也不能盲信 |
| Repair更新nativeCalls | ProviderPolicy接受一个合法的修复结果；不允许重写签名/加密字节 |
| StartTool/FinishTool | 已有执行账本；未得到结果的已开始mutation不可被prune |
| Deliver身份检查 | request/session/owner/binding关联；错误响应usage仍需独立结算 |
| Switch丢native并重建 | 仅在target profile支持semantic import时可走；否则显式失败 |
| Compact清空模型历史 | 抽象fresh compacted base；实际summary、retained history与source CAS另测 |

模型Begin预选该次输出的tool集合，用一个Deliver事件抽象完整provider回复。实际SSE分帧、ITEM_COMMITTED提前执行、provider中断和output recovery必须另外做组合测试。

## 未证明的性质

- 服务端prompt cache是否命中、TTL、计费、路由、tokenizer或隐藏模板。
- HTTP JSON/raw response完全等同；签名、encrypted content的有效性。
- open-tool hard crash后的外部副作用恢复。Restart仅在closed safe point。
- live切换时配置对provider原生推理状态的影响。
- 真实持久化的原子性、身份认证、进程死亡、权限隔离。
- liveness：无公平性假设，不承诺provider/tool永远返回；`CHECK_DEADLOCK FALSE`是有意的安全检查配置，不代表忽略产品死锁。
- Python实现与TLA实现的机械等价性。

下一步可以根据实现细化open-crash/reconcile模型，但不为未知远端缓存增加虚构状态机。
