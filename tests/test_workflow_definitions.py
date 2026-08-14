"""
Workflow 层测试 —— 工作流定义与真实代码的一致性验证
====================================================

本文件的核心价值不是「JSON 能被解析」，而是**防文档漂移**：

    Workflow/*.workflow.json 是对真实代码结构的声明。
    只要真实代码改了节点名、改了路由分支、改了阈值常量，
    而声明没跟着改，本文件的测试就必须失败。

因此测试分三层，与 loader.py 的三层防线一一对应：

    第一层 结构层：文件存在 / 是合法 JSON / 通过 JSON Schema 校验
    第二层 图论层：loader 的缺陷检测（悬空引用、孤儿节点、不可达、未声明的环）
    第三层 代码层：声明的节点名/状态键/路由分支/阈值 == 真实代码里的值

第三层是重点。它通过 **import 真实模块 + 编译真实 LangGraph 图 + 实际调用路由函数**
来取得「真值」，而不是读注释或字符串比对文档。
"""
from __future__ import annotations

import copy
import inspect
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

# 确保 src/ 与 examples/weather_analysis/ 都在路径中（与 tests/test_p05_weather_demo.py 一致）
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "examples" / "weather_analysis"))

from agent_platform.workflow.loader import (  # noqa: E402
    END_SENTINEL,
    START_SENTINEL,
    WorkflowDefinition,
    WorkflowValidationError,
    available_workflows,
    default_workflow_dir,
    lint_definition,
    load_all_workflows,
    load_definition,
    load_schema,
    load_workflow,
    load_workflow_file,
    split_target,
    validate_against_schema,
)

WORKFLOW_DIR = default_workflow_dir()
SCHEMA_PATH = WORKFLOW_DIR / "workflow.schema.json"
SECURITIES_PATH = WORKFLOW_DIR / "securities_analysis.workflow.json"
WEATHER_PATH = WORKFLOW_DIR / "weather_analysis.workflow.json"

ALL_WORKFLOW_PATHS = [SECURITIES_PATH, WEATHER_PATH]


# ══════════════════════════════════════════════════════════════════════════════
# 测试夹具：一个最小合法定义，供缺陷检测测试「故意弄坏」
# ══════════════════════════════════════════════════════════════════════════════

def _minimal_definition() -> dict[str, Any]:
    """
    返回一个能通过 Schema 校验且 lint 干净的最小工作流定义。

    缺陷检测测试的套路是：拷贝这个基线 → 只破坏一处 → 断言 loader 精确报出该缺陷。
    这样失败信息才能定位到「是哪一类缺陷没被检出」，而不是一团糟的多重错误。

    implementation 指向一个**不存在**的模块是故意的：loader 的设计约定是
    绝不 import 被描述的模块，因此这里可以安全地用占位模块名做纯结构测试。
    """
    return {
        "$schema": "./workflow.schema.json",
        "schema_version": "1.0",
        "workflow_id": "unit_test_flow",
        "name": "单元测试用最小工作流",
        "version": "1.0.0",
        "engine": "harness_sequence",
        "description": "仅用于测试 loader 缺陷检测能力的最小合法定义。",
        "implementation": {"module": "tests.fixture_not_imported"},
        "nodes": [
            {
                "id": "step_a",
                "title": "步骤 A",
                "type": "compute",
                "description": "第一步。",
                "implementation": "tests.fixture_not_imported:step_a",
            },
            {
                "id": "step_b",
                "title": "步骤 B",
                "type": "compute",
                "description": "第二步。",
                "implementation": "tests.fixture_not_imported:step_b",
            },
        ],
        "edges": [
            {"from": START_SENTINEL, "to": "step_a", "type": "direct", "description": "入口。"},
            {"from": "step_a", "to": "step_b", "type": "direct", "description": "串行。"},
            {"from": "step_b", "to": END_SENTINEL, "type": "direct", "description": "出口。"},
        ],
    }


def _resolve(target: str) -> Any:
    """
    把 "module:attr" 或 "module:Class.method" 解析成真实的 Python 对象。

    weather 工作流里存在 "weather_agent:WeatherAnalysisAgent.analyze" 这种
    带点的属性路径，所以需要逐级 getattr，不能只做一次。
    """
    module_name, attr_path = split_target(target)
    module = __import__(module_name, fromlist=["_"])
    obj: Any = module
    for part in attr_path.split("."):
        obj = getattr(obj, part)
    return obj


def _squash(text: str) -> str:
    """删除全部空白字符，让源码断言不受换行/缩进/对齐风格影响。"""
    return re.sub(r"\s+", "", text)


# ══════════════════════════════════════════════════════════════════════════════
# 第一层 结构层：文件存在性与 JSON 合法性
# ══════════════════════════════════════════════════════════════════════════════

class TestWorkflowFilesExist:
    """Workflow 目录不能是空目录 —— 三个文件必须真实存在且是合法 JSON。"""

    def test_workflow_dir_exists(self) -> None:
        assert WORKFLOW_DIR.is_dir(), f"Workflow 目录不存在：{WORKFLOW_DIR}"

    @pytest.mark.parametrize(
        "path",
        [SCHEMA_PATH, SECURITIES_PATH, WEATHER_PATH],
        ids=["schema", "securities", "weather"],
    )
    def test_file_exists_and_non_empty(self, path: Path) -> None:
        assert path.is_file(), f"缺少文件：{path}"
        assert path.stat().st_size > 0, f"文件为空：{path}"

    @pytest.mark.parametrize(
        "path",
        [SCHEMA_PATH, SECURITIES_PATH, WEATHER_PATH],
        ids=["schema", "securities", "weather"],
    )
    def test_file_is_valid_json(self, path: Path) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict), f"{path.name} 顶层应为 JSON 对象"

    @pytest.mark.parametrize("path", ALL_WORKFLOW_PATHS, ids=["securities", "weather"])
    def test_workflow_declares_schema_ref(self, path: Path) -> None:
        """每个工作流定义都要用 $schema 指回校验用的 Schema，方便编辑器补全。"""
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data.get("$schema") == "./workflow.schema.json"

    def test_workflow_files_discovered_by_loader(self) -> None:
        assert available_workflows() == ["securities_analysis", "weather_analysis"]


