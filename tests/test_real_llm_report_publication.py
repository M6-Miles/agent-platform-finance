from __future__ import annotations

import json
from pathlib import Path

from Scripts.run_real_llm_replay import publish_canonical_reports


def _source_reports(tmp_path: Path, result: dict) -> tuple[Path, Path]:
    json_path = tmp_path / "run.json"
    md_path = tmp_path / "run.md"
    json_path.write_text(json.dumps(result), encoding="utf-8")
    md_path.write_text(f"status={result['status']}", encoding="utf-8")
    return json_path, md_path


def test_skipped_attempt_does_not_publish_success_or_legacy_latest(tmp_path: Path) -> None:
    result = {
        "status": "skipped_no_credentials", "provider_kind": "", "sample_count": 0,
    }
    json_path, md_path = _source_reports(tmp_path, result)
    published = publish_canonical_reports(
        result=result, provider="deepseek", suite="enterprise100",
        json_path=json_path, md_path=md_path, project_root=tmp_path,
    )
    names = {path.name for path in published}
    assert names == {
        "real_llm_replay_deepseek_latest_attempt.json",
        "real_llm_replay_deepseek_latest_attempt.md",
    }


def test_three_task_smoke_cannot_overwrite_enterprise_canonical(tmp_path: Path) -> None:
    result = {"status": "completed", "provider_kind": "real", "sample_count": 3}
    json_path, md_path = _source_reports(tmp_path, result)
    published = publish_canonical_reports(
        result=result, provider="deepseek", suite="default3",
        json_path=json_path, md_path=md_path, project_root=tmp_path,
    )
    names = {path.name for path in published}
    assert "real_llm_replay_deepseek_smoke_latest.json" in names
    assert "real_llm_replay_deepseek_latest.json" not in names
    assert "real_llm_replay_deepseek_final.json" not in names


def test_enterprise100_atomically_publishes_all_canonical_names(tmp_path: Path) -> None:
    result = {"status": "completed", "provider_kind": "real", "sample_count": 100}
    json_path, md_path = _source_reports(tmp_path, result)
    published = publish_canonical_reports(
        result=result, provider="deepseek", suite="enterprise100",
        json_path=json_path, md_path=md_path, project_root=tmp_path,
    )
    names = {path.name for path in published}
    for stem in ("latest_attempt", "latest"):
        assert f"real_llm_replay_deepseek_{stem}.json" in names
        assert f"real_llm_replay_deepseek_{stem}.md" in names
    assert not (tmp_path / "docs" / "experiments" / "real_llm_replay_deepseek_final.json").exists()
    assert not (tmp_path / "docs" / "experiments" / "real_llm_replay_deepseek_final.md").exists()
    latest = json.loads(
        (tmp_path / "docs" / "experiments" / "real_llm_replay_deepseek_latest.json")
        .read_text(encoding="utf-8")
    )
    assert latest["sample_count"] == 100
    assert "task_results" not in latest
    assert latest["archive_json"] == "run.json"
    assert not list((tmp_path / "docs" / "experiments").glob("*.tmp"))
