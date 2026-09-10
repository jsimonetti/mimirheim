# 5. Debug solve dump

## Decision: structured JSON dump when debug.enabled is True

When `config.debug.enabled` is True, mimirheim writes two files to a configurable dump directory after every solve:

```
<dump_dir>/
  <iso_timestamp>_input.json    # SolveBundle — identical to golden file input.json (null fields omitted)
  <iso_timestamp>_output.json   # SolveResult — human-readable post-processed form (see below)
```

Timestamps use UTC ISO 8601 with seconds, e.g. `2026-03-30T14-15-00Z_input.json`.

## Output file format

The output file is a post-processed form of `SolveResult` optimised for human readability and self-contained analysis:

- Step index `t` is replaced by the UTC datetime string for that step (computed from `bundle.solve_time_utc + t * 15 min`).
- Per-step `import_price_eur_per_kwh` and `export_price_eur_per_kwh` are added to each schedule entry from the corresponding bundle horizon arrays, making the output file self-contained.
- All float values are rounded to 4 decimal places (0.1 W resolution).
- Floating-point solver residuals smaller than 1e-6 in absolute value are clamped to 0.0 (e.g. `-1.3e-12` becomes `0.0`).
- Null device fields (`power_limit_kw`, `zero_export_mode`) are omitted.

The input file is written verbatim from `SolveBundle.model_dump_json` with null fields omitted. It remains format-compatible with golden scenario input files.

## Rationale

- **Debug level, not always-on.** Production solves run at INFO. Writing files on every solve (every 15 minutes, indefinitely) would grow unbounded. The dump only activates when a developer or user explicitly sets `debug.enabled: true`.
- **Human-readable output.** Datetime strings replace opaque step indices. Prices are co-located with the schedule step that uses them. Solver noise is suppressed. This makes the output useful for debugging without further post-processing.
- **Structured JSON, not log lines.** Solver inputs and outputs are rich structured data. Separate files are unambiguous and directly loadable by analysis scripts.

## Configuration

```yaml
debug:
  enabled: true                  # enables DEBUG logging and dump file writing
  dump_dir: "/tmp/mimirheim_dumps"   # directory for solve dumps; null = no files written
  max_dumps: 50                  # rotate oldest files when limit is reached; 0 = unlimited
```

`dump_dir: null` disables file writing entirely. This is the default.

`debug_dump` is called by the solve loop after `build_and_solve()` returns when `config.debug.enabled` is True.

## Security note

Dump files contain energy price data, device state (SOC values, EV plug state), and schedule decisions. They do not contain credentials or personal identifiers. The dump directory should have filesystem permissions restricted to the mimirheim process user. Document this in the deployment guide.
