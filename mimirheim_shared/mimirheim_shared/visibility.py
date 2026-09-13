"""Conditional Visibility: a declarative rule for whether a field is shown.

A Conditional Visibility rule compares one field's Candidate Value against a
fixed value (a ``Comparison``), optionally combined with other rules via
AND/OR (a ``ConditionGroup``). It is always data, never executable code,
since it must cross the MQTT boundary between a Config Owner and the Config
Editor safely: a Config Owner cannot be trusted to hand the editor a
callable to run.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class ComparisonOperator(str, Enum):
    """Supported comparisons for a Conditional Visibility ``Comparison``."""

    EQ = "eq"
    NE = "ne"


class BooleanOperator(str, Enum):
    """How a ``ConditionGroup``'s sub-conditions combine."""

    AND = "and"
    OR = "or"


class Comparison(BaseModel):
    """A single leaf condition: does ``field``'s Candidate Value satisfy ``operator`` against ``value``?"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["comparison"] = "comparison"
    field: str
    operator: ComparisonOperator
    value: Any


class ConditionGroup(BaseModel):
    """An AND/OR combination of nested Conditions."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["group"] = "group"
    operator: BooleanOperator
    conditions: list[Condition]


Condition = Annotated[Union[Comparison, ConditionGroup], Field(discriminator="kind")]

ConditionGroup.model_rebuild()


def evaluate_condition(condition: Comparison | ConditionGroup, values: dict[str, Any]) -> bool:
    """Evaluate a Conditional Visibility rule against a set of Candidate Values.

    Args:
        condition: The rule to evaluate, a single ``Comparison`` or a
            ``ConditionGroup`` combining nested conditions with AND/OR.
        values: The current Candidate Values, keyed by field name.

    Returns:
        Whether the field this condition guards should be visible.
    """
    if isinstance(condition, ConditionGroup):
        results = (evaluate_condition(nested, values) for nested in condition.conditions)
        if condition.operator is BooleanOperator.AND:
            return all(results)
        return any(results)

    return _evaluate_comparison(condition, values)


def _evaluate_comparison(comparison: Comparison, values: dict[str, Any]) -> bool:
    actual = values.get(comparison.field)
    if comparison.operator is ComparisonOperator.EQ:
        return actual == comparison.value
    if comparison.operator is ComparisonOperator.NE:
        return actual != comparison.value
    raise ValueError(f"Unsupported comparison operator: {comparison.operator!r}")
