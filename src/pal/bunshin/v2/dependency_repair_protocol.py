"""Reference-only, graph-generation-local dependency repair coordination.

These values contain control identities and immutable artifact references, never
report bodies. Process/lease cleanup is performed by the runtime. A closure is
an explicit attestation about one exact admitted incarnation, not an inference
from a logical cycle state. All transitions must be persisted under the same
write transaction as graph admission and role-submission closure.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Mapping


class DependencyRepairDeferred(ValueError):
    """An independent reporter belongs to a later cohort, after current apply."""


_REF_FIELDS = frozenset({
    "sha256", "artifact_type", "schema_version", "media_type", "byte_size",
    "durable",
})


_INTENT_FIELDS = frozenset({
    'graph_id',
    'generation',
    'generation_hash',
    'source_node',
    'source_cycle_id',
    'source_aggregate_id',
    'source_assignment_id',
    'source_input_fingerprint',
    'source_product_ref',
    'source_fencing_token',
    'candidate_ref',
    'packet_ref',
    'candidate_digest',
    'submission_payload_hash',
    'provider_nodes',
    'pending_verification_ref',
})

_INCARNATION_FIELDS = frozenset({
    'node_name',
    'cycle_id',
    'generation',
    'input_fingerprint',
    'slot',
    'aggregate_id',
    'effect_id',
    'effect_key',
    'role_assignment_id',
    'attempt_id',
    'lease_resource',
    'lease_owner',
    'fencing_token',
    'snapshot_lease_resource',
    'snapshot_lease_owner',
    'snapshot_fencing_token',
})

_CLOSURE_FIELDS = frozenset({
    'incarnation',
    'task_closed',
    'process_reaped',
    'workspace_released',
    'lease_released',
    'submission_cut',
    'receipt_refs',
})

_COHORT_FIELDS = frozenset({
    'intents',
    'scope',
    'frontier',
    'captures',
    'closures',
    'status',
    'superseded_reason',
})

def _ref(value: Mapping[str, Any], *, optional: bool = False) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("repair evidence must be an artifact reference")
    copied = dict(value)
    if optional and not copied:
        return MappingProxyType({})
    if set(copied) - _REF_FIELDS or not isinstance(copied.get("sha256"), str) or not copied["sha256"]:
        raise ValueError("repair evidence requires a reference SHA and no payload")
    for name in ("artifact_type", "schema_version", "media_type"):
        if name in copied and not isinstance(copied[name], str):
            raise ValueError(f"repair reference {name} must be a string")
    if "byte_size" in copied and (
        type(copied["byte_size"]) is not int or copied["byte_size"] < 0
    ):
        raise ValueError("repair reference byte_size must be a nonnegative integer")
    if "durable" in copied and type(copied["durable"]) is not bool:
        raise ValueError("repair reference durable must be a boolean")
    return MappingProxyType(copied)


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _required(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"repair {name} is required")


def _positive(value: Any, name: str) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"repair {name} must be a positive integer")


def _strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"repair {name} must be a sequence of nonempty strings")
    return tuple(sorted(set(value)))


def _mapping(value: Any, required: set[str] | frozenset[str], optional: frozenset[str] = frozenset()) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - required - optional or required - set(value):
        raise ValueError("malformed dependency repair protocol section")
    return dict(value)


@dataclass(frozen=True)
class RepairIntent:
    graph_id: str
    generation: int
    generation_hash: str
    source_node: str
    source_cycle_id: str
    source_aggregate_id: str
    source_assignment_id: str
    source_input_fingerprint: str
    source_product_ref: str
    source_fencing_token: int
    candidate_ref: Mapping[str, Any]
    packet_ref: Mapping[str, Any]
    candidate_digest: str
    submission_payload_hash: str
    provider_nodes: tuple[str, ...]
    pending_verification_ref: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in {
            "graph_id": self.graph_id,
            "generation_hash": self.generation_hash,
            "source_node": self.source_node,
            "source_cycle_id": self.source_cycle_id,
            "source_aggregate_id": self.source_aggregate_id,
            "source_assignment_id": self.source_assignment_id,
            "source_input_fingerprint": self.source_input_fingerprint,
            "source_product_ref": self.source_product_ref,
            "candidate_digest": self.candidate_digest,
            "submission_payload_hash": self.submission_payload_hash,
        }.items():
            _required(value, name)
        _positive(self.generation, "generation")
        _positive(self.source_fencing_token, "source_fencing_token")
        object.__setattr__(self, "candidate_ref", _ref(self.candidate_ref))
        object.__setattr__(self, "packet_ref", _ref(self.packet_ref))
        object.__setattr__(self, "pending_verification_ref", _ref(self.pending_verification_ref, optional=True))
        object.__setattr__(self, "provider_nodes", _strings(self.provider_nodes, "provider_nodes"))
        if not self.provider_nodes:
            raise ValueError("dependency repair intent requires validated providers")

    @property
    def packet_sha256(self) -> str:
        return str(self.packet_ref["sha256"])

    @property
    def key(self) -> str:
        return _hash([self.graph_id, self.generation, self.generation_hash,
                      self.source_aggregate_id, self.source_cycle_id,
                      self.source_assignment_id, self.packet_sha256])

    def to_dict(self) -> dict[str, Any]:
        return {
            "graph_id": self.graph_id,
            "generation": self.generation,
            "generation_hash": self.generation_hash,
            "source_node": self.source_node,
            "source_cycle_id": self.source_cycle_id,
            "source_aggregate_id": self.source_aggregate_id,
            "source_assignment_id": self.source_assignment_id,
            "source_input_fingerprint": self.source_input_fingerprint,
            "source_product_ref": self.source_product_ref,
            "source_fencing_token": self.source_fencing_token,
            "candidate_ref": dict(self.candidate_ref),
            "packet_ref": dict(self.packet_ref),
            "candidate_digest": self.candidate_digest,
            "submission_payload_hash": self.submission_payload_hash,
            "provider_nodes": list(self.provider_nodes),
            "pending_verification_ref": dict(self.pending_verification_ref),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> RepairIntent:
        return cls(**_mapping(value, _INTENT_FIELDS))


@dataclass(frozen=True)
class RepairIncarnation:
    node_name: str
    cycle_id: str
    generation: int
    input_fingerprint: str
    slot: str
    aggregate_id: str
    effect_id: str
    effect_key: str
    role_assignment_id: str = ""
    attempt_id: str = ""
    lease_resource: str = ""
    lease_owner: str = ""
    fencing_token: int = 0
    snapshot_lease_resource: str = ""
    snapshot_lease_owner: str = ""
    snapshot_fencing_token: int = 0

    def __post_init__(self) -> None:
        for name, value in {
            "node_name": self.node_name,
            "cycle_id": self.cycle_id,
            "input_fingerprint": self.input_fingerprint,
            "aggregate_id": self.aggregate_id,
            "effect_id": self.effect_id,
            "effect_key": self.effect_key,
        }.items():
            _required(value, name)
        _positive(self.generation, "generation")
        if self.slot not in {"producer", "checker"}:
            raise ValueError("repair incarnation slot must be producer or checker")
        for name, value in {
            "role_assignment_id": self.role_assignment_id,
            "attempt_id": self.attempt_id,
            "lease_resource": self.lease_resource,
            "lease_owner": self.lease_owner,
            "snapshot_lease_resource": self.snapshot_lease_resource,
            "snapshot_lease_owner": self.snapshot_lease_owner,
        }.items():
            if not isinstance(value, str):
                raise ValueError(f"repair {name} must be a string")
        if type(self.fencing_token) is not int or self.fencing_token < 0:
            raise ValueError("repair fencing_token must be a nonnegative integer")
        if bool(self.lease_resource) != bool(self.lease_owner) or bool(self.lease_owner) != bool(self.fencing_token):
            raise ValueError("repair lease identity must be complete or absent")
        if type(self.snapshot_fencing_token) is not int or self.snapshot_fencing_token < 0:
            raise ValueError("repair snapshot_fencing_token must be a nonnegative integer")
        if (bool(self.snapshot_lease_resource) != bool(self.snapshot_lease_owner)
                or bool(self.snapshot_lease_owner) != bool(self.snapshot_fencing_token)):
            raise ValueError("repair snapshot lease identity must be complete or absent")
        if self.attempt_id and not self.role_assignment_id:
            raise ValueError("repair attempt requires its role assignment")

    @property
    def key(self) -> str:
        # Setup owns this identity before a role row or lease exists. Late
        # binding can enrich it but cannot create a replacement admission.
        return _hash([self.node_name, self.cycle_id, self.generation,
                      self.input_fingerprint, self.slot, self.aggregate_id,
                      self.effect_id, self.effect_key])

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_name": self.node_name,
            "cycle_id": self.cycle_id,
            "generation": self.generation,
            "input_fingerprint": self.input_fingerprint,
            "slot": self.slot,
            "aggregate_id": self.aggregate_id,
            "effect_id": self.effect_id,
            "effect_key": self.effect_key,
            "role_assignment_id": self.role_assignment_id,
            "attempt_id": self.attempt_id,
            "lease_resource": self.lease_resource,
            "lease_owner": self.lease_owner,
            "fencing_token": self.fencing_token,
            "snapshot_lease_resource": self.snapshot_lease_resource,
            "snapshot_lease_owner": self.snapshot_lease_owner,
            "snapshot_fencing_token": self.snapshot_fencing_token,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> RepairIncarnation:
        return cls(**_mapping(value, _INCARNATION_FIELDS))

    def bind(self, newer: RepairIncarnation) -> RepairIncarnation:
        if self.key != newer.key:
            raise ValueError("repair frontier cannot admit a replacement incarnation")
        old, new = self.to_dict(), newer.to_dict()
        if any(old[name] and old[name] != value for name, value in new.items()):
            raise ValueError("repair incarnation ownership changed after admission")
        return newer


@dataclass(frozen=True)
class RepairClosure:
    """Proof for the original admission, including both original/snapshot leases.

    lease_released attests that neither exact saved lease identity remains
    owned. A snapshot's rebound token never overwrites the submission token.
    """
    incarnation: RepairIncarnation
    task_closed: bool
    process_reaped: bool
    workspace_released: bool
    lease_released: bool
    submission_cut: bool
    receipt_refs: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.incarnation, RepairIncarnation):
            raise ValueError("repair closure requires an exact incarnation")
        for name, value in {
            "task_closed": self.task_closed,
            "process_reaped": self.process_reaped,
            "workspace_released": self.workspace_released,
            "lease_released": self.lease_released,
            "submission_cut": self.submission_cut,
        }.items():
            if value is not True:
                raise ValueError(f"repair closure requires {name}")
        object.__setattr__(self, "receipt_refs", _refs(self.receipt_refs))

    def to_dict(self) -> dict[str, Any]:
        return {"incarnation": self.incarnation.to_dict(),
                "task_closed": self.task_closed, "process_reaped": self.process_reaped,
                "workspace_released": self.workspace_released, "lease_released": self.lease_released,
                "submission_cut": self.submission_cut,
                "receipt_refs": [dict(ref) for ref in self.receipt_refs]}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> RepairClosure:
        data = _mapping(value, _CLOSURE_FIELDS)
        data["incarnation"] = RepairIncarnation.from_mapping(data["incarnation"])
        return cls(**data)


def _refs(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("repair receipt_refs must be an array")
    refs = [_ref(ref) for ref in value]
    by_sha: dict[str, Mapping[str, Any]] = {}
    for ref in refs:
        previous = by_sha.setdefault(ref["sha256"], ref)
        if previous != ref:
            raise ValueError("repair reference SHA is bound to changed metadata")
    return tuple(by_sha[sha] for sha in sorted(by_sha))


@dataclass(frozen=True)
class DependencyRepairCohort:
    intents: Mapping[str, RepairIntent]
    scope: tuple[str, ...]
    frontier: Mapping[str, RepairIncarnation] = field(default_factory=dict)
    captures: Mapping[str, tuple[Mapping[str, Any], ...]] = field(default_factory=dict)
    closures: Mapping[str, RepairClosure] = field(default_factory=dict)
    status: str = "pending"
    superseded_reason: str = ""

    def __post_init__(self) -> None:
        if not self.intents or any(not isinstance(intent, RepairIntent) or key != intent.key for key, intent in self.intents.items()):
            raise ValueError("repair cohort has invalid intent identities")
        identities = {(item.graph_id, item.generation, item.generation_hash) for item in self.intents.values()}
        if len(identities) != 1:
            raise ValueError("repair cohort cannot cross graph generations")
        object.__setattr__(self, "scope", _strings(self.scope, "scope"))
        if any(item.source_node not in self.scope or set(item.provider_nodes) - set(self.scope) for item in self.intents.values()):
            raise ValueError("repair cohort scope excludes an intent participant")
        if any(not isinstance(item, RepairIncarnation) or key != item.key or item.node_name not in self.scope for key, item in self.frontier.items()):
            raise ValueError("repair cohort frontier has invalid incarnation identities")
        generation = next(iter(identities))[1]
        if any(item.generation != generation for item in self.frontier.values()):
            raise ValueError("repair frontier cannot cross graph generations")
        if len({item.node_name for item in self.frontier.values()}) != len(self.frontier):
            raise ValueError("repair cohort cannot freeze multiple incarnations of one node")
        if set(self.captures) - set(self.frontier) or set(self.closures) - set(self.frontier):
            raise ValueError("repair evidence refers to an unadmitted incarnation")
        captures = {key: _refs(refs) for key, refs in self.captures.items()}
        for key, closure in self.closures.items():
            if not isinstance(closure, RepairClosure) or closure.incarnation != self.frontier[key]:
                raise ValueError("repair closure does not match frozen ownership")
            if key not in captures or captures[key] != closure.receipt_refs:
                raise ValueError("repair closure must capture the complete final receipt set")
        if self.status not in {"pending", "applied", "superseded"}:
            raise ValueError("unknown repair cohort status")
        if (not isinstance(self.superseded_reason, str)
                or bool(self.superseded_reason) != (self.status == "superseded")):
            raise ValueError("superseded repair cohort requires a reason")
        if self.status == "applied" and set(self.closures) != set(self.frontier):
            raise ValueError("applied repair cohort has an unclosed frontier")
        object.__setattr__(self, "intents", MappingProxyType(dict(self.intents)))
        object.__setattr__(self, "frontier", MappingProxyType(dict(self.frontier)))
        object.__setattr__(self, "closures", MappingProxyType(dict(self.closures)))
        object.__setattr__(self, "captures", MappingProxyType(captures))

    @property
    def key(self) -> str:
        return _hash(sorted(self.intents))

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(sorted({name for intent in self.intents.values() for name in intent.provider_nodes}))

    @property
    def ready(self) -> bool:
        return self.status == "pending" and set(self.closures) == set(self.frontier)

    def to_dict(self) -> dict[str, Any]:
        return {"intents": {key: item.to_dict() for key, item in sorted(self.intents.items())},
                "scope": list(self.scope),
                "frontier": {key: item.to_dict() for key, item in sorted(self.frontier.items())},
                "captures": {key: [dict(ref) for ref in refs] for key, refs in sorted(self.captures.items())},
                "closures": {key: item.to_dict() for key, item in sorted(self.closures.items())},
                "status": self.status, "superseded_reason": self.superseded_reason}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> DependencyRepairCohort:
        data = _mapping(value, _COHORT_FIELDS)
        for name, factory in (("intents", RepairIntent), ("frontier", RepairIncarnation), ("closures", RepairClosure)):
            if not isinstance(data[name], Mapping):
                raise ValueError("malformed repair cohort collection")
            data[name] = {key: factory.from_mapping(item) for key, item in data[name].items()}
        if not isinstance(data["captures"], Mapping):
            raise ValueError("malformed repair captures")
        return cls(**data)

    def with_frontier(self, frontier: tuple[RepairIncarnation, ...]) -> DependencyRepairCohort:
        if self.status != "pending":
            raise ValueError("a sealed repair cohort cannot change its frontier")
        values = dict(self.frontier)
        by_node = {item.node_name: key for key, item in values.items()}
        for item in frontier:
            if item.node_name in by_node and by_node[item.node_name] != item.key:
                raise ValueError("repair admission fence forbids a replacement incarnation")
            if item.key in values:
                values[item.key].bind(item)
                if item.key in self.closures and item != values[item.key]:
                    raise ValueError("a closed repair incarnation cannot acquire ownership")
            values[item.key] = item
            by_node[item.node_name] = item.key
        return replace(self, frontier=values)

    def capture(self, key: str, receipt_refs: tuple[Mapping[str, Any], ...]) -> DependencyRepairCohort:
        if self.status != "pending" or key not in self.frontier:
            raise ValueError("capture requires a pending frozen incarnation")
        refs = _refs((*self.captures.get(key, ()), *receipt_refs))
        if key in self.closures and refs != self.captures[key]:
            raise ValueError("submission cut forbids another receipt after closure")
        return replace(self, captures={**self.captures, key: refs})

    def close(self, proof: RepairClosure) -> DependencyRepairCohort:
        key = proof.incarnation.key
        if self.status != "pending" or self.frontier.get(key) != proof.incarnation:
            raise ValueError("repair closure does not match the exact frozen incarnation")
        result = self.capture(key, proof.receipt_refs)
        if result.captures[key] != proof.receipt_refs:
            raise ValueError("repair closure omitted a captured submission")
        if key in self.closures and self.closures[key] != proof:
            raise ValueError("repair closure replay changed its proof")
        return replace(result, closures={**result.closures, key: proof})


@dataclass(frozen=True)
class DependencyRepairLedger:
    pending: DependencyRepairCohort | None = None
    history: tuple[DependencyRepairCohort, ...] = ()

    VERSION = 1

    def __post_init__(self) -> None:
        if self.pending is not None and self.pending.status != "pending":
            raise ValueError("repair pending cohort must be unsealed")
        if any(item.status == "pending" for item in self.history):
            raise ValueError("repair history cannot contain pending cohorts")
        keys = [key for cohort in (*self.history, *((self.pending,) if self.pending else ())) for key in cohort.intents]
        if len(keys) != len(set(keys)):
            raise ValueError("repair intent belongs to more than one cohort")
        object.__setattr__(self, "history", tuple(self.history))

    @property
    def fenced_nodes(self) -> frozenset[str]:
        return frozenset(self.pending.scope if self.pending else ())

    def find_intent(self, key: str) -> RepairIntent | None:
        for cohort in (*self.history, *((self.pending,) if self.pending else ())):
            if key in cohort.intents:
                return cohort.intents[key]
        return None

    def archive(self, *, status: str, reason: str = "") -> DependencyRepairLedger:
        if self.pending is None:
            return self
        if status not in {"applied", "superseded"}:
            raise ValueError("repair archive requires a terminal status")
        return replace(self, pending=None, history=(*self.history, replace(self.pending, status=status, superseded_reason=reason)))

    def assert_successor(self, newer: DependencyRepairLedger) -> None:
        """Reject stale whole-execution writes that would lose durable fences.

        This check is performed against the currently stored ledger while the
        database write lock is held. Pure transitions alone cannot prevent a
        caller which read before registration from overwriting its evidence.
        """
        if newer.history[:len(self.history)] != self.history:
            raise ValueError("stale graph execution would discard sealed repair evidence")
        if self.pending is None:
            return
        old = self.pending
        candidates = (*newer.history[len(self.history):],
                      *((newer.pending,) if newer.pending else ()))
        matches = [item for item in candidates if set(old.intents) <= set(item.intents)]
        if len(matches) != 1:
            raise ValueError("stale graph execution would discard pending repair fences")
        new = matches[0]
        if not set(old.scope) <= set(new.scope):
            raise ValueError("stale graph execution would shrink repair scope")
        for key, intent in old.intents.items():
            if new.intents[key] != intent:
                raise ValueError("stale graph execution would change repair intent identity")
        for key, member in old.frontier.items():
            if key not in new.frontier:
                raise ValueError("stale graph execution would discard frozen ownership")
            member.bind(new.frontier[key])
        for key, refs in old.captures.items():
            newer_refs = {ref["sha256"]: ref for ref in new.captures.get(key, ())}
            if any(newer_refs.get(ref["sha256"]) != ref for ref in refs):
                raise ValueError("stale graph execution would discard captured receipts")
        if any(new.closures.get(key) != proof for key, proof in old.closures.items()):
            raise ValueError("stale graph execution would discard matched closure proof")

    def to_dict(self) -> dict[str, Any]:
        return {"version": self.VERSION, "pending": self.pending.to_dict() if self.pending else None,
                "history": [item.to_dict() for item in self.history]}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> DependencyRepairLedger:
        # Absence is the only legacy default. Present malformed sections fail
        # closed rather than silently discarding their admission fences.
        if value is None:
            return cls()
        data = _mapping(value, {"version", "pending", "history"})
        if type(data["version"]) is not int or data["version"] != cls.VERSION:
            raise ValueError("unsupported dependency repair protocol version")
        if not isinstance(data["history"], list):
            raise ValueError("repair history must be an array")
        return cls(pending=DependencyRepairCohort.from_mapping(data["pending"]) if data["pending"] is not None else None,
                   history=tuple(DependencyRepairCohort.from_mapping(item) for item in data["history"]))
