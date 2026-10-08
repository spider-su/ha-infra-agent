"""Small declarative value extraction, conversion, and health rules."""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timezone
from typing import Any, Mapping


class MappingError(ValueError):
    """Safe configuration or mapping error; messages never include source values."""


_MISSING = object()
_PRIMITIVE = (str, int, float, bool, type(None))
_TYPES = {"string", "integer", "number", "boolean", "timestamp"}
_TRANSFORMS = {"multiply", "divide", "round", "map"}
_EXTRACT_OPTIONS = {"path", "type", "required", "default", *_TRANSFORMS}
_HEALTH_OPERATORS = {"equals", "notEquals", "greaterThan", "lessThan",
                     "greaterThanOrEqual", "lessThanOrEqual", "equalsField", "exists"}


def parse_path(path: str) -> list[str | int]:
    """Parse the supported `$`, `.property`, and `[integer]` path syntax."""
    if not isinstance(path, str) or not path.startswith("$"):
        raise MappingError("path must start with '$'")
    tokens: list[str | int] = []
    index = 1
    while index < len(path):
        if path[index] == ".":
            match = re.match(r"[A-Za-z_][A-Za-z0-9_-]*", path[index + 1:])
            if not match:
                raise MappingError("path contains an invalid object property")
            tokens.append(match.group(0))
            index += 1 + len(match.group(0))
        elif path[index] == "[":
            match = re.match(r"\[(0|[1-9][0-9]*)\]", path[index:])
            if not match:
                raise MappingError("path array selectors must be non-negative integer indices")
            tokens.append(int(match.group(1)))
            index += len(match.group(0))
        else:
            raise MappingError("path may contain only object properties and array indices")
    return tokens


def _lookup(value: Any, tokens: list[str | int]) -> Any:
    for token in tokens:
        try:
            if isinstance(token, int):
                if not isinstance(value, list):
                    return _MISSING
                value = value[token]
            else:
                if not isinstance(value, dict):
                    return _MISSING
                value = value[token]
        except (KeyError, IndexError):
            return _MISSING
    return value


def _is_primitive(value: Any) -> bool:
    return ((isinstance(value, _PRIMITIVE) and not isinstance(value, float))
            or (isinstance(value, float) and math.isfinite(value))
            or isinstance(value, (date, datetime)))


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise MappingError(f"field {field}: value cannot be converted to a number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise MappingError(f"field {field}: value cannot be converted to a number") from None
    if not math.isfinite(number):
        raise MappingError(f"field {field}: number must be finite")
    return number


def _convert(value: Any, kind: str, field: str) -> Any:
    if value is None:
        return None
    if kind == "string":
        if isinstance(value, (dict, list)):
            raise MappingError(f"field {field}: value cannot be converted to a string")
        return str(value)
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            return value.lower() == "true"
        raise MappingError(f"field {field}: value must be a boolean")
    if kind == "integer":
        if isinstance(value, bool):
            raise MappingError(f"field {field}: value cannot be converted to an integer")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and math.isfinite(value) and value.is_integer():
            return int(value)
        if isinstance(value, str) and re.fullmatch(r"[+-]?[0-9]+", value.strip()):
            return int(value.strip())
        raise MappingError(f"field {field}: value cannot be converted to an integer")
    if kind == "number":
        return _number(value, field)
    if kind == "timestamp":
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, date):
            parsed = datetime.combine(value, datetime.min.time())
        elif isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError:
                raise MappingError(f"field {field}: value must be an ISO timestamp") from None
        else:
            raise MappingError(f"field {field}: value must be an ISO timestamp")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.isoformat(timespec="seconds")
    raise MappingError(f"field {field}: unsupported extraction type")


def validate_extractions(extract: Any, task_id: str = "task") -> None:
    if not isinstance(extract, Mapping) or not extract:
        raise MappingError(f"task {task_id}: extract must be a non-empty mapping")
    for field, spec in extract.items():
        prefix = f"task {task_id} extract.{field}"
        if not isinstance(field, str) or not field.strip() or not isinstance(spec, Mapping):
            raise MappingError(f"{prefix}: expected a named mapping")
        unknown = set(spec) - _EXTRACT_OPTIONS
        if unknown:
            raise MappingError(f"{prefix}: unsupported option {sorted(unknown)[0]}")
        if not isinstance(spec.get("path"), str):
            raise MappingError(f"{prefix}.path is required")
        try:
            parse_path(spec["path"])
        except MappingError as exc:
            raise MappingError(f"{prefix}.path: {exc}") from None
        if spec.get("type") not in _TYPES:
            raise MappingError(f"{prefix}.type must be one of {', '.join(sorted(_TYPES))}")
        if "required" in spec and not isinstance(spec["required"], bool):
            raise MappingError(f"{prefix}.required must be a boolean")
        if spec.get("required") and "default" in spec:
            raise MappingError(f"{prefix} cannot combine required and default")
        if "default" in spec and not _is_primitive(spec["default"]):
            raise MappingError(f"{prefix}.default must be a scalar value or null")
        if "default" in spec:
            try:
                _convert(spec["default"], spec["type"], str(field))
            except MappingError as exc:
                raise MappingError(f"{prefix}.default: {exc}") from None
        for operation in ("multiply", "divide"):
            if operation in spec:
                factor = spec[operation]
                if isinstance(factor, bool) or not isinstance(factor, (int, float)) or not math.isfinite(factor):
                    raise MappingError(f"{prefix}.{operation} must be a finite number")
                if operation == "divide" and factor == 0:
                    raise MappingError(f"{prefix}.divide cannot be zero")
        if "round" in spec and (isinstance(spec["round"], bool) or not isinstance(spec["round"], int)
                                or not 0 <= spec["round"] <= 12):
            raise MappingError(f"{prefix}.round must be an integer from 0 to 12")
        if "map" in spec:
            mapping = spec["map"]
            if not isinstance(mapping, Mapping) or any(not _is_primitive(v) for v in mapping.values()):
                raise MappingError(f"{prefix}.map must map scalar values to scalar values")
            for mapped_value in mapping.values():
                try:
                    _convert(mapped_value, spec["type"], str(field))
                except MappingError as exc:
                    raise MappingError(f"{prefix}.map value: {exc}") from None
        if any(k in spec for k in ("multiply", "divide", "round")) and spec["type"] not in {"integer", "number"}:
            raise MappingError(f"{prefix}: arithmetic transformations require integer or number type")


