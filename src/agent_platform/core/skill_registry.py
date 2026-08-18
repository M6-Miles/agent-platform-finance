"""可插拔 Skill 注册中心。

Skill 是放在项目 ``skills/`` 目录中的受控插件包。每个包必须包含
``skill.json``。本地插件还需要 Python 入口函数；从官方仓库安装的说明型
Skill 只包含 ``SKILL.md``，只作为受限上下文提供给 Agent，不执行其脚本。

这里的插件是“受信任的本地代码”，不是任意远程代码沙箱。生产环境仍应
只安装经过审查和签名的 Skill；交易、Broker、订单和数据库写入权限在清单
层面被明确禁止。
"""
from __future__ import annotations

import ast
import importlib.util
import json
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from agent_platform.core.tools import RegisteredTool, ToolRegistry


_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
_ALLOWED_PERMISSIONS = {"network", "filesystem_read", "llm"}
_FORBIDDEN_IMPORTS = {
    "subprocess", "socket", "ctypes", "pickle", "marshal", "sqlite3",
    "importlib", "multiprocessing", "signal",
}
_FORBIDDEN_PERMISSION_WORDS = {"broker", "trading", "orders", "database_write"}


class SkillValidationError(ValueError):
    """Skill 清单或入口不符合平台约束。"""


@dataclass(frozen=True, slots=True)
class SkillManifest:
    name: str
    version: str
    description: str
    entrypoint: str = ""
    permissions: tuple[str, ...] = ()
    allowed_agents: tuple[str, ...] = ()
    kind: str = "plugin"
    instructions_file: str = ""
    source_url: str = ""
    sha256: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SkillManifest":
        required = ("name", "version", "description")
        missing = [key for key in required if not isinstance(raw.get(key), str) or not raw[key].strip()]
        if missing:
            raise SkillValidationError(f"缺少必填字段：{', '.join(missing)}")
        name = raw["name"].strip()
        if not _NAME_RE.fullmatch(name):
            raise SkillValidationError("name 必须是 2-64 位小写字母、数字、下划线或连字符")
        permissions = tuple(str(item).strip().lower() for item in raw.get("permissions", []))
        unknown = set(permissions) - _ALLOWED_PERMISSIONS
        forbidden = set(permissions) & _FORBIDDEN_PERMISSION_WORDS
        if unknown:
            raise SkillValidationError(f"不支持的权限：{', '.join(sorted(unknown))}")
        if forbidden:
            raise SkillValidationError(f"禁止的权限：{', '.join(sorted(forbidden))}")
        agents = tuple(str(item).strip() for item in raw.get("allowed_agents", []))
        if not all(agents):
            raise SkillValidationError("allowed_agents 不能包含空值")
        kind = str(raw.get("kind", "plugin")).strip().lower()
        if kind not in {"plugin", "instruction"}:
            raise SkillValidationError("kind 只能是 plugin 或 instruction")
        entrypoint = str(raw.get("entrypoint", "")).strip()
        instructions_file = str(raw.get("instructions_file", "")).strip()
        if kind == "plugin":
            if ":" not in entrypoint:
                raise SkillValidationError("entrypoint 必须是 module:function 格式")
            module, function = entrypoint.split(":", 1)
            if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_.]*", module) or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", function):
                raise SkillValidationError("entrypoint 包含非法模块或函数名")
        elif not re.fullmatch(r"[a-zA-Z0-9_.-]+\.md", instructions_file):
            raise SkillValidationError("说明型 Skill 必须提供目录内的 instructions_file")
        sha256 = str(raw.get("sha256", "")).strip().lower()
        if sha256 and not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise SkillValidationError("sha256 必须是 64 位小写十六进制")
        return cls(
            name, raw["version"].strip(), raw["description"].strip(), entrypoint,
            permissions, agents, kind, instructions_file,
            str(raw.get("source_url", "")).strip(), sha256,
        )