class TestSchemaItself:
    """Schema 自身必须是一份声明了版本的、可用的 JSON Schema。"""

    def test_declares_dialect(self) -> None:
        schema = load_schema()
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"

    def test_has_defs_and_required(self) -> None:
        schema = load_schema()
        assert "$defs" in schema
        for name in ("identifier", "endpoint", "node", "edge", "interrupt", "checkpoint"):
            assert name in schema["$defs"], f"Schema 缺少 $defs/{name}"
        assert "nodes" in schema["required"]
        assert "edges" in schema["required"]

    def test_engine_enum_covers_both_engines(self) -> None:
        """平台目前只有两种执行引擎，Schema 必须显式枚举而不是放任任意字符串。"""
        schema = load_schema()
        assert set(schema["properties"]["engine"]["enum"]) == {"langgraph", "harness_sequence"}


class TestSchemaValidation:
    """两份工作流定义都必须通过 Schema 校验，且两条校验路径结论一致。"""

    @pytest.mark.parametrize("path", ALL_WORKFLOW_PATHS, ids=["securities", "weather"])
    def test_passes_jsonschema(self, path: Path) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        errors = validate_against_schema(data, load_schema())
        assert errors == [], f"{path.name} 未通过 jsonschema 校验：{errors}"

    @pytest.mark.parametrize("path", ALL_WORKFLOW_PATHS, ids=["securities", "weather"])
    def test_passes_builtin_minimal_validator(self, path: Path) -> None:
        """
        loader 内置了不依赖第三方库的最小校验器作为兜底。
        即使环境里没有 jsonschema，校验能力也不能消失，所以这条路径要单独测。
        """
        data = json.loads(path.read_text(encoding="utf-8"))
        errors = validate_against_schema(data, load_schema(), prefer_jsonschema=False)
        assert errors == [], f"{path.name} 未通过内置最小校验器：{errors}"

    def test_both_validators_reject_the_same_bad_document(self) -> None:
        """两条校验路径对同一份坏文档都要报错，否则兜底实现形同虚设。"""
        bad = _minimal_definition()
        bad["schema_version"] = "9.9"          # const 不匹配
        bad["version"] = "not-a-version"       # pattern 不匹配
        schema = load_schema()
        assert validate_against_schema(bad, schema) != []
        assert validate_against_schema(bad, schema, prefer_jsonschema=False) != []

    @pytest.mark.parametrize(
        "mutate, reason",
        [
            (lambda d: d.pop("nodes"), "缺少必填字段 nodes"),
            (lambda d: d.update(engine="airflow"), "engine 不在枚举内"),
            (lambda d: d.update(workflow_id="Bad-ID"), "workflow_id 不符合命名规范"),
            (lambda d: d.update(unexpected_key=1), "顶层出现未定义字段"),
            (lambda d: d["nodes"][0].update(type="wizard"), "节点 type 不在枚举内"),
            (lambda d: d["nodes"][0].pop("description"), "节点缺少 description"),
            (lambda d: d["edges"][0].update(type="maybe"), "边 type 不在枚举内"),
            (lambda d: d.update(nodes=[]), "nodes 为空数组"),
        ],
    )
    def test_schema_rejects_malformed_documents(self, mutate, reason: str) -> None:
        """Schema 用 additionalProperties:false + enum + pattern 让笔误在校验期暴露。"""
        bad = _minimal_definition()
        mutate(bad)
        errors = validate_against_schema(bad, load_schema())
        assert errors != [], f"Schema 未能拒绝：{reason}"


class TestLoaderBasics:
    """loader 的基本加载能力。"""

    def test_baseline_fixture_is_clean(self) -> None:
        """
        基线夹具必须自身干净 —— 这是所有缺陷检测测试的前提。
        如果这条失败，说明后面那些「只破坏一处」的测试结论都不可信。
        """
        wf = load_definition(_minimal_definition(), source="<baseline>")
        assert wf.node_ids() == ["step_a", "step_b"]
        assert lint_definition(_minimal_definition()) == []

    @pytest.mark.parametrize(
        "workflow_id", ["securities_analysis", "weather_analysis"]
    )
    def test_load_workflow_by_id(self, workflow_id: str) -> None:
        wf = load_workflow(workflow_id)
        assert isinstance(wf, WorkflowDefinition)
        assert wf.workflow_id == workflow_id
        assert wf.nodes, "节点列表不应为空"
        assert wf.edges, "边列表不应为空"

    @pytest.mark.parametrize("path", ALL_WORKFLOW_PATHS, ids=["securities", "weather"])
    def test_load_workflow_file_lints_clean(self, path: Path) -> None:
        """真实工作流定义在 loader 的图论体检下必须零缺陷。"""
        wf = load_workflow_file(path)
        assert lint_definition(wf.raw) == [], f"{path.name} 存在图结构缺陷"

    def test_load_all_workflows(self) -> None:
        all_wf = load_all_workflows()
        assert set(all_wf) == {"securities_analysis", "weather_analysis"}

    def test_describe_returns_summary_dict(self) -> None:
        """describe() 返回结构化摘要字典，供 CLI / 报告直接消费。"""
        info = load_workflow("securities_analysis").describe()
        assert info["workflow_id"] == "securities_analysis"
        assert info["engine"] == "langgraph"
        assert info["node_count"] == 12
        assert info["edge_count"] == 16
        assert info["conditional_edge_count"] == 5
        assert info["checkpoint_enabled"] is True
        assert info["parallel_groups"] == ["analysis_fanout"]
        assert info["interrupt_nodes"] == [
            "debate_approval",
            "human_approval",
            "trading_harness",
        ]
        # 摘要必须自证无缺陷，否则摘要本身就在掩盖问题
        assert info["unreachable_nodes"] == []
        assert info["cycles"] == []

    def test_unknown_workflow_id_raises(self) -> None:
        with pytest.raises((FileNotFoundError, WorkflowValidationError, ValueError)):
            load_workflow("no_such_workflow")

    def test_api_workflow_definition_uses_validated_loader(self) -> None:
        from agent_platform.api.main import get_workflow_definition

        body = get_workflow_definition("securities_analysis")

        assert body["workflow_id"] == "securities_analysis"
        assert any(node["id"] == "evaluator_agent" for node in body["nodes"])
        assert any(edge["from"] == "evaluator_agent" for edge in body["edges"])

    def test_frontend_renders_definition_driven_topology(self) -> None:
        html = (_ROOT / "frontend_prototype.html").read_text(encoding="utf-8")

        assert 'id="workflow-topology"' in html
        assert "/workflows/securities_analysis" in html
        assert "renderWorkflowTopology" in html
        assert "definition.edges.forEach" in html

    @pytest.mark.parametrize(
        "target, expected",
        [
            ("pkg.mod:func", ("pkg.mod", "func")),
            ("weather_agent:WeatherAnalysisAgent.analyze",
             ("weather_agent", "WeatherAnalysisAgent.analyze")),
        ],
    )
    def test_split_target(self, target: str, expected: tuple[str, str]) -> None:
        assert split_target(target) == expected

    @pytest.mark.parametrize("bad", ["no_colon", ":missing_module", "mod:"])
    def test_split_target_rejects_malformed(self, bad: str) -> None:
        with pytest.raises(ValueError):
            split_target(bad)


