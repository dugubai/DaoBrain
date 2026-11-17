"""Fixed isolated test entry. This file is trusted and never copied from a workspace.

Run as python -I /trusted/sandbox_test_entry.py. The container provides no network,
secrets, control socket or business data. Tests exercise untrusted business imports,
not production inputs. Passing these focused tests is not release approval.
"""
import os
from pathlib import Path
import sys

# The only recipe definition. Enrollment supplies pinned hashes, never test names.
# Importing these constants on the host does not execute main() or import pytest.
SUITES = ("test_card_workflow_r5.py", "test_business_r5.py", "test_local_modules.py")
TEST_FIXTURE_PATHS = tuple("tests/" + name for name in SUITES)


def main():
    source = Path("/sandbox")
    trusted = Path("/trusted")
    tests = trusted / "tests"
    if not source.is_dir() or not tests.is_dir():
        raise RuntimeError("isolated fixtures not provisioned")
    for relative in TEST_FIXTURE_PATHS:
        path = trusted / relative
        if not path.is_file() or path.is_symlink():
            raise RuntimeError("trusted test suite missing")
    if Path("/var/run/docker.sock").exists() or Path("/run/secrets").exists():
        raise RuntimeError("unsafe sandbox mounts")
    # Disable discovery of arbitrary environment/site pytest plugins and conftest.
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["TMPDIR"] = "/tmp"
    sys.path = [str(source)] + [p for p in sys.path if p and p != "/trusted"]
    import pytest
    return pytest.main(["-q", "-p", "no:cacheprovider", "--noconftest",
                        "--import-mode=importlib", "--confcutdir=/trusted/tests"] +
                       [str(trusted / relative) for relative in TEST_FIXTURE_PATHS])


if __name__ == "__main__":
    sys.exit(main())
