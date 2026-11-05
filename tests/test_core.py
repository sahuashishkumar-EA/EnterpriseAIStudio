import time

import pytest

from eap_core import plugins, schema
from eap_core.context import BudgetExceeded, CallContext, CycleDetected, DepthExceeded, InvalidContext
from eap_core.events import Event, parse_push, push_envelope
from eap_core.policy import PLATFORM_BASELINE, PolicyOverlay, check_definition, effective_guardrails, effective_limits, merge, validate_overlay
from eap_core.schema import Guardrail
from eap_governor import evals


def defn(**kw):
    base = {"name": "demo-agent", "owner_bu": "retail", "prompt": {"system": "hi"}, "model": {"catalog_id": "mock-small"}}
    base.update(kw)
    return schema.parse_definition(base)


# ------------------------------------------------------------------- policy


def test_overlay_cannot_loosen():
    errs = validate_overlay(PLATFORM_BASELINE, PolicyOverlay.model_validate({
        "limits": {"max_cost_usd": 50},
        "release": {"min_approvals": 0, "separation_of_duties": False, "require_eval": False},
        "required_guardrails": [{"type": "prompt_injection", "stage": "input", "action": "warn"}],
    }))
    assert any("max_cost_usd" in e for e in errs)
    assert any("min_approvals" in e for e in errs)
    assert any("separation_of_duties" in e for e in errs)
    assert any("require_eval" in e for e in errs)
    assert any("cannot weaken" in e for e in errs)


def test_merge_is_tighten_only_even_without_validation():
    o = PolicyOverlay.model_validate({"limits": {"max_cost_usd": 50, "max_depth": 2}, "denied_models": ["x"],
                                      "release": {"min_approvals": 2}, "soft_controls": ["Be brief."]})
    eff = merge(PLATFORM_BASELINE, o)
    assert eff.limits["max_cost_usd"] == PLATFORM_BASELINE.limits["max_cost_usd"]  # 50 ignored
    assert eff.limits["max_depth"] == 2
    assert eff.release.min_approvals == 2 and eff.release.separation_of_duties
    assert "x" in eff.denied_models
    assert set(PLATFORM_BASELINE.soft_controls) <= set(eff.soft_controls) and "Be brief." in eff.soft_controls
    keys = {g.key() for g in eff.required_guardrails}
    assert {g.key() for g in PLATFORM_BASELINE.required_guardrails} <= keys


def test_allowed_lists_intersect():
    base = PLATFORM_BASELINE.model_copy(update={"allowed_models": ["a", "b"]})
    assert validate_overlay(base, PolicyOverlay(allowed_models=["a", "c"]))
    assert merge(base, PolicyOverlay(allowed_models=["a", "c"])).allowed_models == ["a"]


def test_definition_cannot_drop_required_guardrails_or_exceed_limits():
    d = defn(guardrails=[{"type": "prompt_injection", "stage": "input", "action": "warn"}],
             limits={"max_cost_usd": 99, "max_depth": 1})
    eff = effective_guardrails(d, PLATFORM_BASELINE)
    pi = next(g for g in eff if g.key() == ("prompt_injection", "input"))
    assert pi.action == "block"  # stricter platform action wins
    lim = effective_limits(d, PLATFORM_BASELINE)
    assert lim["max_cost_usd"] == 5.0 and lim["max_depth"] == 1
    v = check_definition(d, PLATFORM_BASELINE)
    assert any(x.code == "limit_exceeds_policy" for x in v)


def test_check_definition_catalog_and_plugins():
    d = defn(pattern={"type": "map_reduce", "params": {"bogus": 1}},
             bindings={"tools": [{"ref": "nope"}]},
             guardrails=[{"type": "json_schema", "stage": "input"}])
    catalog = {"models": {"mock-small": {"status": "deprecated"}}, "tools": {}, "knowledge": {}}
    codes = {v.code for v in check_definition(d, PLATFORM_BASELINE, catalog)}
    assert {"model_inactive", "tool_unknown", "invalid_params", "pattern_requirements", "invalid_stage"} <= codes


def test_classification_violation():
    d = defn(data_classification="internal", bindings={"tools": [{"ref": "crm"}]})
    catalog = {"models": {"mock-small": {"status": "active"}},
               "tools": {"crm": {"status": "active", "data_classification": "confidential"}}, "knowledge": {}}
    assert any(v.code == "classification" for v in check_definition(d, PLATFORM_BASELINE, catalog))


# ------------------------------------------------------------------ context


def test_child_context_inherits_and_enforces():
    root = CallContext.root(on_behalf_of="user@x", budget_usd=1.0, max_depth=3).enter("a")
    c1 = root.child("b", 0.5)
    assert c1.on_behalf_of == "user@x" and c1.trace_id == root.trace_id and c1.caller == "agent:a"
    assert c1.depth == 1 and c1.chain == ("a", "b") and c1.parent_run_id == root.run_id
    with pytest.raises(BudgetExceeded):
        c1.child("c", 0.6)
    with pytest.raises(CycleDetected):
        c1.child("a", 0.1)
    c3 = c1.child("c", 0.4).child("d", 0.3)
    assert c3.depth == 3
    with pytest.raises(DepthExceeded):
        c3.child("e", 0.1)


def test_platform_depth_cap_cannot_be_raised():
    ctx = CallContext.root(on_behalf_of="u", max_depth=10)
    assert ctx.max_depth == 3
    forged = CallContext.from_claims({**ctx.to_claims(), "max_depth": 99})
    assert forged.max_depth == 3


