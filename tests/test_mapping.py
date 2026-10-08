import math

import pytest

from home_infra_agent.mapping import (
    MappingError,
    evaluate_health,
    extract_values,
    parse_path,
    validate_extractions,
    validate_health,
)
from home_infra_agent.config import validate_discovery_identifiers, validate_entity_metadata


def test_paths_support_root_nested_properties_and_array_indices():
    payload = {"devices": [{"temperature": "21.25"}, {"temperature": "19.5"}]}
    assert parse_path("$.devices[0].temperature") == ["devices", 0, "temperature"]
    assert extract_values(payload, {"temperature": {"path": "$.devices[1].temperature", "type": "number"}}) == {
        "temperature": 19.5
    }
    assert extract_values(["ok"], {"first": {"path": "$[0]", "type": "string"}}) == {"first": "ok"}


def test_optional_missing_and_null_defaults_are_distinct():
    result = extract_values({"data": {"known": None}}, {
        "not_returned": {"path": "$.data.missing", "type": "number"},
        "default_null": {"path": "$.data.missing", "type": "number", "default": None},
        "explicit_null": {"path": "$.data.known", "type": "number"},
    })
    assert result == {"default_null": None, "explicit_null": None}


@pytest.mark.parametrize("payload", [{}, {"data": {"value": None}}])
def test_required_missing_and_null_fields_fail_without_echoing_source(payload):
    spec = {"required": {"path": "$.data.value", "type": "number", "required": True}}
    with pytest.raises(MappingError, match="required path") as error:
        extract_values(payload, spec)
    assert "password" not in str(error.value)


def test_conversion_mapping_scaling_and_rounding():
    payload = {"sensor": {"temperature_centi": "2134", "state": "ready", "ready": "4", "online": "true"}}
    result = extract_values(payload, {
        "temperature": {"path": "$.sensor.temperature_centi", "type": "number", "multiply": 0.01, "round": 1},
        "state": {"path": "$.sensor.state", "type": "string", "map": {"ready": "UP", "offline": "DOWN"}},
        "ready": {"path": "$.sensor.ready", "type": "integer"},
        "online": {"path": "$.sensor.online", "type": "boolean"},
    })
    assert result == {"temperature": 21.3, "state": "UP", "ready": 4, "online": True}


def test_divide_and_timestamp_conversion():
    result = extract_values({"raw": 12345, "at": "2026-10-08T12:30:00Z"}, {
        "reading": {"path": "$.raw", "type": "number", "divide": 100},
        "updated": {"path": "$.at", "type": "timestamp"},
    })
    assert result == {"reading": 123.45, "updated": "2026-10-08T12:30:00+00:00"}


@pytest.mark.parametrize("payload,spec", [
    ({"n": "12x"}, {"x": {"path": "$.n", "type": "number"}}),
    ({"n": "NaN"}, {"x": {"path": "$.n", "type": "number"}}),
    ({"n": 4.5}, {"x": {"path": "$.n", "type": "integer"}}),
    ({"b": "yes"}, {"x": {"path": "$.b", "type": "boolean"}}),
    ({"t": "not-a-time"}, {"x": {"path": "$.t", "type": "timestamp"}}),
])
def test_invalid_type_conversions_fail(payload, spec):
    with pytest.raises(MappingError):
        extract_values(payload, spec)


@pytest.mark.parametrize("path", ["", "status", "$.items[]", "$.items[-1]", "$..status", "$.items[1x]"])
def test_invalid_paths_are_rejected(path):
    with pytest.raises(MappingError):
        parse_path(path)


@pytest.mark.parametrize("spec", [
    {"x": {"path": "$.x", "type": "object"}},
    {"x": {"path": "$.x", "type": "number", "divide": 0}},
    {"x": {"path": "$.x", "type": "number", "multiply": math.inf}},
    {"x": {"path": "$.x", "type": "number", "round": 20}},
    {"x": {"path": "$.x", "type": "number", "expr": "1+1"}},
    {"x": {"path": "$.x", "type": "number", "default": "many"}},
])
def test_invalid_extraction_configuration_fails_validation(spec):
    with pytest.raises(MappingError):
        validate_extractions(spec, "sample")


def test_health_rules_pass_fail_missing_fields_and_preserve_provider_failure():
    health = {"rules": [
        {"field": "ready", "equalsField": "total"},
        {"field": "online", "equals": True},
        {"field": "temperature", "lessThanOrEqual": 40},
    ], "onFailure": "WARN"}
    validate_health(health, "sample")
    assert evaluate_health("OK", {"ready": 3, "total": 3, "online": True, "temperature": 22}, health) == "OK"
    assert evaluate_health("OK", {"ready": 2, "total": 3, "online": True, "temperature": 22}, health) == "WARN"
    assert evaluate_health("OK", {"ready": 3, "total": 3, "online": True}, health) == "WARN"
    assert evaluate_health("WARN", {"ready": 3, "total": 3, "online": True, "temperature": 22}, health) == "WARN"
    assert evaluate_health("ERROR", {"ready": 3, "total": 3}, health) == "ERROR"


def test_exists_and_error_health_outcome():
    health = {"rules": [{"field": "optional", "exists": False}], "onFailure": "ERROR"}
    assert evaluate_health("OK", {}, health) == "OK"
    assert evaluate_health("OK", {"optional": 1}, health) == "ERROR"
    validate_health({"rules": [{"field": "x", "greaterThan": 0}], "onFailure": "ERROR"})
    with pytest.raises(MappingError):
        validate_health({"rules": [{"field": "x", "equals": 1, "exists": True}]})


def test_entity_metadata_validation_and_duplicate_identifiers():
    validate_entity_metadata({"entities": {"online": {"component": "binary_sensor", "payload_on": "UP",
                                                        "payload_off": "DOWN", "expire_after": 30}}}, "sample")
    with pytest.raises(MappingError, match="duplicates entity identifier"):
        validate_entity_metadata({"entities": {"node.a": {}, "node-a": {}}}, "sample")
    with pytest.raises(MappingError, match="built-in entity"):
        validate_entity_metadata({"entities": {"status": {}}}, "sample")
    with pytest.raises(MappingError, match="unsupported option"):
        validate_entity_metadata({"entities": {"x": {"state_topic": "other/topic"}}}, "sample")


def test_known_extraction_identifiers_cannot_collide():
    with pytest.raises(MappingError, match="identifiers collide"):
        validate_discovery_identifiers({
            "a_b": {"extract": {"c": {}}},
            "a": {"extract": {"b_c": {}}},
        }, {}, "sample")
    with pytest.raises(MappingError, match="built-in MQTT entity"):
        validate_discovery_identifiers({"http": {"extract": {"status": {}}}}, {}, "sample")
