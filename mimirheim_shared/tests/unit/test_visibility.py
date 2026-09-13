"""Tests for the Conditional Visibility condition types and evaluator."""

import pytest
from pydantic import TypeAdapter, ValidationError

from mimirheim_shared.visibility import (
    BooleanOperator,
    Comparison,
    ComparisonOperator,
    Condition,
    ConditionGroup,
    evaluate_condition,
)

CONDITION_ADAPTER: TypeAdapter = TypeAdapter(Condition)


def test_single_eq_comparison_true() -> None:
    condition = Comparison(field="autodiscovery_enabled", operator=ComparisonOperator.EQ, value=True)

    assert evaluate_condition(condition, {"autodiscovery_enabled": True}) is True


def test_single_eq_comparison_false() -> None:
    condition = Comparison(field="autodiscovery_enabled", operator=ComparisonOperator.EQ, value=True)

    assert evaluate_condition(condition, {"autodiscovery_enabled": False}) is False


def test_single_ne_comparison() -> None:
    condition = Comparison(field="mode", operator=ComparisonOperator.NE, value="off")

    assert evaluate_condition(condition, {"mode": "on"}) is True
    assert evaluate_condition(condition, {"mode": "off"}) is False


def test_comparison_against_missing_field_is_false_for_eq() -> None:
    condition = Comparison(field="missing_field", operator=ComparisonOperator.EQ, value=True)

    assert evaluate_condition(condition, {}) is False


def test_and_group_requires_all_true() -> None:
    group = ConditionGroup(
        operator=BooleanOperator.AND,
        conditions=[
            Comparison(field="a", operator=ComparisonOperator.EQ, value=True),
            Comparison(field="b", operator=ComparisonOperator.EQ, value=True),
        ],
    )

    assert evaluate_condition(group, {"a": True, "b": True}) is True
    assert evaluate_condition(group, {"a": True, "b": False}) is False


def test_or_group_requires_any_true() -> None:
    group = ConditionGroup(
        operator=BooleanOperator.OR,
        conditions=[
            Comparison(field="a", operator=ComparisonOperator.EQ, value=True),
            Comparison(field="b", operator=ComparisonOperator.EQ, value=True),
        ],
    )

    assert evaluate_condition(group, {"a": False, "b": True}) is True
    assert evaluate_condition(group, {"a": False, "b": False}) is False


def test_nested_group() -> None:
    nested = ConditionGroup(
        operator=BooleanOperator.OR,
        conditions=[
            Comparison(field="autodiscovery_enabled", operator=ComparisonOperator.EQ, value=True),
            ConditionGroup(
                operator=BooleanOperator.AND,
                conditions=[
                    Comparison(field="mode", operator=ComparisonOperator.EQ, value="expert"),
                    Comparison(field="advanced_shown", operator=ComparisonOperator.EQ, value=True),
                ],
            ),
        ],
    )

    assert evaluate_condition(nested, {"autodiscovery_enabled": False, "mode": "expert", "advanced_shown": True}) is True
    assert evaluate_condition(nested, {"autodiscovery_enabled": False, "mode": "expert", "advanced_shown": False}) is False
    assert evaluate_condition(nested, {"autodiscovery_enabled": True, "mode": "basic", "advanced_shown": False}) is True


def test_condition_is_json_serializable_round_trip() -> None:
    original = ConditionGroup(
        operator=BooleanOperator.AND,
        conditions=[
            Comparison(field="a", operator=ComparisonOperator.EQ, value=1),
        ],
    )

    payload = CONDITION_ADAPTER.dump_json(original)
    restored = CONDITION_ADAPTER.validate_json(payload)

    assert evaluate_condition(restored, {"a": 1}) is True


def test_comparison_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        Comparison(field="a", operator=ComparisonOperator.EQ, value=1, nonsense="z")


def test_condition_group_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        ConditionGroup(operator=BooleanOperator.AND, conditions=[], nonsense="z")
