# Container-level service enablement is gated purely by ENABLE_<SERVICE> (must equal literal "true"), never by configuration-file presence

Every Config Owner's s6 service previously started or idled based on
whether its configuration file existed on disk, in addition to (helpers)
or instead of (core) any explicit enable flag; `config-editor` additionally
checked its own `disabled` field via a shell grep. This conflated "should
this process run at all" with "is its configuration currently valid" —
exactly the ambiguity Awaiting Configuration (ADR-0009-0011) needs not to
exist at the process level, since a Config Owner with no configuration file
is now expected to start and idle in Awaiting Configuration rather than
refuse to start.

File-presence gating is removed entirely, as is config-editor's bespoke
`disabled` field (`ConfigEditorConfig.disabled`) and its shell grep — kept
as a second, redundant off-switch, this would be exactly the kind of
per-service special-casing this decision removes for everyone else. All 12
s6 services — core and config-editor included —
gate uniformly on their own `ENABLE_<SERVICE>` environment variable, which
must equal exactly `"true"` to start; previously it defaulted to enabled
when unset.

Verified safe for the Home Assistant add-on: its manifest
(`hassio-repository/mimirheim/config.yaml`) already writes an explicit
`true`/`false` for every service on every start regardless of the shell
script's own former fallback, and already defaults every helper to `false`
and `config-editor` to `true`. This decision changes nothing observable
under Home Assistant.

It is a breaking change for plain Docker/Compose/bare-metal deployments,
which previously got a helper enabled "for free" once its file existed:
every such deployment must now set the relevant `ENABLE_<SERVICE>`
variables explicitly, including `ENABLE_CONFIG_EDITOR`, on upgrade.

## Considered Options

- Keep file-presence as an additional, OR'd signal alongside
  `ENABLE_<SERVICE>` (start if either says yes), to avoid the breaking
  change. Rejected: reintroduces exactly the ambiguity this decision
  removes — "no file yet" would again mean two different things
  (deliberately disabled vs. not yet configured) depending on which of two
  independent signals happened to be set.
- Default `enable_config_editor` to `false` under Home Assistant too, for
  uniformity with every other helper's manifest default. Rejected: a fresh
  Home Assistant install would then have no running Config Owner capable of
  accepting configuration at all, defeating the purpose of a browser-based
  editor for users who cannot hand-author YAML. The manifest's default
  values are a deployment-policy choice, independent of the shell script
  logic being identical across all 12 services.
