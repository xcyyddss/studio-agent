"""One explicit capacity contract for assembly, dispatch and receipts.

Tokenization of visible text is not measurement of Codex's hidden envelope.
The proxy tokenizer, margin, image allowance and reserves are always disclosed.
"""
from functools import lru_cache
import base64
import hashlib
import json
import math
import re
from pathlib import Path

from .contracts import canonical

MODEL_CONTEXT_TOKENS = 872000
MAX_MODEL_CONTEXT_TOKENS = 1050000
# Codex turn/start independently bounds the prompt string. This is a transport
# limit, not a substitute for token accounting or an attention-window policy.
CLI_MAX_INPUT_CHARS = 1_048_576


def character_error(diagnostic):
    """Extract only public numeric capacity facts, never echo arbitrary stderr."""
    if not isinstance(diagnostic, str):
        return None
    if "input_too_large" not in diagnostic.lower() and "input exceeds the maximum length" not in diagnostic.lower():
        return None
    maximum = (re.search(r'"max_chars"\s*:\s*(\d+)', diagnostic)
               or re.search(r"maximum length of (\d+) characters", diagnostic, re.IGNORECASE))
    actual = re.search(r'"actual_chars"\s*:\s*(\d+)', diagnostic)
    return {"category": "context_capacity", "boundary": "cli_input_chars",
            "max_chars": int(maximum[1]) if maximum else CLI_MAX_INPUT_CHARS,
            **({"actual_chars": int(actual[1])} if actual else {}),
            "message": "CLI 单次输入超过字符上限，需要重新组装输入；原始输入已保留。",
            "retryable": False}
TOKENIZER_DATA = Path(__file__).with_name("tokenizer_data")
ENCODING_SHA256 = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"


@lru_cache(maxsize=1)
def tokenizer():
    try:
        import tiktoken
        # Do not use get_encoding/encoding_for_model here. Those constructors
        # may download missing tables, including while a workflow transaction
        # is open. Assembly must work offline and must never await a network.
        table = (TOKENIZER_DATA / "o200k_base.tiktoken").read_bytes()
        if hashlib.sha256(table).hexdigest() != ENCODING_SHA256:
            raise ValueError("bundled tokenizer table checksum mismatch")
        spec = json.loads((TOKENIZER_DATA / "o200k_base.json").read_text(encoding="utf-8"))
        ranks = {base64.b64decode(token, validate=True): int(rank)
                 for token, rank in (line.split() for line in table.splitlines() if line)}
        return tiktoken.Encoding(name=spec["name"], pat_str=spec["pat_str"],
            mergeable_ranks=ranks, special_tokens=spec["special_tokens"]), "o200k_base_proxy"
    except Exception:
        # A missing/corrupt installation must not silently create false precision.
        return None, "utf8_bytes_upper_bound"


@lru_cache(maxsize=96)
def text_tokens(text):
    codec, method = tokenizer()
    if codec is None:
        return len(text.encode("utf-8")), method
    return len(codec.encode(text, disallowed_special=())), method


def capacity(prompt, schema, *, images=0, settings=None):
    if settings is None:
        from .context_lifecycle import DEFAULT_POLICY
        settings = DEFAULT_POLICY
    p = settings
    n, method = text_tokens(prompt)
    s, _ = text_tokens(canonical(schema))
    visible = n + s
    # Schema appears both in the prompt and in --output-schema. Count both;
    # their actual provider transport overhead is not known locally.
    margin = 0 if method == "utf8_bytes_upper_bound" else math.ceil(
        visible * p["token_margin_percent"] / 100)
    reserved = p["output_reserve_tokens"] + p["platform_reserve_tokens"] + images * p["image_reserve_tokens"]
    usable = p["model_context_tokens"] * p["effective_window_percent"] // 100
    available = max(0, usable - reserved)
    charged = visible + margin
    return {
        "method": method, "visible_text_tokens": visible, "margin_tokens": margin,
        "accounted_text_tokens": charged, "reserved_tokens": reserved,
        "context_tokens": p["model_context_tokens"], "effective_context_tokens": usable,
        "available_text_tokens": available, "remaining_text_tokens": available - charged,
        "prompt_chars": len(prompt), "max_prompt_chars": CLI_MAX_INPUT_CHARS,
        "remaining_prompt_chars": CLI_MAX_INPUT_CHARS - len(prompt),
        "token_fits": charged <= available, "character_fits": len(prompt) <= CLI_MAX_INPUT_CHARS,
        "fits": charged <= available and len(prompt) <= CLI_MAX_INPUT_CHARS,
        "output_reserve_tokens": p["output_reserve_tokens"],
        "boundary": "Local text preflight, not exact provider token usage. Proxy tokenizer includes a safety margin. Hidden instructions, native tool growth and image tokenization are reserved, not measured. Provider capacity errors remain recoverable.",
    }
