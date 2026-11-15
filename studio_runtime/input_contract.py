"""Measure complete role inputs and enforce bounded abstract/scenario windows."""
import json


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def generation_request_context(member, records):
    """Preserve received request bodies and provenance as supplemental B input.

    Selection supplies these records from its immutable received-request journal.
    This pure check validates structure/member binding, not external truth of a
    receipt. It cannot replace the core's real parent request or establish that
    materials were delivered; normal selection response checks still do that.
    """
    if not isinstance(records, list) or not records:
        raise ValueError("generation request context requires nonempty received records")
    seen = set()
    for item in records:
        if not isinstance(item, dict) or set(item) != {"member", "request", "source"} or item["member"] != member:
            raise ValueError("generation request context must match the actual member")
        request, source = item["request"], item["source"]
        if not isinstance(request, dict) or set(request) != {"id", "desired_difference", "reason", "material_ids"}:
            raise ValueError("generation context requires the full received selection request")
        if any(not isinstance(request[k], str) or not request[k].strip() for k in ("id", "desired_difference", "reason")):
            raise ValueError("received selection request fields must be nonempty")
        if request["id"] in seen or not isinstance(request["material_ids"], list) or any(not isinstance(v, str) or not v.strip() for v in request["material_ids"]):
            raise ValueError("invalid or duplicate received selection request")
        seen.add(request["id"])
        if not isinstance(source, dict) or any(not isinstance(source.get(k), str) or not source[k].strip()
                for k in ("call_id", "agent_id", "input_sha256", "reference", "mode", "evidence_kind")):
            raise ValueError("received selection request requires its original source")
        output = source.get("raw_output")
        if not isinstance(output, dict) or any(not isinstance(output.get(k), str) or not output[k].strip() for k in ("id", "sha256")):
            raise ValueError("received selection request requires its original output reference")
    return json.loads(_encode(records))


def measure_input(payload, *, task_windows=None, core_request=False, mapping_source=None):
    """Account for EVERY explicit field, preserving literal mixed partitions.

    core_request is retained for callers, but grants no free-form envelope
    exemption. Numeric limits and declared semantic allocation are distinct.
    Unmapped task and extra fields default to scenario, never silently disappear.
    """
    if not isinstance(payload, dict) or any(not isinstance(payload.get(w), list) for w in ("A", "B")):
        raise ValueError("member input requires explicit A and B lists")
    task = payload.get("task", {})
    mapping = {} if task_windows is None else task_windows
    if not isinstance(task, dict) or not isinstance(mapping, dict) or not set(mapping) <= set(task):
        raise ValueError("task_windows must map existing task fields")
    basis = mapping_source or {"source": "input_contract default", "reason":
        "A/B retain caller selection; rules/perspective/strategy/contracts use A; all other context uses B. Semantic fit remains unverified."}
    if not isinstance(basis, dict) or any(not isinstance(basis.get(k), str) or not basis[k].strip() for k in ("source", "reason")):
        raise ValueError("input mapping requires source and reason")
    charged = {"abstract": {}, "scenario": {}}
    fields = {"A": "abstract", "B": "scenario"}
    for key, value in task.items():
        choice = mapping.get(key, "scenario")
        if isinstance(choice, list):
            if not isinstance(value, str) or not choice or any(
                not isinstance(p, dict) or set(p) != {"window", "text"}
                or p["window"] not in charged or not isinstance(p["text"], str) for p in choice
            ) or "".join(p["text"] for p in choice) != value:
                raise ValueError("mixed partitions must reconstruct the original task string exactly")
            for window in charged:
                parts = [p["text"] for p in choice if p["window"] == window]
                if parts:
                    charged[window]["task." + key] = parts
        elif isinstance(choice, str) and choice in charged:
            charged[choice]["task." + key] = value
        else:
            raise ValueError("task content cannot be excluded as an envelope")
        fields["task." + key] = choice
    abstract_fields = {"rules", "perspective", "strategy", "output_contract", "tool_policy", "cognitive_protocol"}
    for key, value in payload.items():
        if key in {"A", "B", "task"}:
            continue
        window = "abstract" if key in abstract_fields else "scenario"
        charged[window][key] = value
        fields[key] = window
    physical = {"abstract_chars": len(_encode(payload["A"])), "scenario_chars": len(_encode(payload["B"])),
                "total_chars": len(_encode(payload))}
    effective = {"abstract_chars": physical["abstract_chars"] + len(_encode(charged["abstract"])),
                 "scenario_chars": physical["scenario_chars"] + len(_encode(charged["scenario"])),
                 "total_chars": physical["total_chars"]}
    materials = [{"window": window, "id": item.get("id"), "source": item.get("source"),
                  "use": item.get("use", "as_recorded"), "basis": "explicit caller/material declaration"}
                 for window in ("A", "B") for item in payload[window] if isinstance(item, dict)]
    return {"schema": "operation_input.v1", "physical_windows": physical, "effective_windows": effective,
            "field_mapping": fields, "mapping_source": basis, "material_mapping": materials,
            "unselected_scope": "Only supplied input is measured; selection journal separately records considered/unselected items.",
            "semantic_allocation_verified": False, "core_request": bool(core_request),
            "unit": "Python Unicode characters in canonical JSON; not tokens or attention",
            "platform_context_included": False, "unmeasured": ["platform system/developer context", "tokenization", "model attention"],
            "attention_proportion_controlled": False}


def checked_input(payload, *, limits=None, prompt=None, **kwargs):
    """Shared strict member gate, including noncanonical exact prompt length."""
    from .parliament import DEFAULT_LIMITS
    chosen = dict(DEFAULT_LIMITS if limits is None else limits)
    if set(chosen) != set(DEFAULT_LIMITS) or any(type(v) is not int or v <= 0 for v in chosen.values()):
        raise ValueError("member limits must be explicit positive character counts")
    accounting = measure_input(payload, **kwargs)
    if prompt is not None:
        if json.loads(prompt) != payload:
            raise ValueError("exact prompt disagrees with measured payload")
        accounting["physical_windows"]["total_chars"] = len(prompt)
        accounting["effective_windows"]["total_chars"] = len(prompt)
    if any(accounting["effective_windows"][k] > chosen[k] for k in chosen):
        raise ValueError("member effective window overflow; explicitly reselect without truncation: " + _encode(accounting["effective_windows"]))
    return accounting, chosen
