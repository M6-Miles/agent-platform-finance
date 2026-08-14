# Workflow —— 工作流定义层

本目录存放**机器可读的工作流定义**。它不是文档，而是可被程序加载、校验、并与真实代码做一致性比对的结构化声明。

设计动机很直接：LangGraph 的图结构写在 Python 里，随着迭代很容易和文档说明脱节。把图结构显式声明成 JSON，再用测试把声明和真实代码钉在一起，「文档漂移」就会变成一次测试失败，而不是上线后才发现的认知偏差。

## 文件说明

| 文件 | 用途 |
| --- | --- |
| `workflow.schema.json` | JSON Schema（draft 2020-12），校验下面两份定义的结构。全域 `additionalProperties: false`，字段拼错会在校验期暴露而不是被静默忽略。 |
| `securities_analysis.workflow.json` | 证券分析工作流定义，对应 `src/agent_platform/finance/securities_graph.py` 的真实 LangGraph 图：12 个节点、16 条边、4 路并行扇出、5 条条件边、3 个人工审批中断点、checkpoint 配置。 |
| `weather_analysis.workflow.json` | 天气分析工作流定义，对应 `examples/weather_analysis/`。引擎标为 `harness_sequence`（Guardrail 顺序管道，非 LangGraph 图），是平台可移植性的证据。 |

加载与校验代码在 `src/agent_platform/workflow/loader.py`，测试在 `tests/test_workflow_definitions.py`。

## 加载与校验

```python
from agent_platform.workflow import load_workflow, lint_definition

wf = load_workflow("securities_analysis")   # 加载 + Schema 校验 + 图结构体检

wf.node_ids()                # 全部节点名
wf.conditional_edges()       # 条件边及其分支
wf.interrupt_nodes()         # 会触发 interrupt() 的节点
wf.successors("__start__")   # 并行扇出的 4 个分析 Agent
wf.find_cycles()             # 环检测（Tarjan SCC）
print(wf.describe())         # 可读摘要
```

`load_workflow` 默认会做三层检查，任一层失败都抛 `WorkflowValidationError`，其 `.problems` 携带完整问题清单：

1. **结构层** —— JSON Schema 校验。若环境缺少 `jsonschema` 库，loader 内置的最小校验器会自动接手，校验能力不会消失（不引入新依赖）。
2. **图论层** —— `lint_definition()`：悬空节点引用、孤儿节点、从 `__start__` 不可达、无法到达 `__end__`、未声明的环、条件边缺 router、分支取值重复、中断点与 checkpoint 的语义矛盾等。
3. **代码层** —— 由测试完成，见下节。

命令行快速体检：

```bash
python -c "from agent_platform.workflow import load_all_workflows; load_all_workflows(); print('OK')"
```

一个设计约定：**loader 从不 import 它所描述的模块**，因此加载和校验是无副作用的，可以安全地放进 CI 或编辑器插件里。是否 import 真实实现由测试决定。

## 与真实实现的对应关系

`securities_analysis.workflow.json` 声明的每一项都能在代码里找到对应物：

| 定义中的字段 | 真实来源 |
| --- | --- |
| `nodes[].id` | `build_securities_graph()` 里 `add_node()` 注册的节点名 |
| `nodes[].implementation` | `securities_graph.py` 中的 `node_*` 函数 |
| `edges[].router` | `route_after_synthesis` / `route_after_trader` / `route_after_human_approval` / `route_after_preflight` |
| `edges[].branches[].value` | 路由函数的实际返回值 |
| `parallel_groups[0]` | `add_edge(START, ...)` 的 4 条扇出边与汇聚到 `synthesis_agent` 的 4 条边 |
| `interrupts[]` | 真正调用了 `interrupt()` 的节点函数 |
| `state.keys[]` | `SecuritiesAnalysisState` 的类型标注 |
| `state.keys[].reducer` | `Annotated[list, operator.add]`，并行分支并发写同一键时的安全累加 |
| `parameters[].value` | `_LOW_CONFIDENCE_THRESHOLD`、`_MAX_AUTO_POSITION_PCT` 等真实常量 |
| `checkpoint` | `build_securities_graph(checkpointer=...)` 与 `MemorySaver` / `SqliteSaver` |

`tests/test_workflow_definitions.py` 通过**编译真实图、import 真实函数、实际调用路由、读真实常量**来验证这些对应关系。举几个具体的:

- 节点名比对用 `build_securities_graph()` 编译后的节点表，改了节点名而没改 JSON 会立刻失败；
- 路由分支用真实 state 调用真实路由函数，断言返回值落在声明的分支取值里；
- 阈值参数与 `source_ref` 指向的常量做等值比较；
- 中断点集合与源码中真正调用 `interrupt()` 的函数集合做双向比对。

两处需要说明的诚实标注：

- `trading_harness` 条件边有一个 `"reachable": false` 的 `__end__` 分支。它是 `add_conditional_edges` 里的兜底映射，而 `route_after_preflight` 只会返回 `execute` 或 `block`，所以这条分支实际走不到。定义里如实标成不可达并写明原因，而不是从图上删掉。
- `weather_analysis.workflow.json` 的四个计算节点都指向 `WeatherAnalysisAgent.analyze`。它们是该方法内部的顺序步骤，并非独立函数，因此不编造函数名。

## 新增工作流

1. 建 `Workflow/<name>.workflow.json`，`workflow_id` 与文件名一致（小写下划线）。
2. 顶层加 `"$schema": "./workflow.schema.json"` 以获得编辑器补全。
3. 引擎选 `langgraph`（StateGraph 编排）或 `harness_sequence`（Guardrail 顺序管道）。
4. 跑 `load_workflow("<name>")` 确认三层检查通过。
5. 在 `tests/test_workflow_definitions.py` 里补上与真实代码的一致性断言 —— 这一步是关键，缺了它定义就会重新退化成一份会过期的文档。
