"""Goal tree integrity — orphans, parent deletion cascade, path correctness, deep nesting."""

from sqlalchemy import text

import pytest

from src.goals.repository import GoalRepository


@pytest.fixture
def repo():
    return GoalRepository()


class TestPathCorrectness:
    async def test_root_goal_has_root_path(self, async_db, repo):
        goal = await repo.create("Root", level="vision")
        assert goal.path == "/"

    async def test_child_path_includes_parent(self, async_db, repo):
        parent = await repo.create("Parent", level="vision")
        child = await repo.create("Child", level="annual", parent_id=parent.id)
        assert child.path == f"/{parent.id}/"

    async def test_grandchild_path_includes_ancestors(self, async_db, repo):
        grandparent = await repo.create("GP", level="vision")
        parent = await repo.create("P", level="annual", parent_id=grandparent.id)
        child = await repo.create("C", level="monthly", parent_id=parent.id)
        assert child.path == f"/{grandparent.id}/{parent.id}/"

    async def test_deep_nesting_four_levels(self, async_db, repo):
        g1 = await repo.create("L1", level="vision")
        g2 = await repo.create("L2", level="annual", parent_id=g1.id)
        g3 = await repo.create("L3", level="monthly", parent_id=g2.id)
        g4 = await repo.create("L4", level="weekly", parent_id=g3.id)
        assert g4.path == f"/{g1.id}/{g2.id}/{g3.id}/"


class TestOrphanDetection:
    async def test_orphan_goal_surfaces_as_root(self, async_db, repo):
        """A goal whose parent_id references a non-existent goal should appear as root."""
        parent = await repo.create("Parent", level="vision")
        child = await repo.create("Orphan Goal", level="annual", parent_id=parent.id)

        # Simulate legacy/corrupt data by bypassing FK enforcement for one delete.
        from src.db.engine import get_session

        async with get_session() as db:
            await db.execute(text("PRAGMA foreign_keys=OFF"))
            await db.execute(text("DELETE FROM goals WHERE id = :goal_id"), {"goal_id": parent.id})
            await db.flush()
            await db.execute(text("PRAGMA foreign_keys=ON"))

        tree = await repo.get_tree()
        orphan_node = next((n for n in tree if n["id"] == child.id), None)
        assert orphan_node is not None, "Orphan should appear as root in tree"
        assert orphan_node["title"] == "Orphan Goal"

    async def test_orphan_does_not_appear_under_wrong_parent(self, async_db, repo):
        """Orphan should NOT be attached to any other parent's children list."""
        from src.db.engine import get_session

        real = await repo.create("Real Parent", level="vision")
        vanished_parent = await repo.create("Ghost Parent", level="vision")
        orphan = await repo.create("Orphan 2", level="daily", parent_id=vanished_parent.id)
        async with get_session() as db:
            await db.execute(text("PRAGMA foreign_keys=OFF"))
            await db.execute(text("DELETE FROM goals WHERE id = :goal_id"), {"goal_id": vanished_parent.id})
            await db.flush()
            await db.execute(text("PRAGMA foreign_keys=ON"))

        tree = await repo.get_tree()
        real_node = next(n for n in tree if n["id"] == real.id)
        assert len(real_node["children"]) == 0
        assert any(node["id"] == orphan.id for node in tree)


class TestCascadeDeletion:
    async def test_delete_parent_removes_all_descendants(self, async_db, repo):
        gp = await repo.create("Grandparent", level="vision")
        p = await repo.create("Parent", level="annual", parent_id=gp.id)
        c = await repo.create("Child", level="monthly", parent_id=p.id)

        await repo.delete(gp.id)

        assert await repo.get(gp.id) is None
        assert await repo.get(p.id) is None
        assert await repo.get(c.id) is None

    async def test_delete_middle_node_preserves_siblings(self, async_db, repo):
        parent = await repo.create("Parent", level="vision")
        child_a = await repo.create("A", level="annual", parent_id=parent.id)
        child_b = await repo.create("B", level="annual", parent_id=parent.id)

        await repo.delete(child_a.id)

        assert await repo.get(child_a.id) is None
        assert await repo.get(child_b.id) is not None
        assert await repo.get(parent.id) is not None

    async def test_delete_leaf_preserves_parent(self, async_db, repo):
        parent = await repo.create("Parent", level="vision")
        child = await repo.create("Child", level="annual", parent_id=parent.id)

        await repo.delete(child.id)

        assert await repo.get(parent.id) is not None
        assert await repo.get(child.id) is None


class TestTreeStructure:
    async def test_multiple_roots(self, async_db, repo):
        await repo.create("Root A", level="vision", domain="health")
        await repo.create("Root B", level="vision", domain="growth")
        tree = await repo.get_tree()
        assert len(tree) == 2

    async def test_siblings_under_same_parent(self, async_db, repo):
        parent = await repo.create("Parent", level="vision")
        await repo.create("A", level="annual", parent_id=parent.id)
        await repo.create("B", level="annual", parent_id=parent.id)
        await repo.create("C", level="annual", parent_id=parent.id)

        tree = await repo.get_tree()
        assert len(tree) == 1
        assert len(tree[0]["children"]) == 3

    async def test_tree_includes_all_fields(self, async_db, repo):
        goal = await repo.create("Test", level="daily", domain="health", description="desc")
        tree = await repo.get_tree()
        node = tree[0]
        assert node["id"] == goal.id
        assert node["title"] == "Test"
        assert node["level"] == "daily"
        assert node["domain"] == "health"
        assert node["description"] == "desc"
        assert node["status"] == "active"
        assert "children" in node
