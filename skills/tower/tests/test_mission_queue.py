"""Mission queue tests — MissionQueue, sync_from_dir, pipeline logic.

Covers:
  - Basic CRUD (add, remove, update_status, mark_complete)
  - queued() filtering and numeric sort order
  - next() priority + FIFO ordering
  - sync_from_dir file loading, skip logic, and cleanup
  - Re-queue bug: missions in terminal state must re-queue from file (PR-* regression)
  - Pipeline: next_in_pipeline, ready_to_fan_out, seq_complete
  - Auto-deploy gating
  - parse_spec_file
  - add_from_spec and add_adhoc
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from mission_queue import Mission, MissionQueue, parse_spec_file


# ── Helpers ──────────────────────────────────────────────────────────────

def _make_mission(
    id: str = "ENG-100",
    title: str = "Test mission",
    status: str = "QUEUED",
    priority: int = 2,
    source: str = "linear",
    model: str = "sonnet",
    created_at: float = 0.0,
    pipeline_id: str = "",
    pipeline_seq: int = 0,
    next_mission_id: str = "",
    on_failure: str = "abort",
) -> Mission:
    return Mission(
        id=id,
        title=title,
        source=source,
        priority=priority,
        directives=[],
        agent_count=1,
        model=model,
        status=status,
        spec_content="",
        created_at=created_at or time.time(),
        pipeline_id=pipeline_id,
        pipeline_seq=pipeline_seq,
        next_mission_id=next_mission_id,
        on_failure=on_failure,
    )


def _write_mission_json(queue_dir: Path, mission_id: str, **overrides) -> Path:
    """Write a mission JSON file to queue_dir. Returns the file path."""
    data = {
        "id": mission_id,
        "title": overrides.get("title", f"Mission {mission_id}"),
        "source": overrides.get("source", "linear"),
        "priority": overrides.get("priority", 2),
        "model": overrides.get("model", "sonnet"),
        "agent_count": overrides.get("agent_count", 1),
        "directive": overrides.get("directive", ""),
        "created_at": overrides.get("created_at", int(time.time())),
    }
    data.update({k: v for k, v in overrides.items() if k not in data})
    fp = queue_dir / f"{mission_id}.json"
    fp.write_text(json.dumps(data), encoding="utf-8")
    return fp


# ── Basic CRUD ───────────────────────────────────────────────────────────

class TestMissionQueueCRUD:
    def test_add_and_get(self):
        q = MissionQueue()
        m = _make_mission(id="ENG-1")
        q.add(m)
        assert q.get("ENG-1") is m

    def test_add_sets_created_at_if_zero(self):
        q = MissionQueue()
        m = _make_mission(id="ENG-2", created_at=0.0)
        # created_at is set by _make_mission unless 0 — but Mission stores 0
        m.created_at = 0.0
        q.add(m)
        assert q.get("ENG-2").created_at > 0

    def test_get_missing_returns_none(self):
        q = MissionQueue()
        assert q.get("NOPE") is None

    def test_remove(self):
        q = MissionQueue()
        q.add(_make_mission(id="ENG-3"))
        q.remove("ENG-3")
        assert q.get("ENG-3") is None

    def test_remove_nonexistent_is_noop(self):
        q = MissionQueue()
        q.remove("NOPE")  # should not raise

    def test_update_status(self):
        q = MissionQueue()
        q.add(_make_mission(id="ENG-4"))
        q.update_status("ENG-4", "ACTIVE")
        assert q.get("ENG-4").status == "ACTIVE"

    def test_update_status_sets_started_at(self):
        q = MissionQueue()
        m = _make_mission(id="ENG-5")
        m.started_at = 0.0
        q.add(m)
        q.update_status("ENG-5", "ACTIVE")
        assert q.get("ENG-5").started_at > 0

    def test_update_status_doesnt_overwrite_started_at(self):
        q = MissionQueue()
        m = _make_mission(id="ENG-6")
        m.started_at = 12345.0
        q.add(m)
        q.update_status("ENG-6", "ACTIVE")
        assert q.get("ENG-6").started_at == 12345.0

    def test_update_status_nonexistent_is_noop(self):
        q = MissionQueue()
        q.update_status("NOPE", "ACTIVE")  # should not raise

    def test_mark_complete(self):
        q = MissionQueue()
        q.add(_make_mission(id="ENG-7"))
        q.mark_complete("ENG-7")
        m = q.get("ENG-7")
        assert m.status == "COMPLETE"
        assert m.completed_at > 0

    def test_mark_complete_nonexistent_is_noop(self):
        q = MissionQueue()
        q.mark_complete("NOPE")  # should not raise

    def test_fail_mission(self):
        q = MissionQueue()
        q.add(_make_mission(id="ENG-8"))
        q.fail_mission("ENG-8")
        m = q.get("ENG-8")
        assert m.status == "FAILED"
        assert m.completed_at > 0

    def test_all_missions(self):
        q = MissionQueue()
        q.add(_make_mission(id="A"))
        q.add(_make_mission(id="B", status="ACTIVE"))
        q.add(_make_mission(id="C", status="COMPLETE"))
        assert len(q.all_missions()) == 3


# ── Queued / Active queries ──────────────────────────────────────────────

class TestQueuedFiltering:
    def test_queued_returns_only_queued(self):
        q = MissionQueue()
        q.add(_make_mission(id="ENG-10", status="QUEUED"))
        q.add(_make_mission(id="ENG-11", status="ACTIVE"))
        q.add(_make_mission(id="ENG-12", status="COMPLETE"))
        q.add(_make_mission(id="ENG-13", status="QUEUED"))
        ids = [m.id for m in q.queued()]
        assert ids == ["ENG-10", "ENG-13"]

    def test_queued_sorts_by_numeric_suffix(self):
        q = MissionQueue()
        q.add(_make_mission(id="ENG-300"))
        q.add(_make_mission(id="ENG-50"))
        q.add(_make_mission(id="ENG-1000"))
        q.add(_make_mission(id="ENG-7"))
        ids = [m.id for m in q.queued()]
        assert ids == ["ENG-7", "ENG-50", "ENG-300", "ENG-1000"]

    def test_queued_mixed_prefixes_sort_by_number(self):
        """PR-608 should sort between ENG-500 and ENG-700."""
        q = MissionQueue()
        q.add(_make_mission(id="ENG-500"))
        q.add(_make_mission(id="PR-608"))
        q.add(_make_mission(id="ENG-700"))
        q.add(_make_mission(id="PRO-76"))
        ids = [m.id for m in q.queued()]
        assert ids == ["PRO-76", "ENG-500", "PR-608", "ENG-700"]

    def test_queued_non_numeric_ids_sort_last(self):
        q = MissionQueue()
        q.add(_make_mission(id="ENG-10"))
        q.add(_make_mission(id="no-number"))
        ids = [m.id for m in q.queued()]
        assert ids == ["ENG-10", "no-number"]

    def test_active_returns_only_active(self):
        q = MissionQueue()
        q.add(_make_mission(id="A", status="QUEUED"))
        q.add(_make_mission(id="B", status="ACTIVE"))
        q.add(_make_mission(id="C", status="ACTIVE"))
        ids = [m.id for m in q.active()]
        assert set(ids) == {"B", "C"}


# ── next() priority + FIFO ──────────────────────────────────────────────

class TestNext:
    def test_next_returns_highest_priority(self):
        q = MissionQueue()
        q.add(_make_mission(id="LOW", priority=3, created_at=1.0))
        q.add(_make_mission(id="URG", priority=1, created_at=2.0))
        q.add(_make_mission(id="MED", priority=2, created_at=3.0))
        assert q.next().id == "URG"

    def test_next_fifo_within_same_priority(self):
        q = MissionQueue()
        q.add(_make_mission(id="FIRST", priority=2, created_at=100.0))
        q.add(_make_mission(id="SECOND", priority=2, created_at=200.0))
        assert q.next().id == "FIRST"

    def test_next_empty_returns_none(self):
        q = MissionQueue()
        assert q.next() is None

    def test_next_skips_non_queued(self):
        q = MissionQueue()
        q.add(_make_mission(id="DONE", priority=1, status="COMPLETE"))
        q.add(_make_mission(id="STILL", priority=2, status="QUEUED"))
        assert q.next().id == "STILL"


# ── sync_from_dir ────────────────────────────────────────────────────────

class TestSyncFromDir:
    def test_loads_json_files(self, tmp_path):
        _write_mission_json(tmp_path, "ENG-100", title="First")
        _write_mission_json(tmp_path, "ENG-200", title="Second")
        q = MissionQueue()
        added = q.sync_from_dir(tmp_path)
        assert added == 2
        assert q.get("ENG-100").title == "First"
        assert q.get("ENG-200").title == "Second"

    def test_all_loaded_missions_are_queued(self, tmp_path):
        _write_mission_json(tmp_path, "ENG-100")
        _write_mission_json(tmp_path, "PR-200")
        q = MissionQueue()
        q.sync_from_dir(tmp_path)
        assert q.get("ENG-100").status == "QUEUED"
        assert q.get("PR-200").status == "QUEUED"

    def test_skips_hidden_files(self, tmp_path):
        _write_mission_json(tmp_path, "ENG-100")
        (tmp_path / ".hidden.json").write_text("{}", encoding="utf-8")
        q = MissionQueue()
        added = q.sync_from_dir(tmp_path)
        assert added == 1

    def test_skips_non_json_files(self, tmp_path):
        _write_mission_json(tmp_path, "ENG-100")
        (tmp_path / "README.md").write_text("hello", encoding="utf-8")
        q = MissionQueue()
        added = q.sync_from_dir(tmp_path)
        assert added == 1

    def test_skips_malformed_json(self, tmp_path):
        _write_mission_json(tmp_path, "ENG-100")
        (tmp_path / "BAD.json").write_text("not json{{{", encoding="utf-8")
        q = MissionQueue()
        added = q.sync_from_dir(tmp_path)
        assert added == 1

    def test_uses_file_stem_when_no_id_field(self, tmp_path):
        fp = tmp_path / "PR-999.json"
        fp.write_text(json.dumps({"title": "No id field"}), encoding="utf-8")
        q = MissionQueue()
        q.sync_from_dir(tmp_path)
        assert q.get("PR-999") is not None
        assert q.get("PR-999").title == "No id field"

    def test_does_not_overwrite_active_mission(self, tmp_path):
        """An ACTIVE mission should not be clobbered by a file re-sync."""
        _write_mission_json(tmp_path, "ENG-100", title="From file")
        q = MissionQueue()
        # Pre-populate as ACTIVE (simulating a deployed agent)
        q.add(_make_mission(id="ENG-100", title="Deployed", status="ACTIVE"))
        added = q.sync_from_dir(tmp_path)
        assert added == 0
        assert q.get("ENG-100").status == "ACTIVE"
        assert q.get("ENG-100").title == "Deployed"

    def test_does_not_overwrite_deploying_mission(self, tmp_path):
        _write_mission_json(tmp_path, "ENG-100")
        q = MissionQueue()
        q.add(_make_mission(id="ENG-100", status="DEPLOYING"))
        q.sync_from_dir(tmp_path)
        assert q.get("ENG-100").status == "DEPLOYING"

    def test_requeues_recovered_mission_from_file(self, tmp_path):
        """BUG FIX: A mission in terminal state (COMPLETE/FAILED/RECOVERED)
        whose JSON file still exists should be re-queued.

        This is the PR-* regression — PR worktrees get discovered as agents
        with a terminal status, then sync_from_dir skips them because the ID
        is already tracked. The mission never appears in queued().
        """
        _write_mission_json(tmp_path, "PR-608", title="Integration branch")
        q = MissionQueue()
        # Simulate: dashboard discovered the PR worktree and set it COMPLETE
        q.add(_make_mission(id="PR-608", title="Old title", status="COMPLETE"))
        q.sync_from_dir(tmp_path)
        m = q.get("PR-608")
        assert m.status == "QUEUED", (
            "Terminal mission with a queue file should be re-queued"
        )
        assert m.title == "Integration branch", (
            "Re-queued mission should pick up fresh data from the JSON file"
        )

    def test_requeues_failed_mission_from_file(self, tmp_path):
        _write_mission_json(tmp_path, "ENG-500", title="Retry me")
        q = MissionQueue()
        q.add(_make_mission(id="ENG-500", status="FAILED"))
        q.sync_from_dir(tmp_path)
        assert q.get("ENG-500").status == "QUEUED"

    def test_removes_deleted_file_missions(self, tmp_path):
        """If a QUEUED mission's JSON file is deleted, it should be removed."""
        _write_mission_json(tmp_path, "ENG-100")
        _write_mission_json(tmp_path, "ENG-200")
        q = MissionQueue()
        q.sync_from_dir(tmp_path)
        assert q.get("ENG-200") is not None

        # Delete ENG-200.json and re-sync
        (tmp_path / "ENG-200.json").unlink()
        q.sync_from_dir(tmp_path)
        assert q.get("ENG-200") is None

    def test_does_not_remove_adhoc_missions_on_sync(self, tmp_path):
        """Adhoc missions (not file-sourced) should survive sync even without a file."""
        _write_mission_json(tmp_path, "ENG-100")
        q = MissionQueue()
        q.add(_make_mission(id="ADHOC-1", source="adhoc"))
        q.sync_from_dir(tmp_path)
        assert q.get("ADHOC-1") is not None

    def test_nonexistent_dir_returns_zero(self, tmp_path):
        q = MissionQueue()
        added = q.sync_from_dir(tmp_path / "nonexistent")
        assert added == 0

    def test_empty_dir_cleans_stale_missions(self, tmp_path):
        """If dir is empty, all file-sourced QUEUED missions should be removed."""
        q = MissionQueue()
        q.add(_make_mission(id="ENG-100", source="linear"))
        q.sync_from_dir(tmp_path)  # empty dir
        assert q.get("ENG-100") is None

    def test_numeric_sort_order_in_loading(self, tmp_path):
        """Files should be processed in numeric order."""
        _write_mission_json(tmp_path, "ENG-300")
        _write_mission_json(tmp_path, "ENG-50")
        _write_mission_json(tmp_path, "PR-100")
        q = MissionQueue()
        q.sync_from_dir(tmp_path)
        queued_ids = [m.id for m in q.queued()]
        assert queued_ids == ["ENG-50", "PR-100", "ENG-300"]

    def test_reads_all_mission_fields(self, tmp_path):
        _write_mission_json(
            tmp_path, "ENG-100",
            title="Full mission",
            source="github",
            priority=1,
            model="opus",
            agent_count=2,
            directive="Do the thing",
            branch_name="feat/thing",
            pipeline_id="pipe-1",
            pipeline_seq=3,
            next_mission_id="ENG-101",
            parent_ticket="ENG-99",
            sub_name="api",
            on_failure="continue",
        )
        q = MissionQueue()
        q.sync_from_dir(tmp_path)
        m = q.get("ENG-100")
        assert m.title == "Full mission"
        assert m.source == "github"
        assert m.priority == 1
        assert m.model == "opus"
        assert m.agent_count == 2
        assert m.branch_name == "feat/thing"
        assert m.pipeline_id == "pipe-1"
        assert m.pipeline_seq == 3
        assert m.next_mission_id == "ENG-101"
        assert m.parent_ticket == "ENG-99"
        assert m.sub_name == "api"
        assert m.on_failure == "continue"


