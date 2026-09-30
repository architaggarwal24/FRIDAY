"""
F.R.I.D.A.Y. — agent/executor.py
Executes a planned multi-step task: runs each step, chains step results
through to the ones after it, injects known past failures into replanning
for self-healing, and streams token-by-token output via on_chunk.
synthesize_results() builds a content-aware summary at completion.
"""

import asyncio
import logging
import re
from typing import Callable, Optional

from config import config

logger = logging.getLogger(__name__)


class StepExecutor:
    def __init__(self, speak_fn: Optional[Callable] = None,
                 on_chunk: Optional[Callable] = None):
        self.speak_fn = speak_fn
        self.on_chunk = on_chunk   # streaming callback: on_chunk(text: str)
        self._context: dict = {}   # {step_N_result: value}
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    async def execute_plan(self, goal: str, plan: list[dict]) -> str:
        from brain.llm import _dispatch_tool
        from agent.planner import plan_task, synthesize_results

        results        = []
        step_results   = {}   # step_num -> raw result (for synthesizer)
        replan_count   = 0
        i              = 0
        known_failures = self._load_known_failures()

        while i < len(plan):
            if self._cancelled:
                return "Task cancelled, boss."

            step     = plan[i]
            step_num = step.get("step", i + 1)
            tool     = step.get("tool", "")
            args     = self._inject_context(dict(step.get("args", {})))
            desc     = step.get("description", tool)
            critical = step.get("critical", True)

            logger.info(f"[Executor] Step {step_num}/{len(plan)}: {tool} — {desc}")

            try:
                result = await asyncio.wait_for(
                    _dispatch_tool(tool, args, self.speak_fn),
                    timeout=config.agent.step_timeout_seconds,
                )
                self._context[f"step_{step_num}_result"] = result
                step_results[step_num] = result
                results.append({"step": step_num, "tool": tool, "description": desc, "status": "ok"})
                i += 1

            except asyncio.TimeoutError:
                logger.warning(f"[Executor] Step {step_num} timed out")
                results.append({"step": step_num, "tool": tool, "description": desc, "status": "timeout"})
                step_results[step_num] = None
                i += 1  # skip on timeout

            except Exception as e:
                err_str = str(e)
                logger.error(f"[Executor] Step {step_num} failed: {err_str}")
                results.append({"step": step_num, "tool": tool, "description": desc, "status": f"error: {err_str[:80]}"})
                step_results[step_num] = None

                # Save failure for future healing
                self._save_failure(f"{tool}:{str(args)[:60]}", err_str)
                known_failures[f"{tool}:{str(args)[:60]}"] = err_str[:120]

                recovery = self._recovery_strategy(err_str, replan_count, critical)

                if recovery == "retry":
                    continue
                elif recovery == "skip":
                    i += 1
                elif recovery == "replan" and replan_count < config.agent.max_replan_attempts:
                    replan_count += 1
                    completed_desc = f"{goal} (continue after: {desc})"
                    new_plan = await plan_task(completed_desc, known_failures=known_failures)
                    if new_plan:
                        plan, i = new_plan, 0
                    else:
                        summary = await synthesize_results(goal, step_results, results)
                        return summary
                else:
                    # "abort" or replan limit reached — stop immediately
                    summary = await synthesize_results(goal, step_results, results)
                    return summary

        # All steps done — synthesize content-aware summary
        summary = await synthesize_results(goal, step_results, results)

        # Stream to UI if callback provided
        if self.on_chunk and summary:
            words = summary.split()
            for w in words:
                self.on_chunk(w + " ")
                await asyncio.sleep(0.03)

        return summary

    def _inject_context(self, args: dict) -> dict:
        """Replace {{step_N_result}} placeholders with actual prior outputs."""
        injected = {}
        for k, v in args.items():
            if isinstance(v, str):
                injected[k] = re.sub(
                    r"\{\{step_(\d+)_result\}\}",
                    lambda m: str(self._context.get(f"step_{m.group(1)}_result", ""))[:2000],
                    v
                )
            else:
                injected[k] = v
        return injected

    def _recovery_strategy(self, error: str, replan_count: int, critical: bool) -> str:
        e = error.lower()
        if any(w in e for w in ["permission", "access denied", "not allowed"]):
            return "abort"
        if any(w in e for w in ["not found", "no such", "does not exist"]):
            return "skip" if not critical else "replan"
        if "timeout" in e:
            return "skip"
        if replan_count < config.agent.max_replan_attempts:
            return "replan"
        return "abort"

    def _load_known_failures(self) -> dict:
        try:
            from memory.memory_store import get_action_errors
            return get_action_errors(limit=8)
        except Exception:
            return {}

    def _save_failure(self, key: str, error: str):
        try:
            from memory.memory_store import save_action_error
            save_action_error(key, error[:200])
        except Exception:
            pass
