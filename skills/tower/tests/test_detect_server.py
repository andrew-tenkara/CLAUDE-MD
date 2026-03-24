"""Unit tests for the dev server auto-detection module."""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "sortie" / "lib"))

import pytest
from detect_server import (
    detect_server_cmd,
    save_server_cmd,
    _detect_node,
    _detect_python,
    _detect_ruby,
    _detect_go,
    _detect_docker,
    _detect_makefile,
)


@pytest.fixture
def project_dir(tmp_path):
    """Create a temporary project directory with .sortie/."""
    sortie = tmp_path / ".sortie"
    sortie.mkdir()
    return tmp_path


# ── Cache behavior ────────────────────────────────────────────────────


class TestCache:
    def test_reads_cached_config(self, project_dir):
        """If .sortie/server-cmd.json exists, use it without probing files."""
        config = {"cmd": "make serve", "install_cmd": "", "pkg_mgr": "custom", "detected_from": "user-provided"}
        (project_dir / ".sortie" / "server-cmd.json").write_text(json.dumps(config))

        result = detect_server_cmd(project_dir)
        assert result["cmd"] == "make serve"
        assert result["detected_from"] == "user-provided"

    def test_ignores_cache_without_cmd(self, project_dir):
        """Cache with empty cmd should be ignored."""
        config = {"cmd": "", "pkg_mgr": "custom"}
        (project_dir / ".sortie" / "server-cmd.json").write_text(json.dumps(config))

        # No other files → should return None
        result = detect_server_cmd(project_dir)
        assert result is None

    def test_caches_detected_result(self, project_dir):
        """Auto-detected result should be written to cache."""
        pkg = {"scripts": {"dev": "next dev"}}
        (project_dir / "package.json").write_text(json.dumps(pkg))
        (project_dir / "pnpm-lock.yaml").write_text("")

        result = detect_server_cmd(project_dir)
        assert result is not None

        # Cache file should now exist
        cache = json.loads((project_dir / ".sortie" / "server-cmd.json").read_text())
        assert cache["cmd"] == "pnpm run dev"
        assert cache["detected"] is True

    def test_save_server_cmd(self, project_dir):
        """save_server_cmd writes user-provided config."""
        result = save_server_cmd(project_dir, "python manage.py runserver 0.0.0.0:8000")
        assert result["cmd"] == "python manage.py runserver 0.0.0.0:8000"
        assert result["detected"] is False

        # Verify it persisted
        cache = json.loads((project_dir / ".sortie" / "server-cmd.json").read_text())
        assert cache["cmd"] == "python manage.py runserver 0.0.0.0:8000"


# ── Node.js detection ────────────────────────────────────────────────


class TestNodeDetection:
    def test_pnpm_dev_script(self, project_dir):
        pkg = {"scripts": {"dev": "next dev", "build": "next build"}}
        (project_dir / "package.json").write_text(json.dumps(pkg))
        (project_dir / "pnpm-lock.yaml").write_text("")

        result = _detect_node(project_dir)
        assert result["cmd"] == "pnpm run dev"
        assert result["pkg_mgr"] == "pnpm"
        assert result["install_cmd"].startswith("pnpm install")

    def test_npm_start_script(self, project_dir):
        pkg = {"scripts": {"start": "node server.js", "build": "tsc"}}
        (project_dir / "package.json").write_text(json.dumps(pkg))
        (project_dir / "package-lock.json").write_text("")

        result = _detect_node(project_dir)
        assert result["cmd"] == "npm run start"
        assert result["pkg_mgr"] == "npm"
        assert "npm ci" in result["install_cmd"]

    def test_yarn_serve_script(self, project_dir):
        pkg = {"scripts": {"serve": "vue-cli-service serve"}}
        (project_dir / "package.json").write_text(json.dumps(pkg))
        (project_dir / "yarn.lock").write_text("")

        result = _detect_node(project_dir)
        assert result["cmd"] == "yarn run serve"
        assert result["pkg_mgr"] == "yarn"

    def test_bun_dev_script(self, project_dir):
        pkg = {"scripts": {"dev": "bun run src/index.ts"}}
        (project_dir / "package.json").write_text(json.dumps(pkg))
        (project_dir / "bun.lockb").write_text("")

        result = _detect_node(project_dir)
        assert result["cmd"] == "bun run dev"
        assert result["pkg_mgr"] == "bun"

    def test_dev_preferred_over_start(self, project_dir):
        """dev script should take priority over start."""
        pkg = {"scripts": {"start": "node .", "dev": "nodemon ."}}
        (project_dir / "package.json").write_text(json.dumps(pkg))
        (project_dir / "package-lock.json").write_text("")

        result = _detect_node(project_dir)
        assert result["cmd"] == "npm run dev"

    def test_no_matching_scripts(self, project_dir):
        """Only build/test scripts should not match."""
        pkg = {"scripts": {"build": "tsc", "test": "jest", "lint": "eslint ."}}
        (project_dir / "package.json").write_text(json.dumps(pkg))

        result = _detect_node(project_dir)
        assert result is None

    def test_no_package_json(self, project_dir):
        result = _detect_node(project_dir)
        assert result is None

    def test_packageManager_field_fallback(self, project_dir):
        """Use packageManager field when no lock file exists."""
        pkg = {"scripts": {"dev": "vite"}, "packageManager": "pnpm@8.0.0"}
        (project_dir / "package.json").write_text(json.dumps(pkg))

        result = _detect_node(project_dir)
        assert result["cmd"] == "pnpm run dev"
        assert result["pkg_mgr"] == "pnpm"

    def test_malformed_package_json(self, project_dir):
        (project_dir / "package.json").write_text("not json at all")
        result = _detect_node(project_dir)
        assert result is None


