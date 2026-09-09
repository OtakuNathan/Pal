from pal.execution.runtime_state import ExecutionRuntimeStatePort


class NativeExecutionStatePort(ExecutionRuntimeStatePort):
    def _has_native_work(self):
        owner = self.runtime.shell_owner
        return bool(owner.sessions or owner.pending
                    or (owner._shell is not None and owner._shell._foreground))

    def snapshot_state(self):
        if self._has_native_work():
            raise RuntimeError("Consume or terminate native shell sessions before snapshotting runtime state.")
        return super().snapshot_state()

    def prepare_restore_state(self, payload):
        if self._has_native_work():
            raise RuntimeError("Reset native shell sessions before restoring runtime state.")
        return super().prepare_restore_state(payload)

    async def reset_state(self, reason):
        await self.runtime.shell_owner.reset()
        super().reset_state(reason)
