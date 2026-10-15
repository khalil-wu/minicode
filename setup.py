"""The backend wheel contains an actual platform-specific native executable."""
import hashlib
import json
from pathlib import Path
import sysconfig

from setuptools import Distribution, setup
from setuptools.command.build_py import build_py
from wheel.bdist_wheel import bdist_wheel


class NativeBuild(build_py):
    def run(self):
        platform = sysconfig.get_platform().replace("-", "_")
        if platform != "win_amd64":
            raise RuntimeError(f"No verified execpolicy runtime is staged for {platform}")
        runtime = Path(__file__).parent / "backend/permissions/resources/execpolicy/x86_64-pc-windows-msvc"
        manifest = json.loads((runtime / "runtime.json").read_text(encoding="utf-8"))
        if manifest["target"] != "x86_64-pc-windows-msvc":
            raise RuntimeError("The staged native engine belongs to a different platform")
        for name, expected in manifest["files"].items():
            if hashlib.sha256((runtime / name).read_bytes()).hexdigest() != expected:
                raise RuntimeError(f"Staged native resource changed: {name}")
        super().run()


class NativeDistribution(Distribution):
    def has_ext_modules(self):
        return True


class NativeWheel(bdist_wheel):
    def get_tag(self):
        return "py3", "none", "win_amd64"


setup(distclass=NativeDistribution, cmdclass={"build_py": NativeBuild, "bdist_wheel": NativeWheel})
