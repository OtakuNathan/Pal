from __future__ import annotations

import asyncio
import contextlib
import unittest

from pal.bunshin.background_assignments import BackgroundAssignments


class BackgroundAssignmentsTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_generation_cannot_clear_replacement_readiness(self):
        assignments = BackgroundAssignments()
        old = asyncio.create_task(asyncio.sleep(0, result={}))
        assignments.track('effect', old)
        # Queue replacement in the task's completion turn, before its old
        # done callback executes.
        while not old.done():
            await asyncio.sleep(0)
        replacement = asyncio.create_task(asyncio.Event().wait())
        assignments.recover('effect', 'new-assignment', replacement)
        await asyncio.sleep(0)
        self.assertIs(assignments.task('effect'), replacement)
        ready = assignments._ready.get('effect')
        self.assertIsNotNone(ready)
        self.assertTrue(ready.is_set())
        await assignments.drain(timeout_seconds=0)

    async def test_cancelled_launcher_retires_only_its_readiness_waiter(self):
        assignments = BackgroundAssignments()
        started = asyncio.Event()
        finish = asyncio.Event()

        async def run_loop(effect, runner):
            started.set()
            await finish.wait()
            return {}

        async def runner(effect):
            return {}

        before = asyncio.all_tasks()
        launch = asyncio.create_task(assignments.launch({'effect_key': 'effect'}, runner, run_loop))
        await started.wait()
        worker = assignments.task('effect')
        launch.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await launch
        try:
            self.assertFalse(worker.done())
            remaining = asyncio.all_tasks() - before - {worker}
            self.assertEqual(remaining, set(), 'launcher leaked its readiness waiter')
        finally:
            finish.set()
            await assignments.drain(timeout_seconds=1)
            # Also clean up a leaked waiter when demonstrating the regression.
            for task in asyncio.all_tasks() - before:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
