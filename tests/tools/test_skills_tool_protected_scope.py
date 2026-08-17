from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Literal

import pytest

from agent.delegation_policy import AccessMode, BackingObjectRef, VisibleObjectGrant
from agent.prompt_builder import build_skills_system_prompt
import tools.skills_tool as skills_tool
from tools.delegation_scope import BackingObjectRecord, BackingObjectRegistry


def _write_skill(root, name, description):
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    return skill_dir


def _protected_authority(
    repository: Path,
    *,
    state: str = "active",
    skill_names=(),
    all_skills: bool = False,
    object_type: Literal["file", "directory"] = "directory",
):
    stat_result = repository.lstat()
    backing = BackingObjectRef(
        object_id="repo",
        kind="host_path",
        identity=str(repository),
        revision=f"{stat_result.st_dev}:{stat_result.st_ino}",
    )
    grant = VisibleObjectGrant(
        visible_path=PurePosixPath(str(repository)),
        mode=AccessMode.RW,
        backing=backing,
        object_type=object_type,
    )
    registry = BackingObjectRegistry(
        {
            backing.object_id: BackingObjectRecord(
                backing=backing,
                object_type=object_type,
                trusted_host_path=True,
            )
        }
    )
    return SimpleNamespace(
        state=state,
        invocation_scope=SimpleNamespace(
            visible_objects=(grant,),
            skill_names=frozenset(skill_names),
            all_skills=all_skills,
        ),
        backing_registry=registry,
    )


