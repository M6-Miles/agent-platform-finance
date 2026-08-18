"""本地和远程 Skill 市场：搜索、哈希校验和用户级安装。"""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from agent_platform.core.skill_registry import SkillRegistry, SkillValidationError, get_user_skill_registry


_REMOTE_CACHE: dict[str, tuple[float, list["CatalogItem"], str | None]] = {}
_REMOTE_CACHE_TTL_S = 300.0
_GITHUB_CACHE: dict[str, tuple[float, list["CatalogItem"], str | None]] = {}
_GITHUB_CACHE_TTL_S = 1800.0
_GITHUB_SOURCE_RE = re.compile(r"^(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\|(?P<path>[A-Za-z0-9_./-]+)$")


@dataclass(frozen=True, slots=True)
class CatalogItem:
    item_id: str
    name: str
    version: str
    description: str
    permissions: tuple[str, ...]
    source: str
    sha256: str
    author: str = "项目维护者"
    download_url: str | None = None
    registry_url: str | None = None
    kind: str = "plugin"
    instructions_url: str | None = None
    commit_sha: str = ""
    git_blob_sha: str = ""

    @property
    def remote(self) -> bool:
        return bool(self.download_url)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.item_id, "name": self.name, "version": self.version,
            "description": self.description, "permissions": list(self.permissions),
            "source": self.source, "sha256": self.sha256, "author": self.author,
            "remote": self.remote,
            "kind": self.kind,
            "integrity": f"git:{self.git_blob_sha}" if self.git_blob_sha else "sha256",
        }


def package_sha256(directory: Path) -> str:
    """对目录中的相对路径和文件内容做稳定哈希。"""
    digest = hashlib.sha256()
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        relative = path.relative_to(directory).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _archive_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _valid_url(value: str, *, allow_http_localhost: bool = False) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" or (
        allow_http_localhost and parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
    )


