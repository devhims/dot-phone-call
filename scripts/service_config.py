"""Build service commands from this checkout; no launchd or network operations."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

MIN_NODE_MAJOR = 24  # The backend uses node:sqlite; older Node fails at every login.


def node_major(node):
    """Major version of the Node binary the login item will run, or None."""
    try:
        output = subprocess.run([node, '--version'], capture_output=True, text=True, timeout=10).stdout
    except OSError:
        return None
    match = re.fullmatch(r'v(\d+)\.\d+\.\d+\s*', output)
    return int(match.group(1)) if match else None


def fresh_commands(root, tunnel_id=None, node=None):
    root = Path(root)
    if tunnel_id is None:
        tunnel_id = json.loads((root / '.secrets/tunnel.json').read_text())['tunnel_id']
    if not re.fullmatch(r'tunnel_[0-9a-f]{32}', tunnel_id):
        raise ValueError('Invalid tunnel ID')
    node = node or shutil.which('node')
    if not node:
        raise ValueError('Node.js 24+ must be on PATH')
    # The login item records this exact binary, so check it, not just the shell.
    major = node_major(node)
    if major is None or major < MIN_NODE_MAJOR:
        found = f'Node {major}' if major else 'an unrecognized Node version'
        raise ValueError(f'Node.js {MIN_NODE_MAJOR}+ is required, but {node} is {found}. '
                         'Run `nvm use` (or install Node 24+), then rerun the installer.')
    binary = root / '.runtime/tunnel-client'
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError('Install the executable tunnel-client at .runtime/tunnel-client first')
    for name in ['local-backend.json', 'mac-companion.json', 'inbound-worker-secrets.json', 'tunnel-runtime-key.txt']:
        path = root / '.secrets' / name
        if not path.is_file() or not path.read_text().strip():
            raise ValueError('Missing private configuration: ' + name)
    config = json.loads((root / '.secrets/local-backend.json').read_text())
    if not isinstance(config.get('mcp_token'), str) or len(config['mcp_token']) < 32:
        raise ValueError('Invalid local MCP token')
    header = root / '.secrets/local-mcp-authorization.txt'
    value = 'Bearer ' + config['mcp_token']
    if header.exists() and header.read_text().strip() != value:
        raise ValueError('Existing MCP header differs; resolve the credential mismatch first')
    if not header.exists():
        with open(header, 'x', opener=lambda name, flags: os.open(name, flags, 0o600)) as f:
            f.write(value)
    tunnel = [str(binary), 'run', '--control-plane.tunnel-id', tunnel_id,
        '--control-plane.api-key', 'file:' + str(root / '.secrets/tunnel-runtime-key.txt'),
        '--mcp.server-url', 'http://127.0.0.1:8894/mcp',
        '--mcp.extra-headers', 'Authorization: file:' + str(header),
        '--mcp.discovery-extra-headers', 'Authorization: file:' + str(header),
        '--mcp.startup-wait-timeout', '30s', '--health.listen-addr', '127.0.0.1:8896',
        '--health.url-file', str(root / '.runtime/mcp-health.url'), '--log.format', 'json', '--log.level', 'warn']
    return {'backend': [str(Path(node).resolve()), str(root / 'src/local-main.mjs')], 'tunnel': tunnel}
