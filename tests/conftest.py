"""Keep collection out of tests/fixtures: the trees under it are gate
TARGETS (tiny repos the e2e tests copy into sandboxes), not part of this
suite. Collecting them directly would run a fixture's baseline tests as if
they were edgeverdict's own -- and would break the day a fixture ships a
deliberately red test."""

import os

# Trusted in-repo fixtures: no sandbox needed. Overridable.
os.environ.setdefault("EDGEVERDICT_EXECUTION_BACKEND", "local")
os.environ.setdefault("EDGEVERDICT_ALLOW_UNSAFE_LOCAL", "1")

collect_ignore = ["fixtures"]
