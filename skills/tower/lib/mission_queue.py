from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class Mission:
    id: str
    title: str
    source: str            # "linear" | "file" | "adhoc"
    priority: int          # 1=urgent, 2=normal, 3=low
    directives: list
    agent_count: int
    model: str             # "opus" | "sonnet" | "haiku"
    status: str            # QUEUED | DEPLOYING | ACTIVE | COMPLETE
    spec_content: str
    branch_name: str = ""       # Linear gitBranchName or spec/<slug>
    created_at: float = 0.0
    started_at: float = 0.0
    completed_at: float = 0.0
    # Pipeline support — chain missions sequentially
    pipeline_id: str = ""       # shared ID across all missions in a pipeline
    pipeline_seq: int = 0       # order within pipeline (0, 1, 2, ...)
    next_mission_id: str = ""   # explicit next mission to deploy on RECOVERED
    prev_worktree: str = ""     # worktree of the previous stage (for context handoff)


def parse_spec_file(path: str) -> dict:
    content = Path(path).read_text(encoding="utf-8")

    # Title: first # heading or first non-empty line
    title = ""
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
        else:
            title = stripped
        break

    # Ticket ID: look for patterns like ENG-123, PROJ-456
    ticket_id = None
    match = re.search(r"\b([A-Z]{2,}-\d+)\b", content)
    if match:
        ticket_id = match.group(1)

    return {
        "title": title,
        "content": content,
        "ticket_id": ticket_id,
    }


class MissionQueue:
    def __init__(self) -> None:
        self._missions: dict[str, Mission] = {}
        self._auto_deploy: bool = False
        self._max_concurrent: int = 3

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def auto_deploy_enabled(self) -> bool:
        return self._auto_deploy

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def add(self, mission: Mission) -> None:
        if not mission.created_at:
            mission.created_at = time.time()
        self._missions[mission.id] = mission

    def add_from_spec(
        self,
        file_path: str,
        model: str = "sonnet",
        priority: int = 2,
    ) -> Mission:
        parsed = parse_spec_file(file_path)
        mission_id = parsed["ticket_id"] or str(uuid.uuid4())
        mission = Mission(
            id=mission_id,
            title=parsed["title"],
            source="file",
            priority=priority,
            directives=[],
            agent_count=0,
            model=model,
            status="QUEUED",
            spec_content=parsed["content"],
            created_at=time.time(),
        )
        self.add(mission)
        return mission

    def add_adhoc(
        self,
        description: str,
        model: str = "sonnet",
        priority: int = 2,
    ) -> Mission:
        mission = Mission(
            id=str(uuid.uuid4()),
            title=description[:80],
            source="adhoc",
            priority=priority,
            directives=[],
            agent_count=0,
            model=model,
            status="QUEUED",
            spec_content=description,
            created_at=time.time(),
        )
        self.add(mission)
        return mission

    def update_status(self, mission_id: str, status: str) -> None:
        mission = self._missions.get(mission_id)
        if mission is None:
            return
        mission.status = status
        if status == "ACTIVE" and not mission.started_at:
            mission.started_at = time.time()

    def mark_complete(self, mission_id: str) -> None:
        mission = self._missions.get(mission_id)
        if mission is None:
            return
        mission.status = "COMPLETE"
        mission.completed_at = time.time()

    def remove(self, mission_id: str) -> None:
        self._missions.pop(mission_id, None)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, mission_id: str) -> Optional[Mission]:
        return self._missions.get(mission_id)

    def next(self) -> Optional[Mission]:
        queued = self.queued()
        if not queued:
            return None
        # sort by priority (1 first), then FIFO via created_at
        queued.sort(key=lambda m: (m.priority, m.created_at))
        return queued[0]

    def queued(self) -> List[Mission]:
        return sorted(
            [m for m in self._missions.values() if m.status == "QUEUED"],
            key=lambda m: (m.priority, m.created_at),
        )

    def active(self) -> List[Mission]:
        return [m for m in self._missions.values() if m.status == "ACTIVE"]

    def all_missions(self) -> List[Mission]:
        return list(self._missions.values())

    def next_in_pipeline(self, completed_mission_id: str) -> Optional[Mission]:
        """Get the next QUEUED mission in a pipeline after the given mission completes."""
        completed = self._missions.get(completed_mission_id)
        if not completed:
            return None

        # Explicit next_mission_id takes priority
        if completed.next_mission_id:
            nxt = self._missions.get(completed.next_mission_id)
            if nxt and nxt.status == "QUEUED":
                return nxt

        # Fall back to pipeline_id + sequence ordering
        if completed.pipeline_id:
            pipeline_missions = [
                m for m in self._missions.values()
                if m.pipeline_id == completed.pipeline_id
                and m.status == "QUEUED"
                and m.pipeline_seq > completed.pipeline_seq
            ]
            if pipeline_missions:
                pipeline_missions.sort(key=lambda m: m.pipeline_seq)
                return pipeline_missions[0]

        return None

    # ------------------------------------------------------------------
    # Auto-deploy
    # ------------------------------------------------------------------

    def set_auto_deploy(self, enabled: bool, max_concurrent: int = 3) -> None:
        self._auto_deploy = enabled
        self._max_concurrent = max_concurrent

    def should_auto_deploy(self, active_count: int) -> bool:
        return (
            self._auto_deploy
            and bool(self.queued())
            and active_count < self._max_concurrent
        )

    # ------------------------------------------------------------------
    # File-based sync (.sortie/mission-queue/ directory)
    # ------------------------------------------------------------------

    def sync_from_dir(self, queue_dir: Path) -> int:
        """Sync missions from a directory of JSON files.

        Each file in .sortie/mission-queue/ is one mission:
          ENG-200.json -> {"id": "ENG-200", "title": "...", ...}

        Returns the number of new missions added.

        File format (what Mini Boss writes):
        {
          "id": "ENG-200",
          "title": "Auth token rotation",
          "source": "linear",
          "priority": 2,
          "model": "sonnet",
          "agent_count": 1,
          "directive": "Implement token rotation...",
          "created_at": 1710345600
        }
        """
        if not queue_dir.is_dir():
            return 0

        added = 0
        seen_ids: set[str] = set()

        for f in sorted(queue_dir.iterdir()):
            if f.suffix != ".json" or f.name.startswith("."):
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue

            mission_id = data.get("id") or f.stem
            seen_ids.add(mission_id)

            # Skip if already tracked (don't overwrite in-progress status)
            if mission_id in self._missions:
                continue

            mission = Mission(
                id=mission_id,
                title=data.get("title", mission_id),
                source=data.get("source", "linear"),
                priority=data.get("priority", 2),
                directives=[data.get("directive", "")],
                agent_count=data.get("agent_count", 1),
                model=data.get("model", "sonnet"),
                status="QUEUED",
                spec_content=data.get("directive", ""),
                branch_name=data.get("branch_name", ""),
                created_at=data.get("created_at", time.time()),
                pipeline_id=data.get("pipeline_id", ""),
                pipeline_seq=data.get("pipeline_seq", 0),
                next_mission_id=data.get("next_mission_id", ""),
            )
            self.add(mission)
            added += 1

        # Remove file-sourced QUEUED missions whose file was deleted
        for mid, m in list(self._missions.items()):
            if m.status == "QUEUED" and mid not in seen_ids:
                # Only remove if it was file-sourced (not added via /queue adhoc)
                if m.source in ("linear", "file"):
                    del self._missions[mid]

        return added
