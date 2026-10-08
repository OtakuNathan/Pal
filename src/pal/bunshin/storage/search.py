from __future__ import annotations
import sqlite3
from dataclasses import dataclass
from typing import Any
from pal.bunshin.contracts import AggregateType
from pal.shared.text_search import compile_jieba_fts_queries
from pal.bunshin.storage.connection_contracts import DatabasePort


@dataclass
class SearchStore:
    database: DatabasePort

    def search_workflows(
        self,
        *,
        actor_id: str,
        task_id: str = "",
        query: str = "",
        include_terminal: bool = False,
        include_archived: bool = False,
        limit: int = 20,
    ) -> tuple[dict[str, Any], ...]:
        self.database.ensure_schema()
        clauses = ["s.aggregate_type = ?", "json_extract(s.payload_json, '$.owner') = ?"]
        parameters: list[Any] = [AggregateType.WORKFLOW.value, str(actor_id or "").strip()]
        if task_id:
            clauses.append("json_extract(s.payload_json, '$.task_id') = ?")
            parameters.append(str(task_id).strip())
        if not include_terminal:
            clauses.append("s.state NOT IN ('COMPLETED', 'REJECTED', 'CANCELLED')")
        if not include_archived:
            clauses.append("coalesce(json_extract(s.payload_json, '$.archived'), 0) = 0")
        text = str(query or "").strip().lower()
        if text:
            clauses.append(
                "(lower(coalesce(json_extract(s.payload_json, '$.workflow_name'), t.title, '')) LIKE ? "
                "OR lower(coalesce(t.objective, '')) LIKE ?)"
            )
            pattern = f"%{text}%"
            parameters.extend((pattern, pattern))
        parameters.append(max(1, min(int(limit), 100)))
        with self.database.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT s.aggregate_id AS workflow_id, s.state AS workflow_state,
                       s.updated_at, coalesce(json_extract(s.payload_json, '$.archived'), 0) AS archived,
                       coalesce(t.title, '') AS task_title,
                       coalesce(json_extract(s.payload_json, '$.workflow_name'), t.title, '') AS workflow_name,
                       coalesce(t.objective, '') AS task_objective
                FROM bunshin_v2_aggregate_snapshots AS s
                LEFT JOIN bunshin_v2_task_projection AS t
                  ON t.task_id = json_extract(s.payload_json, '$.task_id')
                WHERE """
                + " AND ".join(clauses)
                + " ORDER BY s.updated_at DESC, s.aggregate_id LIMIT ?",
                tuple(parameters),
            ).fetchall()
        return tuple({**dict(row), "archived": bool(row["archived"])} for row in rows)

    def search_tasks(
        self,
        *,
        query: str = "",
        task_id: str = "",
        family_id: str = "",
        owner: str = "",
        include_archived: bool = False,
        limit: int = 20,
    ) -> tuple[dict[str, Any], ...]:
        self.database.ensure_schema()
        clauses: list[str] = []
        parameters: list[Any] = []
        if task_id:
            clauses.append("task_id = ?")
            parameters.append(str(task_id))
        if family_id:
            clauses.append("family_id = ?")
            parameters.append(str(family_id))
        if owner:
            clauses.append("owner = ?")
            parameters.append(str(owner))
        if not include_archived:
            clauses.append("state != 'ARCHIVED'")
        text = str(query or "").strip().lower()
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        resolved_limit = max(1, min(int(limit), 100))
        with self.database.read_connection() as connection:
            if text:
                scored: dict[str, tuple[float, dict[str, Any]]] = {}
                fts_filters = [f"t.{clause}" for clause in clauses]
                fts_where = " AND " + " AND ".join(fts_filters) if fts_filters else ""
                for fts_query, query_weight in compile_jieba_fts_queries(text):
                    try:
                        rows = connection.execute(
                            """
                            SELECT t.*, -bm25(bunshin_v2_tasks_fts) AS fts_score
                            FROM bunshin_v2_tasks_fts
                            JOIN bunshin_v2_task_projection AS t
                              ON t.task_id = bunshin_v2_tasks_fts.task_id
                            WHERE bunshin_v2_tasks_fts MATCH ?
                            """
                            + fts_where
                            + " ORDER BY bm25(bunshin_v2_tasks_fts), t.updated_at DESC LIMIT ?",
                            (fts_query, *parameters, resolved_limit),
                        ).fetchall()
                    except sqlite3.OperationalError:
                        continue
                    for row in rows:
                        item = dict(row)
                        score = float(item.pop("fts_score")) * float(query_weight)
                        current = scored.get(str(item["task_id"]))
                        if current is None or score > current[0]:
                            scored[str(item["task_id"])] = (score, item)
                    if rows:
                        break
                if scored:
                    ordered = sorted(
                        scored.values(),
                        key=lambda item: (-item[0], str(item[1]["task_id"])),
                    )[:resolved_limit]
                    return tuple({**item, "score": score} for score, item in ordered)

                like_clauses = [
                    *clauses,
                    "(lower(title) LIKE ? OR lower(objective) LIKE ? OR lower(workspace_key) LIKE ?)",
                ]
                like_where = " WHERE " + " AND ".join(like_clauses)
                pattern = f"%{text}%"
                rows = connection.execute(
                    "SELECT *, 1.0 AS score FROM bunshin_v2_task_projection"
                    + like_where
                    + " ORDER BY updated_at DESC, task_id LIMIT ?",
                    (*parameters, pattern, pattern, pattern, resolved_limit),
                ).fetchall()
                return tuple(dict(row) for row in rows)

            rows = connection.execute(
                "SELECT * FROM bunshin_v2_task_projection"
                + where
                + " ORDER BY updated_at DESC, task_id LIMIT ?",
                (*parameters, resolved_limit),
            ).fetchall()
            return tuple(dict(row) for row in rows)
