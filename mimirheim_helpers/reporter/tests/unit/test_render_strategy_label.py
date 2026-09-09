"""The strategy label must say when the strategy carried out was not the one requested."""

from reporter.render import _strategy_label


def test_plain_strategy_is_rendered_as_is() -> None:
    assert _strategy_label({"strategy": "minimize_cost"}) == "minimize_cost"
    assert _strategy_label({"strategy": "minimize_cost", "strategy_degraded": False}) == "minimize_cost"


def test_degraded_strategy_is_marked() -> None:
    out = {"strategy": "minimize_consumption", "strategy_degraded": True}
    assert _strategy_label(out) == "minimize_consumption (degraded)"


def test_missing_strategy_falls_back_without_crashing() -> None:
    """Dumps written before the flag existed have neither key on a bad day."""
    assert _strategy_label({}) == "?"
