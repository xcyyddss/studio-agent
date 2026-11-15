"""Bounded input composition with explicit sources and material selection."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from studio_runtime.parliament import DEFAULT_LIMITS, MEMBER_RULES, encode
from studio_runtime.selection import MaterialSelector, SelectionBudgetError
from studio_runtime.input_contract import checked_input
from .roles import COMMON, RULES, SCHEMAS
from .cognitive_protocols import attach
from .media import visual_inputs, MAX_IMAGES
from .input_lifecycle import validate_input_task, InputContractError

PROTOCOL = "议会q×I与独立作者、父采纳/有证据冗余拒绝/上报、贡献fresh评估、条件学习；抽象与工程相互修正。固定窗口只度量显式字符，不证明token/注意力或宿主绝对隔离。"


def _hash(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def _public(item):
    return {k: deepcopy(v) for k, v in item.items() if not k.startswith("_")}


def _terms(value):
    """A disclosed retrieval heuristic, never a semantic value judgment."""
    value = encode(value).lower() if not isinstance(value, str) else value.lower()
    words = set(re.findall(r"[a-z0-9_][a-z0-9_.:-]+", value))
    words -= {"the", "and", "with", "this", "that", "from", "for", "task", "id", "kind", "content"}
    for run in re.findall(r"[\u3400-\u9fff]+", value):
        words.update(run[i:i + 2] for i in range(len(run) - 1))
    return words


def _eligibility(item, member, purpose, context):
    if item.get("owner") != member:
        return "outside member ownership"
    declared = item.get("purpose") or item.get("_purpose")
    if (declared in {"profile_only", "user_profile_only"} or
            "profile_only" in item.get("tags", [])) and purpose not in {"user_profile", "profile_only"}:
        return "profile_only: not authorized as design evidence"
    policy = item.get("use_conditions", {})
    if policy.get("allowed_purposes") and purpose not in policy["allowed_purposes"]:
        return "outside declared allowed_purposes"
    applicability = item.get("applicability", {})
    if applicability.get("status") in {"retired", "superseded", "inapplicable"} and not context.get("include_historical"):
        return "historical interpretation; explicit historical retrieval required"
    expected = applicability.get("context", {})
    if expected and any(key in context and context[key] != value for key, value in expected.items()):
        return "explicit applicability context mismatch"
    return None


def material_catalog(materials, member, *, purpose="design_association", use_context=None, cursor=0, page_size=12):
    """An ownership/purpose-filtered directory. References are not delivered bodies."""
    if type(cursor) is not int or cursor < 0 or type(page_size) is not int or not 1 <= page_size <= 100:
        raise ValueError("catalog cursor/page_size out of bounds")
    mid = member["id"] if isinstance(member, dict) else member
    allowed = [m for m in materials if not _eligibility(m, mid, purpose, use_context or {})]
    entries = [{"id": m["id"], "window": m["window"], "form": m.get("form", "raw"),
                "chars": len(encode(_public(m))), "sha256": _hash(_public(m)),
                "source": deepcopy(m.get("source")), "body_delivered": False} for m in allowed]
    end = cursor + page_size
    return {"entries": entries[cursor:end], "total": len(entries),
            "next_cursor": end if end < len(entries) else None,
            "boundary": "Directory only. Request material_ids through selection_requests for original bodies."}


def retrieve_material(materials, member, material_id, *, purpose="design_association", use_context=None, sha256=None):
    """Resolve only an explicitly named private-library item without broadening use."""
    mid = member["id"] if isinstance(member, dict) else member
    item = next((m for m in materials if m["id"] == material_id), None)
    if item is None:
        raise ValueError("unknown material_id")
    reason = _eligibility(item, mid, purpose, use_context or {})
    if reason:
        raise ValueError(reason)
    value = _public(item)
    if sha256 is not None and _hash(value) != sha256:
        raise ValueError("material changed since directory was sealed")
    return value


def select_input(role, member, task, materials, *, strategy="", core=False, limits=None, offset=0,
                 required_ids=(), purpose="design_association", use_context=None, catalog_cursor=0, output_schema=None):
    """Task-conditioned, auditable whole-item retrieval with one exploration slot.

    Lexical matching is explicitly mechanical. Semantic applicability is only
    supplied by a role's recorded applicability judgment, never inferred here.
    """
    chosen_limits = limits or DEFAULT_LIMITS
    required = set(required_ids)
    materials = list(materials)
    by_id = {m["id"]: m for m in materials}
    if len(by_id) != len(materials):
        raise ValueError("duplicate material ids")
    if not required <= set(by_id):
        raise ValueError("required material ids do not exist: " + encode(sorted(required - set(by_id))))
    context = use_context or {}
    tokens = _terms(task)
    ranked, excluded = [], []
    for item in materials:
        reason = _eligibility(item, member["id"], purpose, context)
        if reason:
            if item["id"] in required:
                raise ValueError("required material is ineligible: " + reason)
            excluded.append({"material_id": item["id"], "reason": reason, "body_delivered": False})
            continue
        overlap = sorted(tokens & _terms([item.get("content", ""), item.get("tags", []), item.get("applicability", {})]))
        explicit = item["id"].lower() in tokens
        score = 100 * explicit + len(overlap)
        ranked.append((item, score, overlap))
    ranked.sort(key=lambda r: (-r[1], -int(r[0].get("form") == "synthesis"), -r[0].get("_order", 0), r[0]["id"]))
    # A distinct finite opportunity survives zero keyword overlap. Rotation only
    # selects this slot; it no longer controls the whole relevance ordering.
    pool = [r for r in reversed(ranked) if r[0]["id"] not in required and "runtime_original" not in r[0].get("tags", [])]
    opportunity = pool[offset % len(pool)][0]["id"] if len(pool) > 1 else None
    directory = material_catalog(materials, member, purpose=purpose, use_context=context, cursor=catalog_cursor, page_size=4)
    visible_directory = {"total": directory["total"], "sample_ids": [e["id"] for e in directory["entries"]],
                         "next_cursor": directory["next_cursor"], "bodies_not_implied": True,
                         "retrieve": "selection_requests.material_ids requests any allowed library ID; directory pages use desired_difference='material_catalog cursor=N'"}
    strategy = (strategy or "Task-conditioned private-library retrieval; uncertain usefulness is allowed.") + "\nMaterial directory: " + encode(visible_directory)
    payload = {"rules": COMMON + RULES[role], "perspective": member["perspective"],
               "strategy": strategy,
               "task": task, "A": [], "B": [], "output_contract": output_schema or SCHEMAS[role]}
    if core:
        payload = {"rules": MEMBER_RULES, "perspective": member["perspective"], "task": task,
                   "strategy": COMMON + RULES[role] + strategy + "\nJSON Schema: " + encode(output_schema or SCHEMAS[role]),
                   "A": [], "B": []}
    payload = attach(payload, role)
    ordered = [r for r in ranked if r[0]["id"] in required] + [r for r in ranked if r[0]["id"] not in required]
    options = []
    for item, score, overlap in ordered:
        m = _public(item)
        options.append({"material_id": m["id"], "material": m, "window": m["window"],
                        "channel": "difference" if m["id"] in required else "opportunity" if m["id"] == opportunity else "relevant",
                        "use": "as_recorded", "allowed_uses": ["as_recorded"],
                        "reason": "required source" if m["id"] in required else "rotating exploration; usefulness unknown" if m["id"] == opportunity else
                                  "lexical task match=" + str(score) + "; terms=" + encode(overlap[:12]) + "; semantic relevance unverified",
                        "retrieval_score": score,
                        **({"ineligible_reason": "original available by explicit request; not automatically repeated in every inquiry"}
                           if "runtime_original" in m.get("tags", []) and m["id"] not in required else {})})
    selected = MaterialSelector().select(member, {"payload": payload, "max_items": 12,
               "opportunity_limit": 1, "revisit_limit": 2, "core_request": core}, options, chosen_limits)
    if not required <= set(selected["abstract_ids"] + selected["scenario_ids"]):
        raise SelectionBudgetError("required original evidence needs another explicit batch; it cannot be silently omitted", selected["accounting"])
    payload["A"] = [_public(by_id[i]) for i in selected["abstract_ids"]]
    payload["B"] = [_public(by_id[i]) for i in selected["scenario_ids"]]
    prompt = encode(payload)
    accounting, _ = checked_input(payload, limits=chosen_limits, prompt=prompt, core_request=core)
    omitted = selected["unselected"] + excluded
    for record in omitted:
        original = by_id[record["material_id"]]
        record["body_delivered"] = False
        # Restricted source text is not disclosed through the omission channel.
        if not _eligibility(original, member["id"], purpose, context):
            record.update(source=deepcopy(original.get("source")), sha256=_hash(_public(original)))
    return prompt, {**selected, "unselected": omitted, "omitted_catalog": omitted,
                    "accounting": accounting, "rendered_strategy": payload["strategy"],
                    "required_ids": list(required_ids),
                    "policy": "required originals; explicit use conditions; lexical task match; one rotating exploration slot; no semantic relevance claim",
                    "purpose": purpose, "catalog": directory, "catalog_cursor": catalog_cursor, "offset": offset}


def evidence_batches(role, member, task, materials, required_ids, **kwargs):
    """Partition all explicit source chunks; every chunk must enter one batch."""
    if not required_ids:
        return [[]]
    batches, batch = [], []
    for mid in required_ids:
        try:
            select_input(role, member, task, materials, required_ids=batch + [mid], **kwargs)
        except SelectionBudgetError:
            if not batch:
                raise
            batches.append(batch)
            batch = [mid]
            select_input(role, member, task, materials, required_ids=batch, **kwargs)
        else:
            batch.append(mid)
    if batch:
        batches.append(batch)
    return batches


class ProjectionBudgetError(ValueError):
    """A single indivisible decision/context unit does not fit the bound."""
    def __init__(self, message, *, reference=None, chars=None, max_chars=None, accounting=None):
        super().__init__(message)
        self.reference, self.chars, self.max_chars = reference, chars, max_chars
        self.accounting = accounting


def _pointer(path):
    return "/" + "/".join(str(x).replace("~", "~0").replace("/", "~1") for x in path) if path else ""


def context_reference(task, path=()):
    return {"source_sha256": _hash(task), "pointer": _pointer(path)}


def retrieve_context(task, reference, *, start=0, max_chars=16000):
    """Retrieve an exact original or an explicitly incomplete JSON character range.

    The caller must persist/pass the sealed original task. A hash/reference does
    not grant access to another task, owner, file or live mutable database.
    """
    if type(start) is not int or start < 0 or type(max_chars) is not int or max_chars <= 0:
        raise ValueError("invalid original range")
    if reference.get("source_sha256") != _hash(task):
        raise ValueError("original payload does not match sealed source hash")
    pointer = reference.get("pointer")
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        raise ValueError("invalid JSON pointer")
    value = task
    for token in pointer.split("/")[1:] if pointer else []:
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not token.isdigit() or str(int(token)) != token:
                raise ValueError("invalid list pointer")
            value = value[int(token)]
        elif isinstance(value, dict):
            value = value[token]
        else:
            raise ValueError("pointer traverses a scalar")
    body = encode(value)
    if start > len(body):
        raise ValueError("range starts beyond original")
    end = min(len(body), start + max_chars)
    return {"reference": deepcopy(reference), "content": body[start:end], "encoding": "canonical JSON",
            "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(), "start": start, "end": end,
            "total_chars": len(body), "complete": start == 0 and end == len(body),
            "next_start": end if end < len(body) else None,
            **({"value": deepcopy(value)} if start == 0 and end == len(body) else {})}


def _context_entry(task_hash, path, value):
    return {"id": value.get("id", value.get("path", value.get("return_id"))) if isinstance(value, dict) else None,
            "reference": {"source_sha256": task_hash, "pointer": _pointer(path)},
            "chars": len(encode(value)), "sha256": _hash(value), "body_delivered": False}


def _artifact_catalog(task, digest, cursor, page_size, max_chars):
    path = ("artifacts",) if "artifacts" in task else ("public_evidence", "artifacts")
    records = task.get("artifacts", []) if len(path) == 1 else task.get("public_evidence", {}).get("artifacts", [])
    reference = {"source_sha256": digest, "pointer": _pointer(path)}
    page = {"section": "artifacts", "reference": reference, "entries": [], "total": len(records),
            "next_cursor": cursor if cursor < len(records) else None, "body_delivered": False,
            "order": "reverse sealed record order; recency does not imply acceptance or final version"}
    for index in list(range(len(records) - 1, -1, -1))[cursor:cursor + page_size]:
        record = records[index]
        content = record.get("content", "")
        entry = {"path": record.get("path", record.get("id")), "source_job": record.get("source_job"),
                 "content_chars": record.get('content_chars',len(content)), "content_bytes": record.get('content_bytes',len(content.encode("utf-8"))),
                 "content_sha256": record.get('sha256',hashlib.sha256(content.encode("utf-8")).hexdigest()),
                 "body_delivered": 'content' in record,
                 "retrieve": "artifact.read or cognition.process binding this exact locator",
                 "reference": {"source_sha256": digest, "pointer": _pointer((*path, index))}}
        trial = {**page, "entries": page["entries"] + [entry]}
        end = cursor + len(trial["entries"])
        trial["next_cursor"] = end if end < len(records) else None
        if len(encode(trial)) > max_chars:
            break
        page = trial
    return page


def context_catalog(task, *, cursor=0, page_size=12, section=None):
    """Paginate the sealed task's actual records, rather than inventing summaries."""
    if type(cursor) is not int or cursor < 0 or type(page_size) is not int or not 1 <= page_size <= 100:
        raise ValueError("catalog cursor/page_size out of bounds")
    digest = _hash(task)
    if section == "artifacts":
        page = _artifact_catalog(task, digest, cursor, page_size, 4000)
        if not page["entries"] and cursor < page["total"]:
            raise ProjectionBudgetError("one artifact locator exceeds catalog bound; retrieve its exact record",
                reference=page["reference"], max_chars=4000)
        return page
    if section is not None:
        raise ValueError("unknown context catalog section")
    entries = []
    for key, value in task.items():
        if key == "public_evidence" and isinstance(value, dict):
            for nested, records in value.items():
                if isinstance(records, list):
                    entries.extend(_context_entry(digest, (key, nested, i), record) for i, record in enumerate(records))
                else:
                    entries.append(_context_entry(digest, (key, nested), records))
        elif isinstance(value, list):
            entries.extend(_context_entry(digest, (key, i), record) for i, record in enumerate(value))
        else:
            entries.append(_context_entry(digest, (key,), value))
    end = cursor + page_size
    return {"source_sha256": digest, "entries": entries[cursor:end], "total": len(entries),
            "next_cursor": end if end < len(entries) else None}