# ── Pipeline logic ───────────────────────────────────────────────────────

class TestPipeline:
    def _pipeline_queue(self) -> MissionQueue:
        q = MissionQueue()
        q.add(_make_mission(id="P-1", pipeline_id="pipe", pipeline_seq=0, status="COMPLETE"))
        q.add(_make_mission(id="P-2", pipeline_id="pipe", pipeline_seq=1, status="QUEUED"))
        q.add(_make_mission(id="P-3", pipeline_id="pipe", pipeline_seq=1, status="QUEUED"))
        q.add(_make_mission(id="P-4", pipeline_id="pipe", pipeline_seq=2, status="QUEUED"))
        return q

    def test_next_in_pipeline_explicit(self):
        q = MissionQueue()
        q.add(_make_mission(id="A", next_mission_id="B", status="COMPLETE"))
        q.add(_make_mission(id="B", status="QUEUED"))
        nxt = q.next_in_pipeline("A")
        assert nxt.id == "B"

    def test_next_in_pipeline_explicit_skips_non_queued(self):
        q = MissionQueue()
        q.add(_make_mission(id="A", next_mission_id="B", status="COMPLETE"))
        q.add(_make_mission(id="B", status="ACTIVE"))
        assert q.next_in_pipeline("A") is None

    def test_next_in_pipeline_by_seq(self):
        q = self._pipeline_queue()
        nxt = q.next_in_pipeline("P-1")
        assert nxt.id in ("P-2", "P-3")

    def test_next_in_pipeline_waits_for_fan_in(self):
        """Seq 2 shouldn't fire until both seq 1 missions are done."""
        q = self._pipeline_queue()
        q.update_status("P-2", "COMPLETE")
        # P-3 still QUEUED at seq 1 — seq 2 should NOT fire
        nxt = q.next_in_pipeline("P-2")
        assert nxt is None  # waiting for P-3

    def test_next_in_pipeline_fires_after_fan_in(self):
        q = self._pipeline_queue()
        q.update_status("P-2", "COMPLETE")
        q.update_status("P-3", "COMPLETE")
        nxt = q.next_in_pipeline("P-2")
        assert nxt.id == "P-4"

    def test_next_in_pipeline_nonexistent_returns_none(self):
        q = MissionQueue()
        assert q.next_in_pipeline("NOPE") is None

    def test_ready_to_fan_out(self):
        q = self._pipeline_queue()
        ready = q.ready_to_fan_out("pipe", 1)
        ids = {m.id for m in ready}
        assert ids == {"P-2", "P-3"}

    def test_ready_to_fan_out_empty_seq(self):
        q = self._pipeline_queue()
        assert q.ready_to_fan_out("pipe", 99) == []

    def test_seq_complete(self):
        q = self._pipeline_queue()
        assert q.seq_complete("pipe", 0) is True   # P-1 is COMPLETE
        assert q.seq_complete("pipe", 1) is False   # P-2, P-3 still QUEUED

    def test_seq_complete_empty_is_true(self):
        q = MissionQueue()
        assert q.seq_complete("pipe", 0) is True

    def test_seq_has_failures(self):
        q = self._pipeline_queue()
        q.fail_mission("P-2")
        failures = q.seq_has_failures("pipe", 1)
        assert len(failures) == 1
        assert failures[0].id == "P-2"

    def test_siblings_at_seq(self):
        q = self._pipeline_queue()
        sibs = q.siblings_at_seq("pipe", 1)
        ids = {m.id for m in sibs}
        assert ids == {"P-2", "P-3"}

    def test_active_siblings(self):
        q = self._pipeline_queue()
        q.update_status("P-2", "ACTIVE")
        q.update_status("P-3", "ACTIVE")
        sibs = q.active_siblings("pipe", 1, exclude_id="P-2")
        assert len(sibs) == 1
        assert sibs[0].id == "P-3"


