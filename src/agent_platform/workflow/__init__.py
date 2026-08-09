"""
工作流定义层
============
把 `Workflow/*.workflow.json` 从"给人看的文档"升级为"可被代码加载、校验、断言的对象"。

对外导出 loader 的主要入口，调用方无需关心内部模块划分::

    from agent_platform.workflow import load_workflow

    wf = load_workflow("securities_analysis")
    print(wf.node_ids())
"""
from __future__ import annotations

from agent_platform.workflow.loader import (
    BranchSpec,
    EdgeSpec,
    NodeSpec,
    WorkflowDefinition,
    WorkflowValidationError,
    available_workflows,
    default_workflow_dir,
    lint_definition,
    load_schema,
    load_workflow,
    load_workflow_file,
    validate_against_schema,
)

__all__ = [
    "BranchSpec",
    "EdgeSpec",
    "NodeSpec",
    "WorkflowDefinition",
    "WorkflowValidationError",
    "available_workflows",
    "default_workflow_dir",
    "lint_definition",
    "load_schema",
    "load_workflow",
    "load_workflow_file",
    "validate_against_schema",
]
