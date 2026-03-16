"""Command-line interface for Posthumous."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import click

from posthumous import __version__


def setup_logging(verbose: bool = False, log_file: Path | None = None) -> None:
    """Configure logging."""
    level = logging.DEBUG if verbose else logging.INFO
    format_str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    handlers: list[logging.Handler] = [logging.StreamHandler()]

    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(
        level=level,
        format=format_str,
        handlers=handlers,
    )

    # Reduce noise from third-party libraries
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("apprise").setLevel(logging.WARNING)


@click.group()
@click.version_option(version=__version__)
@click.option('-v', '--verbose', is_flag=True, help='Enable verbose output')
@click.option(
    '-c', '--config',
    type=click.Path(exists=False, path_type=Path),
    help='Path to config file',
)
@click.pass_context
def main(ctx: click.Context, verbose: bool, config: Path | None) -> None:
    """Posthumous - A federated deadman switch."""
    ctx.ensure_object(dict)
    ctx.obj['verbose'] = verbose
    ctx.obj['config_path'] = config


def _resolve_config_path(ctx: click.Context) -> Path:
    """Resolve config path from context or default."""
    from posthumous.config import Config
    return ctx.obj.get('config_path') or Config.get_default_config_path()


def _redact_secret(secret: str) -> str:
    """Redact a secret key, showing only the first 4 characters."""
    if len(secret) <= 4:
        return "****"
    return secret[:4] + "****"


@main.group()
@click.pass_context
def config(ctx: click.Context) -> None:
    """View, validate, and edit configuration."""
    pass


@config.command()
@click.pass_context
def path(ctx: click.Context) -> None:
    """Show file and directory locations."""
    config_path = _resolve_config_path(ctx)
    config_dir = config_path.parent

    paths = [
        ("Config file", config_path),
        ("State file", config_dir / "state.yaml"),
        ("Scripts dir", config_dir / "scripts"),
        ("Logs dir", config_dir / "logs"),
    ]

    for label, p in paths:
        marker = "exists" if p.exists() else "missing"
        click.echo(f"{label}: {p} [{marker}]")


@config.command()
@click.pass_context
def show(ctx: click.Context) -> None:
    """Print current config with secret_key redacted."""
    import yaml
    from posthumous.config import Config

    config_path = _resolve_config_path(ctx)

    if not config_path.exists():
        click.echo(f"Config not found at {config_path}", err=True)
        sys.exit(1)

    cfg = Config.from_yaml(config_path)
    data = cfg.to_dict()
    data['secret_key'] = _redact_secret(data.get('secret_key', ''))

    click.echo(yaml.dump(data, default_flow_style=False, sort_keys=False), nl=False)


@config.command()
@click.pass_context
def validate(ctx: click.Context) -> None:
    """Validate configuration and report errors."""
    from posthumous.config import Config

    config_path = _resolve_config_path(ctx)

    if not config_path.exists():
        click.echo(f"Config not found at {config_path}", err=True)
        sys.exit(1)

    cfg = Config.from_yaml(config_path)
    errors = cfg.validate()

    if errors:
        click.echo("Configuration errors:", err=True)
        for error in errors:
            click.echo(f"  - {error}", err=True)
        sys.exit(1)
    else:
        click.echo("Config OK")


@config.command()
@click.pass_context
def edit(ctx: click.Context) -> None:
    """Open config in $EDITOR, then validate on close."""
    from posthumous.config import Config

    config_path = _resolve_config_path(ctx)

    if not config_path.exists():
        click.echo(f"Config not found at {config_path}", err=True)
        click.echo("Run 'posthumous init' first.", err=True)
        sys.exit(1)

    editor = os.environ.get('VISUAL') or os.environ.get('EDITOR') or 'vi'
    subprocess.call([editor, str(config_path)])

    # Validate after editing
    try:
        cfg = Config.from_yaml(config_path)
        errors = cfg.validate()
        if errors:
            click.echo("Validation errors after edit:", err=True)
            for error in errors:
                click.echo(f"  - {error}", err=True)
        else:
            click.echo("Config OK")
    except Exception as e:
        click.echo(f"Failed to parse config: {e}", err=True)


@main.command()
@click.option('--node-name', prompt='Node name', help='Name for this node')
@click.option('--join', 'join_url', help='URL of existing node to join')
@click.pass_context
def init(ctx: click.Context, node_name: str, join_url: str | None) -> None:
    """Initialize a new Posthumous node."""
    from posthumous.auth import generate_secret, generate_qr_code_terminal, get_provisioning_uri
    from posthumous.config import Config, generate_default_config
    from posthumous.scripts import create_example_script

    config_path = ctx.obj.get('config_path') or Config.get_default_config_path()
    config_dir = config_path.parent

    if config_path.exists():
        click.echo(f"Config already exists at {config_path}", err=True)
        click.echo("Delete it first if you want to reinitialize.", err=True)
        sys.exit(1)

    # Generate or fetch secret
    if join_url:
        click.echo(f"Joining existing federation at {join_url}...")
        # In a real implementation, we'd fetch the secret securely
        # For now, prompt for it
        secret = click.prompt(
            "Enter the shared secret from an existing node",
            hide_input=True,
        )
    else:
        secret = generate_secret()
        click.echo("Generated new TOTP secret.")

    # Create config
    config = generate_default_config(node_name, secret)
    config.config_dir = config_dir

    # Create directories
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "logs").mkdir(exist_ok=True)
    (config_dir / "scripts").mkdir(exist_ok=True)

    # Save config
    config.save(config_path)
    click.echo(f"Config created at {config_path}")

    # Create example script
    script_path = create_example_script(config_dir)
    click.echo(f"Example script created at {script_path}")

    # Show QR code for TOTP setup
    click.echo("\n" + "=" * 50)
    click.echo("TOTP Setup - Scan with your authenticator app:")
    click.echo("=" * 50 + "\n")

    try:
        qr = generate_qr_code_terminal(secret, node_name)
        click.echo(qr)
    except ImportError:
        click.echo("(QR code display requires 'qrcode' package)")

    click.echo(f"\nManual entry URI: {get_provisioning_uri(secret, node_name)}")
    click.echo(f"Secret: {secret}")
    click.echo("\n" + "=" * 50)
    click.echo("IMPORTANT: Save this secret securely!")
    click.echo("You'll need it to set up additional nodes.")
    click.echo("=" * 50)

    click.echo("\nNext steps:")
    click.echo("  1. Add the TOTP to your authenticator app")
    click.echo("  2. Edit ~/.posthumous/config.yaml to configure notifications")
    click.echo("  3. Run: posthumous run")


@main.command()
@click.option('--daemon', '-d', is_flag=True, help='Run in background (daemon mode)')
@click.pass_context
def run(ctx: click.Context, daemon: bool) -> None:
    """Start the Posthumous daemon."""
    from posthumous.config import Config
    from posthumous.state import StateManager
    from posthumous.watchdog import Watchdog
    from posthumous.auth import Authenticator
    from posthumous.notifications import NotificationManager, build_context
    from posthumous.scripts import ScriptRunner, ScriptContext
    from posthumous.scheduler import Scheduler, ScheduledExecution
    from posthumous.server import Server
    from posthumous.peers import PeerManager

    config_path = ctx.obj.get('config_path') or Config.get_default_config_path()

    if not config_path.exists():
        click.echo(f"Config not found at {config_path}", err=True)
        click.echo("Run 'posthumous init' first.", err=True)
        sys.exit(1)

    # Load config
    config = Config.from_yaml(config_path)
    errors = config.validate()
    if errors:
        click.echo("Configuration errors:", err=True)
        for error in errors:
            click.echo(f"  - {error}", err=True)
        sys.exit(1)

    # Setup logging
    log_file = config.config_dir / "logs" / "posthumous.log"
    setup_logging(ctx.obj.get('verbose', False), log_file)
    logger = logging.getLogger(__name__)

    # Initialize components
    state_manager = StateManager(config.config_dir / "state.yaml", config.get_encryption_secret())
    notification_manager = NotificationManager(config.notifications)
    script_runner = ScriptRunner(config.config_dir)
    authenticator = Authenticator(
        secret=config.secret_key,
        api_token=config.api_token,
        max_attempts=config.max_failed_attempts,
        lockout_duration=config.lockout_duration,
    )

    # Peer manager
    peer_manager = PeerManager(config, state_manager)

    # Action callbacks
    from posthumous.config import NotificationAction, ScriptAction

    async def execute_actions(
        event: str,
        status: str,
        title: str,
        actions: list,
        trigger_time: datetime | None = None,
    ) -> None:
        """Execute a list of notification and script actions."""
        logger.info(f"{title} - executing actions")

        context = build_context(
            node_name=config.node_name,
            status=status,
            last_checkin=state_manager.state.last_checkin,
            trigger_time=trigger_time,
            trigger_at=config.trigger_at,
            base_url=config.get_base_url(),
        )

        for action in actions:
            if isinstance(action, NotificationAction):
                await notification_manager.send(
                    action.channel, action.message,
                    title=title, context=context,
                )
            elif isinstance(action, ScriptAction):
                script_context = ScriptContext(
                    event=event,
                    trigger_time=trigger_time,
                    node_name=config.node_name,
                    status=status,
                    last_checkin=state_manager.state.last_checkin,
                )
                await script_runner.run(action.script, script_context)

    async def on_warning():
        await execute_actions("warning", "warning", "Posthumous Warning", config.on_warning)

    async def on_grace():
        await execute_actions("grace", "grace", "Posthumous - URGENT", config.on_grace)

    async def on_trigger():
        # Broadcast to peers first
        if state_manager.state.trigger_time:
            await peer_manager.broadcast_trigger(state_manager.state.trigger_time)

        await execute_actions(
            "trigger", "triggered", "Posthumous Activated",
            config.on_trigger, state_manager.state.trigger_time
        )

    async def on_scheduled_complete(execution: ScheduledExecution):
        await peer_manager.broadcast_scheduled_complete(
            execution.item_name, execution.period_key
        )

    # Create components
    watchdog = Watchdog(
        config, state_manager,
        on_warning=on_warning,
        on_grace=on_grace,
        on_trigger=on_trigger,
    )

    scheduler = Scheduler(
        config, state_manager,
        notification_manager=notification_manager,
        script_runner=script_runner,
        on_scheduled_complete=on_scheduled_complete,
    )

    server = Server(
        config, state_manager, watchdog,
        authenticator, peer_manager,
    )

    async def run_daemon():
        # Setup signal handlers
        loop = asyncio.get_event_loop()
        stop_event = asyncio.Event()

        def handle_signal():
            logger.info("Shutdown signal received")
            stop_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, handle_signal)

        # Start components. Order matters: server must be ready before watchdog,
        # which may fire callbacks that broadcast to peers via peer_manager.
        await server.start()
        peer_manager.start_health_monitoring()
        watchdog.start()
        scheduler.start()

        logger.info(f"Posthumous node '{config.node_name}' started")
        logger.info(f"Status: {state_manager.state.status.value}")
        logger.info(f"Web UI: http://{config.listen}/")

        # Wait for shutdown
        await stop_event.wait()

        # Cleanup
        logger.info("Shutting down...")
        await peer_manager.stop_health_monitoring()
        await scheduler.stop()
        await watchdog.stop()
        await server.stop()
        await peer_manager.close()
        logger.info("Shutdown complete")

    if daemon:
        click.echo("Daemon mode not yet implemented. Running in foreground.")

    click.echo(f"Starting Posthumous node '{config.node_name}'...")
    try:
        asyncio.run(run_daemon())
    except KeyboardInterrupt:
        pass


@main.command()
@click.option('--token', '-t', help='Use API token instead of TOTP')
@click.pass_context
def checkin(ctx: click.Context, token: str | None) -> None:
    """Check in to reset the timer."""
    from posthumous.config import Config
    from posthumous.state import StateManager, Status
    from posthumous.auth import Authenticator, LockedOutError, AuthError

    config_path = ctx.obj.get('config_path') or Config.get_default_config_path()

    if not config_path.exists():
        click.echo(f"Config not found at {config_path}", err=True)
        sys.exit(1)

    config = Config.from_yaml(config_path)
    state_manager = StateManager(config.config_dir / "state.yaml", config.get_encryption_secret())

    if state_manager.state.status == Status.TRIGGERED:
        click.echo("Node is already TRIGGERED. Check-in not possible.", err=True)
        sys.exit(1)

    authenticator = Authenticator(
        secret=config.secret_key,
        api_token=config.api_token,
        max_attempts=config.max_failed_attempts,
        lockout_duration=config.lockout_duration,
    )

    # Get code
    code = None
    if not token:
        code = click.prompt("TOTP code", hide_input=False)

    try:
        success = authenticator.verify(
            code=code,
            token=token,
            state=state_manager.state,
            source="cli",
        )
    except LockedOutError as e:
        state_manager.save()
        click.echo(str(e), err=True)
        sys.exit(1)
    except AuthError as e:
        click.echo(str(e), err=True)
        sys.exit(1)

    if not success:
        state_manager.save()
        click.echo("Invalid code.", err=True)
        sys.exit(1)

    # Record check-in
    state_manager.checkin()

    # Calculate next deadline
    next_deadline = state_manager.state.last_checkin + config.trigger_at

    click.echo(f"✓ Check-in accepted")
    click.echo(f"  Status: {state_manager.state.status.value.upper()}")
    click.echo(f"  Next deadline: {next_deadline.strftime('%Y-%m-%d %H:%M UTC')}")


@main.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show current status."""
    from posthumous.config import Config
    from posthumous.state import StateManager
    from posthumous.watchdog import Watchdog, format_time_remaining

    config_path = ctx.obj.get('config_path') or Config.get_default_config_path()

    if not config_path.exists():
        click.echo(f"Config not found at {config_path}", err=True)
        sys.exit(1)

    config = Config.from_yaml(config_path)
    state_manager = StateManager(config.config_dir / "state.yaml", config.get_encryption_secret())
    watchdog = Watchdog(config, state_manager)

    state = state_manager.state
    tr = watchdog.get_time_remaining()

    click.echo(f"Node: {config.node_name}")
    click.echo(f"Status: {state.status.value.upper()}")

    if state.last_checkin:
        click.echo(f"Last check-in: {state.last_checkin.strftime('%Y-%m-%d %H:%M UTC')}")

    if state.trigger_time:
        click.echo(f"Triggered at: {state.trigger_time.strftime('%Y-%m-%d %H:%M UTC')}")

    click.echo()
    click.echo(format_time_remaining(tr))

    # Show peer status if configured
    if config.peers:
        click.echo()
        click.echo("Peers:")
        for url in config.peers:
            peer_state = state.peer_states.get(url)
            if peer_state and peer_state.last_seen:
                age = datetime.now(timezone.utc) - peer_state.last_seen
                click.echo(f"  {url}: seen {int(age.total_seconds()) // 60}m ago")
            else:
                click.echo(f"  {url}: unknown")


