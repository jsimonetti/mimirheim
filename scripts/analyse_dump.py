"""analyse_dump.py — Plot mimirheim solve dumps for troubleshooting.

Reads a pair of dump files produced by mimirheim's ``debug_dump`` or
``reporting`` mechanism (one ``*_input.json`` and one ``*_output.json``)
and writes a single interactive HTML page per pair.

The page contains:

1. **Summary dashboard** — two tables at the top: economic summary (cost,
   horizon, strategy, status) and exchange/self-sufficiency metrics.

2. **Unoptimised energy flows** — stacked bars showing what would happen with
   no storage dispatch (base load + PV only).

3. **Optimised energy flows** — stacked bars for the full mimirheim schedule, plus
   grid import (red) and grid export (green) bars and a net-exchange line.

4. **SOC vs prices** — one subplot row per dispatchable device, with SOC as a
   filled area on the left axis and import/export prices on the right axis.
   Closed-loop ZEX and LB periods are shaded and labelled.

5. **Step-by-step data table** — rows are time steps, columns include prices,
   forecasts, device setpoints, SOC, ZEX/LB flag columns (with colour
   highlights), and row-level colour-coding by economic state.

All chart timestamps are shifted from UTC to local time in the browser.
The output HTML references ``plotly.min.js`` via a relative path so that
a single copy is shared across all reports in the same directory.

Usage::

    # Analyse a specific dump pair:
    uv run python scripts/analyse_dump.py \\
        mimirheim_dumps/2026-03-30T14-00-00Z_input.json \\
        mimirheim_dumps/2026-03-30T14-00-00Z_output.json

    # Analyse the most recent dump pair in a directory:
    uv run python scripts/analyse_dump.py --dir mimirheim_dumps

    # Analyse all dump pairs in a directory (batch mode):
    uv run python scripts/analyse_dump.py --dir mimirheim_dumps --all

    # Analyse the 3 most recent dump pairs:
    uv run python scripts/analyse_dump.py --dir mimirheim_dumps --last 3

    # Write the HTML to a different directory:
    uv run python scripts/analyse_dump.py --dir mimirheim_dumps \\
        --output-dir mimirheim_dumps/my_analysis

This module has no imports from the mimirheim package; it reads raw JSON so it
can be run independently of a mimirheim installation.

Rendering is provided by the ``reporter.render`` module. If that module is
not installed, the script prints a helpful error and exits.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from reporter.render import build_report_html
except ImportError:
    sys.exit(
        "The mimirheim-reporter package is required to run this script.\n"
        "Install it with:\n"
        "  uv sync\n"
        "Then retry from the mimirheim workspace root:\n"
        "  uv run python scripts/analyse_dump.py --dir mimirheim_dumps"
    )

# Output directory. Must live inside mimirheim_dumps/ so it is covered by .gitignore.
DEFAULT_OUTPUT_DIR = Path("mimirheim_dumps/plots")


# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> dict:
    """Load a JSON file and return the parsed dict.

    Args:
        path: Path to the JSON file to load.

    Returns:
        The parsed JSON dict.
    """
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"Could not read {path}: {exc}")


def _find_latest_pair(dump_dir: Path) -> tuple[Path, Path]:
    """Return the input/output paths for the most recent dump pair in dump_dir.

    Args:
        dump_dir: Directory containing ``*_input.json`` dump files.

    Returns:
        A tuple ``(input_path, output_path)`` for the most recent pair.
    """
    inputs = sorted(dump_dir.glob("*_input.json"))
    if not inputs:
        sys.exit(f"No *_input.json files found in {dump_dir}")
    latest = inputs[-1]
    ts = latest.name[: -len("_input.json")]
    output = dump_dir / f"{ts}_output.json"
    if not output.exists():
        sys.exit(f"Matching output file not found: {output}")
    return latest, output


def _find_all_pairs(dump_dir: Path) -> list[tuple[Path, Path]]:
    """Return all input/output dump pairs in dump_dir, sorted oldest-first.

    Only pairs where both files exist are included. Unmatched input files are
    silently skipped.

    Args:
        dump_dir: Directory containing dump files.

    Returns:
        A list of ``(input_path, output_path)`` tuples, sorted oldest-first.
    """
    inputs = sorted(dump_dir.glob("*_input.json"))
    pairs: list[tuple[Path, Path]] = []
    for inp in inputs:
        ts = inp.name[: -len("_input.json")]
        out = dump_dir / f"{ts}_output.json"
        if out.exists():
            pairs.append((inp, out))
    if not pairs:
        sys.exit(f"No complete dump pairs found in {dump_dir}")
    return pairs


def _ts_from_path(input_path: Path) -> str:
    """Extract the timestamp prefix from a dump file name.

    Args:
        input_path: Path to the ``*_input.json`` file.

    Returns:
        The timestamp prefix, e.g. ``"2026-04-02T14-00-00Z"``.
    """
    name = input_path.name
    if name.endswith("_input.json"):
        return name[: -len("_input.json")]
    return input_path.stem


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        The parsed ``argparse.Namespace``.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Analyse mimirheim solve dumps. Writes interactive HTML reports into the "
            "output directory (default: mimirheim_dumps/plots/, which is gitignored). "
            "All output files share a single plotly.min.js in the output directory."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # Analyse a specific dump pair:
  uv run python scripts/analyse_dump.py \\
      mimirheim_dumps/2026-03-30T14-00-00Z_input.json \\
      mimirheim_dumps/2026-03-30T14-00-00Z_output.json

  # Analyse the most recent dump pair found in a directory:
  uv run python scripts/analyse_dump.py --dir mimirheim_dumps

  # Analyse all dump pairs in a directory:
  uv run python scripts/analyse_dump.py --dir mimirheim_dumps --all

  # Analyse the 3 most recent dump pairs:
  uv run python scripts/analyse_dump.py --dir mimirheim_dumps --last 3
""",
    )
    parser.add_argument(
        "input_json",
        nargs="?",
        type=Path,
        help="Path to the *_input.json dump file.",
    )
    parser.add_argument(
        "output_json",
        nargs="?",
        type=Path,
        help="Path to the matching *_output.json dump file.",
    )
    parser.add_argument(
        "--dir",
        type=Path,
        metavar="DUMP_DIR",
        help="Directory to search for dump pairs. Required for --all and --last.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        default=False,
        help="Process all dump pairs in --dir, sorted oldest-first.",
    )
    parser.add_argument(
        "--last",
        type=int,
        metavar="N",
        default=None,
        help="Process the N most recent dump pairs in --dir (integer >= 1).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        metavar="OUTPUT_DIR",
        help=(
            f"Directory to write the HTML file(s) into. "
            f"Must reside inside mimirheim_dumps/ (default: {DEFAULT_OUTPUT_DIR})."
        ),
    )
    return parser.parse_args()


