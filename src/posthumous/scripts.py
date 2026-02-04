"""Script execution for Posthumous actions."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ScriptResult:
    """Result of a script execution."""
    success: bool
    script: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration: float  # seconds
    error: str | None = None


@dataclass
class ScriptContext:
    """Context passed to scripts via environment and JSON file."""
    event: str  # "warning", "grace", "trigger", "scheduled"
    trigger_time: datetime | None
    node_name: str
    status: str
    last_checkin: datetime | None
    schedule_item: str | None = None  # Name of scheduled item (if scheduled event)
    extra: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "event": self.event,
            "trigger_time": self.trigger_time.isoformat() if self.trigger_time else None,
            "node_name": self.node_name,
            "status": self.status,
            "last_checkin": self.last_checkin.isoformat() if self.last_checkin else None,
            "schedule_item": self.schedule_item,
            "extra": self.extra or {},
        }

    def to_env(self) -> dict[str, str]:
        """Convert to environment variables."""
        env = {
            "POSTHUMOUS_EVENT": self.event,
            "POSTHUMOUS_NODE": self.node_name,
            "POSTHUMOUS_STATUS": self.status,
        }

        if self.trigger_time:
            env["POSTHUMOUS_TRIGGER_TIME"] = self.trigger_time.isoformat()

        if self.last_checkin:
            env["POSTHUMOUS_LAST_CHECKIN"] = self.last_checkin.isoformat()

        if self.schedule_item:
            env["POSTHUMOUS_SCHEDULE_ITEM"] = self.schedule_item

        return env


class ScriptRunner:
    """Executes user scripts with proper environment and error handling."""

    def __init__(
        self,
        config_dir: Path,
        timeout: float = 300.0,  # 5 minutes default
    ):
        self.config_dir = config_dir
        self.timeout = timeout

    def resolve_script_path(self, script: str) -> Path:
        """Resolve script path relative to config directory."""
        path = Path(script)
        if path.is_absolute():
            return path
        return self.config_dir / script

    async def run(
        self,
        script: str,
        context: ScriptContext,
    ) -> ScriptResult:
        """Run a script with the given context.

        The script receives:
        - Environment variables (POSTHUMOUS_*)
        - A context JSON file (path in POSTHUMOUS_CONTEXT_FILE)

        Args:
            script: Path to script (relative to config_dir or absolute)
            context: Context to pass to the script

        Returns:
            ScriptResult with execution details
        """
        script_path = self.resolve_script_path(script)
        start_time = datetime.now(timezone.utc)

        # Check script exists
        if not script_path.exists():
            return ScriptResult(
                success=False,
                script=str(script),
                exit_code=None,
                stdout="",
                stderr="",
                duration=0.0,
                error=f"Script not found: {script_path}",
            )

        # Check script is executable or is a Python file
        is_python = script_path.suffix == ".py"
        is_executable = os.access(script_path, os.X_OK)

        if not is_executable and not is_python:
            return ScriptResult(
                success=False,
                script=str(script),
                exit_code=None,
                stdout="",
                stderr="",
                duration=0.0,
                error=f"Script is not executable: {script_path}",
            )

        # Create context file
        context_file = None
        try:
            context_file = tempfile.NamedTemporaryFile(
                mode='w',
                suffix='.json',
                prefix='posthumous_context_',
                delete=False,
            )
            json.dump(context.to_dict(), context_file, indent=2)
            context_file.close()

            # Build environment
            env = os.environ.copy()
            env.update(context.to_env())
            env["POSTHUMOUS_CONTEXT_FILE"] = context_file.name

            # Build command
            if is_python:
                cmd = [sys.executable, str(script_path)]
            else:
                cmd = [str(script_path)]

            logger.info(f"Running script: {script_path}")

            # Run script
            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    cwd=self.config_dir,
                )

                try:
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(),
                        timeout=self.timeout,
                    )
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()

                    duration = (datetime.now(timezone.utc) - start_time).total_seconds()
                    return ScriptResult(
                        success=False,
                        script=str(script),
                        exit_code=-1,
                        stdout="",
                        stderr="",
                        duration=duration,
                        error=f"Script timed out after {self.timeout}s",
                    )

                duration = (datetime.now(timezone.utc) - start_time).total_seconds()
                success = process.returncode == 0

                result = ScriptResult(
                    success=success,
                    script=str(script),
                    exit_code=process.returncode,
                    stdout=stdout.decode('utf-8', errors='replace'),
                    stderr=stderr.decode('utf-8', errors='replace'),
                    duration=duration,
                    error=None if success else f"Exit code: {process.returncode}",
                )

                if success:
                    logger.info(f"Script completed successfully: {script}")
                else:
                    logger.error(
                        f"Script failed with exit code {process.returncode}: {script}\n"
                        f"stderr: {result.stderr}"
                    )

                return result

            except FileNotFoundError:
                duration = (datetime.now(timezone.utc) - start_time).total_seconds()
                return ScriptResult(
                    success=False,
                    script=str(script),
                    exit_code=None,
                    stdout="",
                    stderr="",
                    duration=duration,
                    error=f"Failed to execute script: {script_path}",
                )

        except Exception as e:
            duration = (datetime.now(timezone.utc) - start_time).total_seconds()
            logger.exception(f"Error running script {script}: {e}")
            return ScriptResult(
                success=False,
                script=str(script),
                exit_code=None,
                stdout="",
                stderr="",
                duration=duration,
                error=str(e),
            )

        finally:
            # Clean up context file
            if context_file and os.path.exists(context_file.name):
                try:
                    os.unlink(context_file.name)
                except OSError:
                    pass

    async def run_multiple(
        self,
        scripts: list[str],
        context: ScriptContext,
        parallel: bool = False,
    ) -> list[ScriptResult]:
        """Run multiple scripts.

        Args:
            scripts: List of script paths
            context: Context to pass to all scripts
            parallel: If True, run scripts concurrently; otherwise sequentially

        Returns:
            List of results in same order as scripts
        """
        if parallel:
            tasks = [self.run(script, context) for script in scripts]
            return await asyncio.gather(*tasks)
        else:
            results = []
            for script in scripts:
                result = await self.run(script, context)
                results.append(result)
            return results


def create_example_script(config_dir: Path, name: str = "on_trigger.py") -> Path:
    """Create an example script in the scripts directory."""
    scripts_dir = config_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    script_path = scripts_dir / name
    script_content = '''#!/usr/bin/env python3
"""Example Posthumous trigger script.

