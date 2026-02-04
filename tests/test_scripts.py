"""Tests for script execution."""

import os
import pytest
from datetime import datetime, timezone
from pathlib import Path

from posthumous.scripts import (
    ScriptContext,
    ScriptResult,
    ScriptRunner,
    create_example_script,
)


def make_context(**overrides):
    """Create a ScriptContext with sensible defaults for testing."""
    defaults = {
        "event": "trigger",
        "trigger_time": datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
        "node_name": "test-node",
        "status": "TRIGGERED",
        "last_checkin": datetime(2026, 1, 10, 8, 30, 0, tzinfo=timezone.utc),
        "schedule_item": None,
        "extra": None,
    }
    defaults.update(overrides)
    return ScriptContext(**defaults)


class TestScriptContext:
    """Tests for ScriptContext serialization."""

    def test_to_dict_all_fields_populated(self):
        ctx = make_context(
            schedule_item="weekly_report",
            extra={"recipient": "alice@example.com", "priority": 1},
        )

        d = ctx.to_dict()

        assert d["event"] == "trigger"
        assert d["trigger_time"] == "2026-01-15T12:00:00+00:00"
        assert d["node_name"] == "test-node"
        assert d["status"] == "TRIGGERED"
        assert d["last_checkin"] == "2026-01-10T08:30:00+00:00"
        assert d["schedule_item"] == "weekly_report"
        assert d["extra"] == {"recipient": "alice@example.com", "priority": 1}

    def test_to_dict_with_none_fields(self):
        ctx = make_context(trigger_time=None, last_checkin=None)

        d = ctx.to_dict()

        assert d["trigger_time"] is None
        assert d["last_checkin"] is None
        assert d["schedule_item"] is None
        assert d["extra"] == {}

    def test_to_env_basic_keys_always_present(self):
        ctx = make_context(trigger_time=None, last_checkin=None)

        env = ctx.to_env()

        assert env["POSTHUMOUS_EVENT"] == "trigger"
        assert env["POSTHUMOUS_NODE"] == "test-node"
        assert env["POSTHUMOUS_STATUS"] == "TRIGGERED"

    def test_to_env_with_trigger_time(self):
        ctx = make_context()

        env = ctx.to_env()

        assert "POSTHUMOUS_TRIGGER_TIME" in env
        assert env["POSTHUMOUS_TRIGGER_TIME"] == "2026-01-15T12:00:00+00:00"

    def test_to_env_with_last_checkin(self):
        ctx = make_context()

        env = ctx.to_env()

        assert "POSTHUMOUS_LAST_CHECKIN" in env
        assert env["POSTHUMOUS_LAST_CHECKIN"] == "2026-01-10T08:30:00+00:00"

    def test_to_env_with_schedule_item(self):
        ctx = make_context(schedule_item="monthly_backup")

        env = ctx.to_env()

        assert env["POSTHUMOUS_SCHEDULE_ITEM"] == "monthly_backup"

    def test_to_env_none_optional_fields_omitted(self):
        ctx = make_context(trigger_time=None, last_checkin=None, schedule_item=None)

        env = ctx.to_env()

        assert "POSTHUMOUS_TRIGGER_TIME" not in env
        assert "POSTHUMOUS_LAST_CHECKIN" not in env
        assert "POSTHUMOUS_SCHEDULE_ITEM" not in env


class TestScriptResult:
    """Tests for ScriptResult dataclass."""

    def test_success_result(self):
        result = ScriptResult(
            success=True,
            script="on_trigger.py",
            exit_code=0,
            stdout="done\n",
            stderr="",
            duration=1.5,
        )

        assert result.success is True
        assert result.exit_code == 0
        assert result.error is None

    def test_failure_result(self):
        result = ScriptResult(
            success=False,
            script="on_trigger.py",
            exit_code=1,
            stdout="",
            stderr="something failed\n",
            duration=0.3,
            error="Exit code: 1",
        )

        assert result.success is False
        assert result.exit_code == 1
        assert result.error == "Exit code: 1"

    def test_none_exit_code_when_script_not_found(self):
        result = ScriptResult(
            success=False,
            script="missing.py",
            exit_code=None,
            stdout="",
            stderr="",
            duration=0.0,
            error="Script not found: /path/to/missing.py",
        )

        assert result.exit_code is None
        assert "not found" in result.error


