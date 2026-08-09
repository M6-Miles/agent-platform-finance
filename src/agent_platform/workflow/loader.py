"""
工作流定义加载与校验器
======================
本模块把 `Workflow/*.workflow.json` 加载成可被代码断言的结构化对象。

为什么需要它
------------
工作流定义文件最大的风险是**文档漂移**：代码里节点改了名、加了条件边，
JSON 文件却还停留在旧版本。一份没人校验的定义文件比没有定义文件更危险，
因为它会误导后续维护者。因此本模块提供三层防线：

1. **结构校验**（`validate_against_schema`）
   按 `Workflow/workflow.schema.json` 校验字段拼写与类型。
   优先使用 jsonschema 库；库不可用时自动降级为内置最小校验器
   （`_minimal_validate`），不新增任何依赖。

2. **图缺陷检测**（`lint_definition`）
   Schema 管不了跨字段的语义一致性，这一层负责：
   引用了不存在的节点、孤立节点、从 `__start__` 不可达、
   走不到 `__end__` 的死端、未声明的环、interrupt 节点未登记、
   并行组成员缺失、guardrail 指向不存在的节点。

3. **与真实代码比对**（`WorkflowDefinition.diff_node_ids`）
   把定义里的节点集合与运行期真实编译出的图节点集合做差集。
   这一步由测试驱动（见 `tests/test_workflow_definitions.py`），
   是唯一能真正阻止文档漂移的手段。

设计约束
--------
- 本模块**不导入**任何被描述的实现模块（如 securities_graph）。
  加载定义文件不应触发 LangGraph 建图或网络相关 import，
  保持"纯数据"特性，让校验本身零副作用、可在任意环境运行。
- 校验失败时抛出 `WorkflowValidationError` 并携带**全部**问题列表，
  而不是只报第一个错 —— 修一次改一处的体验太差。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# LangGraph 的虚拟端点。它们不是真实节点，但在边里合法出现。
START_SENTINEL = "__start__"
END_SENTINEL = "__end__"
_SENTINELS = frozenset({START_SENTINEL, END_SENTINEL})

SCHEMA_FILENAME = "workflow.schema.json"
WORKFLOW_SUFFIX = ".workflow.json"


class WorkflowValidationError(ValueError):
    """
    工作流定义不合法。

    `problems` 保留全部问题，便于一次性修完；`str(exc)` 给出可读摘要。
    """

    def __init__(self, source: str, problems: list[str]) -> None:
        self.source = source
        self.problems = list(problems)
        detail = "\n".join(f"  - {p}" for p in self.problems)
        super().__init__(
            f"工作流定义校验失败：{source}（{len(self.problems)} 个问题）\n{detail}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 路径解析
# ─────────────────────────────────────────────────────────────────────────────

def default_workflow_dir() -> Path:
    """
    返回项目根目录下的 `Workflow/`。

    本文件位于 `<root>/src/agent_platform/workflow/loader.py`，
    因此上溯三级即项目根。
    """
    return Path(__file__).resolve().parents[3] / "Workflow"


def available_workflows(workflow_dir: Path | str | None = None) -> list[str]:
    """列出目录下所有工作流 id（按文件名推导，已排序）。"""
    directory = Path(workflow_dir) if workflow_dir else default_workflow_dir()
    if not directory.is_dir():
        return []
    return sorted(
        p.name[: -len(WORKFLOW_SUFFIX)]
        for p in directory.glob(f"*{WORKFLOW_SUFFIX}")
    )


def load_schema(schema_path: Path | str | None = None) -> dict[str, Any]:
    """加载工作流 JSON Schema。"""
    path = Path(schema_path) if schema_path else default_workflow_dir() / SCHEMA_FILENAME
    if not path.is_file():
        raise FileNotFoundError(f"找不到工作流 Schema：{path}")
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


# ─────────────────────────────────────────────────────────────────────────────
# 内置最小 Schema 校验器（jsonschema 不可用时的降级实现）
# ─────────────────────────────────────────────────────────────────────────────

_TYPE_CHECKS: dict[str, Any] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    # JSON Schema 中布尔不是数字；Python 里 bool 是 int 子类，必须显式排除。
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def _resolve_ref(ref: str, root: dict[str, Any]) -> dict[str, Any]:
    """解析本地 `#/a/b` 形式的 $ref。仅支持同文档引用。"""
    if not ref.startswith("#/"):
        raise ValueError(f"最小校验器仅支持同文档 $ref，收到：{ref}")
    node: Any = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        node = node[part]
    return node


def _minimal_validate(
    value: Any,
    schema: dict[str, Any],
    root: dict[str, Any],
    path: str = "$",
) -> list[str]:
    """
    内置最小 JSON Schema 校验器。

    仅实现本项目 Schema 实际使用的关键字：
    `$ref` / `type` / `required` / `properties` / `additionalProperties` /
    `items` / `minItems` / `uniqueItems` / `minLength` / `enum` / `const` /
    `pattern` / `allOf` / `if`-`then`-`else`。

    这是**降级路径**，不追求完整实现 JSON Schema 规范，
    但覆盖本项目定义文件的全部约束，保证 jsonschema 缺失时校验强度不降。
    """
    problems: list[str] = []

    if "$ref" in schema:
        return _minimal_validate(value, _resolve_ref(schema["$ref"], root), root, path)

    # 空 schema（如 parameter.value）接受任意值
    if not schema:
        return problems

    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_TYPE_CHECKS.get(t, lambda _v: True)(value) for t in types):
            problems.append(
                f"{path}: 类型应为 {expected}，实际 {type(value).__name__}"
            )
            return problems  # 类型不符时继续校验只会产生噪音

    if "const" in schema and value != schema["const"]:
        problems.append(f"{path}: 应为常量 {schema['const']!r}，实际 {value!r}")

    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path}: 应为 {schema['enum']} 之一，实际 {value!r}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            problems.append(f"{path}: 长度应 ≥ {schema['minLength']}")
        pattern = schema.get("pattern")
        if pattern and not re.search(pattern, value):
            problems.append(f"{path}: {value!r} 不匹配模式 {pattern}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{path}: 应 ≥ {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{path}: 应 ≤ {schema['maximum']}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            problems.append(f"{path}: 至少需要 {schema['minItems']} 项")
        if schema.get("uniqueItems"):
            seen: list[Any] = []
            for item in value:
                if item in seen:
                    problems.append(f"{path}: 存在重复项 {item!r}")
                    break
                seen.append(item)
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for idx, item in enumerate(value):
                problems += _minimal_validate(item, item_schema, root, f"{path}[{idx}]")

    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                problems.append(f"{path}: 缺少必填字段 {key!r}")
        props: dict[str, Any] = schema.get("properties", {}) or {}
        for key, sub_schema in props.items():
            if key in value:
                problems += _minimal_validate(
                    value[key], sub_schema, root, f"{path}.{key}"
                )
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(props))
            if unknown:
                problems.append(f"{path}: 出现未定义字段 {unknown}")

    for sub_schema in schema.get("allOf", []):
        problems += _minimal_validate(value, sub_schema, root, path)

    if "if" in schema:
        matched = not _minimal_validate(value, schema["if"], root, path)
        branch = schema.get("then") if matched else schema.get("else")
        if isinstance(branch, dict):
            problems += _minimal_validate(value, branch, root, path)

    return problems


def validate_against_schema(
    data: Any,
    schema: dict[str, Any] | None = None,
    *,
    prefer_jsonschema: bool = True,
) -> list[str]:
    """
    按 Schema 校验工作流定义，返回问题列表（空表示合规）。

    Parameters
    ----------
    data
        已解析的工作流定义。
    schema
        Schema 字典；缺省加载 `Workflow/workflow.schema.json`。
    prefer_jsonschema
        True（默认）优先使用 jsonschema 库；置 False 可强制走内置最小校验器，
        供测试验证两条路径都真实可用。
    """
    effective = schema if schema is not None else load_schema()

    if prefer_jsonschema:
        try:
            import jsonschema
        except ImportError:
            pass
        else:
            validator_cls = jsonschema.validators.validator_for(effective)
            validator = validator_cls(effective)
            return [
                f"{'$' + ''.join(f'[{p!r}]' for p in e.absolute_path)}: {e.message}"
                for e in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
            ]

    return _minimal_validate(data, effective, effective)


# ─────────────────────────────────────────────────────────────────────────────
# 结构化对象
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BranchSpec:
    """条件边的一个分支：路由函数返回 `value` 时走向 `to`。"""

    value: str
    to: str
    when: str
    reachable: bool = True


@dataclass(frozen=True)
class EdgeSpec:
    """一条边。`type == "conditional"` 时 `to` 为 None，实际去向由 branches 决定。"""

    source: str
    type: str
    to: str | None = None
    router: str | None = None
    branches: tuple[BranchSpec, ...] = ()
    description: str | None = None

    @property
    def is_conditional(self) -> bool:
        return self.type == "conditional"

    def targets(self) -> list[str]:
        """本边可能到达的所有端点。"""
        if self.is_conditional:
            return [b.to for b in self.branches]
        return [self.to] if self.to else []


@dataclass(frozen=True)
class NodeSpec:
    """一个工作流节点。"""

    id: str
    title: str
    type: str
    description: str
    implementation: str | None = None
    guardrail_class: str | None = None
    requires: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    parallel_group: str | None = None
    interrupts: bool = False
    on_error: str | None = None
    notes: str | None = None


def split_target(target: str) -> tuple[str, str]:
    """
    拆分 `module:attribute` 形式的实现引用。

    Raises
    ------
    ValueError
        缺少 `:` 分隔符时。测试依赖此格式做真实 import 校验，
        格式错误必须暴露而不是静默跳过。
    """
    if ":" not in target:
        raise ValueError(f"实现引用应为 module:attribute 格式，收到：{target!r}")
    module, _, attr = target.partition(":")
    if not module or not attr:
        raise ValueError(f"实现引用的 module 或 attribute 为空：{target!r}")
    return module, attr


@dataclass
class WorkflowDefinition:
    """
    一份已解析的工作流定义。

    既保留原始字典（`raw`，便于访问 Schema 里的可选字段），
    也提供 `nodes` / `edges` 结构化视图与图分析方法。
    """

    raw: dict[str, Any]
    source_path: Path | None = None
    nodes: tuple[NodeSpec, ...] = field(default_factory=tuple)
    edges: tuple[EdgeSpec, ...] = field(default_factory=tuple)

    # ── 基本属性 ──────────────────────────────────────────────────────────

    @property
    def workflow_id(self) -> str:
        return self.raw["workflow_id"]

    @property
    def name(self) -> str:
        return self.raw["name"]

    @property
    def engine(self) -> str:
        return self.raw["engine"]

    @property
    def version(self) -> str:
        return self.raw["version"]

    @property
    def implementation(self) -> dict[str, Any]:
        return self.raw.get("implementation", {})

    # ── 节点与边查询 ──────────────────────────────────────────────────────

    def node_ids(self) -> list[str]:
        """定义中声明的全部节点 id（保持文件顺序）。"""
        return [n.id for n in self.nodes]

    def node(self, node_id: str) -> NodeSpec:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(f"工作流 {self.workflow_id} 中没有节点 {node_id!r}")

    def has_node(self, node_id: str) -> bool:
        return any(n.id == node_id for n in self.nodes)

    def interrupt_nodes(self) -> list[str]:
        """所有调用 interrupt() 的节点 id。"""
        return [n.id for n in self.nodes if n.interrupts]

    def conditional_edges(self) -> list[EdgeSpec]:
        return [e for e in self.edges if e.is_conditional]

    def edges_from(self, source: str) -> list[EdgeSpec]:
        return [e for e in self.edges if e.source == source]

    def successors(self, node_id: str) -> list[str]:
        """node_id 的全部后继端点（去重，保持首次出现顺序）。"""
        out: list[str] = []
        for edge in self.edges_from(node_id):
            for target in edge.targets():
                if target not in out:
                    out.append(target)
        return out

    def predecessors(self, node_id: str) -> list[str]:
        out: list[str] = []
        for edge in self.edges:
            if node_id in edge.targets() and edge.source not in out:
                out.append(edge.source)
        return out

    def parallel_group_members(self, group_id: str) -> list[str]:
        for group in self.raw.get("parallel_groups", []):
            if group["id"] == group_id:
                return list(group["members"])
        raise KeyError(f"工作流 {self.workflow_id} 中没有并行组 {group_id!r}")

    def parameter(self, name: str) -> dict[str, Any]:
        for param in self.raw.get("parameters", []):
            if param["name"] == name:
                return param
        raise KeyError(f"工作流 {self.workflow_id} 中没有参数 {name!r}")

    def implementation_targets(self) -> list[tuple[str, str]]:
        """返回 (节点 id, `module:attr`) 列表，供测试逐个真实 import 验证。"""
        return [
            (n.id, n.implementation) for n in self.nodes if n.implementation
        ]

    # ── 图分析 ────────────────────────────────────────────────────────────

    def _adjacency(self) -> dict[str, list[str]]:
        """含虚拟端点的邻接表。"""
        adj: dict[str, list[str]] = {n.id: [] for n in self.nodes}
        adj.setdefault(START_SENTINEL, [])
        adj.setdefault(END_SENTINEL, [])
        for edge in self.edges:
            adj.setdefault(edge.source, [])
            for target in edge.targets():
                adj.setdefault(target, [])
                adj[edge.source].append(target)
        return adj

    def reachable_from_start(self) -> set[str]:
        """从 `__start__` 出发可达的端点集合（广度优先）。"""
        adj = self._adjacency()
        seen = {START_SENTINEL}
        queue = [START_SENTINEL]
        while queue:
            current = queue.pop()
            for nxt in adj.get(current, []):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        return seen

    def unreachable_nodes(self) -> list[str]:
        """声明了但从 `__start__` 走不到的节点 —— 死代码，必须暴露。"""
        reachable = self.reachable_from_start()
        return [n.id for n in self.nodes if n.id not in reachable]

    def nodes_without_path_to_end(self) -> list[str]:
        """无法到达 `__end__` 的节点 —— 工作流会挂死在这里。"""
        adj = self._adjacency()
        reverse: dict[str, list[str]] = {k: [] for k in adj}
        for src, targets in adj.items():
            for dst in targets:
                reverse.setdefault(dst, []).append(src)

        seen = {END_SENTINEL}
        queue = [END_SENTINEL]
        while queue:
            current = queue.pop()
            for prev in reverse.get(current, []):
                if prev not in seen:
                    seen.add(prev)
                    queue.append(prev)
        return [n.id for n in self.nodes if n.id not in seen]

    def orphan_nodes(self) -> list[str]:
        """既没有入边也没有出边的孤立节点。"""
        result: list[str] = []
        for node in self.nodes:
            if not self.successors(node.id) and not self.predecessors(node.id):
                result.append(node.id)
        return result

    def dangling_references(self) -> list[str]:
        """
        所有指向不存在节点的引用。

        覆盖：边的 from / to、条件分支的 to、并行组的 members 与扇出扇入端点、
        interrupts 的 node、guardrails 的 applies_to、declared_cycles 的 nodes。
        """
        known = set(self.node_ids()) | _SENTINELS
        problems: list[str] = []

        def check(endpoint: str, where: str) -> None:
            if endpoint not in known:
                problems.append(f"{where} 引用了不存在的节点 {endpoint!r}")

        for idx, edge in enumerate(self.edges):
            check(edge.source, f"edges[{idx}].from")
            if edge.is_conditional:
                for b_idx, branch in enumerate(edge.branches):
                    check(branch.to, f"edges[{idx}].branches[{b_idx}].to")
            elif edge.to is not None:
                check(edge.to, f"edges[{idx}].to")

        for idx, group in enumerate(self.raw.get("parallel_groups", [])):
            for member in group.get("members", []):
                check(member, f"parallel_groups[{idx}].members")
            check(group.get("fan_out_from", ""), f"parallel_groups[{idx}].fan_out_from")
            check(group.get("fan_in_to", ""), f"parallel_groups[{idx}].fan_in_to")

        for idx, item in enumerate(self.raw.get("interrupts", [])):
            check(item.get("node", ""), f"interrupts[{idx}].node")

        for idx, item in enumerate(self.raw.get("guardrails", [])):
            for target in item.get("applies_to", []):
                check(target, f"guardrails[{idx}].applies_to")

        for idx, item in enumerate(self.raw.get("declared_cycles", [])):
            for node_id in item.get("nodes", []):
                check(node_id, f"declared_cycles[{idx}].nodes")

        return problems

    def find_cycles(self) -> list[list[str]]:
        """
        检测环，返回每个环涉及的端点列表（已排序，便于稳定断言）。

        用 Tarjan 强连通分量：分量含 ≥2 个端点，或存在自环，即为环。
        迭代实现，避免深图递归超限。
        """
        adj = self._adjacency()
        index_counter = 0
        indices: dict[str, int] = {}
        low: dict[str, int] = {}
        on_stack: dict[str, bool] = {}
        stack: list[str] = []
        components: list[list[str]] = []

        for root in adj:
            if root in indices:
                continue
            work: list[tuple[str, int]] = [(root, 0)]
            while work:
                node, child_idx = work[-1]
                if child_idx == 0:
                    indices[node] = low[node] = index_counter
                    index_counter += 1
                    stack.append(node)
                    on_stack[node] = True

                recursed = False
                neighbours = adj.get(node, [])
                for next_idx in range(child_idx, len(neighbours)):
                    nxt = neighbours[next_idx]
                    work[-1] = (node, next_idx + 1)
                    if nxt not in indices:
                        work.append((nxt, 0))
                        recursed = True
                        break
                    if on_stack.get(nxt):
                        low[node] = min(low[node], indices[nxt])
                if recursed:
                    continue

                if low[node] == indices[node]:
                    component: list[str] = []
                    while True:
                        member = stack.pop()
                        on_stack[member] = False
                        component.append(member)
                        if member == node:
                            break
                    components.append(component)
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])

        cycles: list[list[str]] = []
        for component in components:
            if len(component) > 1:
                cycles.append(sorted(component))
            else:
                only = component[0]
                if only in adj.get(only, []):
                    cycles.append([only])
        return sorted(cycles)

    def undeclared_cycles(self) -> list[list[str]]:
        """未在 `declared_cycles` 中登记的环。"""
        declared = [
            set(item.get("nodes", [])) for item in self.raw.get("declared_cycles", [])
        ]
        return [cycle for cycle in self.find_cycles() if set(cycle) not in declared]

    def diff_node_ids(self, actual: Iterable[str]) -> dict[str, list[str]]:
        """
        与真实代码的节点集合比对（反文档漂移的核心）。

        Parameters
        ----------
        actual
            运行期真实节点名集合，例如已编译 LangGraph 的 `graph.nodes` 键。
            调用方需自行剔除 `__start__` / `__end__` 等虚拟端点。

        Returns
        -------
        dict
            `missing_in_definition`：代码里有、定义里没有（定义漏了）。
            `missing_in_code`：定义里有、代码里没有（定义写多了或代码删了）。
        """
        declared = set(self.node_ids())
        real = set(actual)
        return {
            "missing_in_definition": sorted(real - declared),
            "missing_in_code": sorted(declared - real),
        }

    def describe(self) -> dict[str, Any]:
        """返回结构摘要，供 README、可观测面板或 CLI 打印。"""
        return {
            "workflow_id": self.workflow_id,
            "name": self.name,
            "engine": self.engine,
            "version": self.version,
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
            "conditional_edge_count": len(self.conditional_edges()),
            "parallel_groups": [g["id"] for g in self.raw.get("parallel_groups", [])],
            "interrupt_nodes": self.interrupt_nodes(),
            "checkpoint_enabled": bool(self.raw.get("checkpoint", {}).get("enabled")),
            "unreachable_nodes": self.unreachable_nodes(),
            "cycles": self.find_cycles(),
        }


# ─────────────────────────────────────────────────────────────────────────────
# 解析
# ─────────────────────────────────────────────────────────────────────────────

def _parse_nodes(raw: dict[str, Any]) -> tuple[NodeSpec, ...]:
    nodes: list[NodeSpec] = []
    for item in raw.get("nodes", []):
        nodes.append(NodeSpec(
            id=item["id"],
            title=item["title"],
            type=item["type"],
            description=item["description"],
            implementation=item.get("implementation"),
            guardrail_class=item.get("guardrail_class"),
            requires=tuple(item.get("requires", [])),
            produces=tuple(item.get("produces", [])),
            parallel_group=item.get("parallel_group"),
            interrupts=bool(item.get("interrupts", False)),
            on_error=item.get("on_error"),
            notes=item.get("notes"),
        ))
    return tuple(nodes)


def _parse_edges(raw: dict[str, Any]) -> tuple[EdgeSpec, ...]:
    edges: list[EdgeSpec] = []
    for item in raw.get("edges", []):
        branches = tuple(
            BranchSpec(
                value=b["value"],
                to=b["to"],
                when=b["when"],
                reachable=bool(b.get("reachable", True)),
            )
            for b in item.get("branches", [])
        )
        edges.append(EdgeSpec(
            source=item["from"],
            type=item["type"],
            to=item.get("to"),
            router=item.get("router"),
            branches=branches,
            description=item.get("description"),
        ))
    return tuple(edges)


# ─────────────────────────────────────────────────────────────────────────────
# 语义体检
# ─────────────────────────────────────────────────────────────────────────────

def lint_definition(raw: dict[str, Any]) -> list[str]:
    """
    检测 Schema 覆盖不到的语义缺陷，返回问题列表（空表示健康）。

    Schema 只能保证"字段拼写和类型对"，无法保证"这张图讲得通"。
    本函数补上跨字段一致性检查，且**不导入**任何实现模块。
    """
    problems: list[str] = []

    node_items = raw.get("nodes", []) or []
    ids = [n.get("id") for n in node_items]
    duplicates = sorted({i for i in ids if ids.count(i) > 1 and i is not None})
    if duplicates:
        problems.append(f"节点 id 重复：{duplicates}")

    # 需要一个可用对象来做图分析；此处不校验 Schema，仅解析
    definition = WorkflowDefinition(
        raw=raw,
        nodes=_parse_nodes(raw),
        edges=_parse_edges(raw),
    )

    problems += definition.dangling_references()

    orphans = definition.orphan_nodes()
    if orphans:
        problems.append(f"孤立节点（无入边也无出边）：{orphans}")

    unreachable = [
        n for n in definition.unreachable_nodes() if n not in orphans
    ]
    if unreachable:
        problems.append(f"从 {START_SENTINEL} 不可达的节点：{unreachable}")

    dead_ends = [
        n for n in definition.nodes_without_path_to_end() if n not in orphans
    ]
    if dead_ends:
        problems.append(f"无法到达 {END_SENTINEL} 的节点：{dead_ends}")

    if not definition.edges_from(START_SENTINEL):
        problems.append(f"缺少从 {START_SENTINEL} 出发的边，工作流无入口")

    if not any(END_SENTINEL in e.targets() for e in definition.edges):
        problems.append(f"没有任何边指向 {END_SENTINEL}，工作流无出口")

    for cycle in definition.undeclared_cycles():
        problems.append(
            f"存在未在 declared_cycles 中声明的环：{cycle}"
        )

    # 并行组一致性
    declared_groups = {g["id"] for g in raw.get("parallel_groups", [])}
    for node in definition.nodes:
        if node.parallel_group and node.parallel_group not in declared_groups:
            problems.append(
                f"节点 {node.id} 声明的并行组 {node.parallel_group!r} 未在 parallel_groups 中定义"
            )
    for group in raw.get("parallel_groups", []):
        for member in group.get("members", []):
            if definition.has_node(member):
                member_group = definition.node(member).parallel_group
                if member_group != group["id"]:
                    problems.append(
                        f"并行组 {group['id']} 的成员 {member} 未反向声明 "
                        f"parallel_group（实际 {member_group!r}）"
                    )

    # interrupt 登记一致性：节点标记与 interrupts 数组必须互相印证
    registered = {item["node"] for item in raw.get("interrupts", []) if "node" in item}
    marked = set(definition.interrupt_nodes())
    for node_id in sorted(marked - registered):
        problems.append(f"节点 {node_id} 标记了 interrupts 但未在 interrupts 数组中登记")
    for node_id in sorted(registered - marked):
        if definition.has_node(node_id):
            problems.append(
                f"interrupts 数组登记了 {node_id}，但该节点未标记 interrupts: true"
            )

    if marked and not raw.get("checkpoint", {}).get("enabled"):
        problems.append(
            "存在 interrupt 节点但 checkpoint.enabled 不为 true："
            "没有 checkpoint 无法恢复被暂停的工作流"
        )

    # 边的形态
    for idx, edge in enumerate(definition.edges):
        if edge.is_conditional:
            if not edge.router:
                problems.append(f"edges[{idx}] 是条件边但缺少 router")
            if not edge.branches:
                problems.append(f"edges[{idx}] 是条件边但缺少 branches")
            values = [b.value for b in edge.branches]
            dup_values = sorted({v for v in values if values.count(v) > 1})
            if dup_values:
                problems.append(f"edges[{idx}] 的 branches 存在重复 value：{dup_values}")
        elif not edge.to:
            problems.append(f"edges[{idx}] 是直接边但缺少 to")

    # 实现引用：langgraph 引擎的每个节点都必须能定位到真实函数
    engine = raw.get("engine")
    for node in definition.nodes:
        target = node.implementation or node.guardrail_class
        if engine == "langgraph" and not node.implementation:
            problems.append(f"节点 {node.id} 缺少 implementation（langgraph 引擎要求逐节点可定位）")
            continue
        if target is None:
            problems.append(
                f"节点 {node.id} 既无 implementation 也无 guardrail_class，无法与代码比对"
            )
            continue
        try:
            split_target(target)
        except ValueError as exc:
            problems.append(f"节点 {node.id} 的实现引用格式错误：{exc}")

    for node in definition.nodes:
        if node.type == "guardrail" and not node.guardrail_class:
            problems.append(f"guardrail 类型节点 {node.id} 缺少 guardrail_class")

    # 状态键与节点读写字段的一致性
    state = raw.get("state")
    if state:
        key_names = [k["name"] for k in state.get("keys", [])]
        dup_keys = sorted({k for k in key_names if key_names.count(k) > 1})
        if dup_keys:
            problems.append(f"state.keys 存在重复字段：{dup_keys}")
        known_keys = set(key_names)
        for node in definition.nodes:
            for key in list(node.requires) + list(node.produces):
                if key not in known_keys:
                    problems.append(
                        f"节点 {node.id} 引用了未在 state.keys 中声明的状态字段 {key!r}"
                    )

    return problems


# ─────────────────────────────────────────────────────────────────────────────
# 加载入口
# ─────────────────────────────────────────────────────────────────────────────

def load_definition(
    raw: dict[str, Any],
    *,
    source: str = "<dict>",
    validate: bool = True,
    schema: dict[str, Any] | None = None,
    lint: bool = True,
) -> WorkflowDefinition:
    """
    从已解析的字典构造 `WorkflowDefinition`。

    Raises
    ------
    WorkflowValidationError
        Schema 校验或语义体检发现问题时，携带全部问题列表。
    """
    problems: list[str] = []
    if validate:
        problems += validate_against_schema(raw, schema)

    # Schema 不通过时结构可能残缺，继续 lint 只会产生连带噪音
    if not problems and lint:
        problems += lint_definition(raw)

    if problems:
        raise WorkflowValidationError(source, problems)

    return WorkflowDefinition(
        raw=raw,
        nodes=_parse_nodes(raw),
        edges=_parse_edges(raw),
    )


def load_workflow_file(
    path: Path | str,
    *,
    validate: bool = True,
    schema: dict[str, Any] | None = None,
    lint: bool = True,
) -> WorkflowDefinition:
    """加载单个工作流定义文件。"""
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"找不到工作流定义文件：{file_path}")
    try:
        with file_path.open(encoding="utf-8") as fh:
            raw = json.load(fh)
    except json.JSONDecodeError as exc:
        raise WorkflowValidationError(
            str(file_path), [f"不是合法 JSON：{exc}"]
        ) from exc

    definition = load_definition(
        raw, source=str(file_path), validate=validate, schema=schema, lint=lint
    )
    definition.source_path = file_path

    # 文件名必须与 workflow_id 一致，否则 load_workflow(id) 会找不到文件
    expected = f"{raw.get('workflow_id')}{WORKFLOW_SUFFIX}"
    if file_path.name != expected:
        raise WorkflowValidationError(
            str(file_path),
            [f"文件名应为 {expected}，与 workflow_id 保持一致"],
        )
    return definition


def load_workflow(
    workflow_id: str,
    *,
    workflow_dir: Path | str | None = None,
    validate: bool = True,
    lint: bool = True,
) -> WorkflowDefinition:
    """
    按 workflow_id 加载定义。

    Examples
    --------
    >>> wf = load_workflow("securities_analysis")
    >>> "synthesis_agent" in wf.node_ids()
    True
    """
    directory = Path(workflow_dir) if workflow_dir else default_workflow_dir()
    path = directory / f"{workflow_id}{WORKFLOW_SUFFIX}"
    if not path.is_file():
        raise FileNotFoundError(
            f"找不到工作流 {workflow_id!r}。可用：{available_workflows(directory)}"
        )
    schema = load_schema(directory / SCHEMA_FILENAME) if validate else None
    return load_workflow_file(path, validate=validate, schema=schema, lint=lint)


def load_all_workflows(
    workflow_dir: Path | str | None = None,
    *,
    validate: bool = True,
    lint: bool = True,
) -> dict[str, WorkflowDefinition]:
    """加载目录下全部工作流定义，返回 {workflow_id: 定义}。"""
    directory = Path(workflow_dir) if workflow_dir else default_workflow_dir()
    schema = load_schema(directory / SCHEMA_FILENAME) if validate else None
    result: dict[str, WorkflowDefinition] = {}
    for path in sorted(directory.glob(f"*{WORKFLOW_SUFFIX}")):
        definition = load_workflow_file(
            path, validate=validate, schema=schema, lint=lint
        )
        result[definition.workflow_id] = definition
    return result
