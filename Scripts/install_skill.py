"""把本地 Skill 压缩包或目录安装到项目 skills/ 目录。

用法：
  python Scripts/install_skill.py path/to/my_skill.zip
  python Scripts/install_skill.py path/to/my_skill_directory

安装后重启服务，或调用 POST /admin/skills/reload 重新扫描。
"""
from __future__ import annotations

import argparse
import shutil
import tempfile
import zipfile
from pathlib import Path

from agent_platform.core.skill_registry import SkillRegistry, SkillValidationError


def _safe_extract(archive: Path, destination: Path) -> Path:
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        for member in members:
            target = (destination / member.filename).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise ValueError("压缩包包含越界路径，已拒绝安装")
        bundle.extractall(destination)
    candidates = list(destination.glob("*/skill.json"))
    if len(candidates) != 1:
        raise ValueError("压缩包必须包含一个顶层 Skill 目录和 skill.json")
    return candidates[0].parent


def install(source: Path, skills_dir: Path) -> str:
    skills_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="skill-install-") as temp:
        staging = Path(temp)
        if source.is_dir():
            package = staging / source.name
            shutil.copytree(source, package)
        elif source.is_file() and source.suffix.lower() == ".zip":
            package = _safe_extract(source, staging)
        else:
            raise ValueError("输入必须是 Skill 目录或 .zip 压缩包")
        # 使用注册器的同一套清单和入口校验，避免安装后才发现不可加载。
        records = SkillRegistry(staging).discover()
        valid = [item for item in records if item.status == "ready"]
        if len(valid) != 1:
            error = records[0].error if records else "未找到有效 Skill"
            raise SkillValidationError(error or "Skill 校验失败")
        name = valid[0].manifest.name
        target = (skills_dir / name).resolve()
        if not target.is_relative_to(skills_dir.resolve()):
            raise ValueError("Skill 目标路径越界")
        if target.exists():
            raise FileExistsError(f"Skill 已存在：{name}")
        shutil.copytree(package, target)
        return name


def main() -> int:
    parser = argparse.ArgumentParser(description="安装本地 Agent Skill")
    parser.add_argument("source", type=Path)
    parser.add_argument("--skills-dir", type=Path, default=Path("skills"))
    args = parser.parse_args()
    try:
        name = install(args.source.resolve(), args.skills_dir.resolve())
    except (OSError, ValueError, SkillValidationError) as exc:
        parser.error(str(exc))
    print(f"已安装 Skill：{name}")
    print("请重启服务，或由管理员调用 POST /admin/skills/reload。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
