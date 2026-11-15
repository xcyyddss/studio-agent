"""Serializable execution boundary; metadata is never silently added to a prompt."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
import os
import uuid


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def atomic_json(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


@dataclass(frozen=True)
class JobSpec:
    job_id: str
    run_id: str
    kind: str
    prompt: str
    schema: dict
    metadata: dict = field(default_factory=dict)
    role: str = ""

    def __post_init__(self):
        for key in ("job_id", "run_id", "kind"):
            if not isinstance(getattr(self, key), str) or not getattr(self, key):
                raise ValueError(f"{key} must be a nonempty string")
        if not isinstance(self.prompt, str) or not isinstance(self.schema, dict):
            raise ValueError("prompt must be text and schema must be an object")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata must be an object")
        canonical(self.to_dict())

    @property
    def input_text(self):
        return self.prompt

    @property
    def output_schema(self):
        return self.schema

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        return cls(**value)


class SchemaError(ValueError):
    pass


def validate_output(value, schema: dict, path="$", root=None):
    """Strict standard-library validation for the JSON-schema subset used by roles.

    Unsupported assertion keywords fail closed instead of claiming validation.
    """
    root = schema if root is None else root
    if isinstance(schema, bool):
        if not schema:
            raise SchemaError(f"{path}: forbidden by schema")
        return
    supported = {"type", "properties", "required", "additionalProperties", "items", "enum", "const",
                 "anyOf", "oneOf", "allOf", "$ref", "$defs", "definitions", "$schema", "$id",
                 "title", "description", "default", "examples", "minItems", "maxItems", "uniqueItems",
                 "minLength", "maxLength", "pattern", "minimum", "maximum", "exclusiveMinimum",
                 "exclusiveMaximum", "minProperties", "maxProperties"}
    unknown = set(schema) - supported
    if unknown:
        raise SchemaError(f"{path}: unsupported schema keywords {sorted(unknown)}")
    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            raise SchemaError("Only local schema references are supported")
        target = root
        for part in ref[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        validate_output(value, target, path, root)
    for keyword in ("anyOf", "oneOf", "allOf"):
        if keyword in schema:
            passed = 0
            for branch in schema[keyword]:
                try:
                    validate_output(value, branch, path, root)
                    passed += 1
                except SchemaError:
                    pass
            expected = len(schema[keyword]) if keyword == "allOf" else 1
            if (keyword == "anyOf" and passed == 0) or (keyword != "anyOf" and passed != expected):
                raise SchemaError(f"{path}: {keyword} failed")
    types = schema.get("type")
    if types:
        types = [types] if isinstance(types, str) else types
        checks = {"object": isinstance(value, dict), "array": isinstance(value, list),
                  "string": isinstance(value, str), "boolean": isinstance(value, bool),
                  "null": value is None, "integer": isinstance(value, int) and not isinstance(value, bool),
                  "number": isinstance(value, (int, float)) and not isinstance(value, bool)}
        if not any(checks.get(t, False) for t in types):
            raise SchemaError(f"{path}: expected {types}")
    if "enum" in schema and not any(canonical(value) == canonical(item) for item in schema["enum"]):
        raise SchemaError(f"{path}: invalid enum")
    if "const" in schema and canonical(value) != canonical(schema["const"]):
        raise SchemaError(f"{path}: invalid const")
    if isinstance(value, dict):
        missing = set(schema.get("required", [])) - set(value)
        if missing:
            raise SchemaError(f"{path}: missing {sorted(missing)}")
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in props:
                validate_output(item, props[key], f"{path}.{key}", root)
            elif extra is False:
                raise SchemaError(f"{path}: unexpected property {key}")
            elif isinstance(extra, dict):
                validate_output(item, extra, f"{path}.{key}", root)
        for key, op in (("minProperties", lambda a, b: a < b), ("maxProperties", lambda a, b: a > b)):
            if key in schema and op(len(value), schema[key]):
                raise SchemaError(f"{path}: {key}")
    if isinstance(value, list):
        for key, op in (("minItems", lambda a, b: a < b), ("maxItems", lambda a, b: a > b)):
            if key in schema and op(len(value), schema[key]):
                raise SchemaError(f"{path}: {key}")
        if schema.get("uniqueItems") and len({canonical(item) for item in value}) != len(value):
            raise SchemaError(f"{path}: duplicate items")
        for index, item in enumerate(value):
            if "items" in schema:
                validate_output(item, schema["items"], f"{path}[{index}]", root)
    if isinstance(value, str):
        import re
        for key, op in (("minLength", lambda a, b: a < b), ("maxLength", lambda a, b: a > b)):
            if key in schema and op(len(value), schema[key]):
                raise SchemaError(f"{path}: {key}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise SchemaError(f"{path}: pattern")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        for key, op in (("minimum", lambda a, b: a < b), ("maximum", lambda a, b: a > b),
                        ("exclusiveMinimum", lambda a, b: a <= b), ("exclusiveMaximum", lambda a, b: a >= b)):
            if key in schema and op(value, schema[key]):
                raise SchemaError(f"{path}: {key}")
    canonical(value)