# ── Auto-deploy ──────────────────────────────────────────────────────────

class TestAutoDeploy:
    def test_default_disabled(self):
        q = MissionQueue()
        assert q.auto_deploy_enabled is False

    def test_enable(self):
        q = MissionQueue()
        q.set_auto_deploy(True, max_concurrent=5)
        assert q.auto_deploy_enabled is True

    def test_should_auto_deploy_when_enabled_and_has_queued(self):
        q = MissionQueue()
        q.add(_make_mission(id="ENG-1"))
        q.set_auto_deploy(True, max_concurrent=3)
        assert q.should_auto_deploy(active_count=0) is True

    def test_should_not_auto_deploy_at_capacity(self):
        q = MissionQueue()
        q.add(_make_mission(id="ENG-1"))
        q.set_auto_deploy(True, max_concurrent=3)
        assert q.should_auto_deploy(active_count=3) is False

    def test_should_not_auto_deploy_when_disabled(self):
        q = MissionQueue()
        q.add(_make_mission(id="ENG-1"))
        assert q.should_auto_deploy(active_count=0) is False

    def test_should_not_auto_deploy_empty_queue(self):
        q = MissionQueue()
        q.set_auto_deploy(True)
        assert q.should_auto_deploy(active_count=0) is False


# ── parse_spec_file ──────────────────────────────────────────────────────

