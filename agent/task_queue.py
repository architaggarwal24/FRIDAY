"""
F.R.I.D.A.Y. — agent/task_queue.py
Thread-safe priority queue for background agent tasks.
"""

import asyncio
import logging
import threading
import uuid
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class TaskPriority(IntEnum):
    HIGH   = 1
    NORMAL = 2
    LOW    = 3


@dataclass(order=True)
class AgentTask:
    priority:  TaskPriority
    task_id:   str  = field(compare=False)
    goal:      str  = field(compare=False)
    speak:     Optional[Callable] = field(compare=False, default=None)
    on_chunk:  Optional[Callable] = field(compare=False, default=None)
    cancelled: bool = field(compare=False, default=False)
    plan:      Optional[list] = field(compare=False, default=None)


class TaskQueue:
    def __init__(self):
        self._queue:  list[AgentTask] = []
        self._lock = threading.Lock()
        self._active: dict[str, AgentTask] = {}
        self._started = False

    def submit(self, goal: str,
               priority: TaskPriority = TaskPriority.NORMAL,
               speak: Optional[Callable] = None,
               on_chunk: Optional[Callable] = None,
               plan: Optional[list] = None) -> str:
        task_id = uuid.uuid4().hex[:8]
        task = AgentTask(priority=priority, task_id=task_id, goal=goal, speak=speak,
                          on_chunk=on_chunk, plan=plan)
        with self._lock:
            self._queue.append(task)
            self._queue.sort()
        logger.info(f"[Queue] Queued {task_id}: {goal[:60]}" + (" (pre-planned)" if plan else ""))
        self._ensure_started()
        return task_id

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            for t in self._queue:
                if t.task_id == task_id:
                    t.cancelled = True
                    self._queue.remove(t)
                    return True
            if task_id in self._active:
                self._active[task_id].cancelled = True
                return True
        return False

    def _ensure_started(self):
        with self._lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._loop())

    async def _loop(self):
        from agent.planner import plan_task
        from agent.executor import StepExecutor

        while True:
            task = None
            with self._lock:
                if self._queue:
                    task = self._queue.pop(0)

            if task is None:
                await asyncio.sleep(0.4)
                continue

            if task.cancelled:
                continue

            logger.info(f"[Queue] Running {task.task_id}: {task.goal[:60]}")
            self._active[task.task_id] = task

            try:
                plan = task.plan
                if plan is None:
                    plan = await plan_task(task.goal)
                if plan is None:
                    msg = f"Couldn't plan: {task.goal[:50]}. Try rephrasing, boss."
                    if task.speak:
                        task.speak(msg)
                    self._notify_transcript(msg)
                    continue

                executor = StepExecutor(speak_fn=task.speak, on_chunk=task.on_chunk)
                result = await executor.execute_plan(task.goal, plan)

                # execute_plan() already streamed `result` to TTS word-by-word
                # via on_chunk (== task.speak — same closure, see brain/llm.py's
                # agent_task dispatch) as it finished. Calling task.speak(result)
                # again here spoke the whole thing a second time. Neither call
                # ever reached the chat transcript though — task.speak/on_chunk
                # only ever enqueue to TTS (see main.py's _speak/_speak_text) —
                # so a background task's result never showed up in the chat
                # window at all. Push it to the transcript directly instead of
                # re-speaking it.
                self._notify_transcript(result)

                self._notify_ui({"event": "toast", "message": f"Task done: {task.goal[:40]}", "kind": "success"})

            except Exception as e:
                logger.error(f"[Queue] Task {task.task_id} crashed: {e}")
                msg = f"Task failed, boss: {e}"
                if task.speak:
                    task.speak(msg)
                self._notify_transcript(msg)
            finally:
                self._active.pop(task.task_id, None)

    def _notify_ui(self, event: dict):
        """Thread-safe broadcast to the UI from this queue's own background-
        thread event loop. broadcast_from_thread uses run_coroutine_threadsafe
        against the main loop — the previous code here used
        loop.create_task(send_toast(...)) on the queue's OWN loop, but the
        websocket connections send_toast touches belong to the main thread's
        loop, which is a cross-loop hazard asyncio doesn't support. That was
        masked by a bare `except Exception: pass`, so a broken toast never
        surfaced as an error — it just silently never arrived."""
        try:
            from ui.ws_server import broadcast_from_thread
            broadcast_from_thread(event)
        except Exception as e:
            logger.debug(f"[Queue] Could not notify UI ({event.get('event')}): {e}")

    def _notify_transcript(self, text: str):
        if text:
            self._notify_ui({"event": "transcript", "role": "friday", "text": text})


_queue_instance: Optional[TaskQueue] = None
_queue_lock = threading.Lock()  # FIX: guard singleton creation against race condition


def get_queue() -> TaskQueue:
    global _queue_instance
    # FIX: double-checked locking — fast path for already-initialised case
    if _queue_instance is None:
        with _queue_lock:
            if _queue_instance is None:
                _queue_instance = TaskQueue()
    return _queue_instance