# ══════════════════════════════════════════════════════════════════════════════
# 第二层 图论层：loader 的缺陷检测能力
# ══════════════════════════════════════════════════════════════════════════════

class TestLoaderDefectDetection:
    """
    每条测试只破坏一处，断言 loader 能精确报出对应缺陷。

    这是 loader 存在的意义：把「图画错了」从运行期崩溃提前到加载期报错。
    """

    def test_detects_edge_to_undefined_node(self) -> None:
        """引用不存在的节点 —— 最常见的手写错误。"""
        bad = _minimal_definition()
        bad["edges"].append(
            {"from": "step_b", "to": "ghost_node", "type": "direct", "description": "指向幽灵。"}
        )
        with pytest.raises(WorkflowValidationError) as exc:
            load_definition(bad, source="<dangling>")
        assert any("ghost_node" in p for p in exc.value.problems)

    def test_detects_conditional_branch_to_undefined_node(self) -> None:
        """条件边的分支目标同样要查，不能只查直连边。"""
        bad = _minimal_definition()
        bad["edges"][1] = {
            "from": "step_a",
            "type": "conditional",
            "router": "tests.fixture_not_imported:router",
            "description": "分支目标不存在。",
            "branches": [
                {"value": "go", "to": "step_b", "when": "正常。"},
                {"value": "nowhere", "to": "ghost_branch", "when": "幽灵分支。"},
            ],
        }
        with pytest.raises(WorkflowValidationError) as exc:
            load_definition(bad, source="<dangling-branch>")
        assert any("ghost_branch" in p for p in exc.value.problems)

    def test_detects_orphan_node(self) -> None:
        """孤儿节点：声明了却没有任何边连上它。"""
        bad = _minimal_definition()
        bad["nodes"].append(
            {
                "id": "step_orphan",
                "title": "孤儿步骤",
                "type": "compute",
                "description": "没有任何边连接。",
                "implementation": "tests.fixture_not_imported:orphan",
            }
        )
        with pytest.raises(WorkflowValidationError) as exc:
            load_definition(bad, source="<orphan>")
        assert any("step_orphan" in p for p in exc.value.problems)

    def test_detects_node_unreachable_from_start(self) -> None:
        """有入边但从 __start__ 走不到 —— 永远不会执行的死代码。"""
        bad = _minimal_definition()
        bad["nodes"].append(
            {
                "id": "step_island",
                "title": "孤岛步骤",
                "type": "compute",
                "description": "只有自己指向出口，没人指向它。",
                "implementation": "tests.fixture_not_imported:island",
            }
        )
        bad["edges"].append(
            {"from": "step_island", "to": END_SENTINEL, "type": "direct", "description": "孤岛出口。"}
        )
        problems = lint_definition(bad)
        assert any("step_island" in p for p in problems), problems

    def test_detects_node_without_path_to_end(self) -> None:
        """走得到但出不去 —— 工作流会卡死在这里。"""
        bad = _minimal_definition()
        bad["edges"] = [
            {"from": START_SENTINEL, "to": "step_a", "type": "direct", "description": "入口。"},
            {"from": "step_a", "to": "step_b", "type": "direct", "description": "串行。"},
        ]
        problems = lint_definition(bad)
        assert any("step_b" in p for p in problems), problems

    def test_detects_undeclared_cycle(self) -> None:
        """环必须显式声明理由，默认视为缺陷。"""
        bad = _minimal_definition()
        bad["edges"].append(
            {"from": "step_b", "to": "step_a", "type": "direct", "description": "回边。"}
        )
        with pytest.raises(WorkflowValidationError) as exc:
            load_definition(bad, source="<cycle>")
        assert any("环" in p for p in exc.value.problems)

    def test_declared_cycle_is_accepted(self) -> None:
        """显式声明并给出理由后，同一个环就不再是缺陷 —— 证明这不是「一律禁环」。"""
        ok = _minimal_definition()
        ok["edges"].append(
            {"from": "step_b", "to": "step_a", "type": "direct", "description": "回边。"}
        )
        ok["declared_cycles"] = [
            {"nodes": ["step_a", "step_b"], "reason": "重试循环，由外层最大轮次限制。"}
        ]
        wf = load_definition(ok, source="<declared-cycle>")
        assert wf.find_cycles() == [["step_a", "step_b"]]
        assert wf.undeclared_cycles() == []

    def test_detects_duplicate_node_id(self) -> None:
        bad = _minimal_definition()
        bad["nodes"].append(copy.deepcopy(bad["nodes"][0]))
        problems = lint_definition(bad)
        assert any("step_a" in p for p in problems), problems

    def test_detects_conditional_edge_without_router(self) -> None:
        """条件边缺 router 会让定义无法对应到真实的 add_conditional_edges。"""
        bad = _minimal_definition()
        bad["edges"][1] = {
            "from": "step_a",
            "type": "conditional",
            "description": "缺少 router。",
            "branches": [{"value": "go", "to": "step_b", "when": "唯一分支。"}],
        }
        errors = validate_against_schema(bad, load_schema())
        problems = lint_definition(bad)
        assert errors != [] or problems != [], "缺 router 的条件边应被拒绝"

    def test_detects_duplicate_branch_value(self) -> None:
        bad = _minimal_definition()
        bad["edges"][1] = {
            "from": "step_a",
            "type": "conditional",
            "router": "tests.fixture_not_imported:router",
            "description": "分支取值重复。",
            "branches": [
                {"value": "go", "to": "step_b", "when": "第一次。"},
                {"value": "go", "to": END_SENTINEL, "when": "重复取值。"},
            ],
        }
        problems = lint_definition(bad)
        assert any("go" in p for p in problems), problems

    def test_detects_interrupt_on_undeclared_node(self) -> None:
        """interrupts 里写了一个图上没有的节点。"""
        bad = _minimal_definition()
        bad["checkpoint"] = {"enabled": True}
        bad["interrupts"] = [
            {
                "node": "ghost_gate",
                "payload_type": "approval",
                "trigger": "不存在的节点。",
                "resume_values": ["approve", "reject"],
            }
        ]
        problems = lint_definition(bad)
        assert any("ghost_gate" in p for p in problems), problems

    def test_detects_interrupt_without_checkpoint(self) -> None:
        """
        interrupt 依赖 checkpoint 才能恢复执行。
        声明了中断点却关掉 checkpoint 是语义矛盾，必须报错。
        """
        bad = _minimal_definition()
        bad["nodes"][1]["interrupts"] = True
        bad["checkpoint"] = {"enabled": False}
        bad["interrupts"] = [
            {
                "node": "step_b",
                "payload_type": "approval",
                "trigger": "人工确认。",
                "resume_values": ["approve", "reject"],
            }
        ]
        problems = lint_definition(bad)
        assert any("checkpoint" in p for p in problems), problems

    def test_detects_node_interrupt_flag_mismatch(self) -> None:
        """节点标了 interrupts:true 但 interrupts 区块没登记，双向一致性都要查。"""
        bad = _minimal_definition()
        bad["nodes"][1]["interrupts"] = True
        problems = lint_definition(bad)
        assert any("step_b" in p for p in problems), problems

    def test_load_definition_can_skip_lint(self) -> None:
        """lint=False 时只做结构校验 —— 便于工具链分阶段报错。"""
        bad = _minimal_definition()
        bad["edges"].append(
            {"from": "step_b", "to": "ghost_node", "type": "direct", "description": "悬空。"}
        )
        wf = load_definition(bad, source="<no-lint>", lint=False)
        # lint 被跳过 => 不抛异常，但按需调用检查器仍能报出同一处缺陷
        problems = wf.dangling_references()
        assert any("ghost_node" in p for p in problems), problems

    def test_validation_error_carries_all_problems(self) -> None:
        """错误对象要携带完整问题清单，而不是只抛第一条。"""
        bad = _minimal_definition()
        bad["edges"].append(
            {"from": "step_b", "to": "ghost_one", "type": "direct", "description": "悬空一。"}
        )
        bad["nodes"].append(
            {
                "id": "step_orphan",
                "title": "孤儿",
                "type": "compute",
                "description": "孤儿节点。",
                "implementation": "tests.fixture_not_imported:orphan",
            }
        )
        with pytest.raises(WorkflowValidationError) as exc:
            load_definition(bad, source="<multi>")
        assert len(exc.value.problems) >= 2
        assert str(exc.value)