@dataclass(frozen=True, slots=True)
class SkillRecord:
    manifest: SkillManifest
    path: str
    enabled: bool
    status: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self.manifest)
        result.update({"path": self.path, "enabled": self.enabled, "status": self.status, "error": self.error})
        result["permissions"] = list(self.manifest.permissions)
        result["allowed_agents"] = list(self.manifest.allowed_agents)
        return result


class SkillRegistry:
    def __init__(self, skills_dir: Path, state_path: Path | None = None) -> None:
        self.skills_dir = skills_dir.resolve()
        self.state_path = (state_path or skills_dir / ".state.json").resolve()
        self._lock = threading.RLock()
        self._records: dict[str, SkillRecord] = {}
        self._handlers: dict[str, Callable[..., Any]] = {}
        self._instructions: dict[str, str] = {}
        self.discover()

    def _read_state(self) -> dict[str, bool]:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            return {str(key): bool(value) for key, value in raw.items()}
        except (OSError, json.JSONDecodeError, AttributeError):
            return {}

    def _write_state(self, state: dict[str, bool]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.state_path.with_suffix(".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.state_path)

    @staticmethod
    def _check_source(source: Path) -> None:
        try:
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        except (OSError, SyntaxError) as exc:
            raise SkillValidationError(f"入口文件无法解析：{exc}") from exc
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".", 1)[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".", 1)[0]]
            else:
                continue
            forbidden = set(names) & _FORBIDDEN_IMPORTS
            if forbidden:
                raise SkillValidationError(f"入口禁止导入：{', '.join(sorted(forbidden))}")

    def _load_handler(self, directory: Path, manifest: SkillManifest) -> Callable[..., Any]:
        module_name, function_name = manifest.entrypoint.split(":", 1)
        relative = Path(*module_name.split("."))
        source = directory / f"{relative}.py"
        if not source.is_file() or not source.resolve().is_relative_to(directory.resolve()):
            raise SkillValidationError(f"找不到入口文件：{source.name}")
        self._check_source(source)
        unique_name = f"agent_platform_dynamic_skill_{manifest.name}_{manifest.version.replace('.', '_')}"
        spec = importlib.util.spec_from_file_location(unique_name, source)
        if spec is None or spec.loader is None:
            raise SkillValidationError("无法创建 Skill 加载器")
        module: ModuleType = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        handler = getattr(module, function_name, None)
        if not callable(handler):
            raise SkillValidationError(f"入口函数不存在或不可调用：{manifest.entrypoint}")
        return handler

    def discover(self) -> list[SkillRecord]:
        with self._lock:
            self.skills_dir.mkdir(parents=True, exist_ok=True)
            state = self._read_state()
            records: dict[str, SkillRecord] = {}
            handlers: dict[str, Callable[..., Any]] = {}
            instructions: dict[str, str] = {}
            for manifest_path in sorted(self.skills_dir.glob("*/skill.json")):
                directory = manifest_path.parent
                try:
                    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
                    manifest = SkillManifest.from_dict(raw)
                    handler = None
                    instruction_text = ""
                    if manifest.kind == "plugin":
                        handler = self._load_handler(directory, manifest)
                    else:
                        instruction_path = (directory / manifest.instructions_file).resolve()
                        if not instruction_path.is_relative_to(directory.resolve()) or not instruction_path.is_file():
                            raise SkillValidationError("找不到说明文件")
                        if instruction_path.stat().st_size > 256 * 1024:
                            raise SkillValidationError("说明文件超过 256KB 限制")
                        instruction_text = instruction_path.read_text(encoding="utf-8").strip()
                        if not instruction_text:
                            raise SkillValidationError("说明文件不能为空")
                    enabled = state.get(manifest.name, True)
                    records[manifest.name] = SkillRecord(manifest, str(directory), enabled, "ready")
                    if enabled and handler is not None:
                        handlers[manifest.name] = handler
                    if enabled and instruction_text:
                        instructions[manifest.name] = instruction_text
                except (OSError, json.JSONDecodeError, SkillValidationError, ImportError, AttributeError) as exc:
                    name = directory.name
                    safe_name = name if _NAME_RE.fullmatch(name) else f"invalid_{abs(hash(name)) % 10**8}"
                    records[safe_name] = SkillRecord(
                        SkillManifest(name=safe_name, version="unknown", description="无法加载的 Skill", entrypoint="invalid:invalid"),
                        str(directory), False, "invalid", str(exc),
                    )
            self._records = records
            self._handlers = handlers
            self._instructions = instructions
            return list(records.values())

    def list(self) -> list[SkillRecord]:
        with self._lock:
            return list(self._records.values())

    def set_enabled(self, name: str, enabled: bool) -> SkillRecord:
        with self._lock:
            record = self._records.get(name)
            if record is None:
                raise KeyError(f"Skill 不存在：{name}")
            state = self._read_state()
            state[name] = enabled
            self._write_state(state)
            self.discover()
            return self._records[name]

    def register_tools(self, registry: ToolRegistry, agent_name: str = "chat_agent") -> list[str]:
        registered: list[str] = []
        with self._lock:
            for name, handler in self._handlers.items():
                record = self._records[name]
                if record.manifest.allowed_agents and agent_name not in record.manifest.allowed_agents:
                    continue
                tool_name = f"skill_{name}"
                if tool_name in registry.names():
                    continue

                def invoke(_handler=handler, **arguments: Any) -> str:
                    result = _handler(**arguments)
                    return json.dumps(result, ensure_ascii=False, default=str) if not isinstance(result, str) else result

                registry.register(RegisteredTool(tool_name, record.manifest.description, invoke))
                registered.append(tool_name)
        return registered

    def instruction_context(self, agent_name: str = "chat_agent", max_chars: int = 48_000) -> str:
        """返回当前用户启用的说明型 Skill，上下文总量有硬限制。"""
        blocks: list[str] = []
        used = 0
        with self._lock:
            for name, content in self._instructions.items():
                record = self._records[name]
                if record.manifest.allowed_agents and agent_name not in record.manifest.allowed_agents:
                    continue
                header = f"\n\n<user_skill name=\"{name}\">\n"
                footer = "\n</user_skill>"
                remaining = max_chars - used - len(header) - len(footer)
                if remaining <= 0:
                    break
                block = header + content[:remaining] + footer
                blocks.append(block)
                used += len(block)
        return "".join(blocks)

    def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        with self._lock:
            handler = self._handlers.get(name)
            if handler is None:
                raise KeyError(f"Skill 未启用或不存在：{name}")
        return handler(**arguments)


_REGISTRY: SkillRegistry | None = None
_REGISTRY_KEY: tuple[str, str] | None = None


def get_skill_registry(skills_dir: Path, state_path: Path | None = None) -> SkillRegistry:
    global _REGISTRY, _REGISTRY_KEY
    key = (str(skills_dir.resolve()), str((state_path or skills_dir / ".state.json").resolve()))
    if _REGISTRY is None or _REGISTRY_KEY != key:
        _REGISTRY = SkillRegistry(skills_dir, state_path)
        _REGISTRY_KEY = key
    return _REGISTRY


def get_user_skill_registry(base_dir: Path, user_id: str) -> SkillRegistry:
    """返回一个用户私有的 Skill 注册中心。"""
    safe_user_id = re.sub(r"[^a-zA-Z0-9_-]", "_", str(user_id))
    if not safe_user_id or safe_user_id in {".", ".."}:
        raise ValueError("非法用户标识")
    base = base_dir.resolve()
    directory = (base / safe_user_id).resolve()
    if not directory.is_relative_to(base):
        raise ValueError("用户 Skill 路径越界")
    return SkillRegistry(directory, directory / ".state.json")
