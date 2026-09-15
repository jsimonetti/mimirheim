# Awaiting Configuration runs no operational logic and never falls back to partial or defaulted behaviour

A Config Owner in Awaiting Configuration (ADR-0009, ADR-0010) could in
principle still run parts of its function using its schema's own defaults
for whatever failed to validate. We rejected this. Mimirheim is an energy
optimiser: a defaulted value in a field the operator got wrong — a grid
export limit, a battery capacity — is not a safe, reduced-functionality
fallback, it is a wrong physical assumption running unattended. A Config
Owner in this state therefore runs none of its own function until its
configuration validates in full: an all-or-nothing gate over the whole
file, not a per-field one.

## Considered Options

- Run with schema defaults for any field that fails to validate, so a
  Config Owner does "as much as it safely can." Rejected: for domain
  fields, a default is a guess, not a safe reduction — the failure mode is
  silent incorrect operation, not merely reduced functionality.
- Gate per-field or per-section instead of the whole file, e.g. run with
  however many of a Named Collection's entries are individually valid.
  Rejected: materially larger scope than this feature calls for, and
  reintroduces the same "is this partial state safe" question at a finer
  grain without resolving it.
