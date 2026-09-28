"""Check built distributions in isolated environments using cached, locked dependencies."""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("distributions", nargs="+", type=Path)
    args = parser.parse_args()
    checkout = Path(__file__).resolve().parents[2]
    smoke_check = checkout / ".github" / "scripts" / "check_installed.py"
    for distribution in args.distributions:
        distribution = distribution.resolve(strict=True)
        with tempfile.TemporaryDirectory(prefix="apptrail-package-check-") as temporary:
            environment = dict(os.environ)
            environment.pop("VIRTUAL_ENV", None)
            environment.pop("PYTHONPATH", None)
            venv = Path(temporary) / ".venv"
            environment["UV_PROJECT_ENVIRONMENT"] = str(venv)
            subprocess.run(
                [
                    "uv",
                    "sync",
                    "--locked",
                    "--offline",
                    "--no-dev",
                    "--no-install-project",
                    "--python",
                    sys.executable,
                ],
                cwd=checkout,
                env=environment,
                check=True,
            )
            binaries = venv / ("Scripts" if os.name == "nt" else "bin")
            python = binaries / ("python.exe" if os.name == "nt" else "python")
            # A unique local source path also avoids reusing an older sdist build
            # when several commits share the same package version.
            artifact = Path(temporary) / distribution.name
            shutil.copyfile(distribution, artifact)
            subprocess.run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--offline",
                    "--no-deps",
                    "--python",
                    str(python),
                    str(artifact),
                ],
                cwd=temporary,
                env=environment,
                check=True,
            )
            subprocess.run(
                ["uv", "pip", "check", "--python", str(python)],
                cwd=temporary,
                env=environment,
                check=True,
            )
            apptrail = binaries / ("apptrail.exe" if os.name == "nt" else "apptrail")
            subprocess.run([str(apptrail), "--version"], cwd=temporary, env=environment, check=True)
            subprocess.run(
                [str(python), str(smoke_check)], cwd=temporary, env=environment, check=True
            )


if __name__ == "__main__":
    main()
