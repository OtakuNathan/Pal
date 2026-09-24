"""L1-owned output files. Publishing and retiring references are separate from I/O."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import logging
import os
from pathlib import Path
import tempfile
import threading
from uuid import uuid4

from pal.shared.result_snapshot import ResultSnapshotRef, turn_snapshot_refs

LOG = logging.getLogger(__name__)


class ResultSnapshotStore:
    def __init__(self, runtime_root: Path | None = None):
        self.root = ((Path(runtime_root) / "data" / "result-snapshots") if runtime_root
                     else Path(tempfile.mkdtemp(prefix="pal-result-snapshots-")))
        self._refs: dict[str, ResultSnapshotRef] = {}
        self._owners: dict[object, set[str]] = {}
        self._pending: dict[tuple[str, str], set[str]] = {}
        self._histories: dict[int, object] = {}
        self._listeners: dict[int, object] = {}
        self._lock = threading.RLock()

    def capture(self, text: str, *, call_id: str, lifetime: str) -> ResultSnapshotRef:
        return self.capture_chunks((text.encode("utf-8"),), call_id=call_id, lifetime=lifetime)

    def capture_chunks(self, chunks, *, call_id: str, lifetime: str) -> ResultSnapshotRef:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        identity = uuid4().hex
        path = self.root / (identity + ".txt")
        temporary = self.root / (identity + ".pending")
        digest, size = hashlib.sha256(), 0
        try:
            with temporary.open("xb") as output:
                os.chmod(temporary, 0o600)
                for chunk in chunks:
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o400)
        except BaseException:
            temporary.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            raise
        ref = ResultSnapshotRef(identity, str(path.resolve()), digest.hexdigest(), size, call_id)
        with self._lock:
            self._refs[identity] = ref
            self._pending.setdefault((lifetime, call_id), set()).add(identity)
        return ref

    def lookup_path(self, path) -> ResultSnapshotRef | None:
        resolved = str(Path(path).expanduser().resolve())
        with self._lock:
            return next((r for r in self._refs.values() if r.path == resolved), None)

    def manages_path(self, path, *, include_parents=False) -> bool:
        candidate, root = Path(path).expanduser().resolve(), self.root.resolve()
        return candidate.is_relative_to(root) or (include_parents and root.is_relative_to(candidate))

    def pin_history_request(self, history, turn_id: str) -> None:
        self.bind_history(history)
        with self._lock:
            ids = set().union(*(refs for owner, refs in self._owners.items()
                                if isinstance(owner, tuple) and owner[0] == id(history)))
            self.own(("request", turn_id), (self._refs[i] for i in ids))
            self.reap()

    def finish_turn(self, turn_id: str) -> None:
        self.release(("request", turn_id))

    def references(self) -> tuple[ResultSnapshotRef, ...]:
        with self._lock:
            return tuple(self._refs.values())

    def own(self, owner: object, refs) -> None:
        with self._lock:
            refs = tuple(refs)
            for ref in refs:
                self._validate_path(ref)
                self._refs[ref.snapshot_id] = ref
            if refs:
                self._owners[owner] = {ref.snapshot_id for ref in refs}
            else:
                self._owners.pop(owner, None)

    def release(self, owner: object) -> None:
        with self._lock:
            self._owners.pop(owner, None)
            self.reap()

    def finish_references(self, refs) -> None:
        ids = {ref.snapshot_id for ref in refs}
        with self._lock:
            for key, pending in tuple(self._pending.items()):
                pending.difference_update(ids)
                if not pending:
                    self._pending.pop(key, None)
            self.reap()

    def finish_delivery(self, *, lifetime: str, call_id: str) -> None:
        with self._lock:
            self._pending.pop((lifetime, call_id), None)
            self.reap()

    def retain_delivery(self, refs, *, lifetime: str, call_id: str) -> None:
        """Pin an existing copy while a read hands it to a new L1 result."""
        with self._lock:
            for ref in refs:
                self._validate_path(ref)
                self._refs[ref.snapshot_id] = ref
                self._pending.setdefault((lifetime, call_id), set()).add(ref.snapshot_id)

    def bind_history(self, history) -> None:
        key = id(history)
        with self._lock:
            if key in self._histories:
                return
            self._histories[key] = history
            def changed(old, new):
                # Acquire all successor owners before dropping predecessors.
                with self._lock:
                    additions = [(turn.turn_id, turn_snapshot_refs(turn)) for turn in new]
                    for _, refs in additions:
                        for ref in refs:
                            self._validate_path(ref)
                    for identity, refs in additions:
                        self.own((key, identity), refs)
                    new_ids = {t.turn_id for t in new}
                    for turn in old:
                        if turn.turn_id not in new_ids:
                            self._owners.pop((key, turn.turn_id), None)
                    self.reap()
            def validate(turns):
                for turn in turns:
                    for ref in turn_snapshot_refs(turn):
                        self._validate_path(ref)
            validate(history.turns)
            history.add_change_listener(changed, validate=validate)
            self._listeners[key] = changed
            changed((), history.turns)

    def detach_histories(self):
        """Restore boundary: discard old roots while restored references are pinned."""
        with self._lock:
            for key, history in self._histories.items():
                history.remove_change_listener(self._listeners[key])
                for owner in tuple(self._owners):
                    if isinstance(owner, tuple) and owner[0] == key:
                        self._owners.pop(owner)
            self._histories.clear()
            self._listeners.clear()

    def reset_transient(self):
        with self._lock:
            self._pending.clear()
            for owner in tuple(self._owners):
                if owner == "restoring" or isinstance(owner, tuple) and owner[0] == "request":
                    self._owners.pop(owner)
            self.reap()

    @contextmanager
    def pin(self, refs):
        owner = object()
        self.own(owner, refs)
        try:
            yield
        finally:
            self.release(owner)

    def _validate_path(self, ref):
        expected = self.root.resolve() / (ref.snapshot_id + ".txt")
        if (len(ref.snapshot_id) != 32 or any(c not in "0123456789abcdef" for c in ref.snapshot_id)
                or Path(ref.path) != expected
                or expected.is_symlink()):
            raise ValueError("invalid result snapshot path")

    def reap(self) -> None:
        with self._lock:
            live = set().union(*self._owners.values(), *self._pending.values())
            for identity, ref in tuple(self._refs.items()):
                if identity in live:
                    continue
                try:
                    self._validate_path(ref)
                    Path(ref.path).unlink(missing_ok=True)
                except (OSError, ValueError):
                    LOG.exception("Could not retire result snapshot %s", identity)
                else:
                    self._refs.pop(identity, None)

    def snapshot_state(self):
        with self._lock:
            return {"refs": [ref.to_dict() for ref in self._refs.values()]}

    def discard_pending(self):
        with self._lock:
            self._pending.clear()
            self.reap()

    def finish_restore(self):
        self.release("restoring")
        if not self.root.exists():
            return
        for path in self.root.iterdir():
            if (path.suffix not in {".txt", ".pending"} or path.stem in self._refs
                    or len(path.stem) != 32 or any(c not in "0123456789abcdef" for c in path.stem)):
                continue
            try:
                path.unlink()
            except OSError:
                LOG.exception("Could not remove orphan snapshot %s", path.name)

    def restore_refs(self, payload):
        # Do not collect until restored L1 has acquired its references.
        restored = {}
        for item in payload.get("refs", ()):
            ref = ResultSnapshotRef.from_dict(item)
            self._validate_path(ref)
            restored[ref.snapshot_id] = ref
        with self._lock:
            self._refs.update(restored)
            self._pending.clear()
            for owner in tuple(self._owners):
                if isinstance(owner, tuple) and owner[0] == "request":
                    self._owners.pop(owner)
            self._owners["restoring"] = set(restored)


def render_snapshot_hint(ref: ResultSnapshotRef) -> str:
    return (f"Complete output snapshot: {ref.path}\n"
            "Immutable output from this invocation, not current source state. "
            "Search this local file with rg or read selected lines with read_file. "
            "Query again if current state is needed; do not replay side-effecting operations merely to refresh output.")


def head_tail(text: str, budget: int) -> tuple[str, tuple[tuple[int, int], ...]]:
    budget = max(0, budget)
    if len(text) <= budget:
        return text, ((0, len(text)),)
    marker = "\n... [output omitted; see complete snapshot] ...\n"
    if budget < len(marker):
        return "", ()
    usable = max(0, budget - len(marker))
    head, tail = (usable + 1) // 2, usable // 2
    return text[:head] + marker + (text[-tail:] if tail else ""), ((0, head), (len(text)-tail, len(text)))


def capture_stream_files(store, streams, *, call_id, lifetime):
    """Copy only the observed byte intervals, decoding bounded chunks as UTF-8."""
    import codecs
    def chunks():
        for label, path, start, end in streams:
            if end < start:
                raise OSError("output snapshot bounds moved backwards")
            yield (label + ":\n").encode()
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            with Path(path).open("rb") as source:
                source.seek(start)
                remaining = end - start
                while remaining:
                    data = source.read(min(256 * 1024, remaining))
                    if not data:
                        raise OSError("output file is shorter than the captured snapshot")
                    remaining -= len(data)
                    yield decoder.decode(data).encode("utf-8")
                yield decoder.decode(b"", final=True).encode("utf-8")
            yield b"\n"
    return store.capture_chunks(chunks(), call_id=call_id, lifetime=lifetime)


def file_preview(ref, budget=1000):
    size = max(0, int(budget))
    with Path(ref.path).open("rb") as source:
        head = source.read(size // 2)
        if ref.size_bytes <= size:
            source.seek(0)
            return source.read(size).decode("utf-8", errors="replace")
        source.seek(max(0, ref.size_bytes - size // 2))
        tail = source.read(size // 2)
    return head.decode("utf-8", errors="replace") + "\n... [output omitted] ...\n" + tail.decode("utf-8", errors="replace")
