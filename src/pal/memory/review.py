"""Durable, host-owned review of memory proposals from any producer."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from typing import Any

from pal.control.contracts import (ControlRoute, InteractionButtonSpec, InteractionInputSpec,
    InteractionItemSpec, InteractionMessageSpec)
from pal.control.interactions import delivery_for_interaction, delivery_for_reply
from pal.foundation import utc_now
from pal.memory.contracts import L3BatchCommitRequest, L3CommitRequest
from pal.memory.mutations import content_hash
from pal.memory.proposals import MemoryProposalBatch, STAR_FIELDS, normalize_memory_candidates

SCHEMA = """CREATE TABLE IF NOT EXISTS memory_reviews (
 batch_id TEXT PRIMARY KEY, source_hash TEXT NOT NULL, state_json TEXT NOT NULL,
 updated_at TEXT NOT NULL
)"""
FIELD_LABELS = {"title": "标题", "summary": "正文", "search_text": "检索文本", "topics": "Topics",
    "situation": "情境", "task": "任务", "action": "操作", "result": "结果"}


class MemoryReviewService:
    def __init__(self, memory, storage=None):
        self.memory = memory
        self.storage = storage
        self.lock = threading.RLock()
        self._ephemeral = sqlite3.connect(":memory:", check_same_thread=False) if storage is None else None
        with self.connection() as db:
            db.execute(SCHEMA)

    @contextmanager
    def connection(self):
        if self.storage is not None:
            with self.storage.connection(write=True) as db:
                yield db
        else:
            with self._ephemeral as db:
                yield db

    def _read(self, db, batch_id):
        row = db.execute("SELECT state_json FROM memory_reviews WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise ValueError("审核批次不存在。")
        return json.loads(row[0])

    def _save(self, db, state):
        db.execute("UPDATE memory_reviews SET state_json=?,updated_at=? WHERE batch_id=?",
            (json.dumps(state, ensure_ascii=False), utc_now(), state["batch_id"]))

    def stage(self, proposal: MemoryProposalBatch, route: ControlRoute, *, legacy=False):
        if route is None:
            raise ValueError("记忆提案缺少交付路由。")
        source = dict(proposal.source)
        digest = content_hash({"candidates": proposal.candidates, "source": source})
        batch_id = "mr_" + content_hash(proposal.batch_id)[:20]
        with self.lock, self.connection() as db:
            row = db.execute("SELECT source_hash,state_json FROM memory_reviews WHERE batch_id=?", (batch_id,)).fetchone()
            if row:
                state = json.loads(row[1])
                self._check_owner(state, route)
                if row[0] != digest:
                    raise ValueError("同一提案 ID 的内容发生变化；请使用新的提案版本。")
                return state
            candidates, diagnostics = normalize_memory_candidates(list(proposal.candidates), limit=None if legacy else 5)
            diagnostics.extend(source.get("normalization_diagnostics") or [])
            drafts = []
            for index, item in enumerate(candidates):
                item["search_text"] = item.get("search_text") or item["source_excerpt"]
                item.setdefault("topics", [])
                drafts.append({"candidate_id": f"c{index + 1}", "content": item, "decision": "pending", "edited": False})
            state = {"batch_id": batch_id, "revision": 1, "status": "reviewing" if drafts else "completed",
                "source": source, "route": asdict(route), "drafts": drafts, "diagnostics": diagnostics,
                "provider_id": self.memory.l3_selector.active_provider_id, "authorization": "", "result": {}, "error": ""}
            db.execute("INSERT INTO memory_reviews VALUES (?,?,?,?)", (batch_id, digest, json.dumps(state, ensure_ascii=False), utc_now()))
            return state

    def stage_payload(self, payload, route, *, legacy=False):
        source = {key: payload[key] for key in ("source_kind", "source_ref", "source_label", "task_id",
            "workflow_id", "invocation_id", "memory_generation_id", "memory_refs", "source_dependencies", "normalization_diagnostics") if key in payload}
        stable_id = str(payload.get("candidate_batch_id") or payload.get("source_ref") or content_hash(payload))
        return self.stage(MemoryProposalBatch(stable_id, tuple(payload.get("memory_candidates") or ()), source), route, legacy=legacy)

    @staticmethod
    def _check_owner(state, route):
        owner = state["route"]
        if route is None or route.endpoint_id != owner["endpoint_id"]:
            raise ValueError("审核不属于当前 endpoint。")
        old = owner.get("reply_target") or {}
        for key in ("chat_id", "thread_id", "user_id"):
            if key == "user_id" and not old.get(key):
                continue  # Legacy routes rely on the endpoint binding.
            if str(old.get(key) or "") != str(route.reply_target.get(key) or ""):
                raise ValueError("审核不属于当前用户或会话。")

    def get(self, batch_id, route, *, resume=False):
        with self.lock, self.connection() as db:
            if not batch_id:
                rows = db.execute("SELECT state_json FROM memory_reviews ORDER BY updated_at DESC").fetchall()
                states = [json.loads(row[0]) for row in rows]
                state = None
                for item in states:
                    if item["status"] == "completed":
                        continue
                    try:
                        self._check_owner(item, route)
                    except ValueError:
                        continue
                    state = item
                    break
                if state is None:
                    raise ValueError("没有待审核的记忆提案。")
            else:
                state = self._read(db, batch_id)
            self._check_owner(state, route)
            self._reconcile(state)
            if resume:
                state["route"] = asdict(route)
                state["revision"] += 1
            self._save(db, state)
            return state

    def _requests(self, state):
        source = state["source"]
        task_id = str(source.get("task_id") or "") or None
        if source.get("source_kind") == "bunshin" and not task_id:
            raise ValueError("Bunshin 提案缺少真实 Task 绑定。")
        requests = []
        for draft in state["drafts"]:
            if draft["decision"] != "accepted":
                continue
            item = draft["content"]
            star = item.get("star") or {}
            bound_task = task_id or item.get("task_id") or None
            requests.append(L3CommitRequest(kind=item["kind"], title=item["title"], summary=item["summary"],
                search_text=item["search_text"], topics=list(item["topics"]), scope="task" if bound_task else "system",
                task_id=bound_task, canonical_key=item.get("canonical_key"),
                mutation_id=f"{state['batch_id']}:{draft['candidate_id']}",
                payload={"memory_candidate_source": "approval", "source": source,
                    "source_excerpt": item["source_excerpt"], "user_edited": draft["edited"], **star},
                **{f"{key}_text": star.get(key, "") for key in STAR_FIELDS}))
        return tuple(requests)

    def _reconcile(self, state):
        if state["status"] == "completed" or not state.get("authorization"):
            return
        provider = self.memory._resolve_l3_provider()
        repo = getattr(provider, "repository", None)
        if repo is None or getattr(provider, "provider_id", None) != state["provider_id"]:
            return
        receipt = repo.database.execute_sql("SELECT request_hash,results_json FROM memory_batch_receipts WHERE batch_id=?", (state["batch_id"],)).fetchone()
        if receipt is not None:
            if receipt[0] != content_hash([asdict(item) for item in self._requests(state)]):
                raise ValueError("批次提交收据与草稿不一致。")
            self._finish(state, json.loads(receipt[1]))

    @staticmethod
    def _finish(state, refs):
        state["result"] = {"references": refs, "accepted": sum(item["decision"] == "accepted" for item in state["drafts"]),
            "skipped": sum(item["decision"] == "skipped" for item in state["drafts"])}
        state["status"] = "completed"
        state["drafts"] = []
        state["authorization"] = ""
        state["error"] = ""
        state["revision"] += 1

    def apply(self, batch_id, args, route):
        with self.lock, self.connection() as db:
            state = self._read(db, batch_id)
            self._check_owner(state, route)
            self._reconcile(state)
            if state["status"] == "completed":
                self._save(db, state)
                return state
            if str(args.get("revision")) != str(state["revision"]):
                raise ValueError("审核已更新；请重新打开当前批次。")
            operation = args.get("decision")
            if operation in {"submit", "retry"}:
                if any(item["decision"] == "pending" for item in state["drafts"]):
                    raise ValueError("请先标记每条候选，再提交整批结果。")
                requests = self._requests(state)
                if not requests:
                    self._finish(state, [])
                else:
                    state["authorization"] = content_hash([asdict(item) for item in requests])
                    state["status"] = "authorized"
            elif operation in {"accept", "skip", "save"}:
                draft = next((item for item in state["drafts"] if item["candidate_id"] == args.get("candidate_id")), None)
                if draft is None:
                    raise ValueError("候选不存在。")
                if operation == "save":
                    key = str(args.get("field") or "")
                    if key not in self.fields(draft):
                        raise ValueError("字段不可编辑。")
                    values = args.get("input_values")
                    value = values.get("value") if isinstance(values, dict) else None
                    if not isinstance(value, str) or (key != "topics" and not value.strip()):
                        raise ValueError("请输入完整的字段内容。")
                    if key in STAR_FIELDS:
                        draft["content"]["star"][key] = value
                    elif key == "topics":
                        draft["content"][key] = list(dict.fromkeys(line.strip() for line in value.splitlines() if line.strip()))
                    else:
                        draft["content"][key] = value
                    draft["edited"] = True
                    draft["decision"] = "pending"
                else:
                    draft["decision"] = "accepted" if operation == "accept" else "skipped"
                state["authorization"] = ""
                state["status"] = "reviewing"
            elif operation not in {"view", "edit", "field", "overview"}:
                raise ValueError("未知审核操作。")
            state["revision"] += 1
            self._save(db, state)
            return state

    def commit(self, batch_id, *, validate_source=None):
        with self.lock:
            with self.connection() as db:
                state = self._read(db, batch_id)
                self._reconcile(state)
                if state["status"] == "completed":
                    self._save(db, state)
                    return {"status": "ok", **state["result"]}
                requests = self._requests(state)
                digest = content_hash([asdict(item) for item in requests])
                if state["status"] != "authorized" or state["authorization"] != digest:
                    raise ValueError("此批次尚未获得最终提交授权。")
            provider = self.memory._resolve_l3_provider()
            commit = getattr(provider, "commit_batch", None)
            if not callable(commit) or getattr(provider, "provider_id", None) != state["provider_id"]:
                raise ValueError("原记忆 provider 不可用或不支持原子批量提交。")
            repository = getattr(provider, "repository", None)
            if "source_dependencies" in state["source"]:
                if not callable(validate_source) or not validate_source(state["source"]):
                    raise ValueError("提案来源已失效或暂时无法验证；未提交。")
            # Share the mutation boundary with explicit forgetting. A deletion
            # cannot slip between the tombstone check and the batch commit.
            with getattr(repository, "write_lock", nullcontext()):
                catalog = getattr(repository, "catalog", None)
                if catalog and any(catalog.is_deleted(ref) for ref in state["source"].get("memory_refs", [])):
                    raise ValueError("提案引用了已遗忘内容，请重新审核来源。")
                result = commit(L3BatchCommitRequest(batch_id, requests))
            with self.connection() as db:
                if result.status == "ok":
                    self._finish(state, [item.document_id for item in result.results])
                else:
                    state["status"] = "failed"
                    state["error"] = f"整批未提交：{result.status}。可重试或修改候选。"
                    state["revision"] += 1
                self._save(db, state)
            return {"status": result.status, **state["result"]}

    @staticmethod
    def fields(draft):
        return ["title", "summary", "search_text", "topics", *(STAR_FIELDS if draft["content"]["kind"] == "case" else ())]

    def delivery(self, state, route, *, view="overview", candidate_id="", field="", banner="", opening=False):
        batch_id = state["batch_id"]
        revision = str(state["revision"])

        def button(label, operation, candidate="", key=""):
            return InteractionButtonSpec(label, "control.action.dispatch", {
                "action_kind": "memory_candidate_decision", "target_scope": "memory", "target_id": batch_id,
                "args": {"decision": operation, "revision": revision, "candidate_id": candidate, "field": key}})

        if state["status"] == "completed":
            result = state["result"]
            text = "提案因显式遗忘失效，未提交。" if result.get("invalidated") else f"记忆审核完成：接受 {result.get('accepted', 0)} 条，跳过 {result.get('skipped', 0)} 条。"
            if state["diagnostics"]:
                text += f"\n提取时已处理或跳过 {len(state['diagnostics'])} 项格式问题。"
            if opening:
                # A fresh client may have no card to resolve (including when
                # every optional candidate was rejected during normalization).
                return delivery_for_reply(route, text)
            spec = InteractionMessageSpec(batch_id, "memory_candidate_approval", route, text, revision=revision)
            return delivery_for_interaction(route, "interactive_resolve", spec)
        drafts = state["drafts"]
        counts = {key: sum(item["decision"] == key for item in drafts) for key in ("pending", "accepted", "skipped")}
        text = f"记忆提案：待处理 {counts['pending']} · 接受 {counts['accepted']} · 跳过 {counts['skipped']}\n逐项标记后预览，最后统一提交。"
        text += "\n批次：" + batch_id
        if state["source"].get("source_label"):
            text += "\n来源：" + str(state["source"]["source_label"])
        if state["diagnostics"]:
            text += f"\n提取时已处理或跳过 {len(state['diagnostics'])} 项格式问题。"
        text += ("\n" + (banner or state["error"])) if banner or state["error"] else ""
        selected = next((item for item in drafts if item["candidate_id"] == candidate_id), None)
        inputs = ()
        items = []
        rows = []
        if view in {"edit", "field"} and selected:
            content = selected["content"]
            if view == "field" and field in self.fields(selected):
                value = content.get("star", {}).get(field, "") if field in STAR_FIELDS else content.get(field, "")
                if field == "topics":
                    value = "\n".join(value)
                inputs = (InteractionInputSpec("value", FIELD_LABELS[field], value,
                    button("保存修改", "save", candidate_id, field)),)
            else:
                rows.extend((button(FIELD_LABELS[key], "field", candidate_id, key),) for key in self.fields(selected))
            rows.append((button("返回", "view", candidate_id),))
            text += "\n编辑：" + content["title"]
        else:
            visible = [selected] if view == "view" and selected else drafts
            for draft in visible:
                item = draft["content"]
                label = {"pending": "待确认", "accepted": "接受", "skipped": "跳过"}[draft["decision"]]
                if draft["edited"]:
                    label += " · 已修正"
                task_id = state["source"].get("task_id") or item.get("task_id")
                detail = ("范围：task · " + str(task_id) if task_id else "范围：system") + "\n\n" + item["summary"]
                detail += "\n\nTopics：" + ", ".join(item["topics"])
                if item.get("star"):
                    detail += "\n\n" + "\n\n".join(f"{FIELD_LABELS[key]}：\n{value}" for key, value in item["star"].items())
                if item.get("why_durable"):
                    detail += "\n\n保留理由：" + item["why_durable"]
                if view == "view":
                    detail += "\n\n检索文本：\n" + item["search_text"] + "\n\n来源片段／检索依据：\n" + item["source_excerpt"]
                cid = draft["candidate_id"]
                items.append(InteractionItemSpec(cid, f"{cid} · {item['kind']} · {item['title']}", detail, label,
                    ((button(f"{cid} 接受", "accept", cid), button(f"{cid} 跳过", "skip", cid), button(f"{cid} 修正", "edit", cid), button(f"{cid} 详情", "view", cid)),)))
            if view == "view":
                rows.append((button("返回整批概览", "overview"),))
            elif not counts["pending"]:
                rows.append((button("提交已接受项" if counts["accepted"] else "完成（不保存）", "submit"),))
        spec = InteractionMessageSpec(batch_id, "memory_candidate_approval", route, text,
            tuple(rows), revision=revision, items=tuple(items), inputs=inputs)
        return delivery_for_interaction(route, "interactive_open" if opening else "interactive_update", spec)