This script is called when Posthumous triggers.

Environment variables:
    POSTHUMOUS_EVENT: The event type (warning, grace, trigger, scheduled)
    POSTHUMOUS_NODE: The node name
    POSTHUMOUS_TRIGGER_TIME: ISO timestamp of trigger (if triggered)
    POSTHUMOUS_CONTEXT_FILE: Path to JSON file with full context

Exit code 0 for success, non-zero for failure.
"""

import json
import os
import sys


def main():
    event = os.environ.get("POSTHUMOUS_EVENT", "unknown")
    node = os.environ.get("POSTHUMOUS_NODE", "unknown")
    trigger_time = os.environ.get("POSTHUMOUS_TRIGGER_TIME", "N/A")

    # Read full context if needed
    context_file = os.environ.get("POSTHUMOUS_CONTEXT_FILE")
    if context_file:
        with open(context_file) as f:
            context = json.load(f)
        print(f"Full context: {json.dumps(context, indent=2)}")

    print(f"Event: {event}")
    print(f"Node: {node}")
    print(f"Trigger time: {trigger_time}")

    # TODO: Add your custom actions here
    # Examples:
    # - Upload encrypted files to cloud storage
    # - Send emails via external service
    # - Execute cleanup tasks
    # - Notify additional services

    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

    with open(script_path, 'w') as f:
        f.write(script_content)

    # Make executable
    os.chmod(script_path, 0o755)

    return script_path
