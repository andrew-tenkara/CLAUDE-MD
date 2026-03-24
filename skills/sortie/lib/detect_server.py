"""Auto-detect dev server command from project root files.

Checks for a cached config first (.sortie/server-cmd.json), then probes
the file system for common patterns: Node.js, Python, Ruby, Go, Docker,
Makefile. Returns a dict with cmd, install_cmd, and pkg_mgr — or None if
nothing was detected.

Both tower and sortie-commander import this to avoid duplicating logic.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional


def _detect_node(root: Path) -> Optional[dict]:
    """Detect Node.js project — package manager + dev script."""
    pkg_json = root / "package.json"
    if not pkg_json.exists():
        return None

    try:
        pkg = json.loads(pkg_json.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    scripts = pkg.get("scripts", {})
    # Priority: dev > start > serve
    script_name = None
    for candidate in ("dev", "start", "serve"):
        if candidate in scripts:
            script_name = candidate
            break

    if not script_name:
        return None

    # Detect package manager from lock files
    if (root / "pnpm-lock.yaml").exists():
        pkg_mgr = "pnpm"
        install_cmd = "pnpm install --frozen-lockfile 2>/dev/null || pnpm install"
    elif (root / "bun.lockb").exists() or (root / "bun.lock").exists():
        pkg_mgr = "bun"
        install_cmd = "bun install --frozen-lockfile 2>/dev/null || bun install"
    elif (root / "yarn.lock").exists():
        pkg_mgr = "yarn"
        install_cmd = "yarn install --frozen-lockfile 2>/dev/null || yarn install"
    elif (root / "package-lock.json").exists():
        pkg_mgr = "npm"
        install_cmd = "npm ci 2>/dev/null || npm install"
    else:
        # No lock file — guess from packageManager field or default to npm
        pm_field = pkg.get("packageManager", "")
        if pm_field.startswith("pnpm"):
            pkg_mgr = "pnpm"
            install_cmd = "pnpm install"
        elif pm_field.startswith("yarn"):
            pkg_mgr = "yarn"
            install_cmd = "yarn install"
        elif pm_field.startswith("bun"):
            pkg_mgr = "bun"
            install_cmd = "bun install"
        else:
            pkg_mgr = "npm"
            install_cmd = "npm install"

    return {
        "cmd": f"{pkg_mgr} run {script_name}",
        "install_cmd": install_cmd,
        "pkg_mgr": pkg_mgr,
        "detected_from": "package.json",
    }


def _detect_python(root: Path) -> Optional[dict]:
    """Detect Python project — Django, FastAPI/Uvicorn, Flask, or generic."""
    # Django
    manage_py = root / "manage.py"
    if manage_py.exists():
        return {
            "cmd": "python manage.py runserver",
            "install_cmd": _python_install_cmd(root),
            "pkg_mgr": "python",
            "detected_from": "manage.py (Django)",
        }

    # pyproject.toml — check for uvicorn/gunicorn/flask
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        try:
            content = pyproject.read_text()
        except OSError:
            content = ""

        if "uvicorn" in content or "fastapi" in content:
            # Try to find the app module
            app_module = _find_python_app_module(root)
            return {
                "cmd": f"uvicorn {app_module}:app --reload",
                "install_cmd": _python_install_cmd(root),
                "pkg_mgr": "python",
                "detected_from": "pyproject.toml (FastAPI/Uvicorn)",
            }
        if "flask" in content:
            app_module = _find_python_app_module(root)
            return {
                "cmd": f"flask --app {app_module} run --reload",
                "install_cmd": _python_install_cmd(root),
                "pkg_mgr": "python",
                "detected_from": "pyproject.toml (Flask)",
            }

    # requirements.txt + app.py / main.py
    if (root / "requirements.txt").exists():
        for entry in ("app.py", "main.py"):
            if (root / entry).exists():
                return {
                    "cmd": f"python {entry}",
                    "install_cmd": "pip install -r requirements.txt",
                    "pkg_mgr": "python",
                    "detected_from": f"requirements.txt + {entry}",
                }

    return None


def _python_install_cmd(root: Path) -> str:
    if (root / "Pipfile").exists():
        return "pipenv install"
    if (root / "poetry.lock").exists():
        return "poetry install"
    if (root / "pyproject.toml").exists():
        return "pip install -e '.[dev]' 2>/dev/null || pip install -e ."
    if (root / "requirements.txt").exists():
        return "pip install -r requirements.txt"
    return "pip install -e ."


def _find_python_app_module(root: Path) -> str:
    """Try to find the main app module name."""
    for candidate in ("app", "main", "server", "api"):
        if (root / f"{candidate}.py").exists():
            return candidate
        if (root / candidate / "__init__.py").exists():
            return candidate
    return "main"


def _detect_ruby(root: Path) -> Optional[dict]:
    """Detect Ruby project — Rails or Rack."""
    gemfile = root / "Gemfile"
    if not gemfile.exists():
        return None

    config_ru = root / "config.ru"
    rakefile = root / "Rakefile"

    # Rails
    if (root / "bin" / "rails").exists() or (root / "config" / "application.rb").exists():
        return {
            "cmd": "bundle exec rails server",
            "install_cmd": "bundle install",
            "pkg_mgr": "ruby",
            "detected_from": "Gemfile + Rails",
        }

    # Generic Rack app
    if config_ru.exists():
        return {
            "cmd": "bundle exec rackup",
            "install_cmd": "bundle install",
            "pkg_mgr": "ruby",
            "detected_from": "Gemfile + config.ru",
        }

    return None


def _detect_go(root: Path) -> Optional[dict]:
    """Detect Go project."""
    if not (root / "go.mod").exists():
        return None

    # Check for cmd/ directory (common Go project layout)
    cmd_dir = root / "cmd"
    if cmd_dir.is_dir():
        subdirs = [d.name for d in cmd_dir.iterdir() if d.is_dir()]
        for candidate in ("server", "api", "web", "app"):
            if candidate in subdirs:
                return {
                    "cmd": f"go run ./cmd/{candidate}",
                    "install_cmd": "go mod download",
                    "pkg_mgr": "go",
                    "detected_from": f"go.mod + cmd/{candidate}",
                }
        if subdirs:
            return {
                "cmd": f"go run ./cmd/{subdirs[0]}",
                "install_cmd": "go mod download",
                "pkg_mgr": "go",
                "detected_from": f"go.mod + cmd/{subdirs[0]}",
            }

    # Check for main.go at root
    if (root / "main.go").exists():
        return {
            "cmd": "go run .",
            "install_cmd": "go mod download",
            "pkg_mgr": "go",
            "detected_from": "go.mod + main.go",
        }

    return None


def _detect_docker(root: Path) -> Optional[dict]:
    """Detect Docker Compose project."""
    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        if (root / name).exists():
            return {
                "cmd": "docker compose up",
                "install_cmd": "docker compose build",
                "pkg_mgr": "docker",
                "detected_from": name,
            }
    return None


def _detect_makefile(root: Path) -> Optional[dict]:
    """Detect Makefile with common dev targets."""
    makefile = root / "Makefile"
    if not makefile.exists():
        return None

    try:
        content = makefile.read_text()
    except OSError:
        return None

    # Look for common dev targets
    for target in ("dev", "serve", "server", "start", "run"):
        if re.search(rf"^{target}\s*:", content, re.MULTILINE):
            return {
                "cmd": f"make {target}",
                "install_cmd": "make install 2>/dev/null || true",
                "pkg_mgr": "make",
                "detected_from": f"Makefile ({target} target)",
            }

    return None


# ── Public API ────────────────────────────────────────────────────────


def detect_server_cmd(project_dir: str | Path) -> Optional[dict]:
    """Detect the dev server command for a project.

    Returns dict with keys: cmd, install_cmd, pkg_mgr, detected_from
    or None if nothing could be detected.

    Checks .sortie/server-cmd.json cache first. If auto-detected,
    caches the result for future calls.
    """
    root = Path(project_dir)
    cache_file = root / ".sortie" / "server-cmd.json"

    # Check cache first
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text())
            if cached.get("cmd"):
                return cached
        except (json.JSONDecodeError, OSError):
            pass

    # Auto-detect — order matters (Node first since it's the primary target)
    detectors = [
        _detect_node,
        _detect_python,
        _detect_ruby,
        _detect_go,
        _detect_docker,
        _detect_makefile,
    ]

    for detector in detectors:
        result = detector(root)
        if result:
            result["detected"] = True
            # Cache the result
            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps(result, indent=2) + "\n")
            except OSError:
                pass
            return result

    return None


def save_server_cmd(project_dir: str | Path, cmd: str) -> dict:
    """Manually save a server command to the cache.

    Called when the XO asks the user and they provide a command.
    Returns the saved config dict.
    """
    root = Path(project_dir)
    cache_file = root / ".sortie" / "server-cmd.json"

    config = {
        "cmd": cmd,
        "install_cmd": "",
        "pkg_mgr": "custom",
        "detected_from": "user-provided",
        "detected": False,
    }

    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(config, indent=2) + "\n")
    except OSError:
        pass

    return config