class TestScriptRunner:
    """Tests for ScriptRunner execution."""

    def test_resolve_relative_path(self, tmp_config_dir):
        runner = ScriptRunner(tmp_config_dir)

        resolved = runner.resolve_script_path("scripts/on_trigger.py")

        assert resolved == tmp_config_dir / "scripts" / "on_trigger.py"

    def test_resolve_absolute_path(self, tmp_config_dir):
        runner = ScriptRunner(tmp_config_dir)
        abs_path = "/usr/local/bin/some_script.sh"

        resolved = runner.resolve_script_path(abs_path)

        assert resolved == Path(abs_path)

    @pytest.mark.asyncio
    async def test_run_python_script_success(self, tmp_config_dir):
        script_path = tmp_config_dir / "scripts" / "ok.py"
        script_path.write_text(
            "import sys\n"
            "print('hello from script')\n"
            "sys.exit(0)\n"
        )

        runner = ScriptRunner(tmp_config_dir)
        result = await runner.run("scripts/ok.py", make_context())

        assert result.success is True
        assert result.exit_code == 0
        assert "hello from script" in result.stdout
        assert result.duration > 0

    @pytest.mark.asyncio
    async def test_run_python_script_failure(self, tmp_config_dir):
        script_path = tmp_config_dir / "scripts" / "fail.py"
        script_path.write_text(
            "import sys\n"
            "print('about to fail', file=sys.stderr)\n"
            "sys.exit(42)\n"
        )

        runner = ScriptRunner(tmp_config_dir)
        result = await runner.run("scripts/fail.py", make_context())

        assert result.success is False
        assert result.exit_code == 42
        assert "about to fail" in result.stderr
        assert result.error == "Exit code: 42"

    @pytest.mark.asyncio
    async def test_run_executable_bash_script(self, tmp_config_dir):
        script_path = tmp_config_dir / "scripts" / "run.sh"
        script_path.write_text("#!/bin/bash\necho 'bash ok'\nexit 0\n")
        os.chmod(script_path, 0o755)

        runner = ScriptRunner(tmp_config_dir)
        result = await runner.run("scripts/run.sh", make_context())

        assert result.success is True
        assert result.exit_code == 0
        assert "bash ok" in result.stdout

    @pytest.mark.asyncio
    async def test_run_nonexistent_script(self, tmp_config_dir):
        runner = ScriptRunner(tmp_config_dir)
        result = await runner.run("scripts/does_not_exist.py", make_context())

        assert result.success is False
        assert result.exit_code is None
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_run_non_executable_non_python_file(self, tmp_config_dir):
        script_path = tmp_config_dir / "scripts" / "data.txt"
        script_path.write_text("just some text\n")
        # Ensure it is NOT executable
        os.chmod(script_path, 0o644)

        runner = ScriptRunner(tmp_config_dir)
        result = await runner.run("scripts/data.txt", make_context())

        assert result.success is False
        assert result.exit_code is None
        assert "not executable" in result.error.lower()

    @pytest.mark.asyncio
    async def test_run_timeout_kills_script(self, tmp_config_dir):
        script_path = tmp_config_dir / "scripts" / "slow.py"
        script_path.write_text(
            "import time\n"
            "time.sleep(60)\n"
        )

        runner = ScriptRunner(tmp_config_dir, timeout=0.5)
        result = await runner.run("scripts/slow.py", make_context())

        assert result.success is False
        assert result.exit_code == -1
        assert "timed out" in result.error.lower()

    @pytest.mark.asyncio
    async def test_env_var_passthrough(self, tmp_config_dir):
        script_path = tmp_config_dir / "scripts" / "read_env.py"
        script_path.write_text(
            "import os\n"
            "print(os.environ['POSTHUMOUS_EVENT'])\n"
            "print(os.environ['POSTHUMOUS_NODE'])\n"
        )

        ctx = make_context(event="warning", node_name="alpha-node")
        runner = ScriptRunner(tmp_config_dir)
        result = await runner.run("scripts/read_env.py", ctx)

        assert result.success is True
        lines = result.stdout.strip().split("\n")
        assert lines[0] == "warning"
        assert lines[1] == "alpha-node"

    @pytest.mark.asyncio
    async def test_context_json_file_accessible_during_execution(self, tmp_config_dir):
        script_path = tmp_config_dir / "scripts" / "read_ctx.py"
        script_path.write_text(
            "import json, os\n"
            "ctx_path = os.environ['POSTHUMOUS_CONTEXT_FILE']\n"
            "with open(ctx_path) as f:\n"
            "    ctx = json.load(f)\n"
            "print(ctx['event'])\n"
            "print(ctx['node_name'])\n"
        )

        ctx = make_context(event="grace", node_name="beta-node")
        runner = ScriptRunner(tmp_config_dir)
        result = await runner.run("scripts/read_ctx.py", ctx)

        assert result.success is True
        lines = result.stdout.strip().split("\n")
        assert lines[0] == "grace"
        assert lines[1] == "beta-node"

    @pytest.mark.asyncio
    async def test_stdout_and_stderr_capture(self, tmp_config_dir):
        script_path = tmp_config_dir / "scripts" / "both.py"
        script_path.write_text(
            "import sys\n"
            "print('out message')\n"
            "print('err message', file=sys.stderr)\n"
        )

        runner = ScriptRunner(tmp_config_dir)
        result = await runner.run("scripts/both.py", make_context())

        assert result.success is True
        assert "out message" in result.stdout
        assert "err message" in result.stderr

    @pytest.mark.asyncio
    async def test_context_temp_file_cleaned_up(self, tmp_config_dir):
        # This script writes the context file path to stdout so we can check
        # that the file no longer exists after run() returns.
        script_path = tmp_config_dir / "scripts" / "print_path.py"
        script_path.write_text(
            "import os\n"
            "print(os.environ['POSTHUMOUS_CONTEXT_FILE'])\n"
        )

        runner = ScriptRunner(tmp_config_dir)
        result = await runner.run("scripts/print_path.py", make_context())

        assert result.success is True
        context_file_path = result.stdout.strip()
        assert not os.path.exists(context_file_path), (
            f"Context temp file should be cleaned up but still exists: {context_file_path}"
        )


