"""Append-only objects and transactional transitions for the Studio council.

Only heads are mutable. Semantic judgments are supplied, never inferred by this
module. A packet is a bounded input contract, NOT proof of host isolation.
"""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_LIMITS = {"abstract_chars": 64000, "scenario_chars": 48000, "total_chars": 128000}
MEMBER_RULES = """你是议会议员；任务事实与历史以当前任务包的显式材料为准，允许运用注明性质的一般知识和联想。
任务是结合材料提出、修改或另起q×I；允许未成熟关联、改变原问题、反驳原解释和无候选。
抽象推进指发现原先未见的问题，不以措辞宏观为判据。真实性约束不要求先证明新意。
材料来源、模型一般知识和猜想分开；公开产物不冒充唯一因果解释，不要求隐藏思维链。
不要自行加载历史、数据库、工具输出或外部材料。需要更多信息时说明缺口，另建输入包。
输出候选q、实际使用的材料ID与来源说明；可以没有材料归因，不虚构出处。
"""


class ParliamentError(ValueError):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ParliamentError(f"{name}: nonempty text required")
    return value


def ident(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,95}", value):
        raise ParliamentError("id: use 1..96 ASCII letters, digits, _, ., :, -; start with a letter")
    return value


def ids(values, name, *, nonempty=False):
    if not isinstance(values, list) or any(not isinstance(x, str) for x in values):
        raise ParliamentError(f"{name}: list of IDs required")
    if len(values) != len(set(values)) or (nonempty and not values):
        raise ParliamentError(f"{name}: duplicate IDs or empty selection")
    return values


