from __future__ import annotations

# V1 keeps the existing table name to avoid a migration-heavy split.  The
# canonical owner is now pal.skill; behavior re-exports this model for legacy
# tests/imports.
from pal.behavior.models import BehaviorSkillModel as SkillModel


__all__ = ["SkillModel"]