def test_specific_skill_grant_adds_only_named_skill(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    global_skills = tmp_path / "global-skills"
    selected_dir = _write_skill(global_skills, "selected", "selected skill")
    _write_skill(global_skills, "unselected", "unselected skill")
    (selected_dir / "references").mkdir()
    (selected_dir / "references" / "note.md").write_text(
        "selected note",
        encoding="utf-8",
    )

    authority = _protected_authority(repository, skill_names={"selected"})
    monkeypatch.setattr(
        "tools.delegation_scope.attempt_scope_registry.get",
        lambda task_id: authority if task_id == "protected-attempt" else None,
    )
    monkeypatch.setattr(skills_tool, "_skills_dir", lambda: global_skills)
    monkeypatch.setattr("agent.skill_utils.get_external_skills_dirs", lambda: [])
    skills_tool._SKILLS_CACHE.clear()

    listed = json.loads(skills_tool.skills_list(task_id="protected-attempt"))
    selected = json.loads(
        skills_tool.skill_view(
            "selected",
            file_path="references/note.md",
            task_id="protected-attempt",
            preprocess=False,
        )
    )
    unselected = json.loads(
        skills_tool.skill_view(
            "unselected",
            task_id="protected-attempt",
            preprocess=False,
        )
    )

    assert [skill["name"] for skill in listed["skills"]] == ["selected"]
    assert selected["success"] is True
    assert selected["content"] == "selected note"
    assert unselected["success"] is False


def test_protected_skill_view_never_runs_skill_preprocessing(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    global_skills = tmp_path / "global-skills"
    skill_dir = global_skills / "selected"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: selected\ndescription: selected skill\n---\n"
        "raw {{ shell: printf escaped }}",
        encoding="utf-8",
    )
    authority = _protected_authority(repository, skill_names={"selected"})
    monkeypatch.setattr(
        "tools.delegation_scope.attempt_scope_registry.get",
        lambda task_id: authority if task_id == "protected-attempt" else None,
    )
    monkeypatch.setattr(skills_tool, "_skills_dir", lambda: global_skills)
    monkeypatch.setattr("agent.skill_utils.get_external_skills_dirs", lambda: [])
    preprocess_calls = []

    def _record_preprocess(content, *_args, **_kwargs):
        preprocess_calls.append(content)
        return "PREPROCESSED"

    monkeypatch.setattr(
        "agent.skill_preprocessing.preprocess_skill_content",
        _record_preprocess,
    )

    viewed = json.loads(
        skills_tool.skill_view("selected", task_id="protected-attempt")
    )

    assert viewed["success"] is True
    assert "{{ shell: printf escaped }}" in viewed["content"]
    assert preprocess_calls == []


def test_disabled_skill_is_absent_from_protected_catalog(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    global_skills = tmp_path / "global-skills"
    _write_skill(global_skills, "disabled", "disabled skill")
    authority = _protected_authority(repository, all_skills=True)
    monkeypatch.setattr(
        "tools.delegation_scope.attempt_scope_registry.get",
        lambda task_id: authority if task_id == "protected-attempt" else None,
    )
    monkeypatch.setattr(skills_tool, "_skills_dir", lambda: global_skills)
    monkeypatch.setattr("agent.skill_utils.get_external_skills_dirs", lambda: [])
    monkeypatch.setattr(skills_tool, "_get_disabled_skill_names", lambda: {"disabled"})
    skills_tool._SKILLS_CACHE.clear()

    listed = json.loads(skills_tool.skills_list(task_id="protected-attempt"))
    prompt = build_skills_system_prompt(
        available_tools={"skill_view"},
        task_id="protected-attempt",
    )

    assert listed["skills"] == []
    assert "disabled skill" not in prompt


def test_duplicate_name_is_not_exposed_by_protected_catalog(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    first_root = tmp_path / "first-skills"
    second_root = tmp_path / "second-skills"
    _write_skill(first_root, "duplicate", "first duplicate")
    _write_skill(second_root, "duplicate", "second duplicate")
    authority = _protected_authority(repository, all_skills=True)
    monkeypatch.setattr(
        "tools.delegation_scope.attempt_scope_registry.get",
        lambda task_id: authority if task_id == "protected-attempt" else None,
    )
    monkeypatch.setattr(skills_tool, "_skills_dir", lambda: first_root)
    monkeypatch.setattr(
        "agent.skill_utils.get_external_skills_dirs",
        lambda: [second_root],
    )
    skills_tool._SKILLS_CACHE.clear()

    listed = json.loads(skills_tool.skills_list(task_id="protected-attempt"))

    assert listed["skills"] == []


def test_directory_alias_collision_is_not_exposed_by_protected_catalog(
    tmp_path,
    monkeypatch,
):
    repository = tmp_path / "repository"
    repository.mkdir()
    global_skills = tmp_path / "global-skills"
    alias_dir = global_skills / "foo"
    alias_dir.mkdir(parents=True)
    (alias_dir / "SKILL.md").write_text(
        "---\nname: bar\ndescription: alias owner\n---\n",
        encoding="utf-8",
    )
    frontmatter_dir = global_skills / "other"
    frontmatter_dir.mkdir()
    (frontmatter_dir / "SKILL.md").write_text(
        "---\nname: foo\ndescription: frontmatter owner\n---\n",
        encoding="utf-8",
    )
    authority = _protected_authority(repository, all_skills=True)
    monkeypatch.setattr(
        "tools.delegation_scope.attempt_scope_registry.get",
        lambda task_id: authority if task_id == "protected-attempt" else None,
    )
    monkeypatch.setattr(skills_tool, "_skills_dir", lambda: global_skills)
    monkeypatch.setattr("agent.skill_utils.get_external_skills_dirs", lambda: [])
    skills_tool._SKILLS_CACHE.clear()

    listed = json.loads(skills_tool.skills_list(task_id="protected-attempt"))

    assert [skill["name"] for skill in listed["skills"]] == ["bar"]


def test_all_skills_grant_makes_each_listed_skill_viewable(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    global_skills = tmp_path / "global-skills"
    _write_skill(global_skills, "first", "first skill")
    _write_skill(global_skills, "second", "second skill")

    authority = _protected_authority(repository, all_skills=True)
    monkeypatch.setattr(
        "tools.delegation_scope.attempt_scope_registry.get",
        lambda task_id: authority if task_id == "protected-attempt" else None,
    )
    monkeypatch.setattr(skills_tool, "_skills_dir", lambda: global_skills)
    monkeypatch.setattr("agent.skill_utils.get_external_skills_dirs", lambda: [])
    skills_tool._SKILLS_CACHE.clear()

    listed = json.loads(skills_tool.skills_list(task_id="protected-attempt"))

    assert {skill["name"] for skill in listed["skills"]} == {"first", "second"}
    for skill in listed["skills"]:
        viewed = json.loads(
            skills_tool.skill_view(
                skill["name"],
                task_id="protected-attempt",
                preprocess=False,
            )
        )
        assert viewed["success"] is True


def test_protected_prompt_uses_same_specific_skill_catalog(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    global_skills = tmp_path / "global-skills"
    _write_skill(global_skills, "selected", "selected skill")
    _write_skill(global_skills, "unselected", "unselected skill")
    authority = _protected_authority(repository, skill_names={"selected"})
    monkeypatch.setattr(
        "tools.delegation_scope.attempt_scope_registry.get",
        lambda task_id: authority if task_id == "protected-attempt" else None,
    )
    monkeypatch.setattr(skills_tool, "_skills_dir", lambda: global_skills)
    monkeypatch.setattr("agent.skill_utils.get_external_skills_dirs", lambda: [])
    skills_tool._SKILLS_CACHE.clear()

    listed = json.loads(skills_tool.skills_list(task_id="protected-attempt"))
    prompt = build_skills_system_prompt(
        available_tools={"skill_view"},
        task_id="protected-attempt",
    )

    assert [skill["name"] for skill in listed["skills"]] == ["selected"]
    assert "selected skill" in prompt
    assert "unselected skill" not in prompt


def test_visible_skill_file_is_automatically_listed_and_viewable(tmp_path, monkeypatch):
    global_skills = tmp_path / "global-skills"
    skill_dir = _write_skill(global_skills, "file-visible", "visible file")
    skill_md = skill_dir / "SKILL.md"
    authority = _protected_authority(skill_md, object_type="file")
    monkeypatch.setattr(
        "tools.delegation_scope.attempt_scope_registry.get",
        lambda task_id: authority if task_id == "protected-attempt" else None,
    )
    monkeypatch.setattr(skills_tool, "_skills_dir", lambda: global_skills)
    monkeypatch.setattr("agent.skill_utils.get_external_skills_dirs", lambda: [])
    skills_tool._SKILLS_CACHE.clear()

    listed = json.loads(skills_tool.skills_list(task_id="protected-attempt"))
    viewed = json.loads(
        skills_tool.skill_view(
            "file-visible",
            task_id="protected-attempt",
            preprocess=False,
        )
    )

    assert [skill["name"] for skill in listed["skills"]] == ["file-visible"]
    assert viewed["success"] is True, viewed


def test_protected_skill_tools_only_search_roots_inside_attempt_grants(
    tmp_path, monkeypatch
):
    repository = tmp_path / "repository"
    project_skills = repository / ".codex" / "skills"
    global_skills = tmp_path / "global-skills"
    project_dir = _write_skill(project_skills, "project-only", "inside project")
    _write_skill(
        project_skills / "category",
        "categorized",
        "categorized inside project",
    )
    _write_skill(global_skills, "global-only", "outside project")
    (project_dir / "references").mkdir()
    (project_dir / "references" / "note.md").write_text("bounded note", encoding="utf-8")

    authority = _protected_authority(repository)
    monkeypatch.setattr(
        "tools.delegation_scope.attempt_scope_registry.get",
        lambda task_id: authority if task_id == "protected-attempt" else None,
    )
    monkeypatch.setattr(skills_tool, "_skills_dir", lambda: global_skills)
    monkeypatch.setattr(
        "agent.skill_utils.get_external_skills_dirs",
        lambda: [project_skills, global_skills],
    )
    monkeypatch.setattr(
        "hermes_cli.plugins.discover_plugins",
        lambda: (_ for _ in ()).throw(AssertionError("plugin discovery escaped scope")),
    )
    skills_tool._SKILLS_CACHE.clear()

    listed = json.loads(skills_tool.skills_list(task_id="protected-attempt"))
    viewed = json.loads(
        skills_tool.skill_view(
            "project-only",
            file_path="references/note.md",
            task_id="protected-attempt",
            preprocess=False,
        )
    )
    denied = json.loads(
        skills_tool.skill_view(
            "global-only",
            task_id="protected-attempt",
            preprocess=False,
        )
    )
    categorized = json.loads(
        skills_tool.skill_view(
            "category:categorized",
            task_id="protected-attempt",
            preprocess=False,
        )
    )

    assert {skill["name"] for skill in listed["skills"]} == {
        "categorized",
        "project-only",
    }
    assert viewed["success"] is True
    assert viewed["content"] == "bounded note"
    assert denied["success"] is False
    assert "not found" in denied["error"]
    assert categorized["success"] is True


@pytest.mark.parametrize("replacement", ["directory", "symlink"])
def test_protected_skill_tools_reject_replaced_backing_root(
    tmp_path,
    monkeypatch,
    replacement,
):
    repository = tmp_path / "repository"
    project_skills = repository / ".codex" / "skills"
    _write_skill(project_skills, "inside", "inside project")
    authority = _protected_authority(repository)

    original = tmp_path / "original"
    repository.rename(original)
    if replacement == "symlink":
        outside = tmp_path / "outside"
        replacement_skills = outside / ".codex" / "skills"
        _write_skill(replacement_skills, "outside", "must not be read")
        repository.symlink_to(outside, target_is_directory=True)
    else:
        replacement_skills = repository / ".codex" / "skills"
        _write_skill(replacement_skills, "outside", "must not be read")

    monkeypatch.setattr(
        "tools.delegation_scope.attempt_scope_registry.get",
        lambda task_id: authority if task_id == "protected-attempt" else None,
    )
    monkeypatch.setattr(skills_tool, "_skills_dir", lambda: replacement_skills)
    monkeypatch.setattr(
        "agent.skill_utils.get_external_skills_dirs",
        lambda: [repository / ".codex" / "skills"],
    )
    skills_tool._SKILLS_CACHE.clear()

    listed = json.loads(skills_tool.skills_list(task_id="protected-attempt"))
    viewed = json.loads(
        skills_tool.skill_view(
            "outside",
            task_id="protected-attempt",
            preprocess=False,
        )
    )

    assert listed["skills"] == []
    assert viewed["success"] is False


def test_protected_skill_tools_reject_cleaned_attempt(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    project_skills = repository / ".codex" / "skills"
    _write_skill(project_skills, "project-only", "inside project")
    authority = _protected_authority(repository, state="cleaned")
    monkeypatch.setattr(
        "tools.delegation_scope.attempt_scope_registry.get",
        lambda task_id: authority if task_id == "protected-attempt" else None,
    )
    monkeypatch.setattr(skills_tool, "_skills_dir", lambda: project_skills)
    monkeypatch.setattr(
        "agent.skill_utils.get_external_skills_dirs",
        lambda: [project_skills],
    )
    skills_tool._SKILLS_CACHE.clear()

    listed = json.loads(skills_tool.skills_list(task_id="protected-attempt"))
    viewed = json.loads(
        skills_tool.skill_view(
            "project-only",
            task_id="protected-attempt",
            preprocess=False,
        )
    )

    assert listed["skills"] == []
    assert viewed["success"] is False



def test_protected_skill_view_omits_out_of_grant_linked_directory_symlink(
    tmp_path,
    monkeypatch,
):
    repository = tmp_path / "repository"
    project_skills = repository / ".codex" / "skills"
    project_dir = _write_skill(project_skills, "project-only", "inside project")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret-name.md").write_text("must not be discovered", encoding="utf-8")
    (project_dir / "references").symlink_to(outside, target_is_directory=True)
    authority = _protected_authority(repository)
    monkeypatch.setattr(
        "tools.delegation_scope.attempt_scope_registry.get",
        lambda task_id: authority if task_id == "protected-attempt" else None,
    )
    monkeypatch.setattr(skills_tool, "_skills_dir", lambda: project_skills)
    monkeypatch.setattr("agent.skill_utils.get_external_skills_dirs", lambda: [])
    skills_tool._SKILLS_CACHE.clear()

    viewed = json.loads(
        skills_tool.skill_view(
            "project-only",
            task_id="protected-attempt",
            preprocess=False,
        )
    )

    assert viewed["success"] is True
    assert viewed["linked_files"] is None
    assert "secret-name.md" not in json.dumps(viewed)


def test_protected_skill_view_rejects_root_replaced_before_link_discovery(
    tmp_path,
    monkeypatch,
):
    repository = tmp_path / "repository"
    project_skills = repository / ".codex" / "skills"
    _write_skill(project_skills, "project-only", "inside project")
    authority = _protected_authority(repository)
    monkeypatch.setattr(
        "tools.delegation_scope.attempt_scope_registry.get",
        lambda task_id: authority if task_id == "protected-attempt" else None,
    )
    monkeypatch.setattr(skills_tool, "_skills_dir", lambda: project_skills)
    monkeypatch.setattr("agent.skill_utils.get_external_skills_dirs", lambda: [])
    skills_tool._SKILLS_CACHE.clear()

    discover = skills_tool._discover_skill_linked_files

    def replace_then_discover(skill_dir, *, task_id, protected):
        original = tmp_path / "original"
        repository.rename(original)
        replacement_skills = repository / ".codex" / "skills"
        replacement_dir = _write_skill(
            replacement_skills,
            "project-only",
            "replacement",
        )
        (replacement_dir / "references").mkdir()
        (replacement_dir / "references" / "replacement-secret.md").write_text(
            "must not be discovered",
            encoding="utf-8",
        )
        return discover(skill_dir, task_id=task_id, protected=protected)

    monkeypatch.setattr(
        skills_tool,
        "_discover_skill_linked_files",
        replace_then_discover,
    )

    viewed = json.loads(
        skills_tool.skill_view(
            "project-only",
            task_id="protected-attempt",
            preprocess=False,
        )
    )

    assert viewed["success"] is False
    assert "replacement-secret.md" not in json.dumps(viewed)