# ── Python detection ──────────────────────────────────────────────────


class TestPythonDetection:
    def test_django_manage_py(self, project_dir):
        (project_dir / "manage.py").write_text("#!/usr/bin/env python\n")

        result = _detect_python(project_dir)
        assert result["cmd"] == "python manage.py runserver"
        assert "Django" in result["detected_from"]

    def test_fastapi_pyproject(self, project_dir):
        (project_dir / "pyproject.toml").write_text('[project]\ndependencies = ["fastapi", "uvicorn"]\n')
        (project_dir / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")

        result = _detect_python(project_dir)
        assert "uvicorn" in result["cmd"]
        assert "main:app" in result["cmd"]

    def test_flask_pyproject(self, project_dir):
        (project_dir / "pyproject.toml").write_text('[project]\ndependencies = ["flask"]\n')
        (project_dir / "app.py").write_text("from flask import Flask\napp = Flask(__name__)\n")

        result = _detect_python(project_dir)
        assert "flask" in result["cmd"]
        assert "app" in result["cmd"]

    def test_requirements_txt_with_app(self, project_dir):
        (project_dir / "requirements.txt").write_text("flask\n")
        (project_dir / "app.py").write_text("from flask import Flask\n")

        result = _detect_python(project_dir)
        assert result["cmd"] == "python app.py"
        assert "pip install -r requirements.txt" in result["install_cmd"]

    def test_pipenv_install_cmd(self, project_dir):
        (project_dir / "manage.py").write_text("")
        (project_dir / "Pipfile").write_text("[packages]\n")

        result = _detect_python(project_dir)
        assert result["install_cmd"] == "pipenv install"

    def test_poetry_install_cmd(self, project_dir):
        (project_dir / "manage.py").write_text("")
        (project_dir / "poetry.lock").write_text("")

        result = _detect_python(project_dir)
        assert result["install_cmd"] == "poetry install"

    def test_no_python_files(self, project_dir):
        result = _detect_python(project_dir)
        assert result is None


# ── Ruby detection ────────────────────────────────────────────────────


class TestRubyDetection:
    def test_rails_app(self, project_dir):
        (project_dir / "Gemfile").write_text("source 'https://rubygems.org'\n")
        (project_dir / "config").mkdir()
        (project_dir / "config" / "application.rb").write_text("")

        result = _detect_ruby(project_dir)
        assert result["cmd"] == "bundle exec rails server"
        assert result["install_cmd"] == "bundle install"

    def test_rack_app(self, project_dir):
        (project_dir / "Gemfile").write_text("source 'https://rubygems.org'\n")
        (project_dir / "config.ru").write_text("run App\n")

        result = _detect_ruby(project_dir)
        assert result["cmd"] == "bundle exec rackup"

    def test_no_gemfile(self, project_dir):
        result = _detect_ruby(project_dir)
        assert result is None


# ── Go detection ──────────────────────────────────────────────────────


class TestGoDetection:
    def test_go_with_cmd_server(self, project_dir):
        (project_dir / "go.mod").write_text("module example.com/app\n")
        cmd = project_dir / "cmd" / "server"
        cmd.mkdir(parents=True)
        (cmd / "main.go").write_text("package main\n")

        result = _detect_go(project_dir)
        assert result["cmd"] == "go run ./cmd/server"
        assert result["install_cmd"] == "go mod download"

    def test_go_with_root_main(self, project_dir):
        (project_dir / "go.mod").write_text("module example.com/app\n")
        (project_dir / "main.go").write_text("package main\n")

        result = _detect_go(project_dir)
        assert result["cmd"] == "go run ."

    def test_go_cmd_priority(self, project_dir):
        """cmd/api should be preferred over cmd/cli."""
        (project_dir / "go.mod").write_text("module example.com/app\n")
        for sub in ("cli", "api"):
            d = project_dir / "cmd" / sub
            d.mkdir(parents=True)
            (d / "main.go").write_text("package main\n")

        result = _detect_go(project_dir)
        assert result["cmd"] == "go run ./cmd/api"

    def test_no_go_mod(self, project_dir):
        result = _detect_go(project_dir)
        assert result is None


# ── Docker detection ──────────────────────────────────────────────────


class TestDockerDetection:
    def test_docker_compose_yml(self, project_dir):
        (project_dir / "docker-compose.yml").write_text("version: '3'\n")

        result = _detect_docker(project_dir)
        assert result["cmd"] == "docker compose up"
        assert result["install_cmd"] == "docker compose build"

    def test_compose_yaml(self, project_dir):
        (project_dir / "compose.yaml").write_text("services:\n")

        result = _detect_docker(project_dir)
        assert result["cmd"] == "docker compose up"

    def test_no_compose_file(self, project_dir):
        result = _detect_docker(project_dir)
        assert result is None


# ── Makefile detection ────────────────────────────────────────────────


class TestMakefileDetection:
    def test_dev_target(self, project_dir):
        (project_dir / "Makefile").write_text("build:\n\tgo build\n\ndev:\n\tgo run .\n")

        result = _detect_makefile(project_dir)
        assert result["cmd"] == "make dev"

    def test_serve_target(self, project_dir):
        (project_dir / "Makefile").write_text("serve:\n\tpython -m http.server\n")

        result = _detect_makefile(project_dir)
        assert result["cmd"] == "make serve"

    def test_no_dev_targets(self, project_dir):
        (project_dir / "Makefile").write_text("build:\n\tgcc main.c\n\ntest:\n\t./run_tests\n")

        result = _detect_makefile(project_dir)
        assert result is None

    def test_no_makefile(self, project_dir):
        result = _detect_makefile(project_dir)
        assert result is None


# ── Integration: detection priority ───────────────────────────────────


class TestDetectionPriority:
    def test_node_over_makefile(self, project_dir):
        """Node.js should be detected before Makefile."""
        pkg = {"scripts": {"dev": "vite"}}
        (project_dir / "package.json").write_text(json.dumps(pkg))
        (project_dir / "pnpm-lock.yaml").write_text("")
        (project_dir / "Makefile").write_text("dev:\n\tmake something\n")

        result = detect_server_cmd(project_dir)
        assert result["cmd"] == "pnpm run dev"
        assert result["pkg_mgr"] == "pnpm"

    def test_cache_over_detection(self, project_dir):
        """Cached config should override even if project files changed."""
        # Set up a Node project
        pkg = {"scripts": {"dev": "vite"}}
        (project_dir / "package.json").write_text(json.dumps(pkg))
        (project_dir / "pnpm-lock.yaml").write_text("")

        # But cache says something else
        config = {"cmd": "custom-server start", "pkg_mgr": "custom", "detected_from": "user-provided"}
        (project_dir / ".sortie" / "server-cmd.json").write_text(json.dumps(config))

        result = detect_server_cmd(project_dir)
        assert result["cmd"] == "custom-server start"

    def test_returns_none_for_empty_project(self, project_dir):
        result = detect_server_cmd(project_dir)
        assert result is None

    def test_creates_sortie_dir_if_missing(self, tmp_path):
        """Should create .sortie/ when caching, even if it doesn't exist."""
        pkg = {"scripts": {"dev": "next dev"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        (tmp_path / "npm" ).write_text("")  # npm lock doesn't exist, but package.json does

        result = detect_server_cmd(tmp_path)
        assert result is not None
        assert (tmp_path / ".sortie" / "server-cmd.json").exists()