class TestReachabilityAnalysis:
    """可达性与环检测在真实定义上的表现。"""

    @pytest.mark.parametrize(
        "workflow_id", ["securities_analysis", "weather_analysis"]
    )
    def test_all_real_nodes_reachable_from_start(self, workflow_id: str) -> None:
        wf = load_workflow(workflow_id)
        reachable = set(wf.reachable_from_start())
        assert START_SENTINEL in reachable
        assert END_SENTINEL in reachable, "END 必须可达，否则工作流没有出口"
        missing = [n for n in wf.node_ids() if n not in reachable]
        assert missing == [], f"存在从 START 不可达的节点：{missing}"
        assert wf.unreachable_nodes() == []

    @pytest.mark.parametrize(
        "workflow_id", ["securities_analysis", "weather_analysis"]
    )
    def test_every_node_can_reach_end(self, workflow_id: str) -> None:
        wf = load_workflow(workflow_id)
        assert wf.nodes_without_path_to_end() == []

    @pytest.mark.parametrize(
        "workflow_id", ["securities_analysis", "weather_analysis"]
    )
    def test_real_workflows_are_acyclic(self, workflow_id: str) -> None:
        """两个真实工作流都是 DAG；将来若引入环，必须在 declared_cycles 里写明理由。"""
        wf = load_workflow(workflow_id)
        assert wf.find_cycles() == []
        assert wf.raw.get("declared_cycles", []) == []

    def test_orphan_and_dangling_are_empty_on_real_workflows(self) -> None:
        for workflow_id in ("securities_analysis", "weather_analysis"):
            wf = load_workflow(workflow_id)
            assert wf.orphan_nodes() == [], workflow_id
            assert wf.dangling_references() == [], workflow_id


# ══════════════════════════════════════════════════════════════════════════════
# 第三层 代码层：证券工作流声明 vs securities_graph.py 真实实现
# ══════════════════════════════════════════════════════════════════════════════