def project_context(role, task, *, max_chars=28000, schema=None, capacity_check=None):
    """Project real parent/author input; split whole mandatory decision groups.

    This is a transport projection, not a semantic summarizer. Optional historical
    bodies may be omitted with retrievable references. Required bodies are never
    truncated. Multiple batches are partial scopes, not independently sufficient
    author completions or permission to terminate the whole parent task.
    """
    if role not in {"parent", "author"} or not isinstance(task, dict):
        raise ValueError("parent/author role and actual task object required")
    if type(max_chars) is not int or max_chars <= 0:
        raise ValueError("projection bound must be within ordinary input maximum")
    if "context_projection" in task:
        raise ValueError("project the sealed original task, not a previous projection")
    original = deepcopy(task)
    validate_input_task(original)
    digest = _hash(original)
    contract = schema or SCHEMAS[role]
    base = deepcopy(original)
    optional = []

    def optional_field(key):
        value = base.pop(key, None)
        if value is None:
            return
        if isinstance(value, list):
            base[key] = []
            optional.extend(((key, i), item) for i, item in enumerate(value))
        elif isinstance(value, dict):
            base[key] = {}
            for nested, records in value.items():
                if isinstance(records, list):
                    base[key][nested] = []
                    optional.extend(((key, nested, i), item) for i, item in enumerate(records))
                else:
                    base[key][nested] = records
        else:
            base[key] = value

    for key in ("public_evidence", "artifacts", "existing_substitutes", "escalation_outcomes", "prior_tool_result_delivery"):
        optional_field(key)
    # Conditional history and completed transport receipts remain retrievable;
    # neither is a new decision obligation merely because it is historical.
    # Current selections already have complete required groups. Remove an exact
    # duplicate from the base, never a distinct or partially matching record.
    unresolved = original.get("unresolved", [])
    historical_unresolved = {i for i, item in enumerate(unresolved)
                            if isinstance(item, dict) and item.get("state") == "conditional"}
    transport_unresolved = {i for i, item in enumerate(unresolved)
                           if isinstance(item, dict) and item.get("kind") == "selection"
                           and item.get("state") in {"delivery_prepared", "consumer_processed", "delivery_acknowledged"}}
    duplicate_selections = {i for i, item in enumerate(unresolved)
                            if role == "parent" and isinstance(item, dict) and item.get("state") == "open"
                            and item.get("kind") == "selection" and item in original.get("selection_requests", [])}
    optional_unresolved = historical_unresolved | transport_unresolved
    if optional_unresolved or duplicate_selections:
        base["unresolved"] = [deepcopy(item) for i, item in enumerate(unresolved)
                              if i not in optional_unresolved | duplicate_selections]
        optional.extend((("unresolved", i), unresolved[i]) for i in sorted(optional_unresolved))
    # Every result not yet successfully delivered in this role/scope is a whole
    # required evidence group, paired with its actual request. It is not part of
    # every batch's fixed constraints. Only proven prior delivery makes a result
    # optional; relative age or a newer tool request never does. Human/operator
    # inputs remain intact in the base.
    groups = []
    inputs = original.get("user_inputs", [])
    tools = original.get("tool_requests", [])
    tool_inputs = [item for item in inputs if item.get("kind") == "tool_result"]
    prior_delivery = {item["input_id"]: item["input_sha256"] for item in original.get("prior_tool_result_delivery", [])}
    delivered_tool_ids = {item.get("id") for item in tool_inputs if prior_delivery.get(item.get("id")) == _hash(item)}
    obligations = set(original.get("input_obligations", []))
    optional_tool_ids = delivered_tool_ids - obligations
    current_input_ids = {item.get("id") for item in tool_inputs} - optional_tool_ids
    if tool_inputs:
        by_request = {item["id"]: item for item in tools if "id" in item}
        base["user_inputs"] = []
        # Prefer inputs never delivered, then least recently delivered unfinished
        # inputs. A followup spanning several calls must not starve later batches.
        delivery_rank = {item["input_id"]: n for n, item in enumerate(original.get("prior_tool_result_delivery", []))}
        ordered_inputs = sorted(enumerate(inputs), key=lambda row: (delivery_rank.get(row[1].get("id"), -1), row[0]))
        for index, item in ordered_inputs:
            if item.get("kind") == "tool_result":
                if item.get("id") in optional_tool_ids:
                    optional.append((("user_inputs", index), item))
                else:
                    request = by_request.get(item.get("request_id"))
                    group = {"user_inputs": [deepcopy(item)], "tool_requests": [deepcopy(request)] if request else []}
                    groups.append((_context_entry(digest, ("user_inputs", index), item), group))
            else:
                base["user_inputs"].append(deepcopy(item))
        current_request_ids = {item.get("request_id") for item in tool_inputs if item.get("id") in current_input_ids}
        base["tool_requests"] = []
        for index, item in enumerate(tools):
            if item.get("state") == "answered":
                if item.get("id") not in current_request_ids:
                    optional.append((("tool_requests", index), item))
            else:
                base["tool_requests"].append(deepcopy(item))
    required_fields = ("inquiry_results", "undisposed_candidates", "new_returns", "selection_requests", "escalations", "late_originals") if role == "parent" else ("I",)
    if role == "parent":
        base["requests"] = []
        base["assessments"] = []
    for field in required_fields:
        if field not in original:
            continue
        base[field] = []
        for index, item in enumerate(original[field]):
            group = {field: [deepcopy(item)]}
            if field == "undisposed_candidates":
                request = item.get("request")
                group["requests"] = [deepcopy(r) for r in original.get("requests", []) if r.get("id") == request]
            if field == "inquiry_results":
                group["requests"] = [deepcopy(r) for r in original.get("requests", []) if r.get("id") == item["id"]]
            if field == "new_returns":
                group["assessments"] = [deepcopy(a) for a in original.get("assessments", []) if a.get("returned") == item.get("id")]
            groups.append((_context_entry(digest, (field, index), item), group))
    # A request with no remaining candidate is historical inquiry context, not
    # another mandatory decision group. Keep its exact original retrievable and
    # disclose the zero-candidate scope. Current selection duties remain required.
    requests_without_candidates = []
    if role == "parent":
        linked_requests = {c.get("request") for c in original.get("undisposed_candidates", [])}
        requests_without_candidates = [i for i, r in enumerate(original.get("requests", [])) if r.get("id") not in linked_requests]
        optional.extend((("requests", i), original["requests"][i]) for i in requests_without_candidates)
        returned = {r["id"] for r in original.get("new_returns", [])}
        optional.extend((("assessments", i), a) for i, a in enumerate(original.get("assessments", [])) if a.get("returned") not in returned)

    catalog = [_context_entry(digest, path, value) for path, value in optional]
    artifact_directory = _artifact_catalog(original, digest, 0, 4, 2000)

    def compose(value, index=0, count=1, remaining=0, included=()):
        value = deepcopy(value)
        visible = {item["id"] for item in value.get("user_inputs", [])}
        if "input_obligations" in original:
            value["input_obligations"] = [ref for ref in original["input_obligations"] if ref in visible]
            value["deferred_input_obligations"] = [ref for ref in original["input_obligations"] if ref not in visible]
        images = visual_inputs(value)
        if images:
            value['native_images'] = images
        returns = [r["id"] for r in value.get("new_returns", [])]
        if role == "parent":
            value["response_scope"] = {**original.get("response_scope", {}), "contribution_decisions_exact_ids": returns}
        manifest = {"source_sha256": digest, "original_reference": {"source_sha256": digest, "pointer": ""},
                    "batch_index": index + 1, "batch_count": count, "remaining_required_groups": remaining,
                    "required_scope_complete": count == 1, "source_complete": False,
                    "terminal_allowed_by_projection": count == 1,
                    "optional_records_total": len(optional), "optional_records_delivered": len(included),
                    **({"required_tool_results": {"total": len(current_input_ids),
                        "visible_ids": [item["id"] for item in value.get("user_inputs", []) if item.get("id") in current_input_ids],
                        "not_in_this_batch": len(current_input_ids - {item.get("id") for item in value.get("user_inputs", [])}),
                        "reference": {"source_sha256": digest, "pointer": "/user_inputs"},
                        "boundary": "Each unseen or unfinished tool result and its request is a complete required group. input_obligations apply to this batch; deferred_input_obligations remain mandatory in later batches. Prior delivery does not discharge processing."}} if current_input_ids else {}),
                    **({"artifact_directory": artifact_directory} if artifact_directory["total"] else {}),
                    **({"prior_tool_result_delivery": {"total": len(delivered_tool_ids),
                        "id_sample": sorted(delivered_tool_ids)[:4],
                        "reference": {"source_sha256": digest, "pointer": "/prior_tool_result_delivery"},
                        "boundary": "Exact bodies appeared in successful calls in this role and scope. Historical delivery is not current reading, adoption or verification; omitted bodies remain retrievable."}} if delivered_tool_ids else {}),
                    **({"requests_without_candidates": {"total": len(requests_without_candidates),
                        "id_sample": [original["requests"][i]["id"] for i in requests_without_candidates[:4]],
                        "reference": {"source_sha256": digest, "pointer": "/requests"},
                        "boundary": "No pending candidate from these requests in this supplied scope. Historical request bodies remain retrievable, not implicitly read. Selection duties remain separate required groups."}} if role == "parent" else {}),
                    "catalog": {"source_sha256": digest, "cursor": 0, "page_size": 12},
                    "unresolved": {"total": len(unresolved), "required": len(unresolved) - len(optional_unresolved),
                        "historical_conditional": len(historical_unresolved), "transport_history": len(transport_unresolved),
                        "represented_by_selection_requests": len(duplicate_selections),
                        "delivered": len(value.get("unresolved", [])) + sum(unresolved[i] in value.get("selection_requests", []) for i in duplicate_selections),
                        "transport_status_sample": [{"id": unresolved[i].get("id"), "state": unresolved[i]["state"],
                            "reference": {"source_sha256": digest, "pointer": _pointer(("unresolved", i))}}
                            for i in sorted(transport_unresolved)[:4]],
                        "reference": {"source_sha256": digest, "pointer": "/unresolved"},
                        "boundary": "Conditional history remains unresolved. Prepared transport is not consumption or acceptance. Current selections are required in selection_requests. Projection never authorizes terminal action."},
                    "omitted_records_sample": [{"id": entry["id"], "pointer": entry["reference"]["pointer"],
                                                "chars": entry["chars"]}
                        for entry in catalog if entry["reference"]["pointer"] not in {_pointer(p) for p in included}][:4],
                    "included_optional_pointers": [_pointer(p) for p in included],
                    "boundary": "Only displayed bodies were delivered. Missing historical bodies remain retrievable from the sealed original; references are not proof of reading. Multiple required batches forbid whole-task completion until all scopes are handled. Request exact original IDs/JSON pointers when evidence is needed."}
        value["context_projection"] = manifest
        value = share_exact_bodies(value)
        prompt = encode(attach({"rules": COMMON + RULES[role], "task": value, "output_contract": contract}, role))
        return {"task": value, "prompt": prompt, "manifest": manifest}

    def merge(value, group):
        value = deepcopy(value)
        for field, records in group.items():
            for record in records:
                if record not in value.setdefault(field, []):
                    value[field].append(deepcopy(record))
        return value

    # Reserve decimal width for all batches while packing; final rendered prompts
    # are checked again including the actual schema and manifest.
    def within(prompt, value):
        return (len(prompt) <= max_chars if capacity_check is None else capacity_check(prompt, contract, len(visual_inputs(value))))

    def fits(value, included=()):
        return len(visual_inputs(value)) <= MAX_IMAGES and within(compose(value, len(groups), max(1, len(groups)), len(groups), included)["prompt"], value)

    # Current constraints and indivisible decisions take priority over the
    # locator sample. Keep the artifact-only catalog entry point even at zero
    # displayed locators. Optional history never displaces this reserved page.
    while artifact_directory["entries"] and (not fits(base) or any(not fits(merge(base, group)) for _, group in groups)):
        artifact_directory["entries"].pop()
        artifact_directory["next_cursor"] = len(artifact_directory["entries"])
    if not fits(base):
        size = len(compose(base)["prompt"])
        raise ProjectionBudgetError("complete goal/revision/user constraints exceed bound; explicit restructuring required", reference=context_reference(original), chars=size, max_chars=max_chars)
    packed, current = [], deepcopy(base)
    current_count = 0
    for reference, group in groups:
        trial = merge(current, group)
        if not fits(trial):
            if current_count:
                packed.append((current, current_count))
            current, current_count = merge(base, group), 1
            if not fits(current):
                raise ProjectionBudgetError("one required decision group exceeds bound; retrieve/restructure it explicitly, never truncate", reference=reference["reference"], chars=len(compose(current)["prompt"]), max_chars=max_chars)
        else:
            current, current_count = trial, current_count + 1
    if current_count or not packed:
        packed.append((current, current_count))

    batches = []
    consumed = 0
    for index, (current, n) in enumerate(packed):
        terms = _terms({k: v for k, v in current.items() if k not in {"public_evidence", "artifacts"}})
        ordered = sorted(optional, key=lambda row: (-len(terms & _terms(row[1])), _pointer(row[0])))
        included = []
        focus = set(original.get("context_focus_jobs", []))
        if "context_focus_jobs" in original:
            # History is available through the exact catalog; it is not free
            # filler. Only fresh artifact bodies are automatically reattached.
            ordered = [(path, value) for path, value in ordered if
                path[0] in {"existing_substitutes", "unresolved", "requests"} or
                isinstance(value, dict) and value.get("source_job") in focus]
        def add_prefix(count):
            trial = deepcopy(current)
            for path, value in ordered[:count]:
                if len(path) == 3: trial[path[0]][path[1]].append(deepcopy(value))
                else: trial[path[0]].append(deepcopy(value))
            return trial
        # Measure whole prefixes logarithmically instead of re-tokenizing the
        # full prompt for every historical item. Required groups are unchanged.
        low, high = 0, len(ordered)
        if high and fits(add_prefix(high), [p for p,_ in ordered]):
            low = high
        else:
            while low < high:
                mid = (low + high + 1) // 2
                if fits(add_prefix(mid), [p for p,_ in ordered[:mid]]): low = mid
                else: high = mid - 1
        if low:
            current = add_prefix(low)
            included = [p for p,_ in ordered[:low]]
        consumed += n
        batch = compose(current, index, len(packed), len(groups) - consumed, included)
        validate_input_task(batch["task"], original=original)
        if not within(batch["prompt"], batch["task"]):
            raise ProjectionBudgetError("projection metadata overflow", chars=len(batch["prompt"]), max_chars=max_chars)
        # The measurement belongs to metadata, not an unmeasured prompt field.
        batch["manifest"] = {**batch["manifest"], "explicit_chars": len(batch["prompt"])}
        batches.append(batch)
    covered = {ref for batch in batches for ref in batch["task"].get("input_obligations", [])}
    if covered != obligations:
        raise InputContractError("required inputs are not covered by the projected batches")
    return {"batches": batches, "catalog": catalog, "source_sha256": digest,
            "max_chars": max_chars, "unit": "canonical JSON Unicode characters, not model tokens or hidden context"}