def _validate_args(args: argparse.Namespace) -> None:
    """Validate the parsed argument combination and exit with a message on error.

    Args:
        args: The parsed argument namespace from ``_parse_args()``.
    """
    batch = args.all or (args.last is not None)
    positional = args.input_json is not None or args.output_json is not None

    if positional and batch:
        sys.exit(
            "Positional INPUT_JSON/OUTPUT_JSON arguments are mutually exclusive "
            "with --all and --last."
        )
    if batch and args.dir is None:
        sys.exit("--all and --last require --dir DUMP_DIR.")
    if args.last is not None and args.last < 1:
        sys.exit("--last N requires N >= 1.")
    if not positional and not batch and args.dir is None:
        sys.exit(
            "Supply either INPUT_JSON OUTPUT_JSON positional arguments, "
            "--dir DUMP_DIR, --dir DUMP_DIR --all, or --dir DUMP_DIR --last N. "
            "Use --help for details."
        )


def _resolve_output_dir(output_dir: Path) -> Path:
    """Resolve and validate the output directory.

    The output directory must reside inside ``mimirheim_dumps/`` to stay gitignored.

    Args:
        output_dir: The user-supplied (or default) output directory path.

    Returns:
        The resolved absolute path.
    """
    resolved = output_dir.resolve()
    gitignored_root = (Path(__file__).parent.parent / "mimirheim_dumps").resolve()
    if not str(resolved).startswith(str(gitignored_root)):
        sys.exit(
            f"Output directory must be inside mimirheim_dumps/ to stay gitignored.\n"
            f"  Requested: {resolved}\n"
            f"  Required root: {gitignored_root}"
        )
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _render_pair(
    input_path: Path,
    output_path: Path,
    output_dir: Path,
    index: int | None,
    total: int | None,
) -> None:
    """Render a single dump pair and write the HTML file.

    Args:
        input_path: Path to the ``*_input.json`` dump file.
        output_path: Path to the matching ``*_output.json`` dump file.
        output_dir: Directory to write the HTML output file into.
        index: 1-based index of this pair in the current batch (for progress).
            ``None`` when processing a single pair.
        total: Total number of pairs being processed (for progress).
            ``None`` when processing a single pair.
    """
    ts = _ts_from_path(input_path)
    safe_ts = ts.replace(":", "-")
    out_path = output_dir / f"{safe_ts}_analysis.html"

    if index is not None and total is not None:
        print(f"[{index}/{total}] {ts} \u2192 {out_path.name}")
    else:
        print(f"Rendering {ts}")

    inp = _load_json(input_path)
    out = _load_json(output_path)
    out_path.write_text(build_report_html(inp, out), encoding="utf-8")


def main() -> None:
    """Entry point: resolve dump files, render combined figure(s), write HTML."""
    args = _parse_args()
    _validate_args(args)

    output_dir = _resolve_output_dir(args.output_dir)

    if args.input_json and args.output_json:
        # Single-pair mode: explicit positional arguments.
        if not args.input_json.exists():
            sys.exit(f"Input file not found: {args.input_json}")
        if not args.output_json.exists():
            sys.exit(f"Output file not found: {args.output_json}")
        _render_pair(args.input_json, args.output_json, output_dir, None, None)

    elif args.all:
        # Batch mode: all pairs in the directory, oldest-first.
        pairs = _find_all_pairs(args.dir)
        print(f"Found {len(pairs)} dump pairs in {args.dir.resolve()}")
        for i, (inp_path, out_path) in enumerate(pairs, start=1):
            _render_pair(inp_path, out_path, output_dir, i, len(pairs))

    elif args.last is not None:
        # Batch mode: N most recent pairs.
        pairs = _find_all_pairs(args.dir)
        pairs = pairs[-args.last :]  # noqa: E203 — recent N entries
        print(
            f"Processing {len(pairs)} most recent pair(s) "
            f"(--last {args.last}) in {args.dir.resolve()}"
        )
        for i, (inp_path, out_path) in enumerate(pairs, start=1):
            _render_pair(inp_path, out_path, output_dir, i, len(pairs))

    else:
        # Default: most recent single pair from --dir.
        print(f"Searching for latest dump pair in: {args.dir.resolve()}")
        inp_path, out_path = _find_latest_pair(args.dir)
        _render_pair(inp_path, out_path, output_dir, None, None)

    print(f"Output written to: {output_dir}")


if __name__ == "__main__":
    main()

