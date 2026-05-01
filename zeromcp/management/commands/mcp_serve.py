"""Run an MCP server over stdio for desktop agents (Claude Desktop, Cursor).

Reads newline-delimited JSON-RPC messages from stdin, dispatches via
``handle_rpc``, writes responses to stdout. Tool calls go through the
same ``BaseResource.dispatch`` as the REST API — auth is provided by the
``MCP_API_KEY`` environment variable.

Example::

    MCP_API_KEY="<key>" python manage.py mcp_serve myapp.urls.endpoints
"""
import asyncio
import importlib
import json
import logging
import os
import sys

from django.core.management.base import BaseCommand, CommandError


logger = logging.getLogger('zeromcp.mcp.stdio')


class Command(BaseCommand):
    help = 'Run an MCP stdio server exposing the given endpoints registry.'

    def add_arguments(self, parser):
        parser.add_argument(
            'endpoints',
            help='Dotted path to the endpoints dict (e.g. myapp.urls.endpoints).',
        )

    def handle(self, *args, endpoints, **opts):
        registry = self._import(endpoints)
        from zeromcp.mcp.tools import list_tools
        from zeromcp.mcp.protocol import handle_rpc

        tools = list_tools(registry)
        ctx = {
            'api_key': os.environ.get('MCP_API_KEY'),
            'cookie': None,
            'user': None,
            'account': None,
        }

        if not ctx['api_key']:
            self.stderr.write(self.style.WARNING(
                'MCP_API_KEY not set — tool calls will be unauthenticated.'
            ))

        asyncio.run(self._loop(tools, ctx, handle_rpc))

    async def _loop(self, tools, ctx, handle_rpc):
        loop = asyncio.get_event_loop()
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                self._write({
                    'jsonrpc': '2.0', 'id': None,
                    'error': {'code': -32700, 'message': f'Parse error: {exc}'},
                })
                continue

            try:
                response = await handle_rpc(message, tools, ctx)
            except Exception as exc:
                logger.exception('mcp.stdio.error')
                response = {
                    'jsonrpc': '2.0', 'id': message.get('id'),
                    'error': {'code': -32603, 'message': str(exc)},
                }

            if response is not None:
                self._write(response)

    def _write(self, payload):
        sys.stdout.write(json.dumps(payload, default=str, ensure_ascii=False) + '\n')
        sys.stdout.flush()

    def _import(self, dotted):
        try:
            module_path, attr = dotted.rsplit('.', 1)
        except ValueError:
            raise CommandError(f'Invalid dotted path: {dotted}')
        try:
            module = importlib.import_module(module_path)
        except ImportError as exc:
            raise CommandError(f'Cannot import {module_path}: {exc}')
        try:
            return getattr(module, attr)
        except AttributeError:
            raise CommandError(f'{module_path} has no attribute {attr}')
