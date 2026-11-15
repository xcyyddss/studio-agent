"""Pure, append-only experience maintenance; semantics come from fresh roles.

No model calls, database writes or string-based semantic deduplication occur here.
The workflow owns source receipts, visibility, transactions and job dispatch.
"""
from __future__ import annotations

from copy import deepcopy
from .context import _hash, _terms, _eligibility, _public, ProjectionBudgetError
from .roles import obj, arr, S, STRINGS, validate
from studio_runtime.parliament import encode


ACTION_STATUSES = {
    "relation": ("active", "disputed", "unknown"),
    "revision": ("active", "disputed", "unknown"),
    "retire": ("retired",),
    "applicability": ("current", "disputed", "unknown", "inapplicable"),
}
STATUS_CONTRACT = "\n".join(
    f"action={action}: status 只能为 {', '.join(statuses)}。"
    for action, statuses in ACTION_STATUSES.items()) + """
active：新关系或修订解释可供有条件复用，不表示已验证为真。
current：仅用于 applicability，表示现存材料在所述条件下的当前适用判断；不创建修订解释，也不证明事实已验证。
disputed：明确保留争议；unknown：当前证据不足，保留未知；二者均不是确认有效。
retired：停止复用现存解释，保留原件；inapplicable：现存材料在所述条件下不适用。
superseded 是软件在 revision 生效后为旧解释记录的内部历史状态，不能作为任何操作的 status 输出。
尤其 revision 必须选择 active/disputed/unknown，不能用 current 表示“这是新版本”。
"""


MEMORY_SCHEMA = obj(
    reviewed_material_ids=STRINGS,
    operations=arr(obj(
        action={"type": "string", "enum": ["relation", "revision", "retire", "applicability"]},
        target_id=S, material_ids=STRINGS,
        relation={"type": "string", "enum": ["supports", "contradicts", "qualifies", "extends", "alternative", "related", "none"]},
        content=S, conditions=S,
        status={"type": "string", "enum": list(dict.fromkeys(
            status for statuses in ACTION_STATUSES.values() for status in statuses)),
            "description": STATUS_CONTRACT},
        source_ids=STRINGS, expected_version=S, reason=S,
        context_json=S)),
    summary=S)

MEMORY_RULES = """你是fresh经验维护角色，只依据本次显式材料提出经验关系、解释修订、退役或适用性判断。
关系可为支持、冲突、限定、拓展、不同解释或关联；不能因同源/近似措辞删除不同解释。
原件与旧版本必须保留。具体实现失败不自动否定抽象问题，采纳或入库不证明长期有效。
profile_only材料只可用于获准的用户理解任务，不能转为未经允许的任务依据。引用ID必须实际出现在允许输入范围。
reviewed_material_ids只列本次已读的新经验；即使无需变更也明确列出，避免无事实变化重复维护。
operations可为空；不为了维护制造修改。每项给理由、适用条件和source_ids，保持未知。
revision替换解释，target_id是现存解释；retire只停用现存解释，不删除原始事实。
expected_version取materials中的memory_version（初始为材料id）；关系新增时target_id和expected_version留空。
context_json为条件匹配的JSON对象字符串（例如{\"topic\":\"ROOT\"}），未知用{}，不编造已验证条件。
relation操作至少关联两项材料；其余操作relation=none。你的判断不是父议题最终决定。
""" + STATUS_CONTRACT


def empty_memory():
    return {"schema": "experience_memory.v1", "records": [], "reviews": [], "evaluated_material_ids": []}


def _state(state):
    result = deepcopy(state or empty_memory())
    if result.get("schema") != "experience_memory.v1" or any(not isinstance(result.get(k), list) for k in ("records", "reviews", "evaluated_material_ids")):
        raise ValueError("invalid experience memory state")
    return result


def material_views(state, materials):
    """Return original plus historical versions; source originals remain untouched."""
    state = _state(state)
    views = {m["id"]: _public(m) for m in materials}
    for item in views.values():
        item["memory_version"] = item["id"]
    for record in state["records"]:
        action, target = record["action"], record["target_id"]
        if action in {"relation", "revision"}:
            template = views.get(target) if target else views.get(record["material_ids"][0])
            if template is None:
                continue
            ancestors = [views[mid] for mid in record["material_ids"] + ([target] if target else []) if mid in views]
            restricted = any(m.get("purpose") in {"profile_only", "user_profile_only"} or "profile_only" in m.get("tags", []) for m in ancestors)
            views[record["id"]] = {
                "id": record["id"], "kind": "material", "owner": template["owner"],
                "window": template["window"] if action == "revision" else "abstract",
                "content": record["content"] + "\n条件／未知：" + record["conditions"],
                "form": "synthesis" if action == "revision" else "relation",
                "source": deepcopy(record["source"]), "refs": list(dict.fromkeys(record["material_ids"] + record["source_ids"])),
                "tags": list(dict.fromkeys([tag for m in ancestors for tag in m.get("tags", [])] + ["memory_generated"])),
                "status": "disputed" if record["status"] == "disputed" else "tentative",
                "memory_version": record["id"], "supersedes": target or None,
                "applicability": {"status": record["status"], "conditions": record["conditions"], "context": record["context"], "source": record["source"]},
                **({"purpose": "profile_only"} if restricted else {"purpose": template["purpose"]} if "purpose" in template else {})}
        if target in views:
            views[target]["memory_version"] = record["id"]
            views[target]["applicability"] = {"status": "superseded" if action == "revision" else record["status"],
                "conditions": record["conditions"], "context": record["context"], "source": deepcopy(record["source"]),
                "current_fact_allowed": record["status"] == "current" and action == "applicability"}
    return list(views.values())


