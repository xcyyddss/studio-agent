"""Durable, scoped semantic memory with independent coverage review.

Originals are immutable. Compression is model work, not string truncation.
Every node has complete source ranges; only independently accepted nodes can
replace a body. Hierarchical nodes retain their children and original lineage.
"""
from copy import deepcopy
import json

from .contracts import canonical, digest
from .roles import obj, arr, S, B
from .cognitive_protocols import attach, bundle


class ContextPending(Exception):
    """A resumable preparation dependency, never a failed business result."""


from .context_budget import capacity, text_tokens, MODEL_CONTEXT_TOKENS, MAX_MODEL_CONTEXT_TOKENS

DEFAULT_POLICY = {
    # Chunk sizes are preparation work units, never independent triggers.
    "source_chunk_chars": 64000,
    "summary_target_chars": 24000,
    "model_context_tokens": MODEL_CONTEXT_TOKENS,
    "output_reserve_tokens": 128000,
    "platform_reserve_tokens": 64000,
    "image_reserve_tokens": 32000,
    "effective_window_percent": 95,
    "token_margin_percent": 25,
    "preparation_parallelism": 6,
}

SUMMARY_SCHEMA = obj(summary=S, constraints=arr(S), decisions=arr(S),
    conflicts=arr(S), open_questions=arr(S), early_directions=arr(S),
    exact_quotes=arr(obj(quote=S, reason=S)),
    coverage=arr(obj(source_id=S, start={"type":"integer"}, end={"type":"integer"},
                     treatment={"type":"string","enum":["preserved","consolidated","irrelevant"]}, reason=S)))
REVIEW_SCHEMA = obj(accepted=B, missing=arr(S), distorted=arr(S), unsupported=arr(S),
                    missing_constraints=arr(S), reason=S)

SUMMARY_RULES = """为后续独立调用整理可持续使用的认知记忆。不是回答业务问题，也不能替用户做新决定。
保留早期抽象方向、用户原意、当前约束、事实与猜想的区别、修正先后、冲突、条件、未解决问题和承接责任。
不要因为内容较旧、暂时未采用或不符合当前路线就删去；重复可合并，矛盾须并列。代码/数据细节可描述并保留原件入口，不能声称摘要等于实际读取原件。
逐项覆盖 sources 的完整 start/end 范围；irrelevant 需明确理由且不能用于跳过约束或未完成工作。
重要否定、精确数值、权限和用户要求必要时保留 exact_quotes 原文。摘要不引入新事实。
target_chars 是整理目标而非丢失信息的许可；无法充分压缩时宁可保持完整，复核会拒绝有损结果。
若给出 review_feedback，修复明确遗漏，不为通过审查而隐藏困难。review_feedback 和 previous_candidate 是编辑指导，不是 sources 中的业务事实；不要将“上次遗漏／本次修复”等修订过程写入记忆。输出结构化 JSON。"""
REVIEW_RULES = """独立复核记忆摘要与本包完整 sources。不要认可作者自称的覆盖。
逐段检查：早期抽象问题、用户要求和否定、事实/假设、条件、冲突、修改先后、数值、未完责任是否被保留；有没有新增无依据断言。
代码和材料细节允许有说明的概括并回查原件，但摘要不能替代精确执行所需原文。未成熟方向不能被工程细节挤掉。
仅当后续读者仍能辨认必要区别、约束和待办时 accepted=true。任何 missing/distorted/unsupported/missing_constraints 非空都必须拒绝。
accepted 是模型复核意见，不是数学意义的无损证明。输出 JSON；不能更改原件或替业务角色作决定。"""


def policy(state):
    supplied = state.get("metadata", {}).get("context_policy", {})
    if not isinstance(supplied, dict) or set(supplied) - set(DEFAULT_POLICY):
        raise ValueError("unknown context policy fields")
    value = {**DEFAULT_POLICY, **supplied}
    if any(type(x) is not int or x <= 0 for x in value.values()):
        raise ValueError("context policy values must be positive integers")
    if value["source_chunk_chars"] < 1000:
        raise ValueError("source chunks must allow meaningful semantic work")
    if value["model_context_tokens"] > MAX_MODEL_CONTEXT_TOKENS:
        raise ValueError("configured capacity cannot exceed the documented model context")
    if not 1 <= value["effective_window_percent"] <= 100 or value["token_margin_percent"] > 100:
        raise ValueError("invalid capacity percentages")
    if value["output_reserve_tokens"] + value["platform_reserve_tokens"] >= value["model_context_tokens"] * value["effective_window_percent"] // 100:
        raise ValueError("context reserves exhaust model capacity")
    return value