class TestParseSpecFile:
    def test_heading_title(self, tmp_path):
        f = tmp_path / "spec.md"
        f.write_text("# My Feature\n\nSome details", encoding="utf-8")
        parsed = parse_spec_file(str(f))
        assert parsed["title"] == "My Feature"

    def test_plain_text_title(self, tmp_path):
        f = tmp_path / "spec.md"
        f.write_text("Plain title line\n\nMore content", encoding="utf-8")
        parsed = parse_spec_file(str(f))
        assert parsed["title"] == "Plain title line"

    def test_extracts_ticket_id(self, tmp_path):
        f = tmp_path / "spec.md"
        f.write_text("# Fix ENG-123 auth bug\n\nDetails", encoding="utf-8")
        parsed = parse_spec_file(str(f))
        assert parsed["ticket_id"] == "ENG-123"

    def test_no_ticket_id(self, tmp_path):
        f = tmp_path / "spec.md"
        f.write_text("# No ticket here\n\nJust a spec", encoding="utf-8")
        parsed = parse_spec_file(str(f))
        assert parsed["ticket_id"] is None

    def test_content_preserved(self, tmp_path):
        content = "# Title\n\nFull content here"
        f = tmp_path / "spec.md"
        f.write_text(content, encoding="utf-8")
        parsed = parse_spec_file(str(f))
        assert parsed["content"] == content


