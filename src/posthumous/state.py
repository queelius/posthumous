"""State persistence for Posthumous with atomic writes."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import yaml


def parse_datetime(value: str | datetime | None) -> datetime | None:
    """Parse a datetime from string or pass through existing datetime.

    Handles ISO format strings, including those with 'Z' suffix.
    Ensures all datetimes have UTC timezone.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def format_datetime(dt: datetime | None) -> str | None:
    """Format a datetime for serialization, or return None."""
    return dt.isoformat() if dt else None


class Status(Enum):
    """Current status of the deadman switch."""
    ARMED = "armed"
    WARNING = "warning"
    GRACE = "grace"
    PENDING_QUORUM = "pending_quorum"
    TRIGGERED = "triggered"


@dataclass
class ScheduleItemState:
    """State for a scheduled item (for deduplication)."""
    last_run: datetime | None = None
    period: str | None = None  # Dedup key like "2026" or "2026-W05"


@dataclass
class FailedAttempt:
    """Record of a failed authentication attempt."""
    timestamp: datetime
    source: str  # IP or identifier


@dataclass
class PeerState:
    """State for a peer node."""
    url: str
    last_seen: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    # Timestamp of the most recent "peer down" alert. Persisted so the
    # guard against repeat-alerting survives daemon restarts. Cleared
    # automatically once `last_seen` moves forward past it.
    alerted_at: datetime | None = None


