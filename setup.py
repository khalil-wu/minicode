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
        target = {
            "win_amd64": "x86_64-pc-windows-msvc",
            "linux_x86_64": "x86_64-unknown-linux-gnu",
        }[platform]
        for resources in ("permissions/resources/execpolicy", "network/resources"):
            runtime = Path(__file__).parent / "backend" / resources / target
            manifest = json.loads((runtime / "runtime.json").read_text(encoding="utf-8"))
            if manifest["target"] != target:
                raise RuntimeError(f"The staged native engine belongs to a different platform: {runtime}")
            for name, expected in manifest["files"].items():
                if hashlib.sha256((runtime / name).read_bytes()).hexdigest() != expected:
                    raise RuntimeError(f"Staged native resource changed: {runtime / name}")
        super().run()
        public = Path(__file__).parent / "frontend" / "public"
        packaged = Path(self.build_lib) / "backend" / "public"
        for directory in ("apps", "pets"):
            self.copy_tree(str(public / directory), str(packaged / directory))
        self.copy_file(str(public / "theme-boot.js"), str(packaged / "theme-boot.js"))
        (packaged.parent / "public_asset_root.py").write_text(
            '"""Public assets staged into this installed wheel."""\n'
            'from pathlib import Path\n\n'
            'PUBLIC_ASSET_ROOT = Path(__file__).resolve().parent / "public"\n',
            encoding="utf-8",
        )


class NativeDistribution(Distribution):
    def has_ext_modules(self):
        return True


class NativeWheel(bdist_wheel):
    def get_tag(self):
        return "py3", "none", sysconfig.get_platform().replace("-", "_")


setup(distclass=NativeDistribution, cmdclass={"build_py": NativeBuild, "bdist_wheel": NativeWheel})
