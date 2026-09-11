"""config-editor-v2: rendering-library-agnostic core.

This package holds the parts of config-editor-v2 that carry no knowledge of
any specific configuration's shape or of the concrete UI rendering library:
the registry of editable configuration files (`registry.py`) and the
adapter's transform dispatch mechanism (`adapter.py`).

It does not run a server, serve static assets, or depend on any rendering
library. See mimirheim_helpers/config_editor_v2/IMPLEMENTATION_DETAILS.md for
the full design.
"""
