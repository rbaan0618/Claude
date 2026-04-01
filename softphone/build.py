"""Build script — compile My Line Telecom Softphone into a standalone .exe."""

import subprocess
import sys

if __name__ == "__main__":
    subprocess.run([sys.executable, "-m", "PyInstaller", "PySoftphone.spec", "--clean"],
                   check=True)