# ── add_from_spec / add_adhoc ────────────────────────────────────────────

class TestAddHelpers:
    def test_add_from_spec_uses_ticket_id(self, tmp_path):
        f = tmp_path / "spec.md"
        f.write_text("# Fix ENG-999\n\nStuff", encoding="utf-8")
        q = MissionQueue()
        m = q.add_from_spec(str(f), model="opus", priority=1)
        assert m.id == "ENG-999"
        assert m.model == "opus"
        assert m.priority == 1
        assert m.source == "file"
        assert q.get("ENG-999") is m

    def test_add_from_spec_generates_uuid_when_no_ticket(self, tmp_path):
        f = tmp_path / "spec.md"
        f.write_text("# No ticket\n\nJust ideas", encoding="utf-8")
        q = MissionQueue()
        m = q.add_from_spec(str(f))
        assert len(m.id) > 10  # UUID

    def test_add_adhoc(self):
        q = MissionQueue()
        m = q.add_adhoc("Fix the date formatter", model="haiku", priority=3)
        assert m.title == "Fix the date formatter"
        assert m.source == "adhoc"
        assert m.model == "haiku"
        assert m.status == "QUEUED"

    def test_add_adhoc_truncates_title(self):
        q = MissionQueue()
        m = q.add_adhoc("x" * 200)
        assert len(m.title) == 80