def test_signed_context_roundtrip_and_tamper():
    key = "k" * 32
    ctx = CallContext.root(on_behalf_of="u", budget_usd=0.7).enter("a")
    tok = ctx.sign(key)
    assert CallContext.verify(tok, key) == ctx
    with pytest.raises(InvalidContext):
        CallContext.verify(tok, "x" * 32)
    with pytest.raises(InvalidContext):
        CallContext.verify(ctx.sign(key, ttl_s=-10), key)


def test_deadline_blocks_children():
    ctx = CallContext.root(on_behalf_of="u", deadline=time.time() - 1).enter("a")
    with pytest.raises(BudgetExceeded):
        ctx.child("b", 0.1)


# ------------------------------------------------------------------- schema


def test_breaking_changes_and_bumps():
    old = defn(contract={"input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
                         "output_schema": {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "number"}},
                                           "required": ["a", "b"]}})
    same_prompt = defn(contract=old.contract.model_dump(), prompt={"system": "changed"})
    assert schema.required_bump(old, same_prompt) == "patch"
    new_tool = defn(contract=old.contract.model_dump(), bindings={"tools": [{"ref": "echo"}]})
    assert schema.required_bump(old, new_tool) == "minor"
    removed = defn(contract={"input_schema": old.contract.input_schema,
                             "output_schema": {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}})
    br = schema.breaking_changes(old, removed)
    assert any("b: field removed" in x for x in br) and schema.required_bump(old, removed) == "major"
    new_required = defn(contract={"input_schema": {"type": "object", "properties": {"q": {"type": "string"}, "z": {"type": "string"}},
                                                   "required": ["z"]}, "output_schema": old.contract.output_schema})
    assert any("new required field" in x for x in schema.breaking_changes(old, new_required))
    assert schema.bump("1.2.3", "major") == "2.0.0" and schema.bump("1.2.3", "minor") == "1.3.0"
    assert schema.bump(None, "patch") == "1.0.0"


def test_content_hash_is_stable():
    a = defn(tags=["x"])
    b = schema.parse_definition(a.canonical())
    assert a.content_hash() == b.content_hash()
    assert defn(tags=["y"]).content_hash() != a.content_hash()


def test_plugin_param_validation():
    assert plugins.validate_params("guardrail", "max_length", {"max_chars": 10}) == []
    assert plugins.validate_params("guardrail", "max_length", {})
    assert plugins.validate_params("pattern", "nope", {})
    cat = plugins.catalog_json()
    assert {"pattern", "guardrail", "tool", "assertion"} <= set(cat)


def test_events_roundtrip():
    ev = Event(event_type="alias.moved", data={"agent": "a", "version": "1.0.0"})
    env = push_envelope(ev)
    assert env["message"]["attributes"]["event_type"] == "alias.moved"
    assert parse_push(env) == ev


# --------------------------------------------------------------------- evals


def test_dataset_formats():
    j = evals.parse_dataset('[{"id": "a", "input": "hello", "expected": {"x": 1}}]', "json")
    assert j[0]["input"] == {"text": "hello"}
    jl = evals.parse_dataset('{"input": {"q": 1}}\n\n{"q": 2, "expected": "y"}\n', "jsonl")
    assert jl[1]["input"] == {"q": 2} and jl[1]["expected"] == {"value": "y"}
    csv = evals.parse_dataset('id,input,expected.name,limit\nc1,"{""q"": 1}",Anna,3\nc2,plain text,Bob,4\n', "csv")
    assert csv[0]["input"] == {"q": 1, "limit": "3"}  # CSV scalars stay strings and csv[0]["expected"] == {"name": "Anna"}
    assert csv[1]["input"] == {"text": "plain text", "limit": "4"}
    with pytest.raises(evals.DatasetError):
        evals.parse_dataset('[{"input": {}, "assertions": [{"type": "bogus"}]}]', "json")
    with pytest.raises(evals.DatasetError):
        evals.parse_dataset("", "jsonl")


def test_assertions():
    result = {"status": "ok", "output": {"name": "Anna de Vries", "tags": ["a", "b"]}, "sources": [{"id": "s"}],
              "cost": {"usd": 0.01}, "latency_ms": 120}
    case = {"expected": {"name": "Anna de Vries"}}
    C = lambda a: evals.check(a, result, case)[0]  # noqa: E731
    assert C({"type": "status", "equals": "ok"})
    assert C({"type": "equals", "path": "output.name", "value_from": "expected.name"})
    assert C({"type": "contains", "path": "output.name", "value": "anna"})
    assert not C({"type": "contains", "path": "output.name", "value": "anna", "case_sensitive": True})
    assert C({"type": "not_contains", "path": "output", "value": "password"})
    assert C({"type": "regex", "path": "output.name", "pattern": r"^Anna"})
    assert C({"type": "min_items", "path": "output.tags", "min": 2})
    assert not C({"type": "min_items", "path": "output.tags", "min": 3})
    assert C({"type": "max_cost_usd", "max": 0.02}) and not C({"type": "max_cost_usd", "max": 0.001})
    assert C({"type": "max_latency_ms", "max": 500})
    assert C({"type": "citations", "min": 1}) and not C({"type": "citations", "min": 2})
    assert not C({"type": "equals", "path": "output.missing", "value": 1})


def test_guardrail_model_key():
    assert Guardrail(type="x", stage="input").key() == ("x", "input")
