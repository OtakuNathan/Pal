from __future__ import annotations

from dataclasses import dataclass

from peewee import DoesNotExist, fn

from pal.artifact.contracts import (
    ARTIFACT_STATUS_RETIRED,
    ARTIFACT_STATUS_RETIRING,
    ArtifactHotState,
    ArtifactRecord,
    ArtifactRepresentation,
)
from pal.artifact.models import ArtifactHotStateModel, ArtifactRecordModel, ArtifactRepresentationModel
from pal.foundation.persistence import utc_now


@dataclass
class ArtifactRepository:
    def upsert_record(self, record: ArtifactRecord) -> ArtifactRecord:
        now = utc_now()
        existing = ArtifactRecordModel.get_or_none(ArtifactRecordModel.artifact_id == record.artifact_id)
        created_at = record.created_at or (existing.created_at if existing is not None else now)
        updated_at = record.updated_at or now
        ArtifactRecordModel.insert(
            artifact_id=record.artifact_id,
            scope_key=record.scope_key,
            turn_id=record.turn_id,
            kind=record.kind,
            source_channel=record.source_channel,
            file_name=record.file_name,
            original_path=record.original_path,
            original_mime_type=record.original_mime_type,
            original_size_bytes=record.original_size_bytes,
            normalized_path=record.normalized_path,
            normalized_mime_type=record.normalized_mime_type,
            normalized_size_bytes=record.normalized_size_bytes,
            summary=record.summary,
            status=record.status,
            notes=record.notes,
            metadata_blob=dict(record.metadata),
            created_at=created_at,
            updated_at=updated_at,
        ).on_conflict_replace().execute()
        return self.get_record(record.artifact_id) or record

    def get_record(self, artifact_id: str) -> ArtifactRecord | None:
        try:
            return _record_from_model(ArtifactRecordModel.get_by_id(artifact_id))
        except DoesNotExist:
            return None

    def list_records(self, *, scope_key: str | None = None, turn_id: str | None = None) -> tuple[ArtifactRecord, ...]:
        query = ArtifactRecordModel.select()
        if scope_key:
            query = query.where(ArtifactRecordModel.scope_key == scope_key)
        if turn_id:
            query = query.where(ArtifactRecordModel.turn_id == turn_id)
        query = query.order_by(ArtifactRecordModel.created_at.desc(), ArtifactRecordModel.artifact_id)
        return tuple(_record_from_model(row) for row in query)

    def list_pending_cleanup_records(self, *, scope_key: str | None = None) -> tuple[ArtifactRecord, ...]:
        query = ArtifactRecordModel.select().where(
            ArtifactRecordModel.status == ARTIFACT_STATUS_RETIRING
        )
        if scope_key:
            query = query.where(ArtifactRecordModel.scope_key == scope_key)
        return tuple(_record_from_model(row) for row in query)

    def migrate_legacy_pending_cleanup_records(self) -> int:
        cleanup_state = ArtifactRecordModel.metadata_blob["managed_cleanup"]
        return int(
            ArtifactRecordModel.update(status=ARTIFACT_STATUS_RETIRING)
            .where(
                (ArtifactRecordModel.status == ARTIFACT_STATUS_RETIRED)
                & (fn.COALESCE(cleanup_state, "") != "complete")
            )
            .execute()
        )

    def list_records_missing_hot_state(self, *, scope_key: str | None = None) -> tuple[ArtifactRecord, ...]:
        hot = ArtifactHotStateModel.alias()
        matching_hot = hot.select(hot.hot_id).where(
            (hot.artifact_id == ArtifactRecordModel.artifact_id)
            & (hot.scope_key == ArtifactRecordModel.scope_key)
        )
        query = ArtifactRecordModel.select().where(
            ArtifactRecordModel.status.not_in(
                (ARTIFACT_STATUS_RETIRING, ARTIFACT_STATUS_RETIRED)
            )
            & ~fn.EXISTS(matching_hot)
        )
        if scope_key:
            query = query.where(ArtifactRecordModel.scope_key == scope_key)
        return tuple(_record_from_model(row) for row in query)

    def upsert_representation(self, representation: ArtifactRepresentation) -> ArtifactRepresentation:
        now = utc_now()
        existing = ArtifactRepresentationModel.get_or_none(
            ArtifactRepresentationModel.representation_id == representation.representation_id
        )
        created_at = representation.created_at or (existing.created_at if existing is not None else now)
        updated_at = representation.updated_at or now
        ArtifactRepresentationModel.insert(
            representation_id=representation.representation_id,
            artifact_id=representation.artifact_id,
            representation_kind=representation.representation_kind,
            selector_blob=dict(representation.selector),
            path=representation.path,
            mime_type=representation.mime_type,
            size_bytes=representation.size_bytes,
            text_preview=representation.text_preview,
            summary=representation.summary,
            status=representation.status,
            metadata_blob=dict(representation.metadata),
            created_at=created_at,
            updated_at=updated_at,
        ).on_conflict_replace().execute()
        return self.get_representation(representation.representation_id) or representation

    def get_representation(self, representation_id: str) -> ArtifactRepresentation | None:
        try:
            return _representation_from_model(ArtifactRepresentationModel.get_by_id(representation_id))
        except DoesNotExist:
            return None

    def list_representations(
        self,
        artifact_id: str,
        *,
        representation_kind: str | None = None,
    ) -> tuple[ArtifactRepresentation, ...]:
        query = ArtifactRepresentationModel.select().where(ArtifactRepresentationModel.artifact_id == artifact_id)
        if representation_kind:
            query = query.where(ArtifactRepresentationModel.representation_kind == representation_kind)
        query = query.order_by(ArtifactRepresentationModel.representation_kind, ArtifactRepresentationModel.representation_id)
        return tuple(_representation_from_model(row) for row in query)

    def delete_representations(self, artifact_id: str) -> int:
        return int(
            ArtifactRepresentationModel.delete()
            .where(ArtifactRepresentationModel.artifact_id == str(artifact_id or ""))
            .execute()
        )

    def upsert_hot_state(self, state: ArtifactHotState) -> ArtifactHotState:
        ArtifactHotStateModel.insert(
            hot_id=state.hot_id,
            artifact_id=state.artifact_id,
            scope_key=state.scope_key,
            last_accessed_at=state.last_accessed_at,
            expires_at=state.expires_at,
            hard_expires_at=state.hard_expires_at,
            access_count=state.access_count,
        ).on_conflict_replace().execute()
        return self.get_hot_state(state.hot_id) or state

    def get_hot_state(self, hot_id: str) -> ArtifactHotState | None:
        try:
            return _hot_from_model(ArtifactHotStateModel.get_by_id(hot_id))
        except DoesNotExist:
            return None

    def list_hot_states(self, *, scope_key: str | None = None) -> tuple[ArtifactHotState, ...]:
        query = ArtifactHotStateModel.select()
        if scope_key:
            query = query.where(ArtifactHotStateModel.scope_key == scope_key)
        query = query.order_by(ArtifactHotStateModel.last_accessed_at.desc(), ArtifactHotStateModel.artifact_id)
        return tuple(_hot_from_model(row) for row in query)

    def list_expired_hot_states(
        self,
        *,
        expires_at: str,
        scope_key: str | None = None,
    ) -> tuple[ArtifactHotState, ...]:
        query = ArtifactHotStateModel.select().where(
            ArtifactHotStateModel.expires_at <= str(expires_at)
        )
        if scope_key:
            query = query.where(ArtifactHotStateModel.scope_key == scope_key)
        return tuple(_hot_from_model(row) for row in query)

    def delete_hot_states(self, artifact_id: str) -> int:
        return int(
            ArtifactHotStateModel.delete()
            .where(ArtifactHotStateModel.artifact_id == str(artifact_id or ""))
            .execute()
        )


