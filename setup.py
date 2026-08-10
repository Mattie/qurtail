"""Setuptools hooks for producing a clean qurtail 1.0 wheel."""

from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


class QurtailBuildPy(build_py):
    """Keep the removed pre-1.0 module out of incremental wheel builds."""

    def run(self) -> None:
        """Remove the known stale module before setuptools copies current sources."""
        build_root = Path(self.build_lib)
        obsolete_paths = [
            build_root / "smartytail.py",
            build_root / "smartytail.pyc",
            *build_root.glob("__pycache__/smartytail.*.pyc"),
        ]
        for obsolete_path in obsolete_paths:
            if obsolete_path.is_file():
                obsolete_path.unlink()
        super().run()


setup(cmdclass={"build_py": QurtailBuildPy})