class SkillMarketplace:
    def __init__(
        self, catalog_path: Path, user_skills_dir: Path,
        remote_urls: tuple[str, ...] = (), github_sources: tuple[str, ...] = (),
    ) -> None:
        self.catalog_path = catalog_path.resolve()
        self.root = self.catalog_path.parent.parent.resolve()
        self.user_skills_dir = user_skills_dir.resolve()
        self.remote_urls = tuple(url.rstrip("/") for url in remote_urls if url.strip())
        self.github_sources = tuple(source.strip() for source in github_sources if source.strip())
        self.remote_errors: list[str] = []

    def _local_catalog(self) -> list[CatalogItem]:
        raw = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("Skill 本地目录必须是数组")
        items: list[CatalogItem] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            source = Path(str(item.get("source", "")))
            source_path = (self.root / source).resolve()
            sha = str(item.get("sha256", "")).lower()
            if not source_path.is_dir() or not source_path.is_relative_to(self.root):
                continue
            if not re.fullmatch(r"[0-9a-f]{64}", sha):
                continue
            items.append(CatalogItem(
                item_id=str(item.get("id", "")), name=str(item.get("name", "")),
                version=str(item.get("version", "")), description=str(item.get("description", "")),
                permissions=tuple(str(p) for p in item.get("permissions", [])),
                source=str(source).replace("\\", "/"), sha256=sha,
                author=str(item.get("author", "项目维护者")),
            ))
        return items

    @staticmethod
    def _remote_items(url: str) -> list[CatalogItem]:
        if not _valid_url(url, allow_http_localhost=True):
            raise ValueError(f"远程目录必须使用 HTTPS：{url}")
        request = urllib.request.Request(url, headers={"User-Agent": "AgentPlatform-SkillMarketplace/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise ValueError(f"远程 Skill 目录读取失败：{url}：{exc}") from exc
        entries = raw.get("skills", []) if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            raise ValueError(f"远程 Skill 目录格式错误：{url}")
        parsed: list[CatalogItem] = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            download_url = str(item.get("download_url", ""))
            sha = str(item.get("sha256", "")).lower()
            if not download_url or not re.fullmatch(r"[0-9a-f]{64}", sha):
                continue
            if not _valid_url(download_url, allow_http_localhost=True):
                continue
            item_id = str(item.get("id", ""))
            if not item_id:
                continue
            # Keep catalog IDs stable across backend restarts and workers.
            registry_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
            parsed.append(CatalogItem(
                item_id=f"remote:{registry_id}:{item_id}",
                name=str(item.get("name", item_id)), version=str(item.get("version", "")),
                description=str(item.get("description", "")),
                permissions=tuple(str(p) for p in item.get("permissions", [])),
                source=str(item.get("source", url)), sha256=sha,
                author=str(item.get("author", "远程目录")), download_url=download_url,
                registry_url=url,
            ))
        return parsed

    @staticmethod
    def _read_url(url: str, *, timeout: float = 12, max_bytes: int = 512 * 1024) -> bytes:
        request = urllib.request.Request(
            url, headers={"User-Agent": "AgentPlatform-SkillMarketplace/1.0", "Accept": "application/vnd.github+json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                content = response.read(max_bytes + 1)
        except (OSError, urllib.error.URLError) as exc:
            raise ValueError(f"远程内容读取失败：{exc}") from exc
        if len(content) > max_bytes:
            raise ValueError("远程内容超过大小限制")
        return content

    @staticmethod
    def _github_install_name(repo: str, skill_name: str) -> str:
        owner = repo.split("/", 1)[0].lower()
        prefix = "openai" if owner == "openai" else "anthropic" if owner == "anthropics" else owner
        safe = re.sub(r"[^a-z0-9_-]", "-", skill_name.lower()).strip("-_")
        return f"{prefix}_{safe}"[:64]

    @classmethod
    def _github_items(cls, source: str) -> list[CatalogItem]:
        match = _GITHUB_SOURCE_RE.fullmatch(source)
        if not match:
            raise ValueError(f"GitHub Skill 来源格式错误：{source}")
        repo, base_path = match.group("repo"), match.group("path").strip("/")
        api_base = f"https://api.github.com/repos/{repo}"
        try:
            commit = json.loads(cls._read_url(f"{api_base}/commits/main", max_bytes=1024 * 1024).decode("utf-8"))
            commit_sha = str(commit["sha"])
            tree = json.loads(cls._read_url(
                f"{api_base}/git/trees/{commit_sha}?recursive=1", max_bytes=8 * 1024 * 1024,
            ).decode("utf-8"))
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise ValueError(f"GitHub Skill 目录响应无效：{repo}") from exc
        entries = tree.get("tree", []) if isinstance(tree, dict) else []
        prefix = f"{base_path}/"
        suffix = "/SKILL.md"
        skill_blobs: list[tuple[str, str, str]] = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("type") != "blob":
                continue
            path = str(entry.get("path", ""))
            if not path.startswith(prefix) or not path.endswith(suffix):
                continue
            relative = path[len(prefix):-len(suffix)]
            if not relative or "/" in relative:
                continue
            skill_blobs.append((relative, path, str(entry.get("sha", ""))))

        items: list[CatalogItem] = []
        for name, path, blob_sha in skill_blobs:
            raw_url = f"https://raw.githubusercontent.com/{repo}/{commit_sha}/{path}"
            installed_name = cls._github_install_name(repo, name)
            item_key = hashlib.sha256(f"{repo}:{path}".encode("utf-8")).hexdigest()[:16]
            author = "OpenAI 官方" if repo.lower() == "openai/skills" else "Anthropic 官方" if repo.lower() == "anthropics/skills" else repo
            items.append(CatalogItem(
                item_id=f"github:{item_key}", name=installed_name,
                version=commit_sha[:12], description=f"{name.replace('-', ' ')} 官方 Agent 工作流程",
                permissions=("llm",), source=f"https://github.com/{repo}/tree/{commit_sha}/{path}",
                sha256="", author=author, download_url=raw_url,
                registry_url=f"https://github.com/{repo}", kind="instruction",
                instructions_url=raw_url, commit_sha=commit_sha, git_blob_sha=blob_sha,
            ))
        return items

    def _catalog(self) -> list[CatalogItem]:
        items = self._local_catalog()
        errors: list[str] = []
        for url in self.remote_urls:
            cached = _REMOTE_CACHE.get(url)
            if cached and time.monotonic() - cached[0] < _REMOTE_CACHE_TTL_S:
                remote_items, error = cached[1], cached[2]
            else:
                try:
                    remote_items, error = self._remote_items(url), None
                except ValueError as exc:
                    remote_items, error = [], str(exc)
                _REMOTE_CACHE[url] = (time.monotonic(), remote_items, error)
            items.extend(remote_items)
            if error:
                errors.append(error)
        for source in self.github_sources:
            cached = _GITHUB_CACHE.get(source)
            if cached and time.monotonic() - cached[0] < _GITHUB_CACHE_TTL_S:
                github_items, error = cached[1], cached[2]
            else:
                try:
                    github_items, error = self._github_items(source), None
                except ValueError as exc:
                    github_items, error = [], str(exc)
                _GITHUB_CACHE[source] = (time.monotonic(), github_items, error)
            items.extend(github_items)
            if error:
                errors.append(error)
        self.remote_errors = errors
        if errors and not items:
            raise ValueError("；".join(errors))
        return items

    def search(self, query: str = "") -> list[CatalogItem]:
        q = query.strip().lower()
        return [item for item in self._catalog() if not q or q in " ".join((item.item_id, item.name, item.description, item.author)).lower()]

    def get(self, item_id: str) -> CatalogItem:
        for item in self._catalog():
            if item.item_id == item_id:
                return item
        raise KeyError(f"Skill 市场中不存在：{item_id}")

    def _local_source(self, item: CatalogItem) -> Path:
        source = (self.root / item.source).resolve()
        if not source.is_relative_to(self.root) or not source.is_dir():
            raise ValueError("Skill 来源路径不安全")
        actual = package_sha256(source)
        if actual != item.sha256:
            raise ValueError(f"SHA256 校验失败：目录声明 {item.sha256}，实际 {actual}")
        return source

    @staticmethod
    def _safe_extract(content: bytes, destination: Path) -> Path:
        with zipfile.ZipFile(io.BytesIO(content)) as bundle:
            for member in bundle.infolist():
                target = (destination / member.filename).resolve()
                if not target.is_relative_to(destination.resolve()):
                    raise ValueError("Skill 压缩包包含越界路径")
            bundle.extractall(destination)
        candidates = list(destination.glob("*/skill.json"))
        if len(candidates) != 1:
            raise ValueError("Skill 压缩包必须包含一个顶层目录和 skill.json")
        return candidates[0].parent

    def _remote_source(self, item: CatalogItem, destination: Path) -> Path:
        if not item.download_url or not item.registry_url:
            raise ValueError("远程 Skill 缺少下载地址")
        registry_host = urlparse(item.registry_url).netloc
        download_host = urlparse(item.download_url).netloc
        if registry_host != download_host:
            raise ValueError("下载地址域名必须与目录来源一致")
        request = urllib.request.Request(item.download_url, headers={"User-Agent": "AgentPlatform-SkillMarketplace/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                content = response.read(20 * 1024 * 1024 + 1)
        except (OSError, urllib.error.URLError) as exc:
            raise ValueError(f"Skill 下载失败：{exc}") from exc
        if len(content) > 20 * 1024 * 1024:
            raise ValueError("Skill 压缩包超过 20MB 限制")
        actual = _archive_sha256(content)
        if actual != item.sha256:
            raise ValueError(f"SHA256 校验失败：目录声明 {item.sha256}，实际 {actual}")
        return self._safe_extract(content, destination)

    def _instruction_source(self, item: CatalogItem, destination: Path) -> Path:
        if not item.instructions_url or not item.commit_sha:
            raise ValueError("说明型 Skill 缺少固定版本下载地址")
        content = self._read_url(item.instructions_url, timeout=20, max_bytes=256 * 1024)
        actual = hashlib.sha256(content).hexdigest()
        if item.sha256 and actual != item.sha256:
            raise ValueError(f"SHA256 校验失败：目录声明 {item.sha256}，实际 {actual}")
        if item.git_blob_sha:
            git_content = f"blob {len(content)}\0".encode("ascii") + content
            actual_blob = hashlib.sha1(git_content).hexdigest()
            if actual_blob != item.git_blob_sha:
                raise ValueError("Git 对象完整性校验失败")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Skill 说明文件不是 UTF-8") from exc
        target = destination / item.name
        target.mkdir()
        (target / "SKILL.md").write_text(text, encoding="utf-8")
        manifest = {
            "name": item.name, "version": item.version, "description": item.description,
            "kind": "instruction", "instructions_file": "SKILL.md",
            "permissions": list(item.permissions), "allowed_agents": ["chat_agent"],
            "source_url": item.source, "sha256": actual,
        }
        (target / "skill.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return target

    def user_items(self, user_id: str) -> list[dict[str, Any]]:
        installed = {record.manifest.name: record for record in get_user_skill_registry(self.user_skills_dir, user_id).list()}
        result = []
        for item in self._catalog():
            record = installed.get(item.name)
            value = item.to_dict()
            if record is not None and not value["sha256"]:
                value["sha256"] = record.manifest.sha256
            value.update({"installed": record is not None, "enabled": record.enabled if record else False, "status": record.status if record else "not_installed"})
            result.append(value)
        return result

    def install(self, item_id: str, user_id: str) -> dict[str, Any]:
        item = self.get(item_id)
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", item.name):
            raise ValueError("Skill 目录名称非法")
        base = self.user_skills_dir.resolve()
        target_root = (base / re.sub(r"[^a-zA-Z0-9_-]", "_", str(user_id))).resolve()
        target = (target_root / item.name).resolve()
        if not target.is_relative_to(base):
            raise ValueError("用户 Skill 安装路径越界")
        self.user_skills_dir.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="skill-market-", dir=str(self.user_skills_dir.parent)) as temp:
            staging_root = Path(temp)
            if item.kind == "instruction":
                staging = self._instruction_source(item, staging_root)
            elif item.remote:
                staging = self._remote_source(item, staging_root)
            else:
                staging = staging_root / item.name
                shutil.copytree(self._local_source(item), staging)
            records = SkillRegistry(staging_root).list()
            if len(records) != 1 or records[0].status != "ready":
                raise SkillValidationError(records[0].error if records else "Skill 校验失败")
            manifest = records[0].manifest
            if manifest.name != item.name or manifest.version != item.version:
                raise SkillValidationError("市场目录与 Skill 清单的名称或版本不一致")
            if set(manifest.permissions) != set(item.permissions):
                raise SkillValidationError("市场目录与 Skill 清单的权限不一致")
            target_root.mkdir(parents=True, exist_ok=True)
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(staging, target)
        record = next(record for record in get_user_skill_registry(self.user_skills_dir, user_id).list() if record.manifest.name == item.name)
        return {"catalog": item.to_dict(), "installed": True, "enabled": record.enabled, "status": record.status}

    def delete(self, name: str, user_id: str) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", name):
            raise ValueError("Skill 名称非法")
        base = self.user_skills_dir.resolve()
        user_dir = (base / re.sub(r"[^a-zA-Z0-9_-]", "_", str(user_id))).resolve()
        target = (user_dir / name).resolve()
        if target == user_dir or not target.is_relative_to(user_dir):
            raise ValueError("用户 Skill 删除路径越界")
        if target.exists():
            shutil.rmtree(target)
