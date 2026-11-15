"""Bounded cognitive work, original selection and operation-specific responses.

This module has no model client and never interprets a planned call as a result.
Workflow persists the plan, runs a fresh job, and returns its real output to the
requesting author. Comparison cannot be prepared before reinterpretation exists.
"""
from copy import deepcopy
import json

from .cognitive_protocols import attach, operation_spec, operation_catalog, bundle
from .contracts import canonical, digest
from .input_contract import checked_input, measure_input
from .parliament import DEFAULT_LIMITS
from .selection import SelectionBudgetError
from .roles import obj, arr, S, COMMON

STRINGS = arr(S)
DIRECTIONS = arr(obj(q=S, basis_refs=STRINGS, connection=S, uncertainty=S))
SOURCE_USES = arr(obj(source_id=S, use=S, effect=S))
CHOICE = lambda *values: {"type": "string", "enum": list(values)}


def output_schema(operation):
    if operation == "web_research":
        raise ValueError("native research requires the web.search tool and its execution contract")
    fields = dict(processing=S, source_uses=SOURCE_USES, unresolved=STRINGS, suggested_next=S)
    if operation == "bounded_review":
        fields["items"] = arr(obj(id=S, status=CHOICE("changed", "no_change", "insufficient_material"),
            source_ids=STRINGS, finding=S, remaining=S))
    if operation in {"open_processing", "reinterpret", "compare", "selection_review"}:
        fields["directions"] = DIRECTIONS
    if operation == "compare":
        fields["comparisons"] = arr(obj(old=S, new=S, relationship=S, conditions=S, implication=S))
    if operation in {"selection_review", "joint_review"}:
        fields["coverage"] = arr(obj(scope=S, fragment=S,
            status=CHOICE("answered", "partial", "uncovered", "out_of_scope", "unknown"), reason=S))
    if operation == "joint_review":
        fields["dependencies"] = arr(obj(scope=S, dependency=S, status=S, remaining_owner=S))
    if operation == "experience_analysis":
        fields["differences"] = arr(obj(material_ids=STRINGS, action=S, rationale=S, conditions=S, unresolved=S))
        fields["synthesis"] = S
    if operation in {"change_impact", "user_effects"}:
        fields["impacts"] = arr(obj(target=S, kind=S, basis_refs=STRINGS, proposed_action=S, uncertainty=S))
    if operation == "continuation":
        fields["options"] = arr(obj(id=S, action=S, expected_effect=S, basis_refs=STRINGS, not_chosen_cost=S))
        fields["selected"] = S
        fields["reopen_conditions"] = STRINGS
    if operation == "delivery_review":
        fields["delivery"] = obj(recipient=S, use=S, usable_parts=STRINGS, prerequisites=STRINGS,
                                 missing=STRINGS, rediscovery_support=S)
    if operation == "handoff_review":
        fields["handoff"] = arr(obj(obligation=S, disposition=CHOICE("accept", "absorbed", "premise_invalid", "out_of_scope", "unknown"),
                                   reason=S, proposed_owner=S))
    if operation_spec(operation)["role"] != "processing":
        raise ValueError("operation is a primary workflow role, not a processing request")
    return obj(**fields)


def tool_catalog():
    choices = [{k:v for k,v in o.items() if k != "role"} for o in operation_catalog()
               if o["role"] == "processing" and o["id"] not in {"compare", "web_research"}]
    return [
        {"kind": "cognition.process", "description":
         "请求一次独立、有界的认知加工。arguments={operation,brief:{situation,observations,interpretation,global_question},"
         "sources:[{id,pointer,window:'A'|'B'}],constraints:[{id,pointer,window}],withheld_sources:[{id,pointer,window}]}。"
         "pointer 是本次获准原始 task 的 JSON 指针（context_projection 来源），不读取隐含历史或他人私库。brief 是你的表述而非验证事实。数组可空。"
         "选择 artifacts 时 id 必须为该原件完整 path；使用封存目录给出的原始 pointer，投影数组下标可能不同。历史文件可只显示精确版本目录；绑定该条目或其/content时，软件按封存来源和字节哈希装入真实正文，后续加工收到正文和读取凭据，不要求先另起一次作者读取。"
         "reinterpret 的旧解释仅放 withheld_sources，事实/用途约束放 constraints，brief 不复述旧解释；软件先真实重释再比较。"
         "其他操作的 withheld_sources 必须空。bounded_review另需checklist:[{id,question,source_ids}]，非空逐项核对范围，source_ids必须对应sources/constraints中实际绑定的正文。不能仅在brief提文件名充当递送。"
         "等待真实 tool_result，以 input_uses 说明采用/保留/不采用及作用，再实际推进。超预算需选更小的精确原件，不暗中截断。", "operations": choices},
        {"kind": "protocol.read", "description":
         "读取某操作的完整原理、方法与依赖正文；arguments={operation}。可在请求加工前阅读，属于系统指引而非任务证据。"}]


