"""Versioned cognitive instructions, rendered before selection and dispatch.

These are application protocols, not task evidence or learned experience.
All applicable units are delivered in full. No LLM chooses which rules to omit.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import hashlib
import json
from pathlib import Path

CORE_ROLES = ("member", "author", "parent", "assessment", "memory_review", "user_review")
ROLES = (*CORE_ROLES, "processing", "context_summary", "context_review")
CATALOG_PATH = Path(__file__).with_name("protocols") / "catalog.json"
OPERATIONS_PATH = Path(__file__).with_name("protocols") / "operations.json"


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_encode(value).encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def _catalog():
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    if data.get("schema") != "studio.cognitive_protocols.v1":
        raise ValueError("unsupported cognitive protocol catalog")
    ids = set()
    for unit in data["units"]:
        if (set(unit) != {"id", "title", "purpose", "roles", "stages", "instructions"}
                or unit["id"] in ids or not unit["roles"] or not set(unit["roles"]) <= set(ROLES)
                or any(not unit[k] for k in ("id", "title", "purpose", "stages", "instructions"))):
            raise ValueError("invalid cognitive protocol unit")
        ids.add(unit["id"])
    if any(not any(role in u["roles"] for u in data["units"]) for role in CORE_ROLES):
        raise ValueError("role has no cognitive protocol")
    return data


def catalog():
    """Return an editable copy; the installed protocol cannot be mutated by callers."""
    return deepcopy(_catalog())


@lru_cache(maxsize=1)
def _operations():
    data = json.loads(OPERATIONS_PATH.read_text(encoding="utf-8"))
    ids = {u["id"] for u in _catalog()["units"]}
    found, covered = set(), set()
    for operation in data["operations"]:
        if (operation["id"] in found or operation["role"] not in ROLES
                or not set(operation["concepts"]) <= ids
                or any(not operation[k] for k in ("background", "trigger", "inputs", "work", "result", "continuation", "example"))):
            raise ValueError("invalid cognitive operation")
        found.add(operation["id"])
        covered.update(operation["concepts"])
    if covered != ids or not set(data["defaults"].values()) <= found:
        raise ValueError("operation dependencies do not cover the complete concept library")
    return data


def operations():
    return deepcopy(_operations())


def operation_spec(operation):
    found = next((o for o in _operations()["operations"] if o["id"] == operation), None)
    if found is None:
        raise ValueError("unknown cognitive operation: " + str(operation))
    return deepcopy(found)


def operation_catalog():
    return [{"id": o["id"], "role": o["role"], "title": o["title"], "trigger": o["trigger"]}
            for o in _operations()["operations"]]


def bundle(role, operation=None):
    if role not in ROLES:
        raise ValueError("unknown cognitive role: " + role)
    data, definitions = _catalog(), _operations()
    operation = operation or definitions["defaults"].get(role)
    spec = operation_spec(operation)
    if spec["role"] != role:
        raise ValueError("cognitive operation does not belong to role")
    units = [{k: deepcopy(v) for k, v in unit.items() if k != "roles"}
             for unit in data["units"] if unit["id"] in spec["concepts"]]
    body = {"schema": "studio.operation_protocol.v2", "version": definitions["version"], "role": role,
            "application": data["application"], "foundation": definitions["foundation"],
            "composition": deepcopy(definitions["composition"]), "operation": spec, "units": units}
    return {**body, "sha256": _hash(body)}


def attach(payload, role, operation=None):
    """Add complete operation semantics and dependencies BEFORE budgeting."""
    result = deepcopy(payload)
    expected = bundle(role, operation)
    if "cognitive_protocol" in result and result["cognitive_protocol"] != expected:
        raise ValueError("cannot silently replace a sealed cognitive protocol")
    result["cognitive_protocol"] = expected
    return result


def inspect_prompt(prompt, role, *, require_current=True):
    """Check actual serialized content, not just a manifest of requested IDs.

    Stored calls may use a previous version. Their self-contained bundle is
    verified against its sealed hash at the provider; new jobs must match the
    installed catalog exactly. Source task text never supplies these rules.
    """
    value = json.loads(prompt).get("cognitive_protocol")
    if not isinstance(value, dict) or value.get("role") != role:
        raise ValueError("actual prompt lacks the role cognitive protocol")
    body = {k: v for k, v in value.items() if k != "sha256"}
    if value.get("sha256") != _hash(body):
        raise ValueError("cognitive protocol content hash mismatch")
    operation = value.get("operation", {}).get("id")
    if require_current and value != bundle(role, operation):
        raise ValueError("actual cognitive instructions differ from installed role protocol")
    return {"schema": value["schema"], "version": value["version"], "role": role,
            "sha256": value["sha256"], "unit_ids": [u["id"] for u in value["units"]],
            "chars": len(_encode(value)), "complete_body_in_prompt": True,
            **({"operation": operation} if operation else {})}