class TestSecuritiesWorkflowMatchesCode:
    """
    防漂移核心。真值来源：
      - 编译真实 LangGraph 图，读它的节点表
      - import 真实的路由函数并实际调用
      - 读真实的阈值常量
      - 读 SecuritiesAnalysisState 的类型标注
    """

    @pytest.fixture(scope="class")
    def wf(self) -> WorkflowDefinition:
        return load_workflow("securities_analysis")

    @pytest.fixture(scope="class")
    def compiled_node_ids(self) -> list[str]:
        """编译真实图并取出节点名。注意编译后的 nodes 含 __start__ 但不含 __end__。"""
        from agent_platform.finance.securities_graph import build_securities_graph

        graph = build_securities_graph()
        return sorted(k for k in graph.nodes if not k.startswith("__"))

    # ── 节点名一致性（最关键的一条） ──────────────────────────────────────────

    def test_declared_nodes_match_compiled_graph(
        self, wf: WorkflowDefinition, compiled_node_ids: list[str]
    ) -> None:
        """
        声明的节点名必须与真实编译图的节点名完全一致。
        改了 securities_graph.py 的节点名而忘了改 JSON，这条会立刻失败。
        """
        diff = wf.diff_node_ids(compiled_node_ids)
        assert diff["missing_in_definition"] == [], (
            f"代码里有但定义里没有的节点：{diff['missing_in_definition']}"
        )
        assert diff["missing_in_code"] == [], (
            f"定义里有但代码里没有的节点：{diff['missing_in_code']}"
        )
        assert sorted(wf.node_ids()) == compiled_node_ids

    def test_node_count_is_twelve(self, compiled_node_ids: list[str]) -> None:
        """真实图有 12 个业务节点；数量变化必须是有意识的改动。"""
        assert len(compiled_node_ids) == 12

    def test_declared_engine_and_implementation(self, wf: WorkflowDefinition) -> None:
        assert wf.engine == "langgraph"
        impl = wf.implementation
        assert impl["module"] == "agent_platform.finance.securities_graph"
        for key in ("builder", "entrypoint", "resume"):
            assert key in impl, f"implementation 缺少 {key}"

    def test_implementation_module_api_exists(self, wf: WorkflowDefinition) -> None:
        """声明的 builder / entrypoint / resume 必须是真实存在的可调用对象。"""
        import agent_platform.finance.securities_graph as sg

        impl = wf.implementation
        for key in ("builder", "entrypoint", "resume"):
            name = impl[key]
            assert hasattr(sg, name), f"securities_graph 没有 {name}"
            assert callable(getattr(sg, name)), f"{name} 不可调用"

    def test_every_node_implementation_resolves(self, wf: WorkflowDefinition) -> None:
        """
        每个节点声明的 implementation 都要能真正 import 到并且可调用。
        这样「节点函数被改名/删除」也会被发现，而不只是节点 key 改名。
        """
        for node_id, target in wf.implementation_targets():
            obj = _resolve(target)
            assert callable(obj), f"节点 {node_id} 的实现 {target} 不可调用"

    # ── 状态定义一致性 ────────────────────────────────────────────────────────

    def test_declared_state_keys_match_typed_dict(self, wf: WorkflowDefinition) -> None:
        """声明的状态键必须与 SecuritiesAnalysisState 的标注完全一致。"""
        from agent_platform.finance.securities_graph import SecuritiesAnalysisState

        actual = sorted(SecuritiesAnalysisState.__annotations__)
        declared = sorted(k["name"] for k in wf.raw["state"]["keys"])
        assert declared == actual, (
            f"状态键漂移：定义多出 {set(declared) - set(actual)}，"
            f"缺少 {set(actual) - set(declared)}"
        )

    def test_state_class_name_matches(self, wf: WorkflowDefinition) -> None:
        from agent_platform.finance.securities_graph import SecuritiesAnalysisState

        assert wf.raw["state"]["name"] == SecuritiesAnalysisState.__name__

    def test_reducer_keys_are_really_annotated_reducers(self, wf: WorkflowDefinition) -> None:
        """
        声明带 reducer 的键（errors / trace_entries）必须在源码里真的用了
        Annotated[..., operator.add]。并行分支同时写同一个键时，只有带 reducer 才安全。
        """
        from agent_platform.finance import securities_graph as sg

        source = _squash(inspect.getsource(sg.SecuritiesAnalysisState))
        declared = [k["name"] for k in wf.raw["state"]["keys"] if k.get("reducer")]
        assert declared, "至少应声明一个带 reducer 的状态键"
        for name in declared:
            assert f"{name}:Annotated[" in source, f"{name} 未使用 Annotated reducer"
            assert "operator.add" in source

    # ── 并行扇出一致性 ────────────────────────────────────────────────────────

    def test_parallel_fanout_matches_source(self, wf: WorkflowDefinition) -> None:
        """
        声明的并行组必须对应源码里真实的 add_edge(START, ...) 与汇聚边。
        这是「START 并行扇出到 4 个分析 Agent」这一结构的硬校验。
        """
        from agent_platform.finance import securities_graph as sg

        source = _squash(inspect.getsource(sg.build_securities_graph))
        groups = wf.raw["parallel_groups"]
        assert len(groups) == 1
        group = groups[0]
        assert group["fan_out_from"] == START_SENTINEL
        assert group["fan_in_to"] == "synthesis_agent"
        assert len(group["members"]) == 4

        for member in group["members"]:
            assert f'add_edge(START,"{member}")' in source, f"源码缺少扇出边 → {member}"
            assert f'add_edge("{member}","synthesis_agent")' in source, (
                f"源码缺少汇聚边 {member} → synthesis_agent"
            )

    def test_parallel_members_equal_start_successors(self, wf: WorkflowDefinition) -> None:
        """定义内部自洽：并行组成员就是 __start__ 的全部后继。"""
        group = wf.raw["parallel_groups"][0]
        assert sorted(group["members"]) == sorted(wf.successors(START_SENTINEL))

    def test_parallel_group_membership_is_marked_on_nodes(self, wf: WorkflowDefinition) -> None:
        group = wf.raw["parallel_groups"][0]
        assert sorted(wf.parallel_group_members(group["id"])) == sorted(group["members"])

    # ── 条件边与路由函数一致性 ────────────────────────────────────────────────

    def test_conditional_edges_match_source(self, wf: WorkflowDefinition) -> None:
        """每条声明的条件边都要在源码里找到对应的 add_conditional_edges 调用。"""
        from agent_platform.finance import securities_graph as sg

        source = _squash(inspect.getsource(sg.build_securities_graph))
        conditional = wf.conditional_edges()
        assert len(conditional) == 5, "真实图有 5 条条件边"
        for edge in conditional:
            _, router_name = split_target(edge.router)
            assert f'add_conditional_edges("{edge.source}",{router_name}' in source, (
                f"源码缺少条件边 {edge.source} → {router_name}"
            )

    def test_router_functions_resolve(self, wf: WorkflowDefinition) -> None:
        for edge in wf.conditional_edges():
            assert callable(_resolve(edge.router)), f"路由函数不可用：{edge.router}"

    @pytest.mark.parametrize(
        "source_node, state, expected",
        [
            # route_after_synthesis：辩论阻断优先，再处理错误和置信度
            (
                "synthesis_agent",
                {"status": "ok", "confidence": 0.9, "synthesis": {"debate_blocked": True}},
                "debate_approval",
            ),
            ("synthesis_agent", {"status": "error", "confidence": 0.9}, END_SENTINEL),
            ("synthesis_agent", {"status": "ok", "confidence": 0.1}, "no_trade"),
            ("synthesis_agent", {"status": "ok", "confidence": 0.3}, "no_trade"),
            ("synthesis_agent", {"status": "ok", "confidence": 0.9}, "trader_agent"),
            # route_after_debate_approval
            ("debate_approval", {"final_action": "block", "confidence": 0.9}, END_SENTINEL),
            ("debate_approval", {"final_action": "execute", "confidence": 0.3}, "no_trade"),
            ("debate_approval", {"final_action": "execute", "confidence": 0.9}, "trader_agent"),
            # route_after_trader：HAR 优先于 error
            ("trader_agent", {"har_required": True, "status": "error"}, "human_approval"),
            ("trader_agent", {"har_required": False, "status": "error"}, END_SENTINEL),
            ("trader_agent", {"har_required": False, "status": "ok"}, "risk_manager"),
            # route_after_human_approval
            ("human_approval", {"final_action": "block"}, END_SENTINEL),
            ("human_approval", {"final_action": "execute"}, "risk_manager"),
            # route_after_preflight 返回分支「取值」，两个取值都映射到 END
            ("trading_harness", {"final_action": "execute"}, "execute"),
            ("trading_harness", {"final_action": "block"}, "block"),
        ],
    )
    def test_router_live_return_is_a_declared_branch_value(
        self, wf: WorkflowDefinition, source_node: str, state: dict, expected: str
    ) -> None:
        """
        实际调用真实路由函数，断言：
          1. 返回值等于预期分支
          2. 该返回值确实是定义里为这条边声明的某个 branch value
        路由逻辑改了而 JSON 没改，这条会失败。
        """
        edge = next(e for e in wf.conditional_edges() if e.source == source_node)
        router = _resolve(edge.router)
        result = router(state)
        assert result == expected, f"{source_node} 路由结果与预期不符"
        declared_values = [b.value for b in edge.branches]
        assert result in declared_values, (
            f"路由返回 {result!r} 未在定义的分支取值 {declared_values} 中"
        )

    def test_all_reachable_branches_are_exercised(self, wf: WorkflowDefinition) -> None:
        """
        标记为 reachable 的分支，必须都被上面的实调用测试覆盖到；
        标记 reachable=false 的分支必须给出说明，防止用它掩盖漏测。
        """
        exercised = {
            ("synthesis_agent", "debate_approval"),
            ("synthesis_agent", END_SENTINEL),
            ("synthesis_agent", "no_trade"),
            ("synthesis_agent", "trader_agent"),
            ("debate_approval", END_SENTINEL),
            ("debate_approval", "no_trade"),
            ("debate_approval", "trader_agent"),
            ("trader_agent", "human_approval"),
            ("trader_agent", END_SENTINEL),
            ("trader_agent", "risk_manager"),
            ("human_approval", END_SENTINEL),
            ("human_approval", "risk_manager"),
            ("trading_harness", "execute"),
            ("trading_harness", "block"),
        }
        for edge in wf.conditional_edges():
            for branch in edge.branches:
                key = (edge.source, branch.value)
                if branch.reachable:
                    assert key in exercised, f"可达分支未被实测覆盖：{key}"
                else:
                    assert branch.when, f"不可达分支 {key} 必须写明原因"

    def test_unreachable_branch_is_the_defensive_end_mapping(
        self, wf: WorkflowDefinition
    ) -> None:
        """
        唯一一个 reachable=false 的分支是 trading_harness 的 END 兜底映射：
        route_after_preflight 只会返回 execute / block，永远不会返回 END。
        把它诚实标成不可达，而不是从定义里删掉。
        """
        unreachable = [
            (e.source, b.value)
            for e in wf.conditional_edges()
            for b in e.branches
            if not b.reachable
        ]
        assert unreachable == [("trading_harness", END_SENTINEL)]

        from agent_platform.finance.securities_graph import route_after_preflight

        returns = {
            route_after_preflight({"final_action": action})
            for action in ("execute", "block", "unknown", "")
        }
        assert returns == {"execute", "block"}, "兜底分支意外变为可达，定义需更新"

    # ── 中断点一致性 ──────────────────────────────────────────────────────────

    def test_interrupt_nodes_match_functions_calling_interrupt(
        self, wf: WorkflowDefinition
    ) -> None:
        """
        声明的中断点必须与源码中真正调用 interrupt() 的节点函数集合完全一致。
        少声明会漏掉人工审批点，多声明会误导运维。
        """
        from agent_platform.finance import securities_graph as sg

        actual: list[str] = []
        for node_id, target in wf.implementation_targets():
            func = _resolve(target)
            body = inspect.getsource(func)
            # 去掉注释与文档字符串影响：只看是否存在实际调用形式
            if re.search(r"=\s*interrupt\s*\(", body) or re.search(r"\binterrupt\s*\(\s*\{", body):
                actual.append(node_id)

        assert sorted(wf.interrupt_nodes()) == sorted(actual), (
            f"中断点漂移：声明 {sorted(wf.interrupt_nodes())}，实际 {sorted(actual)}"
        )
        assert sorted(actual) == ["debate_approval", "human_approval", "trading_harness"]
        assert sg.interrupt is not None  # 确认 interrupt 已在模块中导入

    def test_interrupt_entries_are_well_formed(self, wf: WorkflowDefinition) -> None:
        entries = {i["node"]: i for i in wf.raw["interrupts"]}
        assert set(entries) == {"debate_approval", "human_approval", "trading_harness"}
        assert entries["debate_approval"]["payload_type"] == "debate_review"
        assert entries["human_approval"]["payload_type"] == "har_approval"
        assert entries["trading_harness"]["conditional"] is True, (
            "trading_harness 只在 manual_review 时中断，属条件性中断"
        )
        for entry in entries.values():
            assert entry["resume_values"], "必须声明可用的恢复取值"

    def test_interrupt_requires_checkpoint(self, wf: WorkflowDefinition) -> None:
        """有中断点就必须启用 checkpoint，否则无法 resume。"""
        checkpoint = wf.raw["checkpoint"]
        assert checkpoint["enabled"] is True
        assert checkpoint["required_for_interrupt"] is True

    def test_checkpoint_savers_are_importable(self, wf: WorkflowDefinition) -> None:
        """声明的 checkpointer 必须真实存在。SqliteSaver 属可选依赖，缺失则跳过。"""
        checkpoint = wf.raw["checkpoint"]
        assert callable(_resolve(checkpoint["default_saver"]))
        try:
            assert callable(_resolve(checkpoint["production_saver"]))
        except (ImportError, ModuleNotFoundError):
            pytest.skip("SqliteSaver 为可选依赖，当前环境未安装")

    def test_builder_accepts_checkpointer_argument(self, wf: WorkflowDefinition) -> None:
        """声明的 checkpoint 注入点必须与 builder 的真实签名相符。"""
        from agent_platform.finance.securities_graph import build_securities_graph

        params = inspect.signature(build_securities_graph).parameters
        assert "checkpointer" in params
        assert "checkpointer" in wf.raw["checkpoint"]["injection_point"]

    # ── 阈值常量一致性 ────────────────────────────────────────────────────────

    def test_declared_parameters_match_real_constants(self, wf: WorkflowDefinition) -> None:
        """
        声明的参数值必须等于 source_ref 指向的真实常量。
        改了阈值而没改文档，这条会失败 —— 阈值漂移是最难靠人眼发现的一类。
        """
        parameters = wf.raw["parameters"]
        assert parameters, "证券工作流应声明关键阈值参数"
        for param in parameters:
            source_ref = param.get("source_ref")
            assert source_ref, f"参数 {param['name']} 缺少 source_ref"
            actual = _resolve(source_ref)
            assert actual == param["value"], (
                f"参数 {param['name']} 漂移：定义 {param['value']}，代码 {actual}"
            )

    def test_low_confidence_threshold_drives_no_trade_branch(
        self, wf: WorkflowDefinition
    ) -> None:
        """阈值不只是数字对得上，还要真的控制 no_trade 分支的边界行为。"""
        from agent_platform.finance.securities_graph import route_after_synthesis

        threshold = wf.parameter("low_confidence_threshold")["value"]
        assert route_after_synthesis({"status": "ok", "confidence": threshold}) == "no_trade"
        assert (
            route_after_synthesis({"status": "ok", "confidence": threshold + 0.01})
            == "trader_agent"
        )

    def test_terminal_statuses_appear_in_source(self, wf: WorkflowDefinition) -> None:
        """声明的终态 status 取值必须真的出现在源码里，不能是编出来的。"""
        from agent_platform.finance import securities_graph as sg

        source = inspect.getsource(sg)
        for status in wf.raw["terminal_statuses"]:
            assert f'"{status}"' in source, f"终态 {status!r} 在源码中不存在"

    # ── Guardrail 声明 ────────────────────────────────────────────────────────

    def test_declared_guardrail_classes_exist(self, wf: WorkflowDefinition) -> None:
        """
        声明引用的 Guardrail 类必须真实存在。
        本测试只做存在性核对，不修改也不绕过任何 Guardrail。
        """
        guardrails = wf.raw["guardrails"]
        assert guardrails, "证券工作流应声明其依赖的 Guardrail"
        for item in guardrails:
            obj = _resolve(item["class"])
            assert inspect.isclass(obj), f"{item['class']} 不是类"
            for node_id in item.get("applies_to", []):
                assert wf.has_node(node_id), f"Guardrail 指向不存在的节点 {node_id}"


