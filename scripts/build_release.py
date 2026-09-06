#!/usr/bin/env python3
"""Build the wheel, a locked source bundle, and installer checksums."""
import gzip
import hashlib
import io
import re
import shutil
import subprocess
import tarfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
assert f"BEAUCLAW_VERSION:-{version}" in (ROOT / "install.sh").read_text(), "installer version mismatch"
assert f'__version__ = "{version}"' in (ROOT / "beauclaw/__init__.py").read_text(), "package version mismatch"
assert re.fullmatch(r"\d+\.\d+\.\d+", version)
subprocess.run(["uv", "lock", "--check"], cwd=ROOT, check=True)
output = ROOT / "dist" / f"v{version}"
output.mkdir(parents=True, exist_ok=True)
subprocess.run(["uv", "build", "--wheel", "--out-dir", str(output)], cwd=ROOT, check=True)
files = [ROOT / name for name in ("pyproject.toml", "uv.lock", ".python-version", "README.md")]
files += [path for path in (ROOT / "beauclaw").rglob("*")
          if path.is_file() and "__pycache__" not in path.parts and path.suffix in (".py", ".html")]
archive = output / "beauclaw.tar.gz"
with archive.open("wb") as stream, gzip.GzipFile(filename="", fileobj=stream, mode="wb", mtime=0) as compressed:
    with tarfile.open(fileobj=compressed, mode="w") as tar:
        for path in sorted(files):
            content = path.read_bytes()
            info = tarfile.TarInfo("beauclaw-release/" + str(path.relative_to(ROOT)))
            info.size = len(content)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(content))
shutil.copyfile(ROOT / "install.sh", output / "install.sh")
artifacts = [output / f"beauclaw-{version}-py3-none-any.whl", archive, output / "install.sh"]
(output / "SHA256SUMS").write_text("".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in artifacts))
print(f"Release v{version}: {output}")
