"""Structural input duties shared by projection, dispatch and result import.

Delivery is not processing. A batch owns only visible inputs; the remaining
duties stay explicit and must be delivered in a later batch, never waived.
"""


class InputContractError(ValueError):
    """The software's task packet is inconsistent, not a model output fault."""


def _ids(values, label):
    if (not isinstance(values, list) or any(not isinstance(x, str) or not x for x in values)
            or len(values) != len(set(values))):
        raise InputContractError(label + " must contain unique nonempty input IDs")
    return set(values)


def validate_input_task(task, *, original=None):
    inputs = task.get("user_inputs", [])
    if not isinstance(inputs, list) or any(not isinstance(x, dict) or not x.get("id") for x in inputs):
        raise InputContractError("user_inputs must contain identified input records")
    visible = _ids([x["id"] for x in inputs], "user_inputs")
    required = _ids(task.get("input_obligations", []), "input_obligations")
    deferred = _ids(task.get("deferred_input_obligations", []), "deferred_input_obligations")
    if required - visible:
        raise InputContractError("required inputs are absent from this call: " + ", ".join(sorted(required - visible)))
    if deferred & visible or deferred & required:
        raise InputContractError("deferred inputs must be outside this call's visible scope")
    projection = task.get("context_projection", {})
    if deferred and (projection.get("batch_count", 1) <= 1 or projection.get("required_scope_complete", True)):
        raise InputContractError("deferred inputs require explicit incomplete batch coverage")
    if original is not None:
        expected = _ids(original.get("input_obligations", []), "source input_obligations")
        if required | deferred != expected:
            raise InputContractError("projection lost or invented input obligations")
    return visible


def validate_input_uses(task, output):
    visible = validate_input_task(task)
    uses = output.get("input_uses", [])
    seen = set()
    for use in uses:
        ref = use["input_id"]
        if ref not in visible:
            raise ValueError("input use is not visible in this call: " + ref)
        if ref in seen:
            raise ValueError("duplicate input use in this call: " + ref)
        seen.add(ref)
    missing = set(task.get("input_obligations", [])) - seen
    if missing:
        raise ValueError("every input_obligation needs explicit input_uses acknowledgment: " + ", ".join(sorted(missing)))
    if task.get("deferred_input_obligations") and (
            output.get("action") in {"deliver", "quiesce"} or output.get("status") == "complete"):
        raise ValueError("deferred input obligations prevent terminal completion")
    return visible