# ══════════════════════════════════════════════════════════════════════════════
# 第三层 代码层：天气工作流声明 vs examples/weather_analysis 真实实现
# ══════════════════════════════════════════════════════════════════════════════

class TestWeatherWorkflowMatchesCode:
    """
    天气 Demo 是平台可移植性的证据（P-05）：同一套 Guardrail 机制用在非金融场景。
    它是 Harness 顺序管道而非 LangGraph 图，定义如实标为 harness_sequence。
    """

    @pytest.fixture(scope="class")
    def wf(self) -> WorkflowDefinition:
        return load_workflow("weather_analysis")

    def test_engine_is_harness_sequence(self, wf: WorkflowDefinition) -> None:
        """不把顺序管道伪装成 LangGraph 图 —— 声明要如实反映实现方式。"""
        assert wf.engine == "harness_sequence"
        assert wf.raw["checkpoint"]["enabled"] is False

    def test_implementation_targets_resolve(self, wf: WorkflowDefinition) -> None:
        for node_id, target in wf.implementation_targets():
            obj = _resolve(target)
            assert obj is not None, f"节点 {node_id} 的实现 {target} 无法解析"

    def test_guardrail_classes_match_agent_order(self, wf: WorkflowDefinition) -> None:
        """
        声明的 Guardrail 顺序必须与 WeatherAnalysisAgent._guardrails 的真实顺序一致。
        Guardrail 是有序管道，顺序错了语义就错了。
        """
        from weather_agent import WeatherAnalysisAgent

        actual = [type(g).__name__ for g in WeatherAnalysisAgent()._guardrails]
        declared = [split_target(g["class"])[1] for g in wf.raw["guardrails"]]
        assert declared == actual, f"Guardrail 顺序漂移：定义 {declared}，实际 {actual}"

    def test_guardrail_nodes_follow_declared_order(self, wf: WorkflowDefinition) -> None:
        """Guardrail 节点在图上的先后顺序也要和管道顺序一致。"""
        node_order = [n.id for n in wf.nodes if n.type == "guardrail"]
        applies = [g["applies_to"][0] for g in wf.raw["guardrails"]]
        assert node_order == applies

    def test_output_schema_fields_match_real_schema(self, wf: WorkflowDefinition) -> None:
        """声明的输出字段必须与 WEATHER_REPORT_SCHEMA 的 required 完全一致。"""
        from weather_agent import WEATHER_REPORT_SCHEMA

        actual = WEATHER_REPORT_SCHEMA["required"]
        assert wf.raw["output_schema_fields"] == actual, (
            f"输出字段漂移：定义 {wf.raw['output_schema_fields']}，实际 {actual}"
        )

    def test_output_fields_match_report_dataclass(self, wf: WorkflowDefinition) -> None:
        from weather_agent import WeatherReport

        assert sorted(wf.raw["output_schema_fields"]) == sorted(
            WeatherReport.__dataclass_fields__
        )

    def test_min_data_points_matches_behaviour(self, wf: WorkflowDefinition) -> None:
        """
        声明 min_data_points=2，就要证明少于 2 个点时真的抛错（而非返回残缺结果）。
        """
        from weather_agent import WeatherAnalysisAgent

        minimum = wf.parameter("min_data_points")["value"]
        assert minimum == 2
        agent = WeatherAnalysisAgent()
        with pytest.raises(ValueError):
            agent.analyze("北京", [20.0] * (minimum - 1))
        report = agent.analyze("北京", [20.0, 22.0])
        assert report.period_days == 2

    def test_trend_threshold_matches_behaviour(self, wf: WorkflowDefinition) -> None:
        """
        用跨阈值样本反推声明的 trend_threshold_c：
        后半段均温 − 前半段均温的绝对值需**超过**阈值才判升温/降温。
        差值 0.9 < 1.0 → stable；差值 1.2 > 1.0 → warming / cooling。
        """
        from weather_agent import WeatherAnalysisAgent

        threshold = wf.parameter("trend_threshold_c")["value"]
        assert threshold == 1.0
        agent = WeatherAnalysisAgent()

        below = agent.analyze("样例城市", [20.0, 20.0, 20.9, 20.9])
        assert below.trend == "stable", "差值未超阈值应判为平稳"

        warming = agent.analyze("样例城市", [20.0, 20.0, 21.2, 21.2])
        assert warming.trend == "warming"

        cooling = agent.analyze("样例城市", [21.2, 21.2, 20.0, 20.0])
        assert cooling.trend == "cooling"

    def test_declared_trend_values_cover_real_labels(self, wf: WorkflowDefinition) -> None:
        """classify_trend 节点声明的产出取值要覆盖真实的三种趋势标签。"""
        raw_text = json.dumps(wf.raw, ensure_ascii=False)
        for label in ("warming", "cooling", "stable"):
            assert label in raw_text, f"定义中未提及趋势取值 {label}"

    def test_verified_by_references_existing_tests(self, wf: WorkflowDefinition) -> None:
        """
        参数上的 verified_by 必须指向真实存在的测试，否则就是空头承诺。
        只校验文件与测试函数名存在，不做完整 nodeid 解析。
        """
        for param in wf.raw["parameters"]:
            ref = param.get("verified_by")
            if not ref:
                continue
            file_part = ref.split("::")[0]
            test_file = _ROOT / file_part
            assert test_file.is_file(), f"verified_by 指向不存在的文件：{file_part}"
            content = test_file.read_text(encoding="utf-8")
            for segment in ref.split("::")[1:]:
                name = re.split(r"[（(\s]", segment)[0]
                assert f"{name}" in content, f"{file_part} 中找不到 {name}"

    def test_no_langgraph_dependency_declared(self, wf: WorkflowDefinition) -> None:
        """
        天气 Demo 证明 Guardrail 层不依赖 LangGraph：
        定义里不应出现 langgraph 相关实现引用。
        """
        for _, target in wf.implementation_targets():
            assert "langgraph" not in target