def _record_from_model(row: ArtifactRecordModel) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=row.artifact_id,
        scope_key=row.scope_key,
        turn_id=row.turn_id,
        kind=row.kind,
        source_channel=row.source_channel,
        file_name=row.file_name,
        original_path=row.original_path,
        original_mime_type=row.original_mime_type,
        original_size_bytes=int(row.original_size_bytes),
        normalized_path=row.normalized_path,
        normalized_mime_type=row.normalized_mime_type,
        normalized_size_bytes=int(row.normalized_size_bytes),
        summary=row.summary,
        status=row.status,
        notes=row.notes,
        metadata=dict(row.metadata_blob or {}),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _representation_from_model(row: ArtifactRepresentationModel) -> ArtifactRepresentation:
    return ArtifactRepresentation(
        representation_id=row.representation_id,
        artifact_id=row.artifact_id,
        representation_kind=row.representation_kind,
        selector=dict(row.selector_blob or {}),
        path=row.path,
        mime_type=row.mime_type,
        size_bytes=int(row.size_bytes),
        text_preview=row.text_preview,
        summary=row.summary,
        status=row.status,
        metadata=dict(row.metadata_blob or {}),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _hot_from_model(row: ArtifactHotStateModel) -> ArtifactHotState:
    return ArtifactHotState(
        hot_id=row.hot_id,
        artifact_id=row.artifact_id,
        scope_key=row.scope_key,
        last_accessed_at=row.last_accessed_at,
        expires_at=row.expires_at,
        hard_expires_at=row.hard_expires_at,
        access_count=int(row.access_count),
    )
