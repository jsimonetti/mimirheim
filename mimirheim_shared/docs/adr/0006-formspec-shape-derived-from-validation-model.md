# A FormSpec field's shape is derived from the validation model, not hand-authored

FormSpec today only covers a Config Owner's immediate fields: a field whose
validation model is itself a nested object, a Named Collection, an Ordered
Collection, or an optional object gets one opaque `FieldSpec` and nothing
more, so the Config Editor has no way to render its contents. We decided to
derive a field's Field Shape automatically by walking the validation
model's own type annotation, recursing into a nested model's own FormSpec,
rather than requiring every nesting level to be hand-authored as a flat
structure. `FieldSpec` still carries hand-authored presentation metadata
(label, description, Tier, grouping, Conditional Visibility) at every
level; only the shape itself is derived, and a field may override its
derived shape down to something simpler — never up, since an override can
hide structure but never invent structure the validation model doesn't
have.

## Considered Options

- **Fully hand-authored, recursive FormSpec**: keep authoring shape by hand
  at every level. Rejected: this is exactly the failure mode being fixed —
  a nested field with no hand-authored entry silently has no
  representation at all, and nothing forces an author to add one when a
  nested model gains a field.
- **Fully auto-generate presentation content too**: derive label and
  description from the validation model itself, with no hand-authored
  FormSpec at all. Rejected: this loses the separation between validation
  and presentation, and Tier, grouping, and Conditional Visibility have no
  equivalent on the validation model to derive from.

## Consequences

The completeness check that compares a FormSpec against its validation
model must now walk recursively — previously it only checked immediate
fields, which is the same blind spot this decision closes for rendering.
