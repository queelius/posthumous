"""HTTP server for check-ins and peer sync."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from aiohttp import web

from posthumous.auth import Authenticator, AuthError, LockedOutError

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
    <form method="POST" action="/checkin">
        <input type="text" name="totp" placeholder="000000" maxlength="6" pattern="[0-9]{{6}}" autocomplete="off" autofocus>
        <button type="submit">Check In</button>
    </form>
    <p class="node">Node: {node_name}</p>
</body>
</html>
"""


class Server:
    """HTTP server for check-ins and peer communication."""

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
        self.app.router.add_get('/status', self.handle_status)
        self.app.router.add_get('/health', self.handle_health)

        # Peer sync endpoints
        self.app.router.add_post('/sync/checkin', self.handle_sync_checkin)
        self.app.router.add_post('/sync/trigger', self.handle_sync_trigger)
        self.app.router.add_post('/sync/scheduled', self.handle_sync_scheduled)
        self.app.router.add_get('/sync/state', self.handle_sync_state)

    def _get_time_info(self) -> str:
        """Get time info string for display."""
        tr = self.watchdog.get_time_remaining()

        if tr.since_checkin is None:
            return "No check-ins yet"

        days = tr.since_checkin.days
        hours = int(tr.since_checkin.total_seconds() // 3600) % 24

        if tr.until_trigger:
            trigger_hours = int(tr.until_trigger.total_seconds() // 3600)
            return f"Last check-in: {days}d {hours}h ago<br>Trigger in: {trigger_hours}h"

        return f"Last check-in: {days}d {hours}h ago"

    async def handle_index(self, request: web.Request) -> web.Response:
        """Redirect to check-in form."""
        raise web.HTTPFound('/checkin')

    async def handle_checkin_form(self, request: web.Request) -> web.Response:
        """Render check-in form."""
        status = self.state_manager.state.status.value
        html = CHECKIN_HTML.format(
            status=status.upper(),
            status_class=status,
            time_info=self._get_time_info(),
            node_name=self.config.node_name,
            message="",
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

    # Peer sync handlers

    async def handle_sync_checkin(self, request: web.Request) -> web.Response:
        """Handle check-in broadcast from peer."""
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({'error': 'Invalid JSON'}, status=400)

        # Verify signature
        timestamp = data.get('timestamp')
        signature = data.get('signature')

        if not timestamp or not signature:
            return web.json_response({'error': 'Missing fields'}, status=400)

        from posthumous.auth import verify_signature
        message = f"checkin:{timestamp}"
        if not verify_signature(self.config.secret_key, message, signature):
            logger.warning("Invalid signature on sync checkin")
            return web.json_response({'error': 'Invalid signature'}, status=401)

        # Process check-in
        checkin_time = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
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

        from posthumous.auth import verify_signature
        message = f"trigger:{timestamp}"
        if not verify_signature(self.config.secret_key, message, signature):
            logger.warning("Invalid signature on sync trigger")
            return web.json_response({'error': 'Invalid signature'}, status=401)

        # Mark as triggered
        from posthumous.state import Status
        self.state_manager.transition(Status.TRIGGERED)
        logger.info("Received trigger broadcast from peer")

        return web.json_response({'success': True})

    async def handle_sync_scheduled(self, request: web.Request) -> web.Response:
        """Handle scheduled item completion broadcast from peer."""
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({'error': 'Invalid JSON'}, status=400)

        item_name = data.get('item')
        period = data.get('period')
        signature = data.get('signature')

        if not item_name or not period or not signature:
            return web.json_response({'error': 'Missing fields'}, status=400)

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
        """Return current state for peer sync."""
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
