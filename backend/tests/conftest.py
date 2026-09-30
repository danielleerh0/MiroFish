"""Keep tests away from the real world-model database in <repo>/data."""

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="scenarioiq-test-")
os.environ.setdefault("WORLD_DB_URL", f"sqlite:///{os.path.join(_tmp, 'session.db')}")