@dataclass
class State:
    """Runtime state for Posthumous."""

    # Core state
    last_checkin: datetime | None = None
    status: Status = Status.ARMED
    trigger_time: datetime | None = None

    # Schedule tracking
    schedule_state: dict[str, ScheduleItemState] = field(default_factory=dict)

    # Security
    failed_attempts: list[FailedAttempt] = field(default_factory=list)
    lockout_until: datetime | None = None

    # Peer tracking
    peer_states: dict[str, PeerState] = field(default_factory=dict)

    # Metadata
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_modified: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def get_default_state_path(cls, config_dir: Path | None = None) -> Path:
        """Get the default state file path."""
        if config_dir is None:
            config_dir = Path.home() / ".posthumous"
        return config_dir / "state.yaml"

    @classmethod
    def load(cls, path: Path | str, encryption_secret: str | None = None) -> State:
        """Load state from a YAML file.

        If encryption_secret is provided, transparently decrypts encrypted files.
        Plaintext files are read normally (migration support).
        """
        path = Path(path)

        if not path.exists():
            return cls()

        try:
            if encryption_secret:
                from posthumous.crypto import decrypt_file
                content = decrypt_file(path, encryption_secret)
            else:
                content = path.read_text()
            data = yaml.safe_load(content) or {}
            return cls.from_dict(data)
        except (yaml.YAMLError, KeyError, ValueError) as e:
            # Corrupt state file - return fresh state
            # The caller should handle syncing from peers if available
            raise StateCorruptError(f"Failed to load state from {path}: {e}")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> State:
        """Create state from a dictionary."""
        # Parse schedule state
        schedule_state = {}
        for name, item_data in data.get('schedule_state', {}).items():
            if isinstance(item_data, dict):
                schedule_state[name] = ScheduleItemState(
                    last_run=parse_datetime(item_data.get('last_run')),
                    period=item_data.get('period'),
                )

        # Parse failed attempts
        failed_attempts = []
        for attempt_data in data.get('failed_attempts', []):
            if isinstance(attempt_data, dict):
                failed_attempts.append(FailedAttempt(
                    timestamp=parse_datetime(attempt_data['timestamp']) or datetime.now(timezone.utc),
                    source=attempt_data.get('source', 'unknown'),
                ))

        # Parse peer states
        peer_states = {}
        for url, peer_data in data.get('peer_states', {}).items():
            if isinstance(peer_data, dict):
                peer_states[url] = PeerState(
                    url=url,
                    last_seen=parse_datetime(peer_data.get('last_seen')),
                    last_error=peer_data.get('last_error'),
                    consecutive_failures=peer_data.get('consecutive_failures', 0),
                    alerted_at=parse_datetime(peer_data.get('alerted_at')),
                )

        status_str = data.get('status', 'armed')
        try:
            status = Status(status_str)
        except ValueError:
            status = Status.ARMED

        return cls(
            last_checkin=parse_datetime(data.get('last_checkin')),
            status=status,
            trigger_time=parse_datetime(data.get('trigger_time')),
            schedule_state=schedule_state,
            failed_attempts=failed_attempts,
            lockout_until=parse_datetime(data.get('lockout_until')),
            peer_states=peer_states,
            created_at=parse_datetime(data.get('created_at')) or datetime.now(timezone.utc),
            last_modified=parse_datetime(data.get('last_modified')) or datetime.now(timezone.utc),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert state to a dictionary for serialization."""
        # Serialize schedule state
        schedule_state = {}
        for name, item in self.schedule_state.items():
            schedule_state[name] = {
                'last_run': format_datetime(item.last_run),
                'period': item.period,
            }

        # Serialize failed attempts (keep only recent ones)
        failed_attempts = [
            {'timestamp': format_datetime(a.timestamp), 'source': a.source}
            for a in self.failed_attempts[-100:]  # Keep last 100
        ]

        # Serialize peer states
        peer_states = {}
        for url, peer in self.peer_states.items():
            peer_states[url] = {
                'last_seen': format_datetime(peer.last_seen),
                'last_error': peer.last_error,
                'consecutive_failures': peer.consecutive_failures,
                'alerted_at': format_datetime(peer.alerted_at),
            }

        return {
            'last_checkin': format_datetime(self.last_checkin),
            'status': self.status.value,
            'trigger_time': format_datetime(self.trigger_time),
            'schedule_state': schedule_state,
            'failed_attempts': failed_attempts,
            'lockout_until': format_datetime(self.lockout_until),
            'peer_states': peer_states,
            'created_at': format_datetime(self.created_at),
            'last_modified': format_datetime(datetime.now(timezone.utc)),
        }

    def save(self, path: Path | str, encryption_secret: str | None = None) -> None:
        """Save state atomically to a YAML file.

        Uses write-to-temp-file + rename pattern for atomicity.
        If encryption_secret is provided, encrypts the content before writing.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        self.last_modified = datetime.now(timezone.utc)
        data = self.to_dict()
        content = yaml.dump(data, default_flow_style=False, sort_keys=False)

        if encryption_secret:
            from posthumous.crypto import save_encrypted
            save_encrypted(path, content, encryption_secret)
        else:
            # Write to temp file in same directory (for atomic rename)
            fd, temp_path = tempfile.mkstemp(
                dir=path.parent,
                prefix='.state_',
                suffix='.yaml.tmp'
            )
            try:
                with os.fdopen(fd, 'w') as f:
                    f.write(content)
                # Atomic rename
                os.replace(temp_path, path)
                os.chmod(path, 0o600)
            except Exception:
                # Clean up temp file on error
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
                raise

    def record_checkin(self, timestamp: datetime | None = None) -> None:
        """Record a successful check-in."""
        self.last_checkin = timestamp or datetime.now(timezone.utc)
        self.status = Status.ARMED
        # Clear failed attempts on successful checkin
        self.failed_attempts = []
        self.lockout_until = None

    def transition_to(self, new_status: Status, trigger_time: datetime | None = None) -> bool:
        """Transition to a new status if valid.

        Args:
            new_status: The target status.
            trigger_time: Optional explicit trigger timestamp (e.g. from a peer).
                          Used only for TRIGGERED transitions. Defaults to now().

        Returns True if transition occurred, False if no change needed.
        """
        if self.status == new_status:
            return False

        # Valid transitions
        valid_transitions = {
            Status.ARMED: {Status.WARNING},
            Status.WARNING: {Status.ARMED, Status.GRACE},
            Status.GRACE: {Status.ARMED, Status.PENDING_QUORUM, Status.TRIGGERED},
            Status.PENDING_QUORUM: {Status.ARMED, Status.GRACE, Status.TRIGGERED},
            Status.TRIGGERED: set(),  # No transitions out of TRIGGERED
        }

        # Check-in can always reset to ARMED (except from TRIGGERED)
        if new_status == Status.ARMED and self.status != Status.TRIGGERED:
            self.status = Status.ARMED
            return True

        if new_status in valid_transitions.get(self.status, set()):
            self.status = new_status
            if new_status == Status.TRIGGERED:
                self.trigger_time = trigger_time or datetime.now(timezone.utc)
            return True

        return False

    def record_failed_attempt(self, source: str = "unknown") -> None:
        """Record a failed authentication attempt."""
        self.failed_attempts.append(FailedAttempt(
            timestamp=datetime.now(timezone.utc),
            source=source,
        ))

    def is_locked_out(self) -> bool:
        """Check if authentication is currently locked out."""
        if self.lockout_until is None:
            return False
        return datetime.now(timezone.utc) < self.lockout_until

    def check_and_set_lockout(self, max_attempts: int, lockout_duration) -> bool:
        """Check recent failed attempts and set lockout if needed.

        Returns True if lockout was set.
        """
        now = datetime.now(timezone.utc)
        # Count attempts in the lockout window
        cutoff = now - lockout_duration
        recent_attempts = [a for a in self.failed_attempts if a.timestamp > cutoff]

        if len(recent_attempts) >= max_attempts:
            self.lockout_until = now + lockout_duration
            return True
        return False

    def mark_schedule_item_run(self, name: str, period: str) -> None:
        """Mark a scheduled item as having run for a given period."""
        self.schedule_state[name] = ScheduleItemState(
            last_run=datetime.now(timezone.utc),
            period=period,
        )

    def should_run_schedule_item(self, name: str, period: str) -> bool:
        """Check if a scheduled item should run for the given period."""
        if name not in self.schedule_state:
            return True
        item = self.schedule_state[name]
        return item.period != period

    def update_peer(
        self,
        url: str,
        success: bool,
        error: str | None = None,
    ) -> None:
        """Update state for a peer after communication attempt."""
        if url not in self.peer_states:
            self.peer_states[url] = PeerState(url=url)

        peer = self.peer_states[url]
        if success:
            peer.last_seen = datetime.now(timezone.utc)
            peer.last_error = None
            peer.consecutive_failures = 0
        else:
            peer.last_error = error
            peer.consecutive_failures += 1


class StateCorruptError(Exception):
    """Raised when state file is corrupt and cannot be loaded."""
    pass


class StateManager:
    """Manages state persistence with automatic saving."""

    def __init__(self, path: Path | str, encryption_secret: str | None = None):
        self.path = Path(path)
        self.encryption_secret = encryption_secret
        self._state: State | None = None

    @property
    def state(self) -> State:
        """Get the current state, loading if necessary."""
        if self._state is None:
            try:
                self._state = State.load(self.path, self.encryption_secret)
            except StateCorruptError:
                self._state = State()
        return self._state

    def save(self) -> None:
        """Save the current state."""
        if self._state is not None:
            self._state.save(self.path, self.encryption_secret)

    def checkin(self, timestamp: datetime | None = None) -> None:
        """Record a check-in and save state."""
        self.state.record_checkin(timestamp)
        self.save()

    def transition(self, new_status: Status, trigger_time: datetime | None = None) -> bool:
        """Transition to new status and save if changed."""
        changed = self.state.transition_to(new_status, trigger_time=trigger_time)
        if changed:
            self.save()
        return changed

    def reset(self) -> None:
        """Reset to fresh state (for testing or recovery)."""
        self._state = State()
        self.save()
