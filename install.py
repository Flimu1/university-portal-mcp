#!/usr/bin/env python3
"""Bootstrap a private local MCP installation. No credentials are accepted here."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv


def main():
    if sys.version_info<(3,11):
        uv=shutil.which('uv')
        if uv:return subprocess.call([uv,'run','--python','3.13','--no-project',str(Path(__file__).resolve()),*sys.argv[1:]])
        print('Python 3.11+ is required. Install current Python from python.org, or run: uv run --python 3.13 --no-project install.py',file=sys.stderr)
        return 1
    root=Path(__file__).resolve().parent
    environment=root/'.venv'
    python=environment/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    try:
        if not python.is_file():
            print('Preparing a separate Python environment…',flush=True)
            venv.EnvBuilder(with_pip=True).create(environment)
        print('Installing University Portal MCP…',flush=True)
        subprocess.run([str(python),'-m','pip','install','--disable-pip-version-check',str(root)],check=True)
        return subprocess.call([str(python),'-m','university_mcp.onboarding',*sys.argv[1:]])
    except (OSError,subprocess.CalledProcessError):
        print('Installation did not complete. Check Python and the package download connection, then retry.',file=sys.stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())
