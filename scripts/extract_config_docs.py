"""Extract configuration documentation from Pydantic schema files.

This script parses Python source files that define Pydantic models and
generates Markdown documentation pages from the class docstrings and
``Field(description=...)`` annotations.

Usage::

    python scripts/extract_config_docs.py

The script writes one Markdown file per known schema file to ``wiki/``.
To regenerate a single file pass its logical name::

    python scripts/extract_config_docs.py --only mimirheim
    python scripts/extract_config_docs.py --only nordpool

Supported targets and output paths are listed in ``TARGETS`` at the bottom
of this file.

What this script does not do:
- It does not execute any of the schema modules. All extraction is done via
  the Python ``ast`` module so there are no import side effects and no
  virtual environment is required.
- It does not validate the extracted information against runtime Pydantic
  behaviour. Field defaults shown here are extracted as source literals;
  they may differ from the Pydantic-computed default in edge cases.
"""

from __future__ import annotations

import ast
import argparse
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class FieldDoc:
    """Documentation for a single Pydantic model field."""

    name: str
    type_str: str
    default: str | None  # None means "required" (no default)
    description: str


@dataclass
class ClassDoc:
    """Documentation for a single Pydantic model class."""

    name: str
    docstring: str
    fields: list[FieldDoc] = field(default_factory=list)


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _unparse(node: ast.expr) -> str:
    """Return source text for an AST expression node."""
    return ast.unparse(node)


def _extract_string(node: ast.expr | None) -> str | None:
    """Return the string value of a Constant node, or None."""
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _extract_description(call: ast.Call) -> str | None:
    """Return the ``description`` keyword argument from a ``Field(...)`` call."""
    for kw in call.keywords:
        if kw.arg == "description":
            return _extract_string(kw.value)
    return None


def _extract_default(call: ast.Call) -> str | None:
    """Return the default indicator from a ``Field(...)`` call.

    Checks the first positional arg, the ``default`` keyword, and the
    ``default_factory`` keyword. Returns a human-readable string or None
    to indicate "required" (no default of any kind).
    """
    # First positional arg is the default
    if call.args:
        return _unparse(call.args[0])
    for kw in call.keywords:
        if kw.arg == "default":
            return _unparse(kw.value)
        if kw.arg == "default_factory":
            # Show as "factory: <callable>" to distinguish from a plain default
            return f"factory: {_unparse(kw.value)}"
    return None


def _type_annotation(annotation: ast.expr | None) -> str:
    """Return a human-readable type annotation string."""
    if annotation is None:
        return "—"
    return _unparse(annotation)


# ---------------------------------------------------------------------------
# One-pass class extractor
# ---------------------------------------------------------------------------


