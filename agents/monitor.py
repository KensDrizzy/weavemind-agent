"""Sub-agent heartbeat monitor with stale detection and interruption hooks."""

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class SubAgentStatus(str, Enum):
    RUNNING = "running"
    THINKING = "thinking"
    IN_TOOL = "in_tool"
    IDLE = "idle"
    STALE = "stale"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass
class SubAgentHeartbeat:
    subagent_id: str
    status: SubAgentStatus = SubAgentStatus.RUNNING
    last_heartbeat: float = 0.0
    current_tool: str = ""
    cycle_count: int = 0
    in_tool_cycle_count: int = 0
    future: Any = None

    def __post_init__(self):
        if not self.last_heartbeat:
            self.last_heartbeat = time.time()


class SubAgentMonitor:
    """Tracks sub-agent heartbeats and marks stalled children as stale.

    The monitor distinguishes normal idle/thinking stalls from long-running tool
    stalls. Defaults match the P0 plan: 30s heartbeat interval, 15 idle cycles
    and 40 in-tool cycles.
    """

    def __init__(
        self,
        heartbeat_interval: int = 30,
        stale_cycles_idle: int = 15,
        stale_cycles_in_tool: int = 40,
    ):
        self.heartbeat_interval = heartbeat_interval
        self.stale_cycles_idle = stale_cycles_idle
        self.stale_cycles_in_tool = stale_cycles_in_tool
        self._active: dict[str, SubAgentHeartbeat] = {}
        self._paused = False
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def register(self, subagent_id: str, future=None) -> SubAgentHeartbeat:
        hb = SubAgentHeartbeat(subagent_id=subagent_id, future=future)
        with self._lock:
            self._active[subagent_id] = hb
        return hb

    def attach_future(self, subagent_id: str, future) -> None:
        with self._lock:
            hb = self._active.get(subagent_id)
            if hb:
                hb.future = future

    def heartbeat(
        self,
        subagent_id: str,
        status: SubAgentStatus,
        tool: str = "",
    ) -> None:
        with self._lock:
            hb = self._active.get(subagent_id)
            if not hb:
                return
            hb.last_heartbeat = time.time()
            hb.status = status
            hb.current_tool = tool
            if status == SubAgentStatus.IDLE:
                hb.cycle_count += 1
                hb.in_tool_cycle_count = 0
            elif status == SubAgentStatus.IN_TOOL:
                hb.in_tool_cycle_count += 1
                hb.cycle_count = 0
            elif status not in (SubAgentStatus.STALE, SubAgentStatus.INTERRUPTED):
                hb.cycle_count = 0
                hb.in_tool_cycle_count = 0

    def check_stale(self) -> list[str]:
        stale_ids = []
        with self._lock:
            now = time.time()
            for subagent_id, hb in list(self._active.items()):
                if hb.status in {
                    SubAgentStatus.COMPLETED,
                    SubAgentStatus.FAILED,
                    SubAgentStatus.INTERRUPTED,
                    SubAgentStatus.STALE,
                }:
                    continue

                elapsed = now - hb.last_heartbeat
                if hb.status == SubAgentStatus.IN_TOOL:
                    max_elapsed = self.heartbeat_interval * self.stale_cycles_in_tool
                    stale_by_cycles = hb.in_tool_cycle_count >= self.stale_cycles_in_tool
                    stale_reason = "工具内"
                else:
                    max_elapsed = self.heartbeat_interval * self.stale_cycles_idle
                    stale_by_cycles = hb.cycle_count >= self.stale_cycles_idle
                    stale_reason = "空闲"

                if elapsed >= max_elapsed or stale_by_cycles:
                    hb.status = SubAgentStatus.STALE
                    stale_ids.append(subagent_id)
                    logger.warning(
                        "子 Agent %s %s停滞 %.0fs, 标记为 STALE",
                        subagent_id,
                        stale_reason,
                        elapsed,
                    )
        return stale_ids

    def unregister(self, subagent_id: str) -> None:
        with self._lock:
            self._active.pop(subagent_id, None)

    def get(self, subagent_id: str) -> SubAgentHeartbeat | None:
        with self._lock:
            return self._active.get(subagent_id)

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self._paused = paused

    @property
    def is_paused(self) -> bool:
        with self._lock:
            return self._paused

    def interrupt(self, subagent_id: str) -> bool:
        with self._lock:
            hb = self._active.get(subagent_id)
            if not hb:
                return False
            hb.status = SubAgentStatus.INTERRUPTED
            if hb.future and not hb.future.done():
                hb.future.cancel()
            logger.info("子 Agent %s 已请求中断", subagent_id)
            return True

    def start_heartbeat_thread(self) -> threading.Thread:
        if self._thread and self._thread.is_alive():
            return self._thread

        def _loop():
            while not self._stop_event.wait(self.heartbeat_interval):
                for subagent_id in self.check_stale():
                    self.interrupt(subagent_id)

        self._thread = threading.Thread(target=_loop, daemon=True, name="subagent-hb")
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop_event.set()
