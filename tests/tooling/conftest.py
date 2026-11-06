"""Run repository shell tools in fresh, test-owned bash processes."""

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

SCRIPT_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class ShellScriptRunner:
    """Run a shell script under an environment built from scratch rather than inherited.

    Attributes:
        cwd: Working directory of every run; the test's ``tmp_path``.
        home: Owned ``HOME`` directory.
        temp: Owned ``TMPDIR`` directory, separate from ``home`` so tests can assert it is left empty.
    """

    cwd: Path
    home: Path
    temp: Path

    def __call__(self, script, *args, path_dirs=(), env=None, text=True):
        """Run ``bash script *args`` and return the completed process without raising on failure.

        The child sees only ``PATH``, ``HOME``, ``TMPDIR``, ``LC_ALL`` and ``env``. Its stdin is closed
        so an unexpected read ends at EOF instead of waiting on the terminal under ``pytest -s``.

        Args:
            script: Path of the script to run.
            *args: Arguments passed to the script.
            path_dirs: Directories searched before ``os.defpath``, for example a stub directory.
            env: Additional environment variables for the child.
            text: Decode output as text when true; return bytes otherwise.

        Returns:
            The completed process with captured stdout and stderr.
        """
        bash = shutil.which('bash')
        assert bash is not None, 'bash is required to run repository shell scripts'
        child_env = {
            'PATH': os.pathsep.join([*map(str, path_dirs), os.defpath]),
            'HOME': str(self.home),
            'TMPDIR': str(self.temp),
            'LC_ALL': 'C.UTF-8',
            **(env or {}),
        }
        return subprocess.run(
            [bash, str(script), *args], cwd=self.cwd, env=child_env, stdin=subprocess.DEVNULL, check=False,
            capture_output=True, text=text, timeout=SCRIPT_TIMEOUT_SECONDS,
        )


@pytest.fixture
def run_shell_script(tmp_path):
    """Provide a runner whose working, home and temporary directories all live under ``tmp_path``."""
    home = tmp_path / 'home'
    temp = tmp_path / 'tmp'
    home.mkdir()
    temp.mkdir()
    return ShellScriptRunner(cwd=tmp_path, home=home, temp=temp)
