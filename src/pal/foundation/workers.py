"""Join owner-scoped workers before releasing their database resources."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


async def run_worker(function: Callable[[], T]) -> T:
    task = asyncio.create_task(asyncio.to_thread(function))
    return await join_owned_task(task)


async def join_owned_task(task: asyncio.Task[T]) -> T:
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception as error:
                raise cancelled from error
        try:
            task.result()
        except Exception as error:
            raise cancelled from error
        raise
