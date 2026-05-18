"""HTTP server for check-ins and peer sync."""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from aiohttp import web

from posthumous.auth import Authenticator, AuthError, LockedOutError
from posthumous.state import Status


def _format_duration(td: timedelta) -> str:
    """Format a timedelta into a human-readable string using the most meaningful units."""
    total = int(td.total_seconds())
    if total < 0:
        return "0s"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours > 0:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    if minutes > 0:
        return f"{minutes}m {seconds}s" if seconds else f"{minutes}m"
    return f"{seconds}s"

if TYPE_CHECKING:
    from posthumous.config import Config
    from posthumous.state import StateManager
    from posthumous.watchdog import Watchdog
    from posthumous.peers import PeerManager

logger = logging.getLogger(__name__)


# HTML template for check-in form
CHECKIN_HTML = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Posthumous Check-in</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            max-width: 400px;
            margin: 50px auto;
            padding: 20px;
            background: #1a1a2e;
            color: #eee;
        }}
        h1 {{ color: #00d9ff; text-align: center; }}
        .status {{
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 20px;
            text-align: center;
        }}
        .status.armed {{ background: #1e5128; }}
        .status.warning {{ background: #7d5a00; }}
        .status.pending_quorum {{ background: #6a4a00; }}
        .status.grace {{ background: #8b0000; }}
        .status.triggered {{ background: #4a0000; }}
        form {{
            display: flex;
            flex-direction: column;
            gap: 15px;
        }}
        input[type="text"] {{
            padding: 15px;
            font-size: 24px;
            text-align: center;
            letter-spacing: 8px;
            border: 2px solid #444;
            border-radius: 8px;
            background: #0f0f1a;
            color: #fff;
        }}
        input[type="text"]:focus {{
            outline: none;
            border-color: #00d9ff;
        }}
        button {{
            padding: 15px;
            font-size: 18px;
            background: #00d9ff;
            color: #000;
            border: none;
            border-radius: 8px;
            cursor: pointer;
        }}
        button:hover {{ background: #00b8d9; }}
        .message {{
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 20px;
            text-align: center;
        }}
        .message.success {{ background: #1e5128; }}
        .message.error {{ background: #8b0000; }}
        .node {{ color: #888; text-align: center; font-size: 12px; }}
    </style>
</head>
<body>
    <h1>Posthumous</h1>
    <div class="status {status_class}">
        Status: {status}<br>
        {time_info}
    </div>
    {message}
    {form_html}
    <p class="node">Node: {node_name}</p>
</body>
</html>
"""


DASHBOARD_HTML = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="60">
    <title>Posthumous Dashboard</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            max-width: 700px;
            margin: 30px auto;
            padding: 20px;
            background: #1a1a2e;
            color: #eee;
        }}
        h1 {{ color: #00d9ff; text-align: center; margin-bottom: 5px; }}
        .subtitle {{ color: #888; text-align: center; font-size: 14px; margin-bottom: 25px; }}
        .status-badge {{
            padding: 15px 25px;
            border-radius: 8px;
            text-align: center;
            font-size: 20px;
            font-weight: bold;
            margin-bottom: 20px;
        }}
        .status-badge.armed {{ background: #1e5128; }}
        .status-badge.warning {{ background: #7d5a00; }}
        .status-badge.pending_quorum {{ background: #6a4a00; }}
        .status-badge.grace {{ background: #8b0000; }}
        .status-badge.triggered {{ background: #4a0000; }}
        .section {{
            background: #0f0f1a;
            border: 1px solid #333;
            border-radius: 8px;
            padding: 15px;
            margin-bottom: 15px;
        }}
        .section h2 {{
            color: #00d9ff;
            font-size: 16px;
            margin: 0 0 10px 0;
            border-bottom: 1px solid #333;
            padding-bottom: 8px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
        }}
        td, th {{
            padding: 6px 10px;
            text-align: left;
            border-bottom: 1px solid #222;
        }}
        th {{ color: #888; font-weight: normal; font-size: 13px; }}
        td {{ font-size: 14px; }}
        .ok {{ color: #4caf50; }}
        .warn {{ color: #ff9800; }}
        .err {{ color: #f44336; }}
        .dim {{ color: #666; }}
        .nav {{ text-align: center; margin-bottom: 20px; }}
        .nav a {{
            color: #00d9ff;
            text-decoration: none;
            margin: 0 10px;
            font-size: 14px;
        }}
        .nav a:hover {{ text-decoration: underline; }}
    </style>
</head>
<body>
    <h1>Posthumous</h1>
    <p class="subtitle">Node: {node_name}</p>
    <div class="nav">
        <a href="/checkin">Check In</a> |
        <a href="/dashboard">Dashboard</a> |
        <a href="/status">API Status</a>
    </div>
    <div class="status-badge {status_class}">{status}</div>
    <div class="section">
        <h2>Time Remaining</h2>
        <table>{time_rows}</table>
    </div>
    <div class="section">
        <h2>Recent Check-ins</h2>
        {checkins_content}
    </div>
    <div class="section">
        <h2>Peers</h2>
        {peers_content}
    </div>
    <div class="section">
        <h2>Scheduled Items</h2>
        {schedule_content}
    </div>
    <p class="dim" style="text-align:center;font-size:12px;">Auto-refreshes every 60s</p>
</body>
</html>
"""


class Server:
    """HTTP server for check-ins and peer communication."""

    SYNC_FRESHNESS_SECONDS = 300

    def __init__(
        self,
        config: "Config",
        state_manager: "StateManager",
        watchdog: "Watchdog",
        authenticator: Authenticator,
        peer_manager: "PeerManager | None" = None,
    ):
        self.config = config
        self.state_manager = state_manager
        self.watchdog = watchdog
        self.authenticator = authenticator
        self.peer_manager = peer_manager

        self.app = web.Application()
        self._setup_routes()
        self._runner: web.AppRunner | None = None

    def _setup_routes(self) -> None:
        """Set up HTTP routes."""
        self.app.router.add_get('/', self.handle_index)
        self.app.router.add_get('/checkin', self.handle_checkin_form)
        self.app.router.add_post('/checkin', self.handle_checkin)
        self.app.router.add_get('/dashboard', self.handle_dashboard)
        self.app.router.add_get('/status', self.handle_status)
        self.app.router.add_get('/health', self.handle_health)
        self.app.router.add_get('/favicon.ico', self.handle_favicon)

        # Peer sync endpoints
        self.app.router.add_post('/sync/checkin', self.handle_sync_checkin)
        self.app.router.add_post('/sync/trigger', self.handle_sync_trigger)
        self.app.router.add_post('/sync/trigger_intent', self.handle_sync_trigger_intent)
        self.app.router.add_post('/sync/scheduled', self.handle_sync_scheduled)
        self.app.router.add_get('/sync/state', self.handle_sync_state)

    _TRIGGERED_HTML = '<div class="message error">Node is triggered. Check-in is no longer possible.</div>'

    _LOCKOUT_HTML = '<div class="message error">Account locked. Try again later.</div>'

    def _generate_csrf_token(self) -> str:
        """Generate a CSRF token for double-submit cookie protection."""
        return secrets.token_hex(32)

    def _get_form_html(self, csrf_token: str = "") -> str:
        """Get the appropriate form HTML based on current state."""
        state = self.state_manager.state
        if state.status == Status.TRIGGERED:
            return self._TRIGGERED_HTML
        if state.is_locked_out():
            return self._LOCKOUT_HTML
        return (
            '<form method="POST" action="/checkin">'
            f'<input type="hidden" name="csrf_token" value="{csrf_token}">'
            '<input type="text" name="totp" placeholder="000000" maxlength="6" '
            'pattern="[0-9]{{6}}" autocomplete="off" autofocus>'
            '<button type="submit">Check In</button>'
            '</form>'
        )

    def _get_time_info(self) -> str:
        """Get time info string for display."""
        tr = self.watchdog.get_time_remaining()

        if tr.since_checkin is None:
            return "No check-ins yet"

        since = _format_duration(tr.since_checkin)

        if tr.until_trigger:
            trigger = _format_duration(tr.until_trigger)
            return f"Last check-in: {since} ago<br>Trigger in: {trigger}"

        return f"Last check-in: {since} ago"

    def _self_url(self) -> str:
        """Return the canonical URL used to identify this node in confirmations."""
        return self.config.self_url()

    def _check_timestamp_freshness(self, timestamp_str: str) -> str | None:
        """Return an error string if the timestamp is malformed or outside the freshness window."""
        try:
            msg_time = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            return "Malformed timestamp"
        if abs((datetime.now(timezone.utc) - msg_time).total_seconds()) > self.SYNC_FRESHNESS_SECONDS:
            return "Stale timestamp"
        return None

    def _verify_sync_message(
        self, timestamp_str: str, signature: str, message_prefix: str
    ) -> str | None:
        """Verify freshness and HMAC signature of a sync message.

        Returns None if valid, or an error string if invalid.
        """
        error = self._check_timestamp_freshness(timestamp_str)
        if error:
            return error

        from posthumous.auth import verify_signature
        if not verify_signature(
            self.config.secret_key, f"{message_prefix}:{timestamp_str}", signature
        ):
            return "Invalid signature"

        return None

    async def handle_index(self, request: web.Request) -> web.Response:
        """Redirect to check-in form."""
        raise web.HTTPFound('/checkin')

    async def handle_checkin_form(self, request: web.Request) -> web.Response:
        """Render check-in form."""
        csrf_token = self._generate_csrf_token()
        status = self.state_manager.state.status.value
        html = CHECKIN_HTML.format(
            status=status.upper(),
            status_class=status,
            time_info=self._get_time_info(),
            node_name=self.config.node_name,
            message="",
            form_html=self._get_form_html(csrf_token),
        )
        response = web.Response(text=html, content_type='text/html')
        response.set_cookie('csrf_token', csrf_token, httponly=True, samesite='Strict')
        return response

    async def handle_dashboard(self, request: web.Request) -> web.Response:
        """Render the dashboard page."""
        state = self.state_manager.state
        status = state.status.value
        tr = self.watchdog.get_time_remaining()

        # Build time remaining rows
        time_rows = ""
        if tr.since_checkin:
            time_rows += f"<tr><th>Since check-in</th><td>{_format_duration(tr.since_checkin)}</td></tr>"
        else:
            time_rows += '<tr><th>Since check-in</th><td class="dim">Never</td></tr>'

        if tr.until_warning:
            time_rows += f'<tr><th>Until warning</th><td class="ok">{_format_duration(tr.until_warning)}</td></tr>'
        elif state.status in (Status.WARNING, Status.GRACE, Status.TRIGGERED):
            time_rows += '<tr><th>Warning</th><td class="warn">Active</td></tr>'

        if tr.until_grace:
            time_rows += f'<tr><th>Until grace</th><td class="warn">{_format_duration(tr.until_grace)}</td></tr>'
        elif state.status in (Status.GRACE, Status.TRIGGERED):
            time_rows += '<tr><th>Grace period</th><td class="err">Active</td></tr>'

        if tr.until_trigger:
            time_rows += f'<tr><th>Until trigger</th><td class="err">{_format_duration(tr.until_trigger)}</td></tr>'
        elif state.status == Status.TRIGGERED:
            time_rows += '<tr><th>Trigger</th><td class="err">Activated</td></tr>'

        if tr.is_overdue and state.status != Status.ARMED:
            time_rows += '<tr><th>Status</th><td class="err">OVERDUE</td></tr>'

        if state.trigger_time:
            time_rows += f"<tr><th>Triggered at</th><td>{state.trigger_time.strftime('%Y-%m-%d %H:%M UTC')}</td></tr>"

        # Build check-in history
        if state.last_checkin:
            checkins_content = (
                f"<table>"
                f"<tr><th>Last check-in</th><td>{state.last_checkin.strftime('%Y-%m-%d %H:%M UTC')}</td></tr>"
                f"</table>"
            )
        else:
            checkins_content = '<p class="dim">No check-ins recorded</p>'

        # Build peer status
        if self.config.peers:
            peer_rows = ""
            for url in self.config.peers:
                peer_state = state.peer_states.get(url)
                if peer_state and peer_state.last_seen:
                    age = (datetime.now(timezone.utc) - peer_state.last_seen)
                    minutes = int(age.total_seconds() // 60)
                    css = "ok" if peer_state.consecutive_failures == 0 else "warn"
                    peer_rows += f'<tr><td>{url}</td><td class="{css}">seen {minutes}m ago</td></tr>'
                elif peer_state and peer_state.last_error:
                    peer_rows += f'<tr><td>{url}</td><td class="err">{peer_state.last_error}</td></tr>'
                else:
                    peer_rows += f'<tr><td>{url}</td><td class="dim">unknown</td></tr>'
            peers_content = f"<table>{peer_rows}</table>"
        else:
            peers_content = '<p class="dim">No peers configured</p>'

        # Build schedule info
        if self.config.post_trigger:
            sched_rows = ""
            for item in self.config.post_trigger:
                item_state = state.schedule_state.get(item.name)
                if item_state and item_state.last_run:
                    last = item_state.last_run.strftime('%Y-%m-%d %H:%M')
                    sched_rows += f"<tr><td>{item.name}</td><td>{item.when}</td><td>{last}</td></tr>"
                else:
                    sched_rows += f'<tr><td>{item.name}</td><td>{item.when}</td><td class="dim">never</td></tr>'
            schedule_content = f"<table><tr><th>Name</th><th>When</th><th>Last Run</th></tr>{sched_rows}</table>"
        else:
            schedule_content = '<p class="dim">No post-trigger items configured</p>'

        html = DASHBOARD_HTML.format(
            node_name=self.config.node_name,
            status=status.upper(),
            status_class=status,
            time_rows=time_rows,
            checkins_content=checkins_content,
            peers_content=peers_content,
            schedule_content=schedule_content,
        )
        return web.Response(text=html, content_type='text/html')

    async def handle_checkin(self, request: web.Request) -> web.Response:
        """Handle check-in request (form or API)."""
        content_type = request.content_type

        # Parse input
        totp_code = None
        api_token = None

        if content_type == 'application/json':
            try:
                data = await request.json()
                totp_code = data.get('totp')
                api_token = data.get('token')
            except json.JSONDecodeError:
                return web.json_response(
                    {'success': False, 'error': 'Invalid JSON'},
                    status=400,
                )
        else:
            data = await request.post()
            totp_code = data.get('totp')
            api_token = data.get('token')

            # CSRF double-submit cookie validation for form submissions
            csrf_cookie = request.cookies.get('csrf_token', '')
            csrf_field = data.get('csrf_token', '')
            if not csrf_cookie or not csrf_field or csrf_cookie != csrf_field:
                return web.Response(
                    text='CSRF validation failed',
                    status=403,
                    content_type='text/plain',
                )

        # Get client identifier for rate limiting
        client_ip = request.remote or "unknown"

        # Authenticate
        try:
            success = self.authenticator.verify(
                code=totp_code,
                token=api_token,
                state=self.state_manager.state,
                source=client_ip,
            )
        except LockedOutError as e:
            self.state_manager.save()
            if content_type == 'application/json':
                return web.json_response(
                    {'success': False, 'error': str(e)},
                    status=429,
                )
            html = CHECKIN_HTML.format(
                status=self.state_manager.state.status.value.upper(),
                status_class=self.state_manager.state.status.value,
                time_info=self._get_time_info(),
                node_name=self.config.node_name,
                message=f'<div class="message error">{e}</div>',
                form_html=self._LOCKOUT_HTML,
            )
            return web.Response(text=html, content_type='text/html')
        except AuthError as e:
            if content_type == 'application/json':
                return web.json_response(
                    {'success': False, 'error': str(e)},
                    status=400,
                )
            html = CHECKIN_HTML.format(
                status=self.state_manager.state.status.value.upper(),
                status_class=self.state_manager.state.status.value,
                time_info=self._get_time_info(),
                node_name=self.config.node_name,
                message=f'<div class="message error">{e}</div>',
                form_html=self._get_form_html(),
            )
            return web.Response(text=html, content_type='text/html')

        if not success:
            self.state_manager.save()
            logger.warning(f"Failed check-in attempt from {client_ip}")
            if content_type == 'application/json':
                return web.json_response(
                    {'success': False, 'error': 'Invalid code'},
                    status=401,
                )
            html = CHECKIN_HTML.format(
                status=self.state_manager.state.status.value.upper(),
                status_class=self.state_manager.state.status.value,
                time_info=self._get_time_info(),
                node_name=self.config.node_name,
                message='<div class="message error">Invalid code. Please try again.</div>',
                form_html=self._get_form_html(),
            )
            return web.Response(text=html, content_type='text/html')

        # Process check-in
        checkin_time = datetime.now(timezone.utc)

        if not self.watchdog.checkin(checkin_time):
            if content_type == 'application/json':
                return web.json_response(
                    {'success': False, 'error': 'Node is triggered'},
                    status=409,
                )
            html = CHECKIN_HTML.format(
                status=self.state_manager.state.status.value.upper(),
                status_class=self.state_manager.state.status.value,
                time_info=self._get_time_info(),
                node_name=self.config.node_name,
                message='<div class="message error">Node is already triggered. Check-in rejected.</div>',
                form_html=self._TRIGGERED_HTML,
            )
            return web.Response(text=html, content_type='text/html')

        # Broadcast to peers
        if self.peer_manager:
            await self.peer_manager.broadcast_checkin(checkin_time)

        logger.info(f"Check-in accepted from {client_ip}")

        if content_type == 'application/json':
            tr = self.watchdog.get_time_remaining()
            return web.json_response({
                'success': True,
                'status': self.state_manager.state.status.value,
                'next_deadline': (checkin_time + self.config.trigger_at).isoformat(),
            })

        html = CHECKIN_HTML.format(
            status=self.state_manager.state.status.value.upper(),
            status_class=self.state_manager.state.status.value,
            time_info=self._get_time_info(),
            node_name=self.config.node_name,
            message='<div class="message success">Check-in successful!</div>',
            form_html=self._get_form_html(),
        )
        return web.Response(text=html, content_type='text/html')

    async def handle_status(self, request: web.Request) -> web.Response:
        """Return current status as JSON."""
        state = self.state_manager.state
        tr = self.watchdog.get_time_remaining()

        return web.json_response({
            'node_name': self.config.node_name,
            'status': state.status.value,
            'last_checkin': state.last_checkin.isoformat() if state.last_checkin else None,
            'trigger_time': state.trigger_time.isoformat() if state.trigger_time else None,
            'time_remaining': {
                'until_warning': tr.until_warning.total_seconds() if tr.until_warning else None,
                'until_grace': tr.until_grace.total_seconds() if tr.until_grace else None,
                'until_trigger': tr.until_trigger.total_seconds() if tr.until_trigger else None,
            },
        })

    async def handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        return web.json_response({'status': 'ok', 'node': self.config.node_name})

    async def handle_favicon(self, request: web.Request) -> web.Response:
        """Return an inline SVG favicon."""
        svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y=".9em" font-size="90">\U0001f480</text></svg>'
        return web.Response(
            body=svg.encode(),
            content_type='image/svg+xml',
            headers={'Cache-Control': 'public, max-age=86400'},
        )

    # Peer sync handlers

    async def handle_sync_checkin(self, request: web.Request) -> web.Response:
        """Handle check-in broadcast from peer."""
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({'error': 'Invalid JSON'}, status=400)

        timestamp = data.get('timestamp')
        signature = data.get('signature')

        if not timestamp or not signature:
            return web.json_response({'error': 'Missing fields'}, status=400)

        error = self._verify_sync_message(timestamp, signature, "checkin")
        if error:
            logger.warning(f"Sync checkin rejected: {error}")
            return web.json_response({'error': error}, status=401)

        # Process check-in (defense in depth; _verify_sync_message already parsed it)
        try:
            checkin_time = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            return web.json_response({'error': 'Malformed timestamp'}, status=400)
        self.watchdog.checkin(checkin_time)
        logger.info(f"Processed sync checkin from peer")

        return web.json_response({'success': True})

    async def handle_sync_trigger(self, request: web.Request) -> web.Response:
        """Handle trigger broadcast from peer."""
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({'error': 'Invalid JSON'}, status=400)

        timestamp = data.get('timestamp')
        signature = data.get('signature')

        if not timestamp or not signature:
            return web.json_response({'error': 'Missing fields'}, status=400)

        error = self._verify_sync_message(timestamp, signature, "trigger")
        if error:
            logger.warning(f"Sync trigger rejected: {error}")
            return web.json_response({'error': error}, status=401)

        # v0.7: if quorum is configured locally, require a valid bundle.
        # Even if quorum is not configured, a bundle in the payload is verified
        # if present (defense in depth).
        intent_id = data.get("intent_id")
        intent_timestamp = data.get("intent_timestamp")
        bundle_raw = data.get("confirmations")

        quorum_required = self.config.quorum is not None

        if quorum_required and bundle_raw is None:
            logger.warning("Quorum configured but trigger arrived without bundle")
            return web.json_response({"error": "Quorum bundle required"}, status=401)

        if bundle_raw is not None:
            from posthumous.quorum import Confirmation, QuorumCoordinator
            bundle = [
                Confirmation(peer_url=b["peer_url"], signature=b["signature"])
                for b in bundle_raw
            ]

            # Build set of allowed voter URLs: configured peers plus self.
            self_url = self._self_url()
            allowed = set(self.config.peers) | {self_url}

            coordinator = QuorumCoordinator(self.config, self.state_manager, self.peer_manager)
            if not coordinator.verify_confirmation_bundle(
                bundle,
                intent_id,
                intent_timestamp,
                allowed_peer_urls=allowed,
                max_age_seconds=self.SYNC_FRESHNESS_SECONDS,
            ):
                logger.warning("Quorum bundle verification failed")
                return web.json_response({"error": "Invalid quorum bundle"}, status=401)

        # Mark as triggered with the peer's timestamp (defense in depth)
        from posthumous.state import Status
        try:
            peer_time = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            return web.json_response({'error': 'Malformed timestamp'}, status=400)
        self.state_manager.transition(Status.TRIGGERED, trigger_time=peer_time)
        logger.info("Received trigger broadcast from peer")

        return web.json_response({'success': True})

    async def handle_sync_trigger_intent(self, request: web.Request) -> web.Response:
        """Receive a trigger intent broadcast and respond with a vote."""
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        intent_id = data.get("intent_id")
        timestamp = data.get("timestamp")
        signature = data.get("signature")
        if not intent_id or not timestamp or not signature:
            return web.json_response({"error": "Missing fields"}, status=400)

        from posthumous.quorum import verify_intent, sign_confirmation
        freshness_error = self._check_timestamp_freshness(timestamp)
        if freshness_error:
            status = 400 if freshness_error == "Malformed timestamp" else 401
            return web.json_response({"error": freshness_error}, status=status)
        if not verify_intent(self.config.secret_key, intent_id, timestamp, signature):
            return web.json_response({"error": "Invalid signature"}, status=401)

        # Decide our vote based on our own state. If our calculated status
        # would be TRIGGERED right now, we confirm.
        calculated = self.watchdog.calculate_status()
        peer_url = self._self_url()

        if calculated == Status.TRIGGERED:
            sig = sign_confirmation(self.config.secret_key, intent_id, timestamp, peer_url)
            return web.json_response(
                {
                    "intent_id": intent_id,
                    "vote": "confirm",
                    "peer_url": peer_url,
                    "signature": sig,
                },
                status=200,
            )

        last_checkin = self.state_manager.state.last_checkin
        return web.json_response(
            {
                "intent_id": intent_id,
                "vote": "reject",
                "peer_url": peer_url,
                "last_checkin": last_checkin.isoformat() if last_checkin else None,
            },
            status=409,
        )

    async def handle_sync_scheduled(self, request: web.Request) -> web.Response:
        """Handle scheduled item completion broadcast from peer."""
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({'error': 'Invalid JSON'}, status=400)

        item_name = data.get('item')
        period = data.get('period')
        timestamp = data.get('timestamp')
        signature = data.get('signature')

        if not item_name or not period or not signature:
            return web.json_response({'error': 'Missing fields'}, status=400)

        if timestamp:
            error = self._verify_sync_message(
                timestamp, signature, f"scheduled:{item_name}:{period}"
            )
            if error:
                logger.warning(f"Sync scheduled rejected: {error}")
                return web.json_response({'error': error}, status=401)
        else:
            # Legacy: no timestamp, verify signature only
            from posthumous.auth import verify_signature
            message = f"scheduled:{item_name}:{period}"
            if not verify_signature(self.config.secret_key, message, signature):
                logger.warning("Invalid signature on sync scheduled")
                return web.json_response({'error': 'Invalid signature'}, status=401)

        # Mark item as complete
        self.state_manager.state.mark_schedule_item_run(item_name, period)
        self.state_manager.save()
        logger.info(f"Marked scheduled item '{item_name}' as complete (from peer)")

        return web.json_response({'success': True})

    async def handle_sync_state(self, request: web.Request) -> web.Response:
        """Return current state for peer sync (authenticated)."""
        ts = request.query.get('ts')
        sig = request.query.get('sig')

        if not ts or not sig:
            return web.json_response({'error': 'Authentication required'}, status=401)

        error = self._verify_sync_message(ts, sig, "state")
        if error:
            logger.warning(f"Sync state request rejected: {error}")
            return web.json_response({'error': error}, status=401)

        state = self.state_manager.state

        return web.json_response({
            'node_name': self.config.node_name,
            'status': state.status.value,
            'last_checkin': state.last_checkin.isoformat() if state.last_checkin else None,
            'trigger_time': state.trigger_time.isoformat() if state.trigger_time else None,
            'schedule_state': {
                name: {
                    'last_run': item.last_run.isoformat() if item.last_run else None,
                    'period': item.period,
                }
                for name, item in state.schedule_state.items()
            },
        })

    async def start(self) -> None:
        """Start the HTTP server."""
        host, port = self.config.listen.rsplit(':', 1)
        port = int(port)

        self._runner = web.AppRunner(self.app)
        await self._runner.setup()

        site = web.TCPSite(self._runner, host, port)
        await site.start()

        logger.info(f"Server started on {self.config.listen}")

    async def stop(self) -> None:
        """Stop the HTTP server."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        logger.info("Server stopped")