def ordinary_input(role, task, *, schema=None):
    payload = attach({"rules": COMMON + RULES[role], "task": task, "output_contract": schema or SCHEMAS[role]}, role)
    prompt = encode(payload)
    return prompt


def share_exact_bodies(task):
    """Lossless same-packet interning, after visibility selection.

    Only historical/evidence bodies are interned. Required INPUT records, user
    constraints, decisions and reading ranges keep their exact sealed shape.
    Provenance records stay in place; equality never implies equal permission.
    """
    value = deepcopy(task)
    occurrences = {}
    roots = {"artifacts", "public_evidence", "work_history", "existing_substitutes", "escalation_outcomes"}
    def walk(node, path):
        if isinstance(node, dict):
            for key, body in node.items():
                if key in {"content", "text", "summary", "ans", "con"} and isinstance(body, str) and len(body) >= 2048:
                    occurrences.setdefault(body, []).append((node, key, _pointer(path + (key,))))
                else:
                    walk(body, path + (key,))
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, path + (i,))
    for root in sorted(roots & set(value)):
        walk(value[root], (root,))
    pool = {}
    saved = 0
    for body, refs in occurrences.items():
        if len(refs) < 2:
            continue
        identity = _hash(body)
        pool[identity] = {"body": body, "occurrences": [ref[2] for ref in refs]}
        for node, key, _ in refs:
            node[key] = "[完整同文见本调用 shared_bodies." + identity + ".body；来源与用途以本条记录为准]"
        saved += (len(refs) - 1) * len(body)
    if pool:
        value["shared_bodies"] = pool
        value["body_sharing"] = {"exact_duplicate_chars": saved,
            "rule": "Each shared_bodies body is fully delivered in this call. References replace only exactly equal text, not records, provenance, permissions or input obligations. Original source pointers remain available through context.retrieve."}
    return value
