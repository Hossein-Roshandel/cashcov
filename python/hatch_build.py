"""hatch_build.py — custom hatchling build hook.

Hatchling (and therefore pip / uv) calls :meth:`CustomBuildHook.initialize`
before assembling the wheel or sdist.  This hook:

1. Locates the Go module root by walking up from this file until it finds
   a ``go.mod`` (handles both in-repo and extracted-sdist layouts).
2. Runs ``go build -buildmode=c-shared`` to compile the platform-specific
   shared library into ``cashcov/<libname>``.
3. Registers the compiled library as a wheel artifact so hatchling bundles
   it inside the package directory automatically.
4. Removes the generated CGo header (``.h``) — it is not needed at runtime.

Requirements
------------
- Go 1.21+ on ``PATH``  (``go build`` must be available).
- A C compiler (gcc / clang / MSVC) for CGo.
- The ``cshim/`` directory containing ``shim.go`` must be reachable from the
  Go module root.

Install
-------
::

    uv pip install ./python/          # one-shot wheel build + install
    uv pip install -e ./python/       # editable install (re-uses compiled .so)
    pip install ./python/             # works with plain pip too
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

# ---------------------------------------------------------------------------
# Platform-specific library filename
# ---------------------------------------------------------------------------
_LIB_NAME: dict[str, str] = {
    "linux": "libcashcov.so",
    "darwin": "libcashcov.dylib",
    "win32": "cashcov.dll",
}
_lib_filename = _LIB_NAME.get(sys.platform, "libcashcov.so")


def _find_repo_root(start: Path) -> Path:
    """Walk up from *start* until a directory containing ``go.mod`` is found.

    Raises ``RuntimeError`` if no such directory is found within 10 levels.
    """
    current = start.resolve()
    for _ in range(10):
        if (current / "go.mod").exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    raise RuntimeError(
        f"Could not locate go.mod starting from {start}. "
        "Make sure you are installing from inside the cashcov repository, "
        "or that the sdist was built with `make sdist` which bundles the "
        "Go source alongside the Python package."
    )


class CustomBuildHook(BuildHookInterface):
    """Compile the CGo shared library before hatchling assembles the wheel."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        hook_dir = Path(__file__).parent  # python/
        repo_root = _find_repo_root(hook_dir)

        out_path = hook_dir / "cashcov" / _lib_filename
        header_path = out_path.with_suffix(".h")

        # ------------------------------------------------------------------
        # Prerequisite check
        # ------------------------------------------------------------------
        if not shutil.which("go"):
            raise RuntimeError(
                "Go toolchain not found. Install Go 1.21+ from "
                "https://go.dev/dl/ and ensure it is on PATH, then re-run "
                "the installation."
            )

        # ------------------------------------------------------------------
        # Compile
        # ------------------------------------------------------------------
        self.app.display_info(f"cashcov: compiling {_lib_filename} for {sys.platform} ...")

        result = subprocess.run(
            [
                "go",
                "build",
                "-buildmode=c-shared",
                f"-o={out_path}",
                "./cshim/",
            ],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            raise RuntimeError(f"Go build failed (exit {result.returncode}):\n{result.stderr}")

        # ------------------------------------------------------------------
        # Clean up the generated C header — not needed at runtime
        # ------------------------------------------------------------------
        if header_path.exists():
            header_path.unlink()

        # ------------------------------------------------------------------
        # Tell hatchling to include the compiled library in the wheel
        # ------------------------------------------------------------------
        build_data["artifacts"].append(f"cashcov/{_lib_filename}")

        self.app.display_success(f"cashcov: {_lib_filename} compiled successfully")