def pointer_value(value, pointer):
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("select a non-root JSON pointer from the authorized task")
    for part in pointer[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not part.isdigit() or str(int(part)) != part:
                raise ValueError("invalid list index")
            value = value[int(part)]
        elif isinstance(value, dict):
            value = value[part]
        else:
            raise ValueError("pointer traverses a scalar")
    return deepcopy(value)


def inherited_conditions(original, pointer):
    """Retain declared ancestor permissions even when selecting only content."""
    result, value = [], original
    for part in [None, *pointer[1:].split("/")]:
        if part is not None:
            key = part.replace("~1", "/").replace("~0", "~")
            value = value[int(key)] if isinstance(value, list) else value[key]
        if isinstance(value, dict):
            conditions = {k:deepcopy(value[k]) for k in
                ("allowed_uses", "audience", "allowed_purposes", "use_conditions", "purpose") if k in value}
            if conditions and conditions not in result:
                result.append(conditions)
    return result


def prepare(request, original, *, source_job, source_sha, resolve_artifact=None):
    args = request["arguments"]
    expected = {"operation", "brief", "sources", "constraints", "withheld_sources"}
    if args.get("operation") == "bounded_review":
        expected.add("checklist")
    if set(args) != expected:
        raise ValueError("cognition.process requires operation, brief, sources, constraints, withheld_sources")
    operation = args["operation"]
    output_schema(operation)
    if operation == "compare":
        raise ValueError("compare is created only from an actual reinterpret result")
    if set(args["brief"]) != {"situation", "observations", "interpretation", "global_question"} or any(
            not isinstance(v, str) for v in args["brief"].values()):
        raise ValueError("brief must distinguish situation, observations, interpretation and global_question")
    if not any(v.strip() for v in args["brief"].values()):
        raise ValueError("cognitive request requires an actual processing starting point")
    seen, groups = set(), {}
    for group in ("sources", "constraints", "withheld_sources"):
        if not isinstance(args[group], list):
            raise ValueError("source selection must be a list")
        groups[group] = []
        for item in args[group]:
            if (not isinstance(item, dict) or set(item) != {"id", "pointer", "window"}
                    or not isinstance(item["id"], str) or not item["id"].strip()
                    or item["id"] in seen or item["window"] not in {"A", "B"}):
                raise ValueError("source selections require distinct ids, exact pointers and A/B windows")
            seen.add(item["id"])
            resolved = None
            permission_pointer = item['pointer']
            prefix = next((p for p in ('/artifacts/','/public_evidence/artifacts/') if item['pointer'].startswith(p)), None)
            if prefix:
                tail = item['pointer'][len(prefix):].split('/',1)
                artifact = pointer_value(original, prefix + tail[0])
                if not isinstance(artifact,dict) or item["id"] != artifact.get("path"):
                    raise ValueError("artifact source id must equal its actual canonical path at the ORIGINAL pointer; projected array positions can differ. Use the sealed context catalog reference, not its displayed position.")
                if 'content' not in artifact and (len(tail)==1 or tail[1]=='content'):
                    if resolve_artifact is None:
                        raise ValueError('artifact locator is not a body; exact version resolver required')
                    text, resolved = resolve_artifact(artifact)
                    # The exact-version adapter supplies the body; permission
                    # still comes from the sealed original directory entry.
                    permission_pointer = prefix + tail[0]
                    content = text if len(tail)>1 else {**deepcopy(artifact),'content':text,'body_delivered':True}
                else:
                    content = pointer_value(original,item['pointer'])
            else:
                content = pointer_value(original, item["pointer"])
            groups[group].append({"id": item["id"], "window": item["window"], "content": content,
                "source": {"job_id": source_job, "source_sha256": source_sha,
                           "pointer": item["pointer"], "content_sha256": digest(content),
                           **({'resolved_artifact':resolved, 'permission_pointer':permission_pointer} if resolved else {})},
                "inherited_conditions": inherited_conditions(original, permission_pointer),
                "use": "same purpose and audience as requesting topic; no onward permission inferred"})
    if operation == "reinterpret":
        if not groups["sources"] or not groups["withheld_sources"]:
            raise ValueError("reinterpret needs actual original materials and actual old interpretations")
    elif groups["withheld_sources"]:
        raise ValueError("only reinterpret may withhold old interpretations")
    checklist = args.get("checklist", [])
    if operation == "bounded_review":
        if not isinstance(checklist,list) or not checklist:
            raise ValueError("bounded_review requires a nonempty checklist")
        ids = set()
        for item in checklist:
            if (not isinstance(item,dict) or set(item) != {"id","question","source_ids"}
                    or not isinstance(item["id"],str) or not item["id"].strip() or item["id"] in ids
                    or not isinstance(item["question"],str) or not item["question"].strip()
                    or not isinstance(item["source_ids"],list) or not item["source_ids"]
                    or any(not isinstance(i,str) for i in item["source_ids"])
                    or not set(item["source_ids"]) <= seen):
                raise ValueError("each checklist item requires a unique id, question and explicitly selected source ids")
            ids.add(item["id"])
    return {"id": request["id"], "operation": operation, "brief": deepcopy(args["brief"]),
            "checklist":deepcopy(checklist),
            **groups, "source_job": source_job, "source_sha256": source_sha,
            "topic_id": request["topic_id"], "state": "prepared", "phases": [], "consumption": []}


def _literal_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _literal_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _literal_strings(v)


def packet(plan, phase=None):
    operation = phase or plan["operation"]
    materials = deepcopy(plan["sources"] + plan["constraints"])
    task = {"operation": operation, "brief": deepcopy(plan["brief"]),
            "required_constraint_ids": [m["id"] for m in plan["constraints"]],
            "provenance_boundary": "Brief is requesting author's framing. Sources are exact selected originals, not externally verified facts.",
            "return_to": {"topic": plan["topic_id"], "tool_request": plan["id"],
                "responsibility": "requester must decide how this changes its work; proposals do not change topics, permissions or memory automatically"}}
    if operation == "compare":
        first = next((p for p in plan["phases"] if p["operation"] == "reinterpret" and p.get("result_id")), None)
        if first is None:
            raise ValueError("comparison requires an actual successfully imported reinterpretation")
        selection = plan.get("comparison_scope")
        chosen_old = [m for m in plan["withheld_sources"] if selection is None or m["id"] in selection["old_source_ids"]]
        materials += deepcopy(chosen_old)
        # The actual response is mandatory; a caller cannot replace it with a summary.
        actual = deepcopy(first)
        if selection:
            actual["output"] = {"selected_original_fragments": [
                {"pointer": p, "content": pointer_value(first["output"],p),
                 "sha256": digest(pointer_value(first["output"],p))} for p in selection["result_pointers"]]}
            task["comparison_scope"] = {**deepcopy(selection),
                "full_result_sha256": first["output_sha256"],
                "not_included_old_ids": [m["id"] for m in plan["withheld_sources"] if m not in chosen_old],
                "boundary": "Partial comparison of exact original fragments; unselected result content is not delivered. Full original remains stored; no substitute summary was created."}
        task["actual_reinterpretation"] = actual
    schema = output_schema(operation)
    if operation == "bounded_review":
        task["checklist"] = deepcopy(plan["checklist"])
        task["material_manifest"] = [{"id":m["id"],"sha256":digest(m["content"]),
            "source":m["source"],"boundary":"Exact selected content only; metadata/reference alone does not establish the underlying body."}
            for m in materials]
        schema["properties"]["items"]["items"]["properties"]["id"] = {"type":"string","enum":[i["id"] for i in plan["checklist"]]}
    payload = attach({"rules": COMMON +
        "本次是独立认知操作。按 task.operation 对应协议加工，不运行其他隐含流程。"
        "source_uses 只引用本包 A/B 中的 id；一般知识与猜想写明性质。processing 自由组织公开产物，无进展也可返回。"
        "directions 不必齐备，不强造候选；输出字段中的建议不等于实际动作。各文字字段尽量聚焦，使真实返回可被下一次有界调用完整接收。",
        "task": task, "A": [m for m in materials if m["window"] == "A"],
        "B": [m for m in materials if m["window"] == "B"], "output_contract": schema},
        "processing", operation)
    if operation == "reinterpret":
        # Fail closed on exact leakage, including keys/brief/rules. This does not
        # assert semantic isolation; a paraphrase can still bias the fresh view.
        actual = list(_literal_strings(payload))
        for item in plan["withheld_sources"]:
            for text in _literal_strings(item["content"]):
                if text.strip() and any(text in other for other in actual):
                    raise ValueError("withheld interpretation leaked into fresh input; choose precise interpretation pointers and neutral framing")
    prompt = canonical(payload)
    measured = measure_input(payload)
    if any(measured["effective_windows"][key] > bound for key, bound in DEFAULT_LIMITS.items()):
        raise SelectionBudgetError("cognitive operation exceeds its bounded input; select exact smaller originals without truncation", measured)
    accounting, _ = checked_input(payload, prompt=prompt)
    return prompt, schema, {"operation": operation, "source_sha256": plan["source_sha256"],
        "input_sha256": digest(payload), "source_ids": [m["id"] for m in materials],
        "accounting": accounting, "literal_withholding_checked": operation == "reinterpret",
        "semantic_isolation_verified": False}


def validate_response(output, spec):
    task = json.loads(spec["prompt"])
    valid = {m["id"] for m in task["A"] + task["B"]}
    if task["task"]["operation"] == "bounded_review":
        checklist = {i["id"]:i for i in task["task"]["checklist"]}
        if sorted(i["id"] for i in output["items"]) != sorted(checklist):
            raise ValueError("bounded review must account for every checklist item exactly once")
        used = {u["source_id"] for u in output["source_uses"]}
        for item in output["items"]:
            required = set(checklist[item["id"]]["source_ids"])
            if not item["finding"].strip() or not set(item["source_ids"]) <= valid:
                raise ValueError("review requires an actual finding and supplied sources")
            if item["status"] == "insufficient_material":
                if not item["remaining"].strip():
                    raise ValueError("incomplete review must identify the missing material or work")
            elif not required <= set(item["source_ids"]) & used or item["remaining"].strip():
                raise ValueError("conclusive review must use every required source and leave no unexamined scope")
    if any(u["source_id"] not in valid for u in output["source_uses"]):
        raise ValueError("cognitive source_uses names material not actually supplied")
    for name in ("directions", "impacts", "options"):
        for item in output.get(name, []):
            if not set(item.get("basis_refs", [])) <= valid:
                raise ValueError("cognitive output cites unavailable material")
    for item in output.get("differences", []):
        if not set(item["material_ids"]) <= valid:
            raise ValueError("experience difference cites unavailable material")
    if "selected" in output and output["selected"] not in {i["id"] for i in output["options"]} | {""}:
        raise ValueError("continuation selection must name an actual option")
    if not output["processing"].strip():
        raise ValueError("return actual processing or explicit no-progress; an empty envelope is not a result")


def read_protocol(request):
    operation = request["arguments"].get("operation")
    spec = operation_spec(operation)
    return {"outcome": "succeeded", "payload": bundle(spec["role"], operation),
            "receipt": {"adapter": "installed_cognitive_protocol", "operation": operation,
                        "boundary": "system instructions, not task evidence or performed cognitive work"}}