class Parliament:
    OPERATIONS = frozenset({
        "member", "material", "relation", "root", "request", "packet", "generation",
        "disposition", "revision", "return", "assessment", "learn", "resolve",
        "close_request", "annotation", "root_update", "applicability", "review_invitation", "withdraw_packet",
    })

    def __init__(self, workspace, *, create=False, limits=None):
        self.path = Path(workspace).resolve()
        if (self.path / ".restore-pending").exists():
            raise ParliamentError("restore publication is incomplete; ordinary parliament access is blocked")
        db = self.path / "state.db"
        if create:
            chosen = dict(DEFAULT_LIMITS if limits is None else limits)
            if set(chosen) != set(DEFAULT_LIMITS) or any(type(v) is not int or v <= 0 for v in chosen.values()):
                raise ParliamentError("limits require positive integer character counts")
            if db.exists():
                raise ParliamentError("workspace already initialized; config is immutable")
            self.path.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(db)) as con, con:
                con.executescript("""
                    CREATE TABLE objects(id TEXT PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL);
                    CREATE TABLE heads(topic TEXT PRIMARY KEY, revision TEXT NOT NULL);
                    CREATE TABLE events(seq INTEGER PRIMARY KEY, time TEXT NOT NULL, op TEXT NOT NULL, body TEXT NOT NULL);
                """)
                con.execute("INSERT INTO objects VALUES(?,?,?)", ("CONFIG", "config", encode({"schema": 1, "limits": chosen})))
        if not db.is_file():
            raise ParliamentError("workspace not initialized")
        self.db = db

    def apply(self, command):
        if not isinstance(command, dict):
            raise ParliamentError("command must be an object")
        data = dict(command)
        op = data.pop("op", None)
        if op not in self.OPERATIONS:
            raise ParliamentError(f"unsupported operation: {op}")
        # Serialize before writes; reject NaN, non-JSON objects, etc.
        encode(data)
        try:
            with closing(sqlite3.connect(self.db)) as con, con:
                con.execute("BEGIN IMMEDIATE")
                result = getattr(self, f"_op_{op}")(con, **data)
                con.execute("INSERT INTO events(time,op,body) VALUES(?,?,?)", (
                    datetime.now(timezone.utc).isoformat(), op, encode({"input": data, "result": result})))
                return result
        except (sqlite3.IntegrityError, TypeError) as exc:
            raise ParliamentError(f"invalid command or duplicate immutable ID: {exc}") from exc

    @staticmethod
    def _get(con, id, kind=None):
        row = con.execute("SELECT kind,body FROM objects WHERE id=?", (id,)).fetchone()
        if row is None or (kind is not None and row[0] != kind):
            raise ParliamentError(f"missing {kind or 'object'}: {id}")
        return {"id": id, "kind": row[0], **json.loads(row[1])}

    @staticmethod
    def _all(con, kind):
        return [{"id": r[0], "kind": kind, **json.loads(r[1])}
                for r in con.execute("SELECT id,body FROM objects WHERE kind=? ORDER BY rowid", (kind,))]

    @staticmethod
    def _put(connection, id, kind, **body):
        ident(id)
        connection.execute("INSERT INTO objects VALUES(?,?,?)", (id, kind, encode(body)))
        return id

    def inspect(self, id=None, kind=None):
        with closing(sqlite3.connect(self.db)) as con, con:
            if id is not None:
                return self._get(con, id)
            if kind is None:
                raise ParliamentError("inspect requires id or kind")
            return self._all(con, kind)

    def _head(self, con, topic):
        self._get(con, topic, "topic")
        row = con.execute("SELECT revision FROM heads WHERE topic=?", (topic,)).fetchone()
        return None if row is None else row[0]

    def head(self, topic):
        """Read the authoritative revision without opening a new inquiry."""
        with closing(sqlite3.connect(self.db)) as con:
            return self._head(con, topic)

    def _applicability(self, con, material):
        self._get(con, material, "material")
        row = con.execute("SELECT id FROM objects WHERE kind='applicability' "
            "AND json_extract(body,'$.material')=? ORDER BY rowid DESC LIMIT 1", (material,)).fetchone()
        if row is not None:
            latest = self._get(con, row[0], "applicability")
            return {**latest, "assessment_id": latest["id"],
                    "current_fact_allowed": latest["status"] == "current"}
        return {"material": material, "status": "unreviewed", "current_fact_allowed": False,
                "problem_reason": "", "correction_refs": [], "assessment_id": None,
                "source": None, "actor": None}

    def material_applicability(self, material):
        """Unreviewed material is available without a verified-fact assertion."""
        with closing(sqlite3.connect(self.db)) as con:
            return self._applicability(con, material)

    def _material_view(self, con, material, use):
        original = self._get(con, material, "material")
        if use not in {"as_recorded", "current_fact", "historical_reason", "association", "evaluation"}:
            raise ParliamentError("unknown material use")
        applicability = self._applicability(con, material)
        if use == "current_fact" and not applicability["current_fact_allowed"]:
            raise ParliamentError("material is not applicable as a current fact; select an explicit historical use")
        if use == "historical_reason":
            text(applicability["problem_reason"], "retained problem reason")
            original = {**original, "content": applicability["problem_reason"],
                        "original_material": material, "historical_only": True}
        # Preserve recorded packet bytes when no explicit judgment/use is supplied.
        if applicability["assessment_id"] is not None or use != "as_recorded":
            original = {**original, "use": use, "applicability": applicability}
        return original

    def material_view(self, material, use="as_recorded"):
        with closing(sqlite3.connect(self.db)) as con:
            return self._material_view(con, material, use)

    def _op_applicability(self, con, id, material, status, problem_reason, correction_refs,
                          actor, source, expected_assessment=None):
        current = self._applicability(con, material)
        if current["assessment_id"] != expected_assessment:
            raise ParliamentError("material applicability changed; review the current assessment")
        if status not in {"current", "disputed", "superseded", "unknown"}:
            raise ParliamentError("invalid applicability status")
        text(actor, "actor")
        if not isinstance(problem_reason, str):
            raise ParliamentError("problem_reason must be text; empty explicitly means none retained")
        if not isinstance(source, dict) or source.get("kind") not in {"user", "model", "observation", "external", "fixture"}:
            raise ParliamentError("applicability requires an explicitly attributed judgment source")
        text(source.get("reference"), "source.reference")
        for ref in ids(correction_refs, "correction_refs", nonempty=True):
            self._get(con, ref)
        return self._put(con, id, "applicability", material=material, status=status,
                         problem_reason=problem_reason, correction_refs=correction_refs,
                         actor=actor, source=source, previous=expected_assessment)

    def _owned(self, con, member, material_ids):
        self._get(con, member, "member")
        materials = [self._get(con, i, "material") for i in ids(material_ids, "materials")]
        if any(m["owner"] != member for m in materials):
            raise ParliamentError("material is outside the member's explicit library")
        return materials

    def _op_member(self, con, id, perspective):
        return self._put(con, id, "member", perspective=text(perspective, "perspective"))

    def _op_material(self, con, id, owner, content, window, source, form="raw",
                     refs=None, tags=None, status="tentative", supersedes=None):
        self._get(con, owner, "member")
        if window not in {"abstract", "scenario", "either"} or form not in {"raw", "relation", "synthesis"}:
            raise ParliamentError("invalid window or material form")
        if status not in {"tentative", "accepted_for_reuse", "disputed"}:
            raise ParliamentError("invalid epistemic status")
        if not isinstance(source, dict) or source.get("kind") not in {
            "user", "model", "observation", "external", "fixture"}:
            raise ParliamentError("source requires kind and reference; reference is not auto-loaded")
        text(source.get("reference"), "source.reference")
        refs = ids([] if refs is None else refs, "refs")
        for ref in refs:
            self._get(con, ref)
        if supersedes is not None:
            old = self._get(con, supersedes, "material")
            if old["owner"] != owner or old["form"] == "raw":
                raise ParliamentError("raw originals cannot be replaced; interpretations require same owner")
        tags = [] if tags is None else tags
        if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
            raise ParliamentError("tags must be descriptive strings")
        return self._put(con, id, "material", owner=owner, content=text(content, "content"),
                         window=window, source=source, form=form, refs=refs, tags=tags,
                         status=status, supersedes=supersedes)

    def _op_relation(self, con, id, owner, material_ids, statement, conditions, source, supersedes=None):
        self._owned(con, owner, material_ids)
        if not material_ids:
            raise ParliamentError("a relationship needs source materials")
        return self._op_material(con, id, owner, text(statement, "statement") + "\n条件／未知：" + text(conditions, "conditions"),
                                 "abstract", source,
                                 form="relation", refs=material_ids, supersedes=supersedes)

    def _op_annotation(self, con, id, material, note, purpose):
        self._get(con, material, "material")
        return self._put(con, id, "annotation", material=material, note=text(note, "note"),
                         purpose=text(purpose, "purpose"))

    def _op_root(self, con, id, q, actor=None):
        self._put(con, id, "topic", parent=None, actor=text(actor or id, "actor"))
        revision = id + ".v0"
        self._put(con, revision, "revision", topic=id, parent=None, questions=[text(q, "q")],
                  I=[], candidates=[], previous=None, continuity="fresh", unresolved_previous="", relation="root")
        con.execute("INSERT INTO heads VALUES(?,?)", (id, revision))
        return {"topic": id, "revision": revision}

    def _op_root_update(self, con, id, topic, expected_head, actor, source):
        """Advance a root answer only; q/I changes have no bypass here.

        Actor identity is an explicit local authorization assertion, not host
        authentication. Existing root objects default their author to topic ID.
        """
        root = self._get(con, topic, "topic")
        if root["parent"] is not None or actor != root.get("actor", topic):
            raise ParliamentError("only the bound root author may advance its answer")
        if not isinstance(source, dict) or set(source) != {"kind", "id", "reference"} or source["kind"] != "author_return":
            raise ParliamentError("root update source must name an actual author_return and reference")
        text(source["reference"], "source.reference")
        returned = self._get(con, source["id"], "return")
        if returned["topic"] != topic or returned["revision"] != expected_head:
            raise ParliamentError("root answer source must belong to its expected revision")
        command = {"topic": topic, "expected_head": expected_head, "actor": actor, "source": source}
        existing = con.execute("SELECT kind,body FROM objects WHERE id=?", (id,)).fetchone()
        if existing is not None:
            body = json.loads(existing[1])
            if existing[0] != "revision" or body.get("root_update") != command:
                raise ParliamentError("retry differs from immutable root update")
            return id  # Exact retry never moves the head backwards.
        if self._head(con, topic) != expected_head:
            raise ParliamentError("root head changed; explicit reevaluation required")
        previous = self._get(con, expected_head, "revision")
        body = {k: v for k, v in previous.items() if k not in {"id", "kind", "root_update"}}
        body.update(previous=expected_head, continuity="continue", root_update=command,
                    ans=returned["ans"], con=returned["con"], answer_return=source["id"])
        self._put(con, id, "revision", **body)
        changed = con.execute("UPDATE heads SET revision=? WHERE topic=? AND revision=?",
                              (id, topic, expected_head)).rowcount
        if changed != 1:
            raise ParliamentError("root head changed; no partial update committed")
        return id

    def _op_request(self, con, id, parent, description, observations, interpretation,
                    global_question, child=None, feedback_ids=None):
        parent_revision = self._head(con, parent)
        if child is not None:
            if self._get(con, child, "topic")["parent"] != parent:
                raise ParliamentError("request may only revise a direct child")
        feedback_ids = ids([] if feedback_ids is None else feedback_ids, "feedback_ids")
        for item in feedback_ids:
            feedback = self._get(con, item)
            if feedback["kind"] not in {"return", "assessment", "candidate", "resolution"}:
                raise ParliamentError("feedback reference must identify actual feedback or a proposal")
        return self._put(con, id, "request", parent=parent, parent_revision=parent_revision,
                         child=child, child_revision=None if child is None else self._head(con, child),
                         description=text(description, "description"), observations=text(observations, "observations"),
                         interpretation=text(interpretation, "interpretation"), global_question=text(global_question, "global_question"),
                         feedback_ids=feedback_ids)

    def _open(self, con, request):
        r = self._get(con, request, "request")
        if any(c["request"] == request for c in self._all(con, "closure")):
            raise ParliamentError("request is closed")
        if self._head(con, r["parent"]) != r["parent_revision"]:
            raise ParliamentError("parent changed; prepare an explicit new request")
        if r["child"] is not None and self._head(con, r["child"]) != r["child_revision"]:
            raise ParliamentError("child changed; prepare an explicit new request")
        return r

    def _op_packet(self, con, id, member, request, abstract_ids, scenario_ids, strategy, material_uses=None, task_windows=None, input_mapping=None, previous_candidates=None, selection_requests_context=None):
        r = self._open(con, request)
        m = self._get(con, member, "member")
        a = self._owned(con, member, abstract_ids)
        b = self._owned(con, member, scenario_ids)
        uses = {} if material_uses is None else material_uses
        if not isinstance(uses, dict) or not set(uses) <= set(abstract_ids + scenario_ids):
            raise ParliamentError("material_uses must name only selected materials")
        a = [self._material_view(con, m["id"], uses.get(m["id"], "as_recorded")) for m in a]
        b = [self._material_view(con, m["id"], uses.get(m["id"], "as_recorded")) for m in b]
        if any(x["window"] not in {"abstract", "either"} for x in a) or any(x["window"] not in {"scenario", "either"} for x in b):
            raise ParliamentError("selected material does not match its declared window")
        if set(abstract_ids) & set(scenario_ids):
            raise ParliamentError("do not duplicate a material in both windows")
        limits = self._get(con, "CONFIG", "config")["limits"]
        # Render the entire model input first. No hidden history, no silent truncation.
        payload = {"rules": MEMBER_RULES, "perspective": m["perspective"],
                   "task": r, "strategy": text(strategy, "strategy"), "A": a, "B": b}
        from .cognitive_protocols import attach
        payload = attach(payload, "member")
        if selection_requests_context is not None:
            from .input_contract import generation_request_context
            try:
                payload["selection_requests_context"] = generation_request_context(member, selection_requests_context)
            except ValueError as exc:
                raise ParliamentError(str(exc)) from exc
        if previous_candidates is not None:
            if not isinstance(previous_candidates, list) or not previous_candidates:
                raise ParliamentError("previous_candidates requires explicit nonempty snapshots")
            seen_previous = set()
            for item in previous_candidates:
                if not isinstance(item, dict) or not isinstance(item.get("candidate"), dict):
                    raise ParliamentError("previous candidate snapshot required")
                old = self._get(con, item["candidate"].get("id"), "candidate")
                old_r = self._get(con, old["request"], "request")
                expected = {"candidate": old, "request": old_r,
                    "source": {"packet": old["packet"], "request": old["request"]},
                    "I_sources": [{"id": mid, "source": self._get(con, mid, "material")["source"]} for mid in old["I"]],
                    "boundary": "Old q/I and provenance only. Material content is supplied only when explicitly selected in current A/B."}
                same_target = old["request"] == request or r["child"] is not None and (
                    old_r["child"] == r["child"] or any(v["topic"] == r["child"] and old["id"] in v["candidates"] for v in self._all(con, "revision")))
                if item != expected or old["id"] in seen_previous or old["member"] != member or old_r["parent"] != r["parent"] or not same_target:
                    raise ParliamentError("previous snapshot/member/parent/target mismatch")
                seen_previous.add(old["id"])
            payload["previous_candidates"] = previous_candidates
        prompt = encode(payload)
        from .input_contract import checked_input
        try:
            accounting, limits = checked_input(payload, limits=limits, prompt=prompt,
                task_windows=task_windows, core_request=True, mapping_source=input_mapping)
        except ValueError as exc:
            raise ParliamentError(str(exc)) from exc
        sizes = accounting["effective_windows"]
        return self._put(con, id, "packet", member=member, request=request, prompt=prompt,
                         sha256=hashlib.sha256(prompt.encode()).hexdigest(), sizes=sizes,
                         material_ids=abstract_ids + scenario_ids, fresh_context_required=True,
                         dispatch_status="not_dispatched", protocol_version="0.2",
                         input_accounting=accounting, input_limits=limits)

    def _op_withdraw_packet(self, con, id, packet, reason, source):
        p = self._get(con, packet, "packet")
        self._open(con, p["request"])
        if any(g["packet"] == packet for g in self._all(con, "generation")):
            raise ParliamentError("a returned packet cannot be withdrawn as not dispatched")
        if any(w["packet"] == packet for w in self._all(con, "packet_withdrawal")):
            raise ParliamentError("packet already withdrawn")
        if not isinstance(source, dict) or source.get("kind") not in {"fixture", "host_not_dispatched"}:
            raise ParliamentError("withdrawal requires explicit not-dispatched host evidence")
        text(source.get("reference"), "source.reference")
        return self._put(con, id, "packet_withdrawal", packet=packet, request=p["request"],
                         reason=text(reason, "reason"), source=source,
                         meaning="not dispatched; no model result or cognitive judgment")

    def _op_generation(self, con, id, packet, candidates, execution):
        p = self._get(con, packet, "packet")
        self._open(con, p["request"])
        if any(w["packet"] == packet for w in self._all(con, "packet_withdrawal")):
            raise ParliamentError("packet was withdrawn; preserve any late original separately")
        if any(g["packet"] == packet for g in self._all(con, "generation")):
            raise ParliamentError("packet already has a result; create a new invocation packet")
        if not isinstance(execution, dict) or execution.get("mode") not in {"fixture", "external_import"}:
            raise ParliamentError("execution must disclose fixture or external_import")
        if execution["mode"] == "external_import":
            text(execution.get("provider"), "provider")
            text(execution.get("invocation_id"), "invocation_id")
            if execution.get("fresh_context_asserted") is not True:
                raise ParliamentError("external import requires an isolation assertion; it remains unverified")
            for g in self._all(con, "generation"):
                e = g["execution"]
                if e.get("provider") == execution["provider"] and e.get("invocation_id") == execution["invocation_id"]:
                    raise ParliamentError("invocation identity was already used")
        if not isinstance(candidates, list):
            raise ParliamentError("candidates must be a list; an empty result is allowed")
        created = []
        for c in candidates:
            if not isinstance(c, dict) or not {"id", "q", "source_note"} <= set(c):
                raise ParliamentError("candidate requires id, q, source_note")
            if set(c) - {"id", "q", "I", "source_note", "previous"}:
                raise ParliamentError("unknown candidate fields")
            used = ids(c.get("I", []), "I")
            if not set(used) <= set(p["material_ids"]):
                raise ParliamentError("candidate cites material not in its input packet")
            previous = c.get("previous")
            if previous is not None:
                if p.get("protocol_version") == "0.2" and previous not in {
                    item["candidate"]["id"] for item in json.loads(p["prompt"]).get("previous_candidates", [])}:
                    raise ParliamentError("previous was not explicitly supplied in this fresh packet")
                old = self._get(con, previous, "candidate")
                old_r = self._get(con, old["request"], "request")
                new_r = self._get(con, p["request"], "request")
                if old["member"] != p["member"] or old_r["parent"] != new_r["parent"]:
                    raise ParliamentError("only original member may revise its candidate in this parent scope")
                same_target = old["request"] == p["request"] or (
                    new_r["child"] is not None and (
                        old_r["child"] == new_r["child"] or any(
                            v["topic"] == new_r["child"] and previous in v["candidates"]
                            for v in self._all(con, "revision"))))
                if not same_target:
                    raise ParliamentError("previous candidate belongs to another child or inquiry")
            cid = self._put(con, c["id"], "candidate", member=p["member"], request=p["request"],
                            packet=packet, q=text(c.get("q"), "q"), I=used,
                            source_note=text(c.get("source_note"), "source_note"), previous=previous)
            created.append(cid)
        return self._put(con, id, "generation", packet=packet, candidates=created,
                         execution=execution, isolation_verified=False)

    def _ancestors(self, con, topic):
        current = self._get(con, topic, "topic")["parent"]
        result = []
        while current is not None:
            result.append(current)
            current = self._get(con, current, "topic")["parent"]
        return result

    def _op_disposition(self, con, id, request, candidate, action, reason, replacement=None, target=None, local_effect=None):
        r = self._open(con, request)
        c = self._get(con, candidate, "candidate")
        if c["request"] != request:
            raise ParliamentError("candidate belongs to another request")
        if any(d["candidate"] == candidate for d in self._all(con, "disposition")):
            raise ParliamentError("candidate already disposed; reconsider via a new request")
        if action not in {"adopt", "reject", "escalate"}:
            raise ParliamentError("choose adopt, reject, or escalate")
        if local_effect is not None:
            if action != "adopt":
                raise ParliamentError("local_effect applies only to adoption")
            text(local_effect, "local_effect")
        if action == "reject":
            if replacement is None or replacement == candidate:
                raise ParliamentError("rejection requires an existing substitute; uncertainty is not redundancy")
            if self._get(con, replacement)["kind"] not in {"candidate", "revision", "material"}:
                raise ParliamentError("substitute must be existing work, a proposal, or material")
            # A rejected proposal can delegate to another retained object, but
            # mutually rejected proposals cannot constitute each other's work.
            # Read committed history and earlier edges in this transaction so
            # direct, multi-hop and inherited cycles all fail before insertion.
            replacements = {d["candidate"]: d["replacement"] for d in self._all(con, "disposition")
                            if d["action"] == "reject"}
            seen, current = {candidate}, replacement
            while True:
                if current in seen:
                    raise ParliamentError("rejection substitute chain contains a cycle; retain actual work or material")
                seen.add(current)
                if current not in replacements:
                    break
                current = replacements[current]
        elif replacement is not None:
            raise ParliamentError("replacement applies only to redundancy rejection")
        if action == "escalate":
            ancestors = self._ancestors(con, r["parent"])
            if target not in ancestors and not (not ancestors and target is None):
                raise ParliamentError("escalate to an actual ancestor; root may leave an unresolved external escalation")
        elif target is not None:
            raise ParliamentError("target applies only to escalation")
        return self._put(con, id, "disposition", request=request, candidate=candidate,
                         action=action, reason=text(reason, "reason"), replacement=replacement, target=target,
                         local_effect=local_effect)

    def _op_revision(self, con, id, topic, request, dispositions, relation, continuity, unresolved_previous=""):
        r = self._open(con, request)
        selected = [self._get(con, d, "disposition") for d in ids(dispositions, "dispositions", nonempty=True)]
        if any(d["request"] != request or d["action"] != "adopt" for d in selected):
            raise ParliamentError("child questions must originate from adopted parliament candidates")
        if any(set(v.get("dispositions", [])) & set(dispositions) for v in self._all(con, "revision")):
            raise ParliamentError("an adoption has already been applied")
        if continuity not in {"fresh", "continue"}:
            raise ParliamentError("continuity must be explicit: fresh or continue")
        if r["child"] is None:
            if continuity != "fresh":
                raise ParliamentError("a new topic requires a fresh author")
            self._put(con, topic, "topic", parent=r["parent"])
            previous = None
        else:
            if topic != r["child"]:
                raise ParliamentError("revision must target the requested child")
            previous = r["child_revision"]
            text(unresolved_previous, "unresolved_previous; explicitly state what is retained or none")
        candidates = [self._get(con, d["candidate"], "candidate") for d in selected]
        self._put(con, id, "revision", topic=topic, parent=r["parent"], request=request,
                  questions=[c["q"] for c in candidates], I=list(dict.fromkeys(i for c in candidates for i in c["I"])),
                  candidates=[c["id"] for c in candidates], dispositions=dispositions, previous=previous,
                  continuity=continuity, unresolved_previous=unresolved_previous, relation=text(relation, "relation"))
        con.execute("INSERT INTO heads VALUES(?,?) ON CONFLICT(topic) DO UPDATE SET revision=excluded.revision", (topic, id))
        return id

    def _op_return(self, con, id, revision, ans, con_text, contributions):
        r = self._get(con, revision, "revision")
        if not isinstance(contributions, list):
            raise ParliamentError("contributions must be a list")
        checked = []
        for c in contributions:
            if not isinstance(c, dict):
                raise ParliamentError("contribution must be an object")
            cited = ids(c.get("candidate_ids", []), "candidate_ids")
            if not set(cited) <= set(r["candidates"]):
                raise ParliamentError("contribution attribution must refer to the actual revision")
            checked.append({"text": text(c.get("text"), "contribution.text"), "candidate_ids": cited})
        return self._put(con, id, "return", revision=revision, topic=r["topic"],
                         ans=text(ans, "ans"), con=text(con_text, "con"), contributions=checked,
                         stale_at_receipt=self._head(con, r["topic"]) != revision)

    def _op_review_invitation(self, con, id, member, returned, contribution_index, scope, purpose, source):
        self._get(con, member, "member")
        result = self._get(con, returned, "return")
        if type(contribution_index) is not int or not 0 <= contribution_index < len(result["contributions"]):
            raise ParliamentError("invalid contribution index")
        if not isinstance(source, dict) or source.get("kind") not in {"user", "model", "observation", "external", "fixture"}:
            raise ParliamentError("review invitation requires explicit source kind")
        text(source.get("reference"), "source.reference")
        return self._put(con, id, "review_invitation", member=member, returned=returned,
                         contribution_index=contribution_index, scope=text(scope, "review scope"),
                         purpose=text(purpose, "review purpose"), source=source,
                         meaning="explicit invitation only; not delivery, acceptance, coverage or completion")

    def _op_assessment(self, con, id, member, returned, contribution_index, judgment, reason, invitation=None):
        self._get(con, member, "member")
        result = self._get(con, returned, "return")
        if type(contribution_index) is not int or not 0 <= contribution_index < len(result["contributions"]):
            raise ParliamentError("invalid contribution index")
        c = result["contributions"][contribution_index]
        owners = {self._get(con, i, "candidate")["member"] for i in c["candidate_ids"]}
        if invitation is not None:
            invited = self._get(con, invitation, "review_invitation")
            if (invited["member"], invited["returned"], invited["contribution_index"]) != (member, returned, contribution_index):
                raise ParliamentError("assessment does not match the explicit invitation")
        elif member not in owners:
            raise ParliamentError("assessment by unrelated member requires an explicit review invitation")
        if judgment not in {"sufficient", "insufficient", "uncertain"}:
            raise ParliamentError("invalid contribution judgment")
        return self._put(con, id, "assessment", member=member, returned=returned,
                         contribution_index=contribution_index, judgment=judgment, reason=text(reason, "reason"),
                         parent=self._get(con, result["topic"], "topic")["parent"], final_veto=False,
                         **({"invitation": invitation} if invitation is not None else {}))

    def _op_learn(self, con, id, assessment, abstract, scenario, source, promote=False):
        a = self._get(con, assessment, "assessment")
        if type(promote) is not bool or (promote and a["judgment"] != "sufficient"):
            raise ParliamentError("insufficient/uncertain feedback cannot be promoted as reusable success")
        for prior in self._all(con, "learning"):
            if prior["assessment"] == assessment:
                raise ParliamentError("assessment already integrated; use new material interpretations for revisions")
        status = "accepted_for_reuse" if promote else "tentative"
        created = []
        for suffix, value, window in [(".A", abstract, "abstract"), (".B", scenario, "scenario")]:
            created.append(self._op_material(con, id+suffix, a["member"], value, window,
                                            source,
                                            form="synthesis", refs=[assessment, a["returned"]], status=status))
        return self._put(con, id, "learning", assessment=assessment, materials=created,
                         confidence="supplied judgment, not causal proof", promoted=promote)

    def _op_resolve(self, con, id, disposition, receiver, outcome, note):
        d = self._get(con, disposition, "disposition")
        if d["action"] != "escalate" or receiver != d["target"]:
            raise ParliamentError("only the recorded escalation receiver can record its resolution")
        if any(x["disposition"] == disposition and x["outcome"] == "handled" for x in self._all(con, "resolution")):
            raise ParliamentError("already resolved")
        if outcome not in {"handled", "deferred"}:
            raise ParliamentError("resolution records handled or deferred, not inferred success")
        return self._put(con, id, "resolution", disposition=disposition, receiver=receiver,
                         outcome=outcome, note=text(note, "note"))

    def _op_close_request(self, con, id, request, reason):
        self._get(con, request, "request")
        if any(x["request"] == request for x in self._all(con, "closure")):
            raise ParliamentError("already closed")
        packets = {p["id"] for p in self._all(con, "packet") if p["request"] == request}
        finished = {g["packet"] for g in self._all(con, "generation")}
        withdrawn = {w["packet"] for w in self._all(con, "packet_withdrawal")}
        if not packets or not packets <= finished | withdrawn:
            raise ParliamentError("cannot close without explicit result or withdrawal for every prepared packet")
        candidates = {c["id"] for c in self._all(con, "candidate") if c["request"] == request}
        disposed = {d["candidate"] for d in self._all(con, "disposition")}
        if not candidates <= disposed:
            raise ParliamentError("undisposed candidates remain")
        applied = {d for v in self._all(con, "revision") for d in v.get("dispositions", [])}
        if any(d["request"] == request and d["action"] == "adopt" and not d.get("local_effect")
               and d["id"] not in applied for d in self._all(con, "disposition")):
            raise ParliamentError("adoption needs an explicit local effect or a child revision before closure")
        return self._put(con, id, "closure", request=request, reason=text(reason, "reason"),
                         meaning="this explicit request is closed, not cognitive exhaustion")

    def status(self):
        with closing(sqlite3.connect(self.db)) as con, con:
            closed = {c["request"] for c in self._all(con, "closure")}
            resolved = {x["disposition"] for x in self._all(con, "resolution") if x["outcome"] == "handled"}
            assessments = self._all(con, "assessment")
            applied = {d for v in self._all(con, "revision") for d in v.get("dispositions", [])}
            return {
                "schema": 1, "mode": "transactional_council", "live_agents": "managed by the workflow execution service",
                "objects": dict(con.execute("SELECT kind,COUNT(*) FROM objects GROUP BY kind")),
                "open_requests": [r["id"] for r in self._all(con, "request") if r["id"] not in closed],
                "unresolved_escalations": [d["id"] for d in self._all(con, "disposition")
                                           if d["action"] == "escalate" and d["id"] not in resolved],
                "unapplied_adoptions": [d["id"] for d in self._all(con, "disposition")
                                        if d["action"] == "adopt" and not d.get("local_effect") and d["id"] not in applied],
                "parent_feedback": [{"assessment": a["id"], "parent": a["parent"], "judgment": a["judgment"]}
                                    for a in assessments],
                "cognitive_completion": "not determined",
            }
