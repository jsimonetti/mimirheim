# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## Before exploring, read these

- **`CONTEXT-MAP.md`** at the repo root: it points at one `CONTEXT.md` per modeled context. Read each one relevant to the topic.
- **`docs/adr/`** at the repo root (system-wide decisions), if it exists.
- **`<package>/docs/adr/`**: context-scoped ADRs. Currently only `mimirheim_shared/docs/adr/` exists.

If any of these files don't exist, proceed silently. Don't flag their absence; don't suggest creating them upfront. The `/domain-modeling` skill creates them lazily when terms or decisions actually get resolved.

## File structure (multi-context)

```
/
├── CONTEXT-MAP.md
├── docs/adr/                     ← system-wide decisions (not created yet)
├── mimirheim/
│   ├── CONTEXT.md                ← solver/device domain (not modeled yet)
│   └── docs/adr/
├── mimirheim_helpers/
│   ├── CONTEXT.md                ← individual helper domains (not modeled yet)
│   └── docs/adr/
└── mimirheim_shared/
    ├── CONTEXT.md                ← config service: protocol/vocabulary for exposing and editing config
    └── docs/adr/
        ├── 0001-uniform-mqtt-config-service.md
        ├── 0002-formspec-separate-from-validation-model.md
        ├── 0003-server-rendered-forms.md
        └── 0004-mimirheim-shared-package.md
```

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in the relevant `CONTEXT.md`. Don't drift to synonyms the glossary explicitly avoids.

If the concept you need isn't in the glossary yet, that's a signal: either you're inventing language the project doesn't use (reconsider) or there's a real gap (note it for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0001 (uniform MQTT config service), but worth reopening because..._
