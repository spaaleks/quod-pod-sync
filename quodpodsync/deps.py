"""The system packages the iPod helper needs.

ipod-db links against libgpod, which is easily removed as an unused dependency of another program.
"""
import os
import shutil
import subprocess

from .paths import IPOD_DB

PACKAGES = {
    "pacman": ["libgpod"],
    "apt": ["libgpod4"],
    "dnf": ["libgpod"],
    "zypper": ["libgpod"],
}


class Dependencies:
    """Whether the iPod helper can run, and how to install what it is missing."""

    @staticmethod
    def missing():
        """Reason why ipod-db cannot run, or None."""
        if not os.access(IPOD_DB, os.X_OK):
            return f"The helper {IPOD_DB} is missing. Reinstall QuodPodSync with ./install.sh"
        try:
            result = subprocess.run([IPOD_DB], capture_output=True, text=True, timeout=10)
        except OSError as e:
            return f"The helper {IPOD_DB} cannot be started: {e}"
        if "error while loading shared libraries" in result.stderr:
            library = result.stderr.split("shared libraries:")[-1].split(":")[0].strip()
            return f"The library {library} is missing (it was probably uninstalled with another program)."
        return None

    @staticmethod
    def package_manager():
        for name in PACKAGES:
            if shutil.which(name):
                return name
        return None

    @classmethod
    def install_command(cls):
        """Command that installs the missing packages, or None on an unknown distribution."""
        manager = cls.package_manager()
        if manager == "pacman":
            return ["pacman", "-S", "--needed", "--noconfirm", "--asexplicit", *PACKAGES[manager]]
        if manager == "apt":
            return ["apt-get", "install", "-y", *PACKAGES[manager]]
        if manager in ("dnf", "zypper"):
            return [manager, "install", "-y", *PACKAGES[manager]]
        return None

    @classmethod
    def install(cls, graphical=True):
        """Install as root, pkexec asks for the password. Returns (ok, output)."""
        command = cls.install_command()
        if not command:
            return False, "Unknown distribution, please install libgpod yourself."
        runner = ["pkexec"] if graphical and shutil.which("pkexec") else ["sudo"]
        result = subprocess.run(runner + command, capture_output=True, text=True)
        return result.returncode == 0 and not cls.missing(), (result.stdout + result.stderr).strip()