@main.command()
@click.option('--force', is_flag=True, help='Skip confirmation prompt')
@click.pass_context
def reset(ctx: click.Context, force: bool) -> None:
    """Reset state to ARMED (administrative recovery)."""
    from posthumous.config import Config
    from posthumous.state import StateManager, Status

    config_path = ctx.obj.get('config_path') or Config.get_default_config_path()

    if not config_path.exists():
        click.echo(f"Config not found at {config_path}", err=True)
        sys.exit(1)

    config = Config.from_yaml(config_path)
    state_manager = StateManager(config.config_dir / "state.yaml", config.get_encryption_secret())
    current = state_manager.state.status

    if current == Status.ARMED:
        click.echo("Already ARMED. Nothing to reset.")
        return

    if current == Status.TRIGGERED and not force:
        click.confirm(
            "Node is TRIGGERED. This is irreversible. Reset to ARMED?",
            abort=True,
        )
    elif not force:
        click.confirm(f"Reset from {current.value.upper()} to ARMED?", abort=True)

    state_manager.reset()
    click.echo("State reset to ARMED.")


@main.command()
@click.pass_context
def peers(ctx: click.Context) -> None:
    """Show peer status."""
    from posthumous.config import Config
    from posthumous.state import StateManager
    from posthumous.peers import PeerManager, format_peer_status

    config_path = ctx.obj.get('config_path') or Config.get_default_config_path()

    if not config_path.exists():
        click.echo(f"Config not found at {config_path}", err=True)
        sys.exit(1)

    config = Config.from_yaml(config_path)

    if not config.peers:
        click.echo("No peers configured.")
        return

    state_manager = StateManager(config.config_dir / "state.yaml", config.get_encryption_secret())
    peer_manager = PeerManager(config, state_manager)

    async def check_peers():
        statuses = await peer_manager.get_all_peer_status()
        await peer_manager.close()

        click.echo("Peers:")
        for status in statuses:
            click.echo(f"  {format_peer_status(status)}")

    asyncio.run(check_peers())