def extract_classes(source: str) -> list[ClassDoc]:
    """Parse Python source and return one ClassDoc per Pydantic model class.

    Classes are included if they have ``BaseModel`` as a base (direct only;
    nested class inspection is not performed) and are not imported from another
    module (i.e. they are defined in the file).

    Args:
        source: Full Python source code as a string.

    Returns:
        A list of ClassDoc instances in source order.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError(f"Failed to parse Python source: {exc}") from exc

    classes: list[ClassDoc] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue

        # Only include classes with BaseModel in bases (by name, not resolved)
        base_names = {
            base.id if isinstance(base, ast.Name) else
            (base.attr if isinstance(base, ast.Attribute) else "")
            for base in node.bases
        }
        if "BaseModel" not in base_names:
            continue

        # Docstring
        docstring = ast.get_docstring(node) or ""

        cls_doc = ClassDoc(name=node.name, docstring=docstring)

        # Fields: look for annotated assignments in the class body
        for stmt in node.body:
            if not isinstance(stmt, ast.AnnAssign):
                continue
            if not isinstance(stmt.target, ast.Name):
                continue

            fname = stmt.target.id
            if fname.startswith("_"):
                # Skip private fields and model_config
                continue
            if fname == "model_config":
                continue

            ftype = _type_annotation(stmt.annotation)
            fdesc = ""
            fdefault = None

            if stmt.value is not None:
                val = stmt.value
                if isinstance(val, ast.Call) and isinstance(val.func, ast.Name) and val.func.id == "Field":
                    fdesc = _extract_description(val) or ""
                    fdefault = _extract_default(val)
                elif isinstance(val, ast.Call) and isinstance(val.func, ast.Attribute) and val.func.attr == "Field":
                    fdesc = _extract_description(val) or ""
                    fdefault = _extract_default(val)
                else:
                    # Plain assignment default
                    fdefault = _unparse(val)

            cls_doc.fields.append(FieldDoc(
                name=fname,
                type_str=ftype,
                default=fdefault,
                description=fdesc,
            ))

        classes.append(cls_doc)

    return classes


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _render_docstring_body(docstring: str) -> str:
    """Convert a Google-style docstring body to Markdown.

    Strips the ``Args:`` / ``Attributes:`` sections (those are rendered as
    a field table) and returns the remaining prose paragraphs.
    """
    if not docstring:
        return ""

    lines = textwrap.dedent(docstring).splitlines()
    output_lines: list[str] = []
    in_args_section = False

    for line in lines:
        stripped = line.strip()
        # Detect the start of Args, Attributes, Returns, Raises sections
        if stripped in ("Args:", "Attributes:", "Returns:", "Raises:", "Note:", "Notes:"):
            in_args_section = True
            continue
        # Empty line resets args section (dedent back to level 0)
        if not stripped:
            in_args_section = False
            output_lines.append("")
            continue
        if in_args_section:
            continue
        output_lines.append(line)

    # Strip trailing blank lines
    while output_lines and not output_lines[-1].strip():
        output_lines.pop()

    return "\n".join(output_lines)


def _render_class(cls_doc: ClassDoc, heading_level: int = 3) -> str:
    """Render a ClassDoc as a Markdown section."""
    hashes = "#" * heading_level
    lines: list[str] = []

    lines.append(f"{hashes} `{cls_doc.name}`")
    lines.append("")

    prose = _render_docstring_body(cls_doc.docstring)
    if prose:
        lines.append(prose)
        lines.append("")

    if cls_doc.fields:
        lines.append("| Field | Type | Default | Description |")
        lines.append("|-------|------|---------|-------------|")
        for f in cls_doc.fields:
            default_cell = f"`{f.default}`" if f.default is not None else "*(required)*"
            # Escape pipe chars in description
            desc = f.description.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| `{f.name}` | `{f.type_str}` | {default_cell} | {desc} |")
        lines.append("")

    return "\n".join(lines)


def render_page(title: str, intro: str, classes: list[ClassDoc]) -> str:
    """Render a complete Markdown page for a schema module.

    Args:
        title: Page H1 heading.
        intro: Introductory paragraph inserted below the heading.
        classes: Ordered list of ClassDoc instances to render.

    Returns:
        Complete Markdown page as a string.
    """
    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")
    if intro:
        lines.append(intro)
        lines.append("")

    for cls_doc in classes:
        lines.append(_render_class(cls_doc, heading_level=2))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Target definitions
# ---------------------------------------------------------------------------


@dataclass
class Target:
    """One schema file to extract documentation from."""

    key: str
    source_path: Path
    output_path: Path
    title: str
    intro: str


def _build_targets(root: Path) -> list[Target]:
    """Return the list of all known extraction targets.

    All generated pages are written to the ``wiki/Reference/`` subdirectory.
    """
    wiki = root / "wiki"
    ref = wiki / "Reference"
    helpers = root / "mimirheim_helpers"

    return [
        Target(
            key="mimirheim",
            source_path=root / "mimirheim" / "config" / "schema.py",
            output_path=ref / "Config-Mimirheim.md",
            title="Mimirheim — Full Configuration Reference",
            intro=(
                "This page is auto-generated from `mimirheim/config/schema.py` by "
                "`scripts/extract_config_docs.py`. **Do not edit it manually** — "
                "edit the source schema and run the script to regenerate.\n\n"
                "Each section corresponds to a Pydantic model. Fields marked "
                "*(required)* have no default and must be set explicitly. "
                "Start with [Configuration](../Configuration) for the narrative guide."
            ),
        ),
        Target(
            key="common",
            source_path=helpers / "common" / "helper_common" / "config.py",
            output_path=ref / "Config-Common.md",
            title="Shared Helper Configuration — Reference",
            intro=(
                "Auto-generated from `helper_common/config.py`. "
                "These models are shared by all mimirheim helper tools.\n\n"
                "See [Helpers/Common](../Helpers/Common) for the narrative guide."
            ),
        ),
        Target(
            key="baseload-static",
            source_path=helpers / "baseload" / "static" / "baseload_static" / "config.py",
            output_path=ref / "Config-Baseload-Static.md",
            title="baseload_static — Configuration Reference",
            intro=(
                "Auto-generated from `baseload_static/config.py`. "
                "See [Helpers/Baseload-Static](../Helpers/Baseload-Static) for the guide."
            ),
        ),
        Target(
            key="baseload-ha",
            source_path=helpers / "baseload" / "homeassistant" / "baseload_ha" / "config.py",
            output_path=ref / "Config-Baseload-HA.md",
            title="baseload_ha — Configuration Reference",
            intro=(
                "Auto-generated from `baseload_ha/config.py`. "
                "See [Helpers/Baseload-HA](../Helpers/Baseload-HA) for the guide."
            ),
        ),
        Target(
            key="baseload-ha-db",
            source_path=helpers / "baseload" / "homeassistant_db" / "baseload_ha_db" / "config.py",
            output_path=ref / "Config-Baseload-HA-DB.md",
            title="baseload_ha_db — Configuration Reference",
            intro=(
                "Auto-generated from `baseload_ha_db/config.py`. "
                "See [Helpers/Baseload-HA-DB](../Helpers/Baseload-HA-DB) for the guide."
            ),
        ),
        Target(
            key="nordpool",
            source_path=helpers / "prices" / "nordpool" / "nordpool" / "config.py",
            output_path=ref / "Config-Nordpool.md",
            title="nordpool — Configuration Reference",
            intro=(
                "Auto-generated from `nordpool/config.py`. "
                "See [Helpers/Nordpool](../Helpers/Nordpool) for the guide."
            ),
        ),
        Target(
            key="pv-fetcher",
            source_path=helpers / "pv" / "forecast.solar" / "pv_fetcher" / "config.py",
            output_path=ref / "Config-PV-Fetcher.md",
            title="pv_fetcher (forecast.solar) — Configuration Reference",
            intro=(
                "Auto-generated from `pv_fetcher/config.py`. "
                "See [Helpers/PV-Fetcher](../Helpers/PV-Fetcher) for the guide."
            ),
        ),
        Target(
            key="pv-ml-learner",
            source_path=helpers / "pv" / "pv_ml_learner" / "pv_ml_learner" / "config.py",
            output_path=ref / "Config-PV-ML-Learner.md",
            title="pv_ml_learner — Configuration Reference",
            intro=(
                "Auto-generated from `pv_ml_learner/config.py`. "
                "See [Helpers/PV-ML-Learner](../Helpers/PV-ML-Learner) for the guide."
            ),
        ),
        Target(
            key="reporter",
            source_path=helpers / "reporter" / "reporter" / "config.py",
            output_path=ref / "Config-Reporter.md",
            title="reporter — Configuration Reference",
            intro=(
                "Auto-generated from `reporter/config.py`. "
                "See [Helpers/Reporter](../Helpers/Reporter) for the guide."
            ),
        ),
        Target(
            key="scheduler",
            source_path=helpers / "scheduler" / "scheduler" / "config.py",
            output_path=ref / "Config-Scheduler.md",
            title="scheduler — Configuration Reference",
            intro=(
                "Auto-generated from `scheduler/config.py`. "
                "See [Helpers/Scheduler](../Helpers/Scheduler) for the guide."
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Run the documentation extraction.

    Args:
        argv: Command-line argument list. Defaults to ``sys.argv[1:]``.

    Returns:
        Exit code. 0 for success, 1 for any error.
    """
    parser = argparse.ArgumentParser(
        description="Extract Pydantic config documentation into Markdown wiki pages.",
    )
    parser.add_argument(
        "--only",
        metavar="KEY",
        help="Extract only the named target (e.g. mimirheim, nordpool, reporter).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all known targets and exit.",
    )
    args = parser.parse_args(argv)

    root = Path(__file__).parent.parent
    targets = _build_targets(root)

    if args.list:
        for t in targets:
            print(f"  {t.key:<20} {t.source_path.relative_to(root)} -> {t.output_path.relative_to(root)}")
        return 0

    if args.only:
        targets = [t for t in targets if t.key == args.only]
        if not targets:
            print(f"ERROR: No target named {args.only!r}. Use --list to see available targets.", file=sys.stderr)
            return 1

    errors = 0
    for target in targets:
        if not target.source_path.exists():
            print(f"SKIP {target.key}: source not found at {target.source_path}", file=sys.stderr)
            errors += 1
            continue

        source = target.source_path.read_text(encoding="utf-8")
        try:
            classes = extract_classes(source)
        except ValueError as exc:
            print(f"ERROR {target.key}: {exc}", file=sys.stderr)
            errors += 1
            continue

        page = render_page(target.title, target.intro, classes)

        target.output_path.parent.mkdir(parents=True, exist_ok=True)
        target.output_path.write_text(page, encoding="utf-8")
        print(f"  wrote {target.output_path.relative_to(root)}")

    if errors:
        print(f"\n{errors} error(s) occurred.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
