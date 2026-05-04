from __future__ import annotations

from pal.foundation.persistence import BaseModel


BEHAVIOR_AFFORDANCES_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS behavior_affordances_fts USING fts5(
    affordance_id UNINDEXED,
    title,
    scenario_text,
    prompt_hint,
    activation_terms
)
"""

BEHAVIOR_AFFORDANCES_FTS_TRIGRAM_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS behavior_affordances_fts_trigram USING fts5(
    affordance_id UNINDEXED,
    title,
    scenario_text,
    prompt_hint,
    activation_terms,
    tokenize = 'trigram'
)
"""


def ensure_behavior_schema() -> None:
    db = BaseModel._meta.database
    db.execute_sql(BEHAVIOR_AFFORDANCES_FTS_SQL)
    db.execute_sql(BEHAVIOR_AFFORDANCES_FTS_TRIGRAM_SQL)