@main.command('test-notify')
@click.option('--channel', '-c', help='Test specific channel (default: all)')
@click.pass_context
def test_notify(ctx: click.Context, channel: str | None) -> None:
    """Send test notifications."""
    from posthumous.config import Config
    from posthumous.notifications import NotificationManager

    config_path = ctx.obj.get('config_path') or Config.get_default_config_path()

    if not config_path.exists():
        click.echo(f"Config not found at {config_path}", err=True)
        sys.exit(1)

    config = Config.from_yaml(config_path)
    manager = NotificationManager(config.notifications)

    async def test():
        if channel:
            if channel not in config.notifications:
                click.echo(f"Unknown channel: {channel}", err=True)
                return
            result = await manager.test_channel(channel)
            status = "✓" if result.success else "✗"
            click.echo(f"{status} {channel}: {result.error or 'OK'}")
        else:
            results = await manager.test_all_channels()
            for name, result in results.items():
                status = "✓" if result.success else "✗"
                click.echo(f"{status} {name}: {result.error or 'OK'}")

    asyncio.run(test())


@main.command('test-trigger')
@click.pass_context
def test_trigger(ctx: click.Context) -> None:
    """Show what would happen on trigger (always dry run)."""
    from posthumous.config import Config, NotificationAction, ScriptAction

    config_path = ctx.obj.get('config_path') or Config.get_default_config_path()

    if not config_path.exists():
        click.echo(f"Config not found at {config_path}", err=True)
        sys.exit(1)

    config = Config.from_yaml(config_path)

    click.echo("Trigger actions that would execute:")
    click.echo()

    for i, action in enumerate(config.on_trigger, 1):
        if isinstance(action, NotificationAction):
            click.echo(f"  {i}. Notify channel '{action.channel}':")
            click.echo(f"     Message: {action.message}")
        elif isinstance(action, ScriptAction):
            script_path = config.get_script_path(action.script)
            exists = "✓" if script_path.exists() else "✗ NOT FOUND"
            click.echo(f"  {i}. Run script: {action.script} ({exists})")

    click.echo()
    click.echo("Post-trigger scheduled items:")
    for item in config.post_trigger:
        click.echo(f"  - {item.name}: {item.when}")