def extract_values(payload: Any, extract: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for field, spec in extract.items():
        value = _lookup(payload, parse_path(spec["path"]))
        if value is _MISSING:
            if "default" in spec:
                value = spec["default"]
            elif spec.get("required", False):
                raise MappingError(f"extract.{field}: required path is missing")
            else:
                continue
        if value is None:
            if spec.get("required", False):
                raise MappingError(f"extract.{field}: required path is null")
            values[field] = None
            continue
        value = _convert(value, spec["type"], field)
        value_map = spec.get("map")
        if value_map is not None:
            value = value_map.get(value, value)
            value = _convert(value, spec["type"], field)
        if "multiply" in spec:
            value *= spec["multiply"]
        if "divide" in spec:
            value /= spec["divide"]
        if "round" in spec:
            value = round(value, spec["round"])
        if spec["type"] == "integer" and isinstance(value, float):
            if not math.isfinite(value) or not value.is_integer():
                raise MappingError(f"extract.{field}: transformed value is not an integer")
            value = int(value)
        if spec["type"] == "number":
            value = _number(value, field)
        values[field] = value
    return values


def validate_health(health: Any, task_id: str = "task") -> None:
    if not isinstance(health, Mapping):
        raise MappingError(f"task {task_id}: health must be a mapping")
    unknown = set(health) - {"rules", "onFailure"}
    if unknown:
        raise MappingError(f"task {task_id} health: unsupported option {sorted(unknown)[0]}")
    on_failure = health.get("onFailure", "ERROR")
    if on_failure not in {"WARN", "ERROR"}:
        raise MappingError(f"task {task_id} health.onFailure must be WARN or ERROR")
    rules = health.get("rules", [])
    if not isinstance(rules, list):
        raise MappingError(f"task {task_id} health.rules must be a list")
    for index, rule in enumerate(rules):
        prefix = f"task {task_id} health.rules[{index}]"
        if not isinstance(rule, Mapping):
            raise MappingError(f"{prefix} must be a mapping")
        unknown = set(rule) - {"field", *_HEALTH_OPERATORS}
        if unknown:
            raise MappingError(f"{prefix}: unsupported option {sorted(unknown)[0]}")
        if not isinstance(rule.get("field"), str) or not rule["field"]:
            raise MappingError(f"{prefix}.field is required")
        operators = set(rule) & _HEALTH_OPERATORS
        if len(operators) != 1:
            raise MappingError(f"{prefix} must define exactly one comparison")
        operator = next(iter(operators))
        if operator in {"equals", "notEquals", "greaterThan", "lessThan", "greaterThanOrEqual", "lessThanOrEqual"}:
            if not _is_primitive(rule[operator]):
                raise MappingError(f"{prefix}.{operator} must be a scalar value")
        elif operator == "equalsField" and (not isinstance(rule[operator], str) or not rule[operator]):
            raise MappingError(f"{prefix}.equalsField must name another field")
        elif operator == "exists" and not isinstance(rule[operator], bool):
            raise MappingError(f"{prefix}.exists must be a boolean")


def evaluate_health(status: str, values: Mapping[str, Any], health: Mapping[str, Any] | None) -> str:
    if not health or not health.get("rules") or status in {"ERROR", "UNKNOWN"}:
        return status
    passed = True
    for rule in health["rules"]:
        field = rule["field"]
        exists = field in values and values[field] is not None
        op = next(key for key in rule if key in _HEALTH_OPERATORS)
        expected = rule[op]
        if op == "exists":
            ok = exists is expected
        elif not exists:
            ok = False
        elif op == "equals":
            ok = type(values[field]) is type(expected) and values[field] == expected
        elif op == "notEquals":
            ok = type(values[field]) is not type(expected) or values[field] != expected
        elif op == "equalsField":
            other_exists = expected in values and values[expected] is not None
            ok = other_exists and type(values[field]) is type(values[expected]) and values[field] == values[expected]
        else:
            other = values[field]
            right = expected
            if op == "greaterThan":
                comparison = lambda: other > right
            elif op == "lessThan":
                comparison = lambda: other < right
            elif op == "greaterThanOrEqual":
                comparison = lambda: other >= right
            else:
                comparison = lambda: other <= right
            try:
                ok = type(other) is type(right) and bool(comparison())
            except TypeError:
                ok = False
        passed = passed and ok
    if passed:
        return "WARN" if status == "WARN" else "OK"
    return "ERROR" if status == "ERROR" else health.get("onFailure", "ERROR")
