"""Bounded USER collaboration under ROOT; no database, queue or model calls.

Mutations belong in the caller's workflow transaction. Event source is assigned
by the trusted input adapter, never by model output. Permission metadata is not
consent detection: callers must supply only the explicitly permitted fragments.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from .roles import obj, arr, S, STRINGS, validate
from .cognitive_protocols import attach

KEY = "user_collaboration"
SOURCES = {"human", "operator", "fixture", "model", "runtime"}
KINDS = {"question_proposed", "question_presented", "user_reply", "downstream_use",
         "observation", "review_requested"}
ASSET_KINDS = {"profile_claim", "potential_hypothesis", "guidance_method", "negative_example"}

ASSET_SCHEMA = obj(
    id=S, kind={"type": "string", "enum": sorted(ASSET_KINDS)}, content=S, conditions=S,
    basis={"type": "string", "enum": ["human_observation", "operator_observation", "fixture", "model_inference"]},
    evidence_event_ids=STRINGS, related_asset_ids=STRINGS, revision_of=S, conflict_with=STRINGS,
    allowed_uses={"type": "array", "items": S, "minItems": 1,
                  "description": "Nonempty permitted purposes, each allowed by every visible source. A guidance method may use only collaboration; profile is not mandatory for derived assets."},
    audience={"type": "array", "items": S, "minItems": 1,
              "description": "Nonempty permitted recipients; include USER or * for internal review. Recipients and purposes are independent dimensions; never broaden visible source audiences."},
    topic_ids=STRINGS,
    status={"type": "string", "enum": ["tentative", "supported", "unknown", "retracted"]})
REVIEW_SCHEMA = obj(processed_event_ids=STRINGS, assets=arr(ASSET_SCHEMA), summary=S,
                    unknowns=STRINGS, wake_conditions=STRINGS)
REVIEW_RULES = """你是ROOT下固定USER协作议题的独立作者，仅处理当前任务包中的显式材料，不直接读取文件或调用工具。
职责是理解用户、改善当前目标中的协作，用户明确目标优先；不建立第二终极目标，不把用户成长设为必需。
只输出output_contract规定的JSON。processed_event_ids逐项等于本次pending_event_ids；events也可包含已处理但与本次关联的旧事件，可以无新资产。
原事件是不可改写的来源记录。question_proposed仅表示提出；question_presented才表示实际呈现；user_reply的source=human才是已登记的真实用户输入；operator/fixture/model不得冒充用户。
只引本次events与related_assets中实际见到的id，遗漏目录不算看过证据。不代答，不把未知、沉默、未呈现、拒答推断为能力低。
每项资产有情境conditions、出处和权限。profile_claim是可修订的情境判断，不是永久标签；潜力只写在信息支持和时间条件下的potential_hypothesis，状态不得为supported。
runtime来源仅记录实际提交的产物与版本链，不是用户反馈，也不证明建议的因果效果。无独立可见下游记录时保留tentative，不能强行升级。
guidance_method/negative_example须区分信息前体、反馈和下游作用；提出、回复、引用、采纳不证明有效。supported方法须有真实呈现→human答复→下游引用链，仍不证明因果收益。
所有资产都是本次模型判断。basis=human_observation仅描述已看见的human答复/自述；operator_observation注明操作员观察；含fixture证据的产物须basis=fixture。模型解释用model_inference。
修订生成新id并用revision_of指旧版本；不同解释保留conflict_with，不能删除旧认识。只修订受影响资产。
允许用途与受众不得扩大本次任何可见正文的权限（不只检查所引证据）；profile仅供理解用户，不可转为未经允许的任务依据。要向普通议员供材，来源必须已显式允许collaboration且许可对应受众；不能通过抽象改写或漏引私有材料绕过。
用途allowed_uses与受众audience是独立维度。资产allowed_uses非空即可，不必包含profile；guidance_method可仅为["collaboration"]。audience必须包含USER或*以便内部审阅。仅profile授权的原始材料仍不能派生collaboration用途；USER受众许可不代表追加profile用途。修订旧方法时继续保留其用途限制。
referenced_permissions只给关联方法的用途/受众限制，不表示本次读过其正文，也不是可引用的判断证据；即使正文因预算或fixture隔离未送达，这些限制仍约束本批所有产物。
按需输出A方法/反例、B相关观察/情境。无新观察可休眠，unknowns/wake_conditions保留条件，不阻塞ROOT、不虚构任务维持运行。
"""


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_encode(value).encode("utf-8")).hexdigest()


def _nonempty(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name + " must be a nonempty string")
    return value


def _strings(value, name):
    if not isinstance(value, (list, tuple)) or not value or any(not isinstance(x, str) or not x.strip() for x in value):
        raise ValueError(name + " must be a nonempty string list")
    return list(dict.fromkeys(value))


def _get(state):
    if KEY not in state:
        raise ValueError("USER collaboration is not initialized")
    return state[KEY]


def _permits(values, requested):
    return "*" in values or requested in values


def _subset(requested, granted):
    return "*" in granted or set(requested) <= set(granted)


def event_audience(state, audience, *, links=(), material_refs=()):
    """Intersect new-event recipients with linked events and known assets.

    This does not change the independently granted profile intake purpose.
    Linked method reuse purposes are enforced separately on review outputs.
    """
    data = _get(state)
    recipients = set(_strings(audience, "event audience"))
    by_id = {e["id"]: e for e in data["events"]}
    if any(ref not in by_id for ref in links):
        raise ValueError("event audience links must name existing events")
    sources = [by_id[ref] for ref in links] + [data["assets"][ref] for ref in material_refs if ref in data["assets"]]
    for source in sources:
        granted = set(source["audience"])
        if "*" in recipients:
            recipients = granted
        elif "*" not in granted:
            recipients &= granted
    if not recipients or not ("USER" in recipients or "*" in recipients):
        raise ValueError("event audience intersection must retain USER review access")
    return sorted(recipients)


def _referenced_permissions(data, events):
    ids = sorted({ref for e in events for ref in e["material_refs"] if ref in data["assets"]})
    return [{"asset_id": ref, "allowed_uses": list(data["assets"][ref]["allowed_uses"]),
             "audience": list(data["assets"][ref]["audience"]),
             "fixture": "fixture" in data["assets"][ref]["provenance"]["sources"]}
            for ref in ids]


def initialize(state, goal, profile_seeds=None):
    """Idempotently create the single USER responsibility in caller-owned state.

    Seeds require source, content, allowed_uses and audience explicitly. Existing
    historical materials are never imported or granted profile access implicitly.
    """
    _nonempty(goal, "goal")
    if KEY in state:
        if state[KEY]["goal"] != goal:
            raise ValueError("goal changes require an explicit event, not reinitialization")
        return deepcopy(state[KEY])
    temporary = {KEY: {"schema": "user_collaboration.v1", "topic_id": "USER", "parent": "ROOT",
                      "goal": goal, "events": [], "assets": {}, "superseded_by": {}, "relations": [],
                      "processed_event_ids": [], "reviews": {}, "unknowns": [], "wake_conditions": [],
                      "generation": 0}}
    for index, seed in enumerate(profile_seeds or []):
        for field in ("source", "content", "allowed_uses", "audience"):
            if field not in seed:
                raise ValueError("profile seed requires explicit " + field)
        record_event(temporary, seed.get("id", f"USER.SEED.{index + 1}"), "observation",
                     source=seed["source"], content=seed["content"],
                     topic_id=seed.get("topic_id", "ROOT"), question_id=seed.get("question_id"),
                     job_id=seed.get("job_id"), revision_id=seed.get("revision_id"),
                     allowed_uses=seed["allowed_uses"], audience=seed["audience"],
                     links=seed.get("links"), material_refs=seed.get("material_refs"))
    state[KEY] = temporary[KEY]
    return deepcopy(state[KEY])


def record_event(state, event_id, kind, *, source, topic_id="ROOT", question_id=None,
                 job_id=None, revision_id=None, content="", allowed_uses=("profile",),
                 audience=("USER",), links=None, material_refs=None):
    """Append one externally observed fact, using stable caller event identity.

    links is a list of previous event IDs. material_refs is a list of immutable
    USER asset-version IDs or external source locators. A downstream_use must
    link an earlier event and identify a consuming job and revision. Input
    adapters must not label fixture or model text as human.
    """
    data = _get(state)
    _nonempty(event_id, "event_id")
    _nonempty(topic_id, "topic_id")
    if source not in SOURCES or kind not in KINDS:
        raise ValueError("invalid event source or kind")
    if not isinstance(content, str) or len(content) > 12000:
        raise ValueError("content must be a permitted fragment of at most 12000 characters")
    uses, audiences = _strings(allowed_uses, "allowed_uses"), _strings(audience, "audience")
    if "profile" not in uses or not _permits(audiences, "USER"):
        raise ValueError("USER may consume only explicitly profile-authorized material")
    for value in (question_id, job_id, revision_id):
        if value is not None:
            _nonempty(value, "event reference")
    links, material_refs = list(links or []), list(material_refs or [])
    if any(not isinstance(x, str) or not x for x in links + material_refs):
        raise ValueError("links/material_refs must be string lists")
    event = dict(id=event_id, kind=kind, source=source, topic_id=topic_id, question_id=question_id,
                 job_id=job_id, revision_id=revision_id, content=content,
                 allowed_uses=uses, audience=audiences, links=links, material_refs=material_refs)
    old = next((e for e in data["events"] if e["id"] == event_id), None)
    if old:
        if old != event:
            raise ValueError("event identity cannot be rewritten")
        return {"event": deepcopy(old), "duplicate": True}
    by_id = {e["id"]: e for e in data["events"]}
    if any(ref not in by_id for ref in links):
        raise ValueError("event links must refer to existing events")
    linked = [by_id[ref] for ref in links]
    if kind in {"question_proposed", "question_presented"} and not question_id:
        raise ValueError("question events require question_id")
    if kind == "question_presented":
        if not any(e["kind"] == "question_proposed" and e["question_id"] == question_id and
                   e["topic_id"] == topic_id for e in linked):
            raise ValueError("presentation must link its original proposed question")
    if kind == "user_reply" and question_id:
        if not any(e["kind"] == "question_presented" and e["question_id"] == question_id and
                   e["topic_id"] == topic_id for e in linked):
            raise ValueError("answer must link a recorded presentation; do not infer delivery")
    if kind == "downstream_use" and (not links or not job_id or not revision_id):
        raise ValueError("downstream use requires linked evidence, consuming job and revision")
    data["events"].append(deepcopy(event))
    return {"event": deepcopy(event), "duplicate": False}


def pending_events(state):
    data = _get(state)
    processed = set(data["processed_event_ids"])
    processed.update(e for b in data.get("quarantined_batches", []) if b["state"] == "quarantined" for e in b["event_ids"])
    return deepcopy([e for e in data["events"] if e["id"] not in processed])


def review_prompt(context):
    """Explicit, standalone fresh-call prompt; callers retain the exact context."""
    return _encode(attach({"rules": REVIEW_RULES, "task": context, "output_contract": REVIEW_SCHEMA}, "user_review"))


def context_for_review(state, *, max_chars=24000, max_events=24, max_assets=16, capacity_check=None):
    """Pack whole pending events plus related old versions into a bounded prompt.

    No history transcript or hidden U window is injected. Counts of omissions
    are explicit; omitted bodies are not evidence. A too-small budget yields a
    context with no events; callers should report that condition, not dispatch.
    """
    if min(max_chars, max_events, max_assets) < 1:
        raise ValueError("positive review bounds required")
    data = _get(state)
    pending = pending_events(state)
    context = {"topic_id": "USER", "parent": "ROOT", "goal": data["goal"],
               "generation": data["generation"], "fresh": True, "events": [], "pending_event_ids": [], "related_assets": [],
               "referenced_permissions": [],
               "unknowns": [], "wake_conditions": [],
               "omitted": {"events": len(pending), "assets": len(data["assets"])},
               "budget": {"max_chars": max_chars, "max_events": max_events, "max_assets": max_assets},
               "context_id": "0" * 64}
    def fits(value):
        prompt = review_prompt(value)
        return len(prompt) <= max_chars if capacity_check is None else capacity_check(prompt, REVIEW_SCHEMA)
    if not fits(context):
        raise ValueError("review budget cannot contain the fixed contract and goal")

    def fit(field, item):
        context[field].append(deepcopy(item))
        if fits(context):
            return True
        context[field].pop()
        return False

    # A downstream use may arrive after the presentation/reply were reviewed.
    # Retrieve its exact linked antecedents within the same explicit budget.
    by_id = {e["id"]: e for e in data["events"]}
    pending_ids = {e["id"] for e in pending}
    cohort = None
    for event in pending:
        permissions = (tuple(sorted(event["allowed_uses"])), tuple(sorted(event["audience"])), event["source"] == "fixture")
        if cohort is not None and permissions != cohort:
            continue
        needed, frontier = set(), [event["id"]]
        while frontier:
            ref = frontier.pop()
            if ref not in needed:
                needed.add(ref)
                frontier.extend(by_id[ref]["links"])
        bundle = {e["id"]: e for e in data["events"] if e["id"] in needed}
        previous_events = context["events"]
        previous_pending = context["pending_event_ids"]
        previous_permissions = context["referenced_permissions"]
        already = {e["id"] for e in previous_events}
        context["events"] = previous_events + [deepcopy(e) for ref, e in bundle.items() if ref not in already]
        context["pending_event_ids"] = [e["id"] for e in context["events"] if e["id"] in pending_ids]
        context["referenced_permissions"] = _referenced_permissions(data, context["events"])
        if len(context["events"]) > max_events or not fits(context):
            context["events"] = previous_events
            context["pending_event_ids"] = previous_pending
            context["referenced_permissions"] = previous_permissions
        elif cohort is None:
            cohort = permissions
    seen_ids = set(context["pending_event_ids"])
    topics = {e["topic_id"] for e in context["events"]}
    referenced = {ref for e in context["events"] for ref in e["material_refs"]}
    related = [a for a in data["assets"].values()
               if a["id"] in referenced or ((set(a["topic_ids"]) & (topics | {"*"})) and
                   a["id"] not in data["superseded_by"])]
    related.sort(key=lambda a: (a["id"] not in referenced, -a["generation"], a["id"]))
    for asset in related:
        if len(context["related_assets"]) >= max_assets:
            break
        if not any(e["source"] == "fixture" for e in context["events"]) and "fixture" in asset["provenance"]["sources"]:
            continue
        # Do not accidentally make a shareable event batch consume unrelated
        # restricted personal assets. Explicit references may narrow the batch.
        if asset["id"] not in referenced and any(
                not _subset([use for use in e["allowed_uses"] if use != "profile"] or ["profile"], asset["allowed_uses"]) or
                not _subset(e["audience"], asset["audience"]) for e in context["events"]):
            continue
        fit("related_assets", asset)
    # Prior free-form review notes are retained for inspection, never silently
    # reintroduced into another permission cohort. Reusable unknowns belong in
    # explicitly scoped assets like every other judgment.
    context["omitted"] = {"events": len(pending) - len(seen_ids),
                          "assets": len(data["assets"]) - len(context["related_assets"])}
    context["context_id"] = _hash({k: v for k, v in context.items() if k != "context_id"})
    return context


def _check_context(data, context):
    if context.get("context_id") != _hash({k: v for k, v in context.items() if k != "context_id"}):
        raise ValueError("review context digest mismatch")
    if context.get("topic_id") != "USER" or context.get("parent") != "ROOT" or not context.get("fresh"):
        raise ValueError("a fresh USER context is required")
    events = {e["id"]: e for e in data["events"]}
    for event in context["events"]:
        if events.get(event["id"]) != event:
            raise ValueError("context event does not match its immutable original")
    for asset in context["related_assets"]:
        if data["assets"].get(asset["id"]) != asset:
            raise ValueError("context asset does not match its immutable version")
    if "referenced_permissions" in context and context["referenced_permissions"] != _referenced_permissions(data, context["events"]):
        raise ValueError("context referenced permissions do not match their immutable asset versions")
    if len(review_prompt(context)) > context["budget"]["max_chars"]:
        raise ValueError("context exceeds its declared prompt budget")


def apply_review(state, review, *, job_id, context, source=None, isolate_invalid=False):
    """Validate a model review and atomically add immutable versioned assets.

    Replaying one job is idempotent. Stale overlapping reviews are preserved as
    late results; they cannot overwrite newer judgments or consume unseen input.
    source is assigned by the trusted provider adapter, not by the role output:
    {kind: model|fixture, reference: original result/receipt ID}. Omitting it
    uses the default model source. Fixture outputs are preserved
    unchanged but their derived assets remain quarantined from ordinary reuse.
    """
    data = _get(state)
    _nonempty(job_id, "job_id")
    validate(review, REVIEW_SCHEMA)
    origin = deepcopy(source) if source is not None else {"kind": "model", "reference": job_id}
    if not isinstance(origin, dict) or set(origin) != {"kind", "reference"} or origin["kind"] not in {"model", "fixture"}:
        raise ValueError("review source must identify model or fixture and its original reference")
    _nonempty(origin["reference"], "review source reference")
    # The replay identity includes an explicit source when provided.
    identity = _hash({"review": review, "context": context, **({"source": origin} if source is not None else {})})
    if job_id in data["reviews"]:
        old = data["reviews"][job_id]
        if old["identity"] != identity:
            raise ValueError("review job identity cannot be reused for another result")
        return {**deepcopy(old["outcome"]), "duplicate": True}
    _check_context(data, context)
    visible_events = {e["id"]: e for e in context["events"]}
    visible_assets = {a["id"]: a for a in context["related_assets"]}
    referenced_permissions = _referenced_permissions(data, context["events"])
    processed = review["processed_event_ids"]
    if len(processed) != len(set(processed)) or set(processed) != set(context["pending_event_ids"]) or not processed:
        raise ValueError("review must process exactly its nonempty visible event batch")
    stale = bool(set(processed) & set(data["processed_event_ids"])) or any(
        a["revision_of"] in data["superseded_by"] for a in review["assets"] if a["revision_of"])
    if stale:
        outcome = {"status": "late_preserved", "asset_ids": [], "processed_event_ids": [], "duplicate": False}
        data["reviews"][job_id] = {"identity": identity, "context_id": context["context_id"],
                                  "source": origin, "output": deepcopy(review), "outcome": outcome}
        return deepcopy(outcome)
    new_assets, revised = {}, set()
    def accept_asset(asset):
        _nonempty(asset["id"], "asset id")
        if asset["id"] in data["assets"] or asset["id"] in new_assets:
            raise ValueError("asset IDs name immutable versions and must be new")
        _nonempty(asset["content"], "asset content")
        _nonempty(asset["conditions"], "asset conditions")
        uses = _strings(asset["allowed_uses"], "asset allowed_uses")
        audiences = _strings(asset["audience"], "asset audience")
        _strings(asset["topic_ids"], "asset topic_ids")
        if not _permits(audiences, "USER"):
            raise ValueError("derived assets must remain available to USER for review")
        if any(e not in visible_events for e in asset["evidence_event_ids"]):
            raise ValueError("an unseen event cannot be cited as evidence")
        old_refs = list(dict.fromkeys(asset["related_asset_ids"] + asset["conflict_with"] +
                                     ([asset["revision_of"]] if asset["revision_of"] else [])))
        if any(ref not in visible_assets for ref in old_refs):
            raise ValueError("an unseen asset version cannot be cited or revised")
        if asset["revision_of"] and visible_assets[asset["revision_of"]]["kind"] != asset["kind"]:
            raise ValueError("revision must preserve asset kind; use a related asset for a different interpretation")
        if asset["revision_of"]:
            if asset["revision_of"] in revised:
                raise ValueError("one predecessor cannot have two replacing versions in one review; retain a conflict instead")
            revised.add(asset["revision_of"])
        evidence = [visible_events[e] for e in asset["evidence_event_ids"]]
        ancestors = [visible_assets[ref] for ref in old_refs]
        if not evidence and not ancestors:
            raise ValueError("asset needs visible evidence or a visible predecessor")
        # The model has read the whole bounded packet. Checking only its
        # self-reported citations would let a restricted paragraph be laundered
        # through a public citation. Every visible body constrains the output.
        for source in list(visible_events.values()) + list(visible_assets.values()) + referenced_permissions:
            if not _subset(uses, source["allowed_uses"]) or not _subset(audiences, source["audience"]):
                raise ValueError("derived asset cannot broaden source purpose or audience")
        # All transitive provenance stays attached even when old raw evidence is
        # absent from the current bounded call. It is not claimed as newly read.
        sources = sorted({e["source"] for e in evidence} |
                         {s for a in ancestors for s in a["provenance"]["sources"]})
        human_events = [e for e in evidence if e["source"] == "human" and
                        e["kind"] in {"user_reply", "observation"} and e["content"].strip()]
        observations = [e for e in evidence if e["kind"] in {"user_reply", "observation"} and e["content"].strip()]
        if "fixture" in sources and asset["basis"] != "fixture":
            raise ValueError("fixture-derived assets must preserve fixture basis")
        if asset["basis"] == "human_observation" and not human_events:
            raise ValueError("human_observation requires visible human input, not a model/fixture answer")
        if asset["basis"] == "operator_observation" and not any(e["source"] == "operator" for e in observations):
            raise ValueError("operator_observation requires visible operator observation")
        if asset["basis"] == "fixture" and "fixture" not in sources:
            raise ValueError("fixture basis needs fixture provenance")
        if asset["kind"] == "profile_claim" and asset["status"] not in {"unknown", "retracted"}:
            if not observations and not any(a["kind"] == "profile_claim" for a in ancestors):
                raise ValueError("questions or silence cannot establish a capability or preference claim")
            if observations and all(e["source"] == "model" for e in observations):
                raise ValueError("model-generated replies cannot establish a user profile")
        if asset["kind"] == "potential_hypothesis" and asset["status"] not in {"tentative", "unknown", "retracted"}:
            raise ValueError("potential remains a conditional hypothesis")
        if asset["kind"] in {"guidance_method", "negative_example"} and asset["status"] == "supported":
            if not _visible_method_chain(evidence, asset["revision_of"] or asset["id"]):
                raise ValueError("supported method needs visible presentation, human feedback and downstream use of its version")
        candidate = deepcopy(asset)
        # Provider provenance is independent of the evidence's source and the
        # role's claimed basis. Do not rewrite the preserved raw model payload.
        derived_sources = sorted(set(sources) | ({"fixture"} if origin["kind"] == "fixture" or
                                                  any(ref["fixture"] for ref in referenced_permissions) else set()))
        candidate.update(source={**origin, "job_id": job_id, "context_id": context["context_id"]},
                         generation=data["generation"] + 1,
                         provenance={"sources": derived_sources, "event_ids": sorted({e["id"] for e in evidence} |
                                     {ref for a in ancestors for ref in a["provenance"]["event_ids"]}),
                                     "inherited_asset_ids": old_refs,
                                     "permission_asset_ids": [ref["asset_id"] for ref in referenced_permissions],
                                     "boundary": "model judgment; observed use does not prove causal benefit"})
        new_assets[candidate["id"]] = candidate
    quarantined = []
    for asset in review["assets"]:
        before_revised = set(revised)
        try:
            accept_asset(asset)
        except ValueError as exc:
            if not isolate_invalid:
                raise
            revised.clear(); revised.update(before_revised)
            quarantined.append({"proposal":deepcopy(asset), "error":str(exc), "source_job":job_id,
                "context_id":context["context_id"], "state":"quarantined",
                "boundary":"Rejected proposal retained outside reusable assets; no status downgrade or invented evidence."})
    updated = deepcopy(data)
    for asset in new_assets.values():
        updated["assets"][asset["id"]] = asset
        if asset["revision_of"]:
            updated["superseded_by"][asset["revision_of"]] = asset["id"]
            updated["relations"].append({"kind": "revision", "from": asset["revision_of"], "to": asset["id"], "job_id": job_id})
        for conflict in asset["conflict_with"]:
            updated["relations"].append({"kind": "conflict", "from": asset["id"], "to": conflict, "job_id": job_id})
    updated["processed_event_ids"].extend(processed)
    updated.setdefault("quarantined_assets", []).extend(quarantined)
    updated["generation"] += 1
    updated["unknowns"] = list(dict.fromkeys(updated["unknowns"] + review["unknowns"]))
    updated["wake_conditions"] = list(dict.fromkeys(updated["wake_conditions"] + review["wake_conditions"]))
    outcome = {"status": "applied", "asset_ids": list(new_assets), "processed_event_ids": list(processed), "duplicate": False, "quarantined_count":len(quarantined)}
    updated["reviews"][job_id] = {"identity": identity, "context_id": context["context_id"],
                                 "source": origin, "output": deepcopy(review), "outcome": deepcopy(outcome)}
    state[KEY] = updated
    return outcome


def _visible_method_chain(events, method_id):
    by_id = {e["id"]: e for e in events}
    for use in events:
        if use["kind"] != "downstream_use" or method_id not in use["material_refs"] or use["source"] in {"fixture", "model"}:
            continue
        for reply_id in use["links"]:
            reply = by_id.get(reply_id, {})
            if reply.get("kind") != "user_reply" or reply.get("source") != "human" or not reply.get("content", "").strip():
                continue
            for shown_id in reply["links"]:
                shown = by_id.get(shown_id, {})
                if (shown.get("kind") == "question_presented" and shown.get("question_id") == reply["question_id"]
                        and method_id in shown.get("material_refs", []) and shown.get("source") not in {"fixture", "model"}):
                    return True
    return False


def _material(asset):
    return {"id": asset["id"], "owner": "USER", "window": "abstract" if asset["kind"] in
            {"guidance_method", "negative_example"} else "scenario", "form": "synthesis",
            "content": _encode({k: asset[k] for k in ("kind", "content", "conditions", "basis", "status", "provenance")}),
            "source": {"kind": asset["source"]["kind"], "reference": _encode(asset["source"])},
            "refs": list(asset["provenance"]["event_ids"]), "tags": ["user_collaboration", asset["kind"]]}


def select_materials(state, *, purpose, audience, topic_id, query="", max_chars=6000, max_items=8):
    """Return bounded eligible A methods/B observations; this is not broadcast.

    Selection is explicit topic/lexical matching, not a semantic quality claim.
    Fixture products never enter ordinary selection. Old versions remain
    addressable in state and sealed jobs, but are not chosen for new calls.
    """
    data = _get(state)
    for value, name in ((purpose, "purpose"), (audience, "audience"), (topic_id, "topic_id")):
        _nonempty(value, name)
    if max_chars < 0 or max_items < 0:
        raise ValueError("selection bounds must be nonnegative")
    terms = query.lower().split()
    candidates, omissions = [], {}
    for asset in data["assets"].values():
        reason = None
        if asset["id"] in data["superseded_by"] or asset["status"] == "retracted":
            reason = "historical_version"
        elif "fixture" in asset["provenance"]["sources"]:
            reason = "fixture_only"
        elif not _permits(asset["allowed_uses"], purpose) or not _permits(asset["audience"], audience):
            reason = "permission"
        elif not (topic_id in asset["topic_ids"] or "*" in asset["topic_ids"] or
                  terms and any(t in (asset["content"] + " " + asset["conditions"]).lower() for t in terms)):
            reason = "unrelated"
        if reason:
            omissions[reason] = omissions.get(reason, 0) + 1
        else:
            candidates.append(asset)
    candidates.sort(key=lambda a: (topic_id not in a["topic_ids"], -a["generation"], a["id"]))
    selected, size = [], 0
    for asset in candidates:
        material = _material(asset)
        cost = len(_encode(material))
        if len(selected) >= max_items or size + cost > max_chars:
            omissions["budget"] = omissions.get("budget", 0) + 1
            continue
        selected.append(material)
        size += cost
    return {"A": [m for m in selected if m["window"] == "abstract"],
            "B": [m for m in selected if m["window"] == "scenario"],
            "manifest": {"purpose": purpose, "audience": audience, "topic_id": topic_id,
                         "selected_ids": [m["id"] for m in selected], "selected_chars": size,
                         "max_chars": max_chars, "max_items": max_items, "omitted_counts": omissions,
                         "policy": "explicit permission then topic/lexical matching; all selected material counts in A/B"}}


def status(state):
    data = _get(state)
    pending = pending_events(state)
    current = [a for a in data["assets"].values() if a["id"] not in data["superseded_by"] and a["status"] != "retracted"]
    return {"topic_id": "USER", "parent": "ROOT", "state": "degraded" if any(b["state"] == "quarantined" for b in data.get("quarantined_batches", [])) else "awake" if pending else "sleeping",
            "quarantined_batches":deepcopy(data.get("quarantined_batches", [])),
            "quarantined_assets":deepcopy(data.get("quarantined_assets", [])),
            "blocks_root": False, "pending_count": len(pending), "event_count": len(data["events"]),
            "review_count": len(data["reviews"]), "asset_count": len(data["assets"]),
            "active_asset_ids": [a["id"] for a in current], "generation": data["generation"],
            "unknowns": deepcopy(data["unknowns"]), "wake_conditions": deepcopy(data["wake_conditions"])}