class TestScriptRunnerRunMultiple:
    """Tests for ScriptRunner.run_multiple."""

    @pytest.mark.asyncio
    async def test_sequential_execution(self, tmp_config_dir):
        for name in ("a.py", "b.py"):
            (tmp_config_dir / "scripts" / name).write_text(
                f"print('{name}')\n"
            )

        runner = ScriptRunner(tmp_config_dir)
        results = await runner.run_multiple(
            ["scripts/a.py", "scripts/b.py"],
            make_context(),
            parallel=False,
        )

        assert len(results) == 2
        assert all(r.success for r in results)
        assert "a.py" in results[0].stdout
        assert "b.py" in results[1].stdout

    @pytest.mark.asyncio
    async def test_parallel_execution(self, tmp_config_dir):
        for name in ("p1.py", "p2.py"):
            (tmp_config_dir / "scripts" / name).write_text(
                f"print('{name}')\n"
            )

        runner = ScriptRunner(tmp_config_dir)
        results = await runner.run_multiple(
            ["scripts/p1.py", "scripts/p2.py"],
            make_context(),
            parallel=True,
        )

        assert len(results) == 2
        assert all(r.success for r in results)
        assert "p1.py" in results[0].stdout
        assert "p2.py" in results[1].stdout

    @pytest.mark.asyncio
    async def test_mixed_results(self, tmp_config_dir):
        good = tmp_config_dir / "scripts" / "good.py"
        good.write_text("print('ok')\n")

        bad = tmp_config_dir / "scripts" / "bad.py"
        bad.write_text("import sys\nsys.exit(1)\n")

        runner = ScriptRunner(tmp_config_dir)
        results = await runner.run_multiple(
            ["scripts/good.py", "scripts/bad.py"],
            make_context(),
            parallel=False,
        )

        assert len(results) == 2
        assert results[0].success is True
        assert results[1].success is False
        assert results[1].exit_code == 1


class TestCreateExampleScript:
    """Tests for the create_example_script helper."""

    def test_creates_file_at_correct_path(self, tmp_config_dir):
        path = create_example_script(tmp_config_dir)

        assert path == tmp_config_dir / "scripts" / "on_trigger.py"
        assert path.exists()

    def test_custom_name(self, tmp_config_dir):
        path = create_example_script(tmp_config_dir, name="on_warning.py")

        assert path.name == "on_warning.py"
        assert path.exists()

    def test_file_is_executable(self, tmp_config_dir):
        path = create_example_script(tmp_config_dir)

        assert os.access(path, os.X_OK)

    def test_file_content_is_valid_python(self, tmp_config_dir):
        path = create_example_script(tmp_config_dir)

        source = path.read_text()
        # compile() will raise SyntaxError if the content is not valid Python
        compile(source, str(path), "exec")

    def test_creates_scripts_directory_if_needed(self, tmp_path):
        # Use a fresh directory that does NOT already have a scripts/ subdirectory
        config_dir = tmp_path / "fresh_config"
        config_dir.mkdir()
        assert not (config_dir / "scripts").exists()

        path = create_example_script(config_dir)

        assert (config_dir / "scripts").is_dir()
        assert path.exists()