def pending_material_ids(state, materials):
    """Only genuinely new learned originals trigger maintenance, never its own output."""
    done = set(_state(state)["evaluated_material_ids"])
    return [m["id"] for m in materials if m.get("form") == "synthesis" and
            "memory_generated" not in m.get("tags", []) and m["id"] not in done]


def maintenance_context(state, materials, task, source_ids=(), *, member=None,
                        purpose="design_association", use_context=None, max_chars=16000, capacity_check=None):
    """Build a bounded fresh maintenance task; the caller seals this exact input.

    source_ids is the workflow's authoritative allowlist, not a model claim.
    Bodies not supplied remain references only. The cap measures this task JSON;
    the caller separately budgets the role rules/schema envelope.
    """
    state = _state(state)
    if type(max_chars) is not int or max_chars <= 0:
        raise ValueError("positive maintenance context bound required")
    views = material_views(state, materials)
    if member is None:
        owners = {m["owner"] for m in materials}
        if len(owners) != 1:
            raise ValueError("maintenance requires exactly one explicit library owner")
        member = next(iter(owners))
    owner = member["id"] if isinstance(member, dict) else member
    eligible = [m for m in views if not _eligibility(m, owner, purpose, use_context or {})]
    pending = [mid for mid in pending_material_ids(state, materials) if any(m["id"] == mid for m in eligible)]
    payload = {"task": deepcopy(task), "member_id": owner, "purpose": purpose,
               "new_material_ids": [], "materials": [], "available_source_ids": [],
               "memory_records": [], "pending_material_count": len(pending),
               "omitted_material_count": len(eligible), "memory_state_sha256": _hash(state),
               "boundary": "Explicit displayed originals only; source IDs alone do not imply source bodies. Semantic applicability and benefit remain role judgments."}
    def fits(value):
        return len(encode(value)) <= max_chars if capacity_check is None else capacity_check(value)
    if not fits(payload):
        raise ProjectionBudgetError("maintenance base context exceeds bound", chars=len(encode(payload)), max_chars=max_chars)
    # Sources are exposed only where related to delivered materials. All IDs must
    # still be checked against the workflow's authoritative allowlist on apply.
    allowed = set(source_ids) | {m["id"] for m in views}

    def add(item, fresh):
        trial = deepcopy(payload)
        trial["materials"].append(item)
        if fresh:
            trial["new_material_ids"].append(item["id"])
        related = [item["id"]] + [r for r in item.get("refs", []) if r in allowed]
        trial["available_source_ids"] = list(dict.fromkeys(trial["available_source_ids"] + related))
        relevant = [r for r in state["records"] if r["id"] == item.get("memory_version") or r["target_id"] == item["id"]]
        for record in relevant:
            if record not in trial["memory_records"]:
                trial["memory_records"].append(record)
        trial["omitted_material_count"] = len(eligible) - len(trial["materials"])
        if fits(trial):
            payload.update(trial)
            return True
        return False

    by_id = {m["id"]: m for m in eligible}
    for mid in pending:
        if not add(by_id[mid], True) and not payload["new_material_ids"]:
            raise ProjectionBudgetError("one new experience exceeds maintenance bound; explicit source restructuring required", max_chars=max_chars)
    terms = _terms(task) | _terms(payload["materials"])
    for item in sorted((m for m in eligible if m["id"] not in pending),
                       key=lambda m: (-len(_terms(m["content"]) & terms), m["id"])):
        add(item, False)
    return payload