@main.command()
@click.argument('output', type=click.Path(path_type=Path))
@click.option('--decrypt', is_flag=True, default=False,
              help='Export in plaintext (default; decrypts encrypted state files).')
@click.pass_context
def export(ctx: click.Context, output: Path, decrypt: bool) -> None:
    """Export state for backup.

    Always exports in plaintext YAML. If state is encrypted at rest,
    it is transparently decrypted for the export.
    """
    import yaml
    from posthumous.config import Config
    from posthumous.state import StateManager

    config_path = ctx.obj.get('config_path') or Config.get_default_config_path()

    if not config_path.exists():
        click.echo(f"Config not found at {config_path}", err=True)
        sys.exit(1)

    config = Config.from_yaml(config_path)
    state_manager = StateManager(config.config_dir / "state.yaml", config.get_encryption_secret())

    data = {
        'config': config.to_dict(),
        'state': state_manager.state.to_dict(),
    }

    with open(output, 'w') as f:
        yaml.dump(data, f, default_flow_style=False)
    os.chmod(output, 0o600)

    if config.encrypt_at_rest:
        click.echo(f"Exported to {output} (decrypted)")
    else:
        click.echo(f"Exported to {output}")


@main.command('import')
@click.argument('input_file', type=click.Path(exists=True, path_type=Path))
@click.pass_context
def import_state(ctx: click.Context, input_file: Path) -> None:
    """Import state from backup."""
    import yaml
    from posthumous.config import Config
    from posthumous.state import State, StateManager

    config_path = ctx.obj.get('config_path') or Config.get_default_config_path()

    with open(input_file) as f:
        data = yaml.safe_load(f)

    if 'state' not in data:
        click.echo("No state found in backup file", err=True)
        sys.exit(1)

    # Load or create config
    if config_path.exists():
        config = Config.from_yaml(config_path)
    else:
        if 'config' not in data:
            click.echo("No config in backup and no existing config", err=True)
            sys.exit(1)
        config = Config.from_dict(data['config'])
        config.config_dir = config_path.parent
        config.save(config_path)
        click.echo(f"Config restored to {config_path}")

    # Restore state
    state = State.from_dict(data['state'])
    state_path = config.config_dir / "state.yaml"
    state.save(state_path, encryption_secret=config.get_encryption_secret())

    click.echo(f"State restored. Status: {state.status.value}")


if __name__ == "__main__":
    main()
