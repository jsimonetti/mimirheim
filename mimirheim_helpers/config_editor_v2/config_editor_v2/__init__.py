"""config-editor-v2: rendering-library-agnostic core.

This package holds the parts of config-editor-v2 that carry no knowledge of
any specific configuration's shape or of the concrete UI rendering library:
the registry of editable configuration files (`registry.py`), the adapter's
transform dispatch mechanism (`adapter.py`), and the concrete transforms
built on that mechanism (`transforms.py`).

It does not run a server, serve static assets, or depend on any rendering
library. See mimirheim_helpers/config_editor_v2/IMPLEMENTATION_DETAILS.md for
the full design.
"""

from __future__ import annotations

# Importing this submodule registers the "nullable-list" transform as a
# side effect, so any code that imports this package has it available
# without a separate explicit import. See transforms.py.
from . import transforms as transforms

# Re-exported so `config_editor_v2.registry` is reachable as an attribute
# right after `import config_editor_v2`, with no separate explicit
# `import config_editor_v2.registry` needed. See registry.py.
from . import registry as registry
