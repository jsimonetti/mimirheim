# Security Policy

## Supported versions

Only the latest release receives security fixes. Older versions are not patched.

| Version | Supported |
|---------|-----------|
| Latest  | Yes       |
| Older   | No        |

## Reporting a vulnerability

Please do **not** open a public GitHub issue for security vulnerabilities.

Report privately via [GitHub Security Advisories](https://github.com/jsimonetti/mimirheim/security/advisories/new). GitHub keeps the report confidential until a fix is released. Include:

- A description of the vulnerability and its potential impact
- Steps to reproduce or a proof-of-concept
- The mimirheim version(s) affected
- Any suggested mitigations if you have them

You will receive an acknowledgement within 5 business days. Fixes are coordinated with the reporter before public disclosure.

## Security considerations for operators

### MQTT credentials

mimirheim reads MQTT credentials from its config file (`mimirheim.yaml`). Protect that file with appropriate filesystem permissions — it should not be world-readable. When running as a Home Assistant add-on, credentials are injected via the Supervisor environment and are never written to disk by mimirheim.

Do not publish MQTT credentials in forum posts, bug reports, or GitHub issues. Use redacted config snippets when asking for support.

### MQTT broker access

mimirheim subscribes to input topics and publishes schedule and setpoint topics on your broker. Anyone with write access to those topics can influence the schedules mimirheim produces. Restrict broker ACLs so that only trusted clients can publish to `{prefix}/input/prices`, `{prefix}/input/trigger`, and device state topics.

Anonymous MQTT access (no username/password) is supported for local-only deployments but is not recommended for brokers reachable over a network.

### Config file

mimirheim validates its config at startup with strict Pydantic models (`extra="forbid"`). Unknown fields are rejected immediately. There is no config reloading at runtime — a restart is required after any config change.

Do not expose the config editor (port 8099) directly to the internet. When running as an add-on it is accessible only via HA ingress, which requires an active HA session.

### Solver input data

mimirheim trusts the data it receives over MQTT. A compromised MQTT message (tampered price data, spoofed SOC reading) can cause the solver to produce an incorrect schedule. Ensure your broker is not accessible to untrusted parties and that publisher automations run on trusted hosts.

### Python dependencies

mimirheim's dependencies are pinned in `uv.lock`. Run `uv sync` to install the exact pinned versions. Dependency updates that include security fixes are released as patch versions.

To audit the current dependency tree for known vulnerabilities:

```bash
uv run pip-audit
```
