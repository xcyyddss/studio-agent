"""Deterministic material admission and whole-item packing for role inputs."""
import hashlib
from .parliament import ParliamentError, DEFAULT_LIMITS, encode, ident, text

USES = {"as_recorded", "current_fact", "historical_reason", "association", "evaluation"}
CHANNELS = ("difference", "opportunity", "revisit", "relevant")


class SelectionError(ParliamentError):
    pass


class SelectionBudgetError(SelectionError):
    def __init__(self, message, accounting):
        super().__init__(message)
        self.accounting = accounting


def _sha(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def measure_input(payload, *, task_windows=None, core_request=False, mapping_source=None):
    from .input_contract import measure_input as shared_measure
    try:
        return shared_measure(payload, task_windows=task_windows, core_request=core_request, mapping_source=mapping_source)
    except ValueError as exc:
        raise SelectionError(str(exc)) from exc


class MaterialSelector:
    """Deterministic admission/packing; semantic priorities remain explicit inputs."""
    def select(self, member, request, candidates, limits):
        if set(limits) != set(DEFAULT_LIMITS) or any(type(n) is not int or n <= 0 for n in limits.values()):
            raise SelectionError("limits must be positive integer character counts")
        limits = dict(limits)
        max_items = request.get("max_items", 12)
        opportunity_limit = request.get("opportunity_limit", 1)
        revisit_limit = request.get("revisit_limit", 2)
        if any(type(n) is not int or n < 0 for n in (max_items, opportunity_limit, revisit_limit)):
            raise SelectionError("selection counts must be nonnegative integers")
        payload = {**request["payload"], "A": [], "B": []}
        mapping = request.get("task_windows")
        core_request = request.get("core_request", False)
        measure = lambda: measure_input(payload, task_windows=mapping, core_request=core_request, mapping_source=request.get("input_mapping"))
        accounting = measure()
        if any(accounting["effective_windows"][k] > limits[k] for k in limits):
            raise SelectionBudgetError("complete task exceeds the effective window; explicitly restructure, never truncate", accounting)
        selected, unselected, seen = [], [], set()
        counts = {c: 0 for c in CHANNELS}
        for option in sorted(candidates, key=lambda o: CHANNELS.index(o["channel"])):
            mid = option["material_id"]
            ident(mid)
            if mid in seen:
                raise SelectionError("a material may have only one explicit selection option")
            seen.add(mid)
            reason = option.get("ineligible_reason")
            material = option.get("material")
            channel = option["channel"]
            use = option["use"]
            if not reason:
                if material.get("kind") != "material" or material.get("owner") != member["id"]:
                    reason = "outside member ownership"
                elif use not in USES or use not in option.get("allowed_uses", USES):
                    reason = "outside declared material purpose"
                elif material.get("window") not in {option["window"], "either"}:
                    reason = "declared A/B type mismatch"
                elif use == "current_fact" and not material.get("applicability", {}).get("current_fact_allowed"):
                    reason = "not established as a current fact"
            if channel == "revisit" and not reason:
                trigger = option.get("trigger", {})
                prior = option.get("prior", {})
                if trigger.get("kind") not in {"q", "evaluation", "fact", "user_signal"} or trigger.get("before") == trigger.get("after"):
                    reason = "revisit requires an actual referenced condition change"
                elif not trigger.get("reference") or not prior.get("reason") or not prior.get("reference"):
                    reason = "revisit must retain the prior reason and change source"
            if len(selected) >= max_items and not reason:
                reason = "finite opportunity item limit"
            if channel == "opportunity" and counts[channel] >= opportunity_limit and not reason:
                reason = "finite opportunity slot already allocated"
            if channel == "revisit" and counts[channel] >= revisit_limit and not reason:
                reason = "sparse revisit limit"
            if reason:
                unselected.append({"material_id": mid, "channel": channel, "reason": reason,
                                   **({"use_conditions": option["use_conditions"]} if "use_conditions" in option else {})})
                continue
            text(option["reason"], "selection reason (unknown usefulness is acceptable)")
            window = "A" if option["window"] == "abstract" else "B"
            payload[window].append(material)
            trial = measure()
            if any(trial["effective_windows"][k] > limits[k] for k in limits):
                payload[window].pop()
                unselected.append({"material_id": mid, "channel": channel, "reason": "window overflow; whole item not selected",
                                   "attempted_sizes": trial["effective_windows"],
                                   **({"use_conditions": option["use_conditions"]} if "use_conditions" in option else {})})
                continue
            counts[channel] += 1
            record = {k: v for k, v in option.items() if k not in {"material", "ineligible_reason"}}
            record.update(source=material["source"], material_sha256=_sha(material))
            selected.append(record)
            accounting = trial
        return {"abstract_ids": [m["id"] for m in payload["A"]], "scenario_ids": [m["id"] for m in payload["B"]],
                "material_uses": {o["material_id"]: o["use"] for o in selected}, "selected": selected,
                "unselected": unselected, "considered_ids": [o["material_id"] for o in candidates],
                "accounting": accounting, "limits": limits, "prompt_sha256": hashlib.sha256(encode(payload).encode()).hexdigest(),
                "strategy": "explicit bounded channels; whole-item packing, no semantic scoring",
                "cognitive_effect": "not_established"}