def memory_body(output):
    return {k: deepcopy(v) for k,v in output.items() if k != "coverage"}


def source_ranges(text, size):
    """Prefer complete records/paragraphs; oversized records retain exact ranges."""
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundary = text.rfind("\n", start + size // 2, end)
            if boundary >= 0:
                end = boundary + 1
        yield start, end
        start = end


class ContextLifecycle:
    def _memory_ranges(self, state, topic, role, text, size):
        """Reuse exact sealed prefix blocks before allocating an enlarged tail."""
        known = [u["source"]["text"] for u in state.get("context_memory", {}).get("units", {}).values()
                 if u["topic_id"] == topic and u["role_scope"] == role and u["level"] == 0
                 and u["source"]["text"]]
        known.sort(key=len, reverse=True)
        start = 0
        while start < len(text):
            reusable = next((body for body in known if text.startswith(body, start)
                             and (start + len(body) == len(text) or text[start + len(body)] == "\n")), None)
            if reusable is not None:
                end = start + len(reusable)
                while end < len(text) and text[end] == "\n":
                    end += 1
            else:
                end = min(start + size, len(text))
                if end < len(text):
                    boundary = text.rfind("\n", start + size // 2, end)
                    if boundary >= 0:
                        end = boundary + 1
            yield start, end
            start = end

    def _memory_unit(self, state, topic, role, text, *, children=(), level=0):
        store = state.setdefault("context_memory", {"units":{}, "uses":[]})
        protocol_hash = digest([SUMMARY_RULES, REVIEW_RULES, SUMMARY_SCHEMA, REVIEW_SCHEMA,
            bundle("context_summary")["sha256"], bundle("context_review")["sha256"]])
        key = digest({"scope":[topic,role], "text":text, "protocol":protocol_hash,
                      "target":policy(state)["summary_target_chars"]})
        unit = store["units"].get(key)
        if unit is None:
            source = {"source_id":key, "start":0, "end":len(text), "text":text, "sha256":digest(text)}
            unit = {"id":key, "topic_id":topic, "role_scope":role, "source":source,
                    "children":list(children), "level":level, "state":"new", "generation":0,
                    "versions":[], "target_chars":policy(state)["summary_target_chars"]}
            store["units"][key] = unit
        # Dependencies are provenance of this use, not semantic cache identity.
        # Scope + exact source + protocol + target determine reusable meaning.
        lineage = list(children)
        if lineage not in unit.setdefault("lineages", []):
            unit["lineages"].append(lineage)
        active = state["jobs"].get(unit.get("active_job"), {})
        if unit["state"] in {"summarizing", "reviewing"} and active.get("state") == "superseded":
            unit["state"] = "new"
        if unit["state"] == "new":
            self._memory_dispatch(state, unit, "context_summary")
        return unit

    def _memory_dispatch(self, state, unit, role):
        task = {"sources":[unit["source"]], "target_chars":unit["target_chars"],
                "scope":{"topic":unit["topic_id"], "role":unit["role_scope"]},
                "level":unit["level"], "children":unit["children"]}
        if role == "context_review":
            task["candidate"] = unit["candidate"]
        elif unit.get("review_feedback"):
            task["review_feedback"] = unit["review_feedback"]
            task["previous_candidate"] = unit["candidate"]
        schema = SUMMARY_SCHEMA if role == "context_summary" else REVIEW_SCHEMA
        prompt = canonical(attach({"rules":SUMMARY_RULES if role == "context_summary" else REVIEW_RULES,
                                  "task":task, "output_contract":schema}, role))
        unit["active_job"] = self._job(state, role, unit["topic_id"], prompt,
            {"memory_unit":unit["id"], "memory_generation":unit["generation"]}, schema=schema)
        unit["state"] = "summarizing" if role == "context_summary" else "reviewing"

    def _compact_text(self, state, topic, role, text, target, *, fits=None):
        """Incremental content-addressed chunks; recursive verified consolidation."""
        p = policy(state)
        current, ancestors, spans = text, [], []
        level = 0
        while (not fits(current) if fits is not None else len(current) > target):
            parts, nodes, pending = [], [], False
            ranges = list(self._memory_ranges(state, topic, role, current, p["source_chunk_chars"])) if level == 0 else list(source_ranges(current, p["source_chunk_chars"]))
            source_id = digest({"scope":[topic,role],"text":current,"children":ancestors,"level":level})
            plan = state.setdefault("context_memory", {"units":{},"uses":[]}).setdefault("plans", {}).setdefault(source_id, {
                "id":source_id, "topic_id":topic, "role_scope":role, "level":level,
                "source_sha256":digest(current), "source_chars":len(current),
                "ranges":[{"start":a,"end":b} for a,b in ranges], "covered":False})
            # Append-only history preserves complete prefix chunks for reuse.
            for index, (start,end) in enumerate(ranges):
                piece = current[start:end]
                # Delimiters belong to the container. Adding the next record
                # must not invalidate the previous final record's body.
                body = piece.rstrip("\n")
                separator = piece[len(body):]
                dependencies = [uid for a, b, uid in spans if a < end and b > start]
                unit = self._memory_unit(state, topic, role, body, children=dependencies, level=level)
                plan["ranges"][index]["separator"] = separator
                plan["ranges"][index]["unit_id"] = unit["id"]
                nodes.append(unit["id"])
                if unit["state"] == "accepted":
                    parts.append(canonical(memory_body(unit["candidate"])) + separator)
                elif unit["state"] in {"uncompressible", "no_gain"}:
                    parts.append(piece)
                else:
                    pending = True
                # Dispatch in waves without dropping or sampling any source.
                if pending and sum(j["state"] == "pending" and j["spec"]["role"] in {"context_summary","context_review"}
                                   for j in state["jobs"].values()) >= p["preparation_parallelism"]:
                    break
            if pending:
                raise ContextPending("semantic memory preparation is running or awaiting recovery")
            # Deterministic verification covers every original character, in
            # order, exactly once. This is separate from semantic model review.
            units = state["context_memory"]["units"]
            restored = "".join(units[r["unit_id"]]["source"]["text"] + r.get("separator", "") for r in plan["ranges"])
            if restored != current or digest(restored) != plan["source_sha256"]:
                raise ValueError("semantic memory source coverage failed reconstruction")
            plan["covered"] = True
            plan["original_retained_units"] = [n for n in nodes if units[n]["state"] == "uncompressible"]
            combined = "".join(parts)
            plan["output_sha256"] = digest(combined)
            before_tokens, after_tokens = text_tokens(current)[0], text_tokens(combined)[0]
            plan["reduction"] = {"before_tokens":before_tokens, "after_tokens":after_tokens}
            if after_tokens >= before_tokens:
                plan["stopping_reason"] = "no_gain"
                return current, ancestors, "insufficient_reduction_original_retained"
            if fits is not None and fits(combined):
                plan["stopping_reason"] = "complete_call_fits"
                return combined, nodes, "independently_reviewed"
            if after_tokens >= before_tokens * 0.9:
                # Retain a valid small gain, but do not recursively paraphrase
                # an already dense memory just to meet an arbitrary tiny target.
                plan["stopping_reason"] = "low_gain_no_recursive_rewrite"
                return combined, nodes, "limited_reduction_no_further_recursion"
            offset, spans = 0, []
            for part, node_id in zip(parts, nodes):
                spans.append((offset, offset + len(part), node_id))
                offset += len(part)
            current, ancestors, level = combined, nodes, level + 1
        return current, ancestors, "independently_reviewed"

    def _prepare_context(self, state, role, task, *, force=False, schema=None):
        """Prepare only this role's already-authorized task; never load peers."""
        value = deepcopy(task)
        if not force:
            return value
        from .context import ordinary_input
        p = policy(state)
        pressure = state.get("topics", {}).get(task["topic"], {}).get("context_pressure")
        def task_fits(candidate):
            measured = capacity(ordinary_input(role, candidate, schema=schema), schema or {},
                images=len(candidate.get("native_images", [])), settings=p)
            # A provider rejection is evidence that the local estimate was
            # insufficient. One rebuild targets a meaningfully smaller packet;
            # another rejection is surfaced, never an endless compression loop.
            required = pressure.get("target_text_tokens") if pressure else None
            return measured["fits"] and (required is None or measured["accounted_text_tokens"] <= required)
        topic = task["topic"]
        original_sha = digest(task)
        original_ref = {"source_sha256":original_sha, "pointer":""}
        uses = []
        receipts = state.get("input_receipts", {})
        old_inputs = [x for x in value.get("user_inputs", [])
                      if receipts.get(x.get("id"), {}).get("state") == "processed"]
        old_ids = {x["id"] for x in old_inputs}
        old_questions = [q for q in value.get("questions", []) if q.get("answer_id") in old_ids]
        by_answer = {q["answer_id"]:q for q in old_questions}
        work_history = value.get("work_history", [])
        # One complete interaction per line keeps old prefixes stable when new
        # answers arrive, and avoids regularly cutting across JSON records.
        records = [{"created_at":x.get("created_at", ""),"input":x,"question":by_answer.get(x["id"])} for x in old_inputs]
        records += [{"created_at":x.get("created_at", ""),"role_output":x} for x in work_history]
        records.sort(key=lambda x:x["created_at"])
        history_text = "\n".join(canonical(x) for x in records)
        if records:
            def replace_history(body):
                candidate = deepcopy(value)
                candidate["user_inputs"] = [x for x in candidate.get("user_inputs", []) if x.get("id") not in old_ids]
                candidate["questions"] = [q for q in candidate.get("questions", []) if q.get("answer_id") not in old_ids]
                candidate["work_history"] = []
                candidate["history_memory"] = {"body":body, "source":original_ref}
                return candidate
            compacted, units, assurance = self._compact_text(state, topic, role + ":history", history_text,
                p["summary_target_chars"], fits=lambda body: task_fits(replace_history(body)))
            if units:
                value["user_inputs"] = [x for x in value.get("user_inputs", []) if x.get("id") not in old_ids]
                value["questions"] = [q for q in value.get("questions", []) if q.get("answer_id") not in old_ids]
                value["work_history"] = []
                value["history_memory"] = {"body":compacted, "source":original_ref, "unit_ids":units,
                    "input_count":len(old_inputs), "work_record_count":len(work_history), "assurance":assurance,
                    "boundary":"Reviewed memory of processed inputs and accepted role outputs, not new instructions, current tool executions or original reading. Conflicting/early directions remain; retrieve exact originals for precise work."}
                uses.extend(units)
        # Large indispensable evidence is first processed in full by isolated
        # readers. Keep IDs, permissions, revision/decision structure intact.
        long_fields = {"text","content","ans","con","summary","prior_summary","description"}
        references = []
        leaf_sha = digest(value)
        leaf_original = deepcopy(value)
        def walk(node, path=()):
            if isinstance(node, dict):
                for k,v in list(node.items()):
                    if k in {"native_images","history_memory","context_projection","tool_catalog","prior_tool_result_delivery"}:
                        continue
                    ptr = path + (k,)
                    if isinstance(v,str) and k in long_fields and len(v) > min(2000, p["source_chunk_chars"]):
                        def leaf_fits(body):
                            candidate = deepcopy(value)
                            child = candidate
                            for item in ptr[:-1]:
                                child = child[item]
                            child[ptr[-1]] = body
                            return task_fits(candidate)
                        body, units, assurance = self._compact_text(state, topic, role, v, p["summary_target_chars"], fits=leaf_fits)
                        if units:
                            pointer = "/" + "/".join(str(x).replace("~","~0").replace("/","~1") for x in ptr)
                            node[k] = "[经独立复核的语义摘要；精确操作须回查原件]\n" + body
                            references.append({"pointer":pointer, "reference":{"source_sha256":leaf_sha,"pointer":pointer},
                                               "source_chars":len(v),"unit_ids":units,"assurance":assurance})
                            uses.extend(units)
                    else:
                        walk(v, ptr)
            elif isinstance(node,list):
                for i,v in enumerate(node): walk(v,path+(i,))
        if not task_fits(value):
            # Optional historical corpora are handled by ordinary projection.
            for key in sorted(set(value) - {"public_evidence","artifacts","existing_substitutes","escalation_outcomes",
                    "user_inputs", "questions", "input_obligations", "tool_results", "native_images", "interaction"}):
                if task_fits(value):
                    break
                wrapper = {key:value[key]}
                walk(wrapper)
                value[key] = wrapper[key]
        if references or uses:
            state["context_snapshots"][original_sha] = deepcopy(task)
            if references:
                state["context_snapshots"][leaf_sha] = leaf_original
            value["semantic_memory"] = {"original_source":original_ref, "replacements":references,
                "unit_ids":list(dict.fromkeys(uses)),
                "boundary":"Range coverage plus independent model review, not a proof of perfect semantic equivalence. Summaries never discharge unfinished obligations. Exact originals remain available through context.retrieve."}
        return value

    def _apply_context_summary(self, con, state, spec, output, result):
        unit = state["context_memory"]["units"][spec["metadata"]["memory_unit"]]
        source = unit["source"]
        coverage = output["coverage"]
        if len(coverage) != 1 or any(coverage[0][k] != source[k] for k in ("source_id","start","end")):
            raise ValueError("summary coverage must cover the exact complete source range")
        if not coverage[0]["reason"].strip() or not output["summary"].strip():
            raise ValueError("summary and explicit coverage rationale are required")
        for quote in output["exact_quotes"]:
            if not quote["quote"] or quote["quote"] not in source["text"]:
                raise ValueError("memory exact quote does not occur in its source")
        unit["candidate"] = deepcopy(output)
        unit["summary_result"] = result["result_id"]
        self._memory_dispatch(state, unit, "context_review")

    def _apply_context_review(self, con, state, spec, output, result):
        unit = state["context_memory"]["units"][spec["metadata"]["memory_unit"]]
        if output["accepted"] and any(output[k] for k in ("missing","distorted","unsupported","missing_constraints")):
            raise ValueError("accepted memory review cannot contain unresolved coverage defects")
        if not output["reason"].strip():
            raise ValueError("memory review must explain its judgment")
        unit["versions"].append({"candidate":deepcopy(unit["candidate"]), "review":deepcopy(output),
            "summary_result":unit["summary_result"], "review_result":result["result_id"]})
        if output["accepted"]:
            unit["state"] = "accepted" if text_tokens(canonical(memory_body(unit["candidate"])))[0] < text_tokens(unit["source"]["text"])[0] else "no_gain"
            unit["reduction"] = {"source_tokens":text_tokens(unit["source"]["text"])[0],
                                 "candidate_tokens":text_tokens(canonical(memory_body(unit["candidate"])))[0],
                                 "semantic_accepted":True}
        elif unit["generation"] < 2:
            unit["generation"] += 1
            unit["review_feedback"] = deepcopy(output)
            self._memory_dispatch(state, unit, "context_summary")
        else:
            # Failed compression is not permission to lose the original. The
            # caller can expand to the physical budget or expose a recovery task.
            unit["state"] = "uncompressible"

    def _context_status(self, state):
        units = state.get("context_memory", {}).get("units", {})
        return {"policy":policy(state), "coverage_plans":len(state.get("context_memory",{}).get("plans",{})),
            "verified_range_plans":sum(p["covered"] for p in state.get("context_memory",{}).get("plans",{}).values()),
            "units":[{"id":u["id"],"topic_id":u["topic_id"],
            "state":u["state"],"level":u["level"],"active_job":u.get("active_job"),
            "source_chars":u['source']['end']-u['source']['start'],"review_count":len(u["versions"])} for u in units.values()],
            "boundary":"Originals retained; accepted means range checks and independent model review, not a proof of lossless semantics."}