def apply_maintenance(state, output, materials, source_ids=(), *, source,
                      visible_material_ids=None, new_material_ids=None):
    """Validate source/version identities and append an authored proposal atomically.

    No input state/material is mutated. Duplicate receipt+exact output is
    idempotent; same receipt with different text is an error. Different source
    receipts can retain different interpretations of exactly the same evidence.
    """
    import json
    # The local schema dialect has no action-dependent conditionals. Check this
    # contract explicitly, including values outside the shared schema enum, so
    # a fresh retry receives the precise action-specific choices.
    if isinstance(output, dict) and isinstance(output.get("operations"), list):
        for index, item in enumerate(output["operations"]):
            if not isinstance(item, dict):
                continue
            action, status = item.get("action"), item.get("status")
            if isinstance(action, str) and action in ACTION_STATUSES and isinstance(status, str):
                allowed = ACTION_STATUSES[action]
                if status not in allowed:
                    raise ValueError(f"operations[{index}].status: action={action}, received={status!r}; "
                                     f"allowed values: {', '.join(allowed)}")
    validate(output, MEMORY_SCHEMA)
    result = _state(state)
    if not isinstance(source, dict) or source.get("kind") not in {"model", "fixture"} or not source.get("reference"):
        raise ValueError("maintenance requires an attributed model/fixture receipt")
    receipt = _hash(source)
    output_hash = _hash(output)
    prior = next((r for r in result["reviews"] if r["receipt_sha256"] == receipt), None)
    if prior:
        if prior["output_sha256"] != output_hash:
            raise ValueError("same maintenance receipt cannot change its output")
        return result
    views = {m["id"]: m for m in material_views(result, materials)}
    visible = set(visible_material_ids if visible_material_ids is not None else views)
    if not visible <= set(views):
        raise ValueError("visible materials must exist")
    fresh = set(new_material_ids if new_material_ids is not None else pending_material_ids(result, materials))
    reviewed = output["reviewed_material_ids"]
    if len(reviewed) != len(set(reviewed)) or not set(reviewed) <= visible & fresh:
        raise ValueError("reviewed_material_ids must be unique visible new learned originals")
    if new_material_ids is not None and set(reviewed) != fresh:
        raise ValueError("review every sealed new material even when no change is proposed; do not silently reschedule it")
    allowed_sources = set(source_ids) | visible
    records = []
    targeted = set()
    for index, proposal in enumerate(output["operations"]):
        item = deepcopy(proposal)
        action, target = item["action"], item["target_id"]
        refs = item["material_ids"]
        if len(refs) != len(set(refs)) or not set(refs) <= visible:
            raise ValueError("operation material IDs must be unique visible originals")
        if not item["source_ids"] or not set(item["source_ids"]) <= allowed_sources:
            raise ValueError("operation source IDs must resolve in the sealed input")
        if any(not isinstance(item[k], str) or not item[k].strip() for k in ("reason", "conditions")):
            raise ValueError("authored reason and conditions required")
        try:
            context = json.loads(item.pop("context_json"))
        except (ValueError, TypeError) as exc:
            raise ValueError("context_json must encode an explicit object") from exc
        if not isinstance(context, dict) or any(not isinstance(k, str) or isinstance(v, (dict, list)) for k, v in context.items()):
            raise ValueError("applicability context must contain scalar explicit conditions")
        if action == "relation":
            if target or item["expected_version"] or len(refs) < 2 or item["relation"] == "none" or not item["content"].strip():
                raise ValueError("new relation needs two materials, content and typed relation, without a target/version")
        else:
            if target not in visible or item["relation"] != "none":
                raise ValueError("maintenance target must be visible and relation must be none")
            if target in targeted:
                raise ValueError("one operation per target per review; provide one coherent revision")
            targeted.add(target)
            if item["expected_version"] != views[target]["memory_version"]:
                raise ValueError("stale expected_version; fresh maintenance must inspect the current interpretation")
            if action in {"revision", "retire"} and views[target].get("form", "raw") == "raw":
                raise ValueError("raw originals cannot be revised or retired; reinterpret their use instead")
            if action == "revision" and not item["content"].strip():
                raise ValueError("revision requires nonempty new content; allowed status values: "
                                 + ", ".join(ACTION_STATUSES["revision"]))
        owners = {views[mid]["owner"] for mid in refs + ([target] if target else [])}
        if len(owners) != 1:
            raise ValueError("a maintenance operation cannot merge private libraries")
        records.append({"id": "MEMORY." + receipt[:20] + "." + str(index + 1), **item,
                        "context": context, "source": deepcopy(source),
                        "evidence_kind": "model_authored_judgment" if source["kind"] == "model" else "fixture",
                        "semantic_truth_verified": False})
    result["records"].extend(records)
    result["evaluated_material_ids"] = list(dict.fromkeys(result["evaluated_material_ids"] + reviewed))
    result["reviews"].append({"id": "MEMORY.REVIEW." + receipt[:20], "receipt_sha256": receipt,
        "output_sha256": output_hash, "source": deepcopy(source), "reviewed_material_ids": reviewed,
        "record_ids": [r["id"] for r in records], "summary": output["summary"]})
    return result