# ══════════════════════════════════════════════════════════════════════════════
# 文档
# ══════════════════════════════════════════════════════════════════════════════

def test_frontend_reuses_localized_status_badge_after_state_refresh() -> None:
    frontend = (_ROOT / "frontend_prototype.html").read_text(encoding="utf-8")
    start = frontend.index("function updateStatusCard")
    body = frontend[start:start + 2200]
    assert "updateStatusBadge(data.status || 'not_found')" in body
    assert "badge.textContent = data.status" not in body


class TestWorkflowReadme:
    """Workflow/README.md 必须存在并解释清楚每个文件的用途。"""

    def test_readme_exists(self) -> None:
        readme = WORKFLOW_DIR / "README.md"
        assert readme.is_file(), "缺少 Workflow/README.md"
        assert readme.stat().st_size > 0

    def test_readme_mentions_every_file(self) -> None:
        content = (WORKFLOW_DIR / "README.md").read_text(encoding="utf-8")
        for name in (
            "workflow.schema.json",
            "securities_analysis.workflow.json",
            "weather_analysis.workflow.json",
        ):
            assert name in content, f"README 未说明 {name}"

    def test_readme_documents_loading(self) -> None:
        content = (WORKFLOW_DIR / "README.md").read_text(encoding="utf-8")
        assert "load_workflow" in content, "README 应说明如何加载"
        assert "securities_graph" in content, "README 应说明与真实实现的对应关系"
