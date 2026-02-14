"""Configuration loading and validation for Posthumous."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml


def parse_duration(value: str | int | float) -> timedelta:
    """Parse a duration string like '7 days' or '12 hours' into timedelta.

    Supports: days, hours, minutes, seconds (and singular forms).
    Also accepts raw numbers as seconds.
    """
    if isinstance(value, (int, float)):
        return timedelta(seconds=value)

    value = value.strip().lower()

    # Parse "N unit" format
    parts = value.split()
    if len(parts) == 2:
        amount, unit = parts
        amount = float(amount)

        unit_map = {
            'second': 'seconds', 'seconds': 'seconds', 's': 'seconds',
            'minute': 'minutes', 'minutes': 'minutes', 'm': 'minutes', 'min': 'minutes',
            'hour': 'hours', 'hours': 'hours', 'h': 'hours', 'hr': 'hours',
            'day': 'days', 'days': 'days', 'd': 'days',
            'week': 'weeks', 'weeks': 'weeks', 'w': 'weeks',
        }

        if unit not in unit_map:
            raise ValueError(f"Unknown time unit: {unit}")

        normalized_unit = unit_map[unit]

        if normalized_unit == 'weeks':
            return timedelta(days=amount * 7)
        return timedelta(**{normalized_unit: amount})

    # Try parsing as raw number (seconds)
    try:
        return timedelta(seconds=float(value))
    except ValueError:
        raise ValueError(f"Cannot parse duration: {value}")


@dataclass
class NotificationAction:
    """An action that sends a notification."""
    channel: str  # Name of notification channel (e.g., "default", "urgent")
    message: str


@dataclass
class ScriptAction:
    """An action that runs a script."""
    script: str  # Path to script (relative to config dir or absolute)


@dataclass
class ScheduledItem:
    """A scheduled post-trigger action."""
    name: str
    when: str  # When expression (parsed by DSL)
    script: str | None = None
    notify: str | None = None
    message: str | None = None


@dataclass
class Config:
    """Main configuration for a Posthumous node."""

    # Identity
    node_name: str
    secret_key: str
    listen: str = "0.0.0.0:8420"

    # Timing
    checkin_interval: timedelta = field(default_factory=lambda: timedelta(days=7))
    warning_start: timedelta = field(default_factory=lambda: timedelta(days=8))
    grace_start: timedelta = field(default_factory=lambda: timedelta(days=12))
    trigger_at: timedelta = field(default_factory=lambda: timedelta(days=14))

    # Peers
    peers: list[str] = field(default_factory=list)

    # Notification channels (name -> list of Apprise URLs)
    notifications: dict[str, list[str]] = field(default_factory=dict)

    # Actions by event
    on_warning: list[NotificationAction | ScriptAction] = field(default_factory=list)
    on_grace: list[NotificationAction | ScriptAction] = field(default_factory=list)
    on_trigger: list[NotificationAction | ScriptAction] = field(default_factory=list)

    # Post-trigger schedule
    post_trigger: list[ScheduledItem] = field(default_factory=list)

    # Optional API token for automated check-ins
    api_token: str | None = None

    # Brute force protection
    max_failed_attempts: int = 5
    lockout_duration: timedelta = field(default_factory=lambda: timedelta(minutes=15))

    # Watchdog check interval (None = auto-tune based on timing thresholds)
    check_interval: timedelta | None = None

    # Peer health monitoring
    peer_check_interval: timedelta = field(default_factory=lambda: timedelta(minutes=30))
    peer_down_threshold: timedelta = field(default_factory=lambda: timedelta(hours=6))

    # Base URL for constructing check-in/dashboard links in notifications
    base_url: str | None = None

    # Encryption at rest
    encrypt_at_rest: bool = False

    # Paths (set after loading)
    config_dir: Path = field(default_factory=lambda: Path.home() / ".posthumous")

    @classmethod
    def get_default_config_path(cls) -> Path:
        """Get the default config file path."""
        return Path.home() / ".posthumous" / "config.yaml"

    @classmethod
    def from_yaml(cls, path: Path | str) -> Config:
        """Load configuration from a YAML file."""
        path = Path(path)

        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        with open(path) as f:
            data = yaml.safe_load(f) or {}

        return cls.from_dict(data, config_dir=path.parent)

    @classmethod
    def from_dict(cls, data: dict[str, Any], config_dir: Path | None = None) -> Config:
        """Create configuration from a dictionary."""
        config_dir = config_dir or Path.home() / ".posthumous"

        # Required fields
        if 'node_name' not in data:
            raise ValueError("Config missing required field: node_name")
        if 'secret_key' not in data:
            raise ValueError("Config missing required field: secret_key")

        # Parse timing values
        def get_duration(key: str, default: timedelta) -> timedelta:
            if key in data:
                return parse_duration(data[key])
            return default

        # Parse actions from config
        def parse_actions(actions_data: list[dict] | None) -> list[NotificationAction | ScriptAction]:
            if not actions_data:
                return []

            result = []
            for action in actions_data:
                if 'notify' in action:
                    result.append(NotificationAction(
                        channel=action['notify'],
                        message=action.get('message', '')
                    ))
                elif 'script' in action:
                    result.append(ScriptAction(script=action['script']))
            return result

        # Parse scheduled items
        def parse_scheduled(items_data: list[dict] | None) -> list[ScheduledItem]:
            if not items_data:
                return []

            return [
                ScheduledItem(
                    name=item['name'],
                    when=item['when'],
                    script=item.get('script'),
                    notify=item.get('notify'),
                    message=item.get('message'),
                )
                for item in items_data
            ]

        # Parse notifications
        notifications = {}
        for name, urls in data.get('notifications', {}).items():
            if isinstance(urls, str):
                notifications[name] = [urls]
            else:
                notifications[name] = list(urls)

        # Parse actions from nested structure
        actions_config = data.get('actions', {})

        return cls(
            node_name=data['node_name'],
            secret_key=data['secret_key'],
            listen=data.get('listen', '0.0.0.0:8420'),
            checkin_interval=get_duration('checkin_interval', timedelta(days=7)),
            warning_start=get_duration('warning_start', timedelta(days=8)),
            grace_start=get_duration('grace_start', timedelta(days=12)),
            trigger_at=get_duration('trigger_at', timedelta(days=14)),
            peers=data.get('peers', []),
            notifications=notifications,
            on_warning=parse_actions(actions_config.get('on_warning')),
            on_grace=parse_actions(actions_config.get('on_grace')),
            on_trigger=parse_actions(actions_config.get('on_trigger')),
            post_trigger=parse_scheduled(data.get('post_trigger')),
            api_token=data.get('api_token'),
            max_failed_attempts=data.get('max_failed_attempts', 5),
            lockout_duration=get_duration('lockout_duration', timedelta(minutes=15)),
            check_interval=parse_duration(data['check_interval']) if 'check_interval' in data else None,
            peer_check_interval=get_duration('peer_check_interval', timedelta(minutes=30)),
            peer_down_threshold=get_duration('peer_down_threshold', timedelta(hours=6)),
            base_url=data.get('base_url'),
            encrypt_at_rest=data.get('encrypt_at_rest', False),
            config_dir=config_dir,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert configuration to a dictionary (for saving)."""
        def format_duration(td: timedelta) -> str:
            total_seconds = td.total_seconds()
            # Try each unit from largest to smallest
            for divisor, singular, plural in [
                (86400, "day", "days"),
                (3600, "hour", "hours"),
                (60, "minute", "minutes"),
            ]:
                if total_seconds % divisor == 0:
                    value = int(total_seconds // divisor)
                    unit = singular if value == 1 else plural
                    return f"{value} {unit}"
            return f"{int(total_seconds)} seconds"

        def serialize_actions(actions: list) -> list[dict]:
            result = []
            for action in actions:
                if isinstance(action, NotificationAction):
                    result.append({'notify': action.channel, 'message': action.message})
                elif isinstance(action, ScriptAction):
                    result.append({'script': action.script})
            return result

        def serialize_scheduled(items: list[ScheduledItem]) -> list[dict]:
            result = []
            for item in items:
                d: dict[str, Any] = {'name': item.name, 'when': item.when}
                if item.script:
                    d['script'] = item.script
                if item.notify:
                    d['notify'] = item.notify
                if item.message:
                    d['message'] = item.message
                result.append(d)
            return result

        data: dict[str, Any] = {
            'node_name': self.node_name,
            'secret_key': self.secret_key,
            'listen': self.listen,
            'checkin_interval': format_duration(self.checkin_interval),
            'warning_start': format_duration(self.warning_start),
            'grace_start': format_duration(self.grace_start),
            'trigger_at': format_duration(self.trigger_at),
        }

        if self.base_url:
            data['base_url'] = self.base_url

        if self.peers:
            data['peers'] = self.peers

        if self.notifications:
            data['notifications'] = self.notifications

        actions = {}
        if self.on_warning:
            actions['on_warning'] = serialize_actions(self.on_warning)
        if self.on_grace:
            actions['on_grace'] = serialize_actions(self.on_grace)
        if self.on_trigger:
            actions['on_trigger'] = serialize_actions(self.on_trigger)
        if actions:
            data['actions'] = actions

        if self.post_trigger:
            data['post_trigger'] = serialize_scheduled(self.post_trigger)

        if self.api_token:
            data['api_token'] = self.api_token

        if self.encrypt_at_rest:
            data['encrypt_at_rest'] = True

        return data

    def save(self, path: Path | str | None = None) -> None:
        """Save configuration to a YAML file (atomic write)."""
        path = Path(path) if path else self.config_dir / "config.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)

        # Write to temp file in same directory for atomic rename
        fd, temp_path = tempfile.mkstemp(
            dir=path.parent,
            prefix='.config_',
            suffix='.yaml.tmp',
        )
        try:
            with os.fdopen(fd, 'w') as f:
                yaml.dump(self.to_dict(), f, default_flow_style=False, sort_keys=False)
            os.replace(temp_path, path)
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    def get_base_url(self) -> str:
        """Get the effective base URL for constructing check-in/dashboard links.

        Returns base_url if explicitly set, otherwise auto-constructs from listen address.
        """
        if self.base_url:
            return self.base_url.rstrip('/')
        # Auto-construct from listen, replacing 0.0.0.0 with localhost
        host = self.listen.replace('0.0.0.0', 'localhost')
        return f"http://{host}"

    def get_encryption_key(self) -> bytes | None:
        """Get the encryption key for state files, or None if encryption is disabled."""
        if not self.encrypt_at_rest:
            return None
        from posthumous.crypto import derive_key
        return derive_key(self.secret_key)

    def get_script_path(self, script: str) -> Path:
        """Resolve a script path relative to config directory."""
        script_path = Path(script)
        if script_path.is_absolute():
            return script_path
        return self.config_dir / script

    def validate(self) -> list[str]:
        """Validate configuration and return list of errors."""
        errors = []

        # Check timing makes sense
        if self.warning_start <= self.checkin_interval:
            errors.append("warning_start should be greater than checkin_interval")
        if self.grace_start <= self.warning_start:
            errors.append("grace_start should be greater than warning_start")
        if self.trigger_at <= self.grace_start:
            errors.append("trigger_at should be greater than grace_start")

        # Check base_url format if set
        if self.base_url is not None:
            if not self.base_url.startswith(('http://', 'https://')):
                errors.append("base_url must start with http:// or https://")

        # Check secret key is valid base32
        try:
            import base64
            base64.b32decode(self.secret_key.upper())
        except Exception:
            errors.append("secret_key must be valid base32")

        # Check notification channels referenced in actions exist
        all_channels = set(self.notifications.keys())

        def check_actions(actions: list, event_name: str):
            for action in actions:
                if isinstance(action, NotificationAction):
                    if action.channel not in all_channels:
                        errors.append(
                            f"Action in {event_name} references unknown channel: {action.channel}"
                        )

        check_actions(self.on_warning, 'on_warning')
        check_actions(self.on_grace, 'on_grace')
        check_actions(self.on_trigger, 'on_trigger')

        for item in self.post_trigger:
            if item.notify and item.notify not in all_channels:
                errors.append(
                    f"Scheduled item '{item.name}' references unknown channel: {item.notify}"
                )

        return errors


def generate_default_config(node_name: str, secret_key: str) -> Config:
    """Generate a default configuration with sensible defaults."""
    return Config(
        node_name=node_name,
        secret_key=secret_key,
        notifications={
            'default': ['ntfy://posthumous-default'],
        },
        on_warning=[
            NotificationAction(
                channel='default',
                message='Check-in needed. {days_left} days remaining before trigger.\n\nCheck in: {checkin_url}'
            )
        ],
        on_grace=[
            NotificationAction(
                channel='default',
                message='URGENT: Posthumous triggers in {hours_left} hours.\n\nCheck in: {checkin_url}'
            )
        ],
        on_trigger=[
            NotificationAction(
                channel='default',
                message='Posthumous has activated.\n\nDashboard: {dashboard_url}'
            )
        ],
    )
