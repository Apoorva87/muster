"""ArtifactStore is public API: agents pass artifacts by reference."""
import json

import pytest

from app.kernel.artifacts import FilesystemArtifactStore


@pytest.fixture
def store(tmp_path):
    return FilesystemArtifactStore(root=tmp_path)


async def test_put_returns_reference_not_content(store):
    ref = await store.put(project_id="proj_1", task_id="task_1",
                          created_by="research", content="# Big report\n" * 500)
    assert ref.id.startswith("art_")
    # The reference is small: it is what crosses between agents.
    assert len(ref.model_dump_json()) < 512


async def test_get_round_trips_text(store):
    ref = await store.put(project_id="proj_1", task_id="task_1",
                          created_by="research", content="hello world")
    assert await store.get(ref.id) == "hello world"


async def test_json_artifacts_round_trip(store):
    payload = {"pe_ratio": 31.4, "verdict": "rich"}
    ref = await store.put(project_id="proj_1", task_id="task_1",
                          created_by="finance", content=payload, type="json")
    assert json.loads(await store.get(ref.id)) == payload


async def test_layout_matches_prd(store, tmp_path):
    ref = await store.put(project_id="proj_1", task_id="task_1",
                          created_by="research", content="x")
    # PRD: ./data/artifacts/<project-id>/<artifact-id>.md
    assert (tmp_path / "proj_1" / f"{ref.id}.md").is_file()


async def test_get_unknown_artifact_raises(store):
    with pytest.raises(KeyError):
        await store.get("art_doesnotexist")


async def test_projects_are_isolated_on_disk(store, tmp_path):
    await store.put(project_id="proj_a", task_id="t", created_by="x", content="a")
    await store.put(project_id="proj_b", task_id="t", created_by="x", content="b")
    assert {p.name for p in tmp_path.iterdir()} == {"proj_a", "proj_b"}


async def test_put_rejects_path_traversal_in_project_id(store):
    with pytest.raises(ValueError):
        await store.put(project_id="../../etc", task_id="t",
                        created_by="x", content="a")
