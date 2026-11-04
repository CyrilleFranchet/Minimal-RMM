"""Prepare and run single- or multi-workspace deployments using the same services."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

from rmm_settings import FIELDS, SCHEMA, SettingsError, SettingsStore, definition
from rmm_workspace import valid_workspace_id

CODE = Path(__file__).resolve().parent


def write_private(path, document):
    """Write a new owner-readable JSON file without overwriting an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(document, stream, indent=2)
        stream.write('\n')


def prepare(output, preset, workspace_ids, central_url=None, legacy_settings=None):
    """Generate isolated credentials and settings in a fresh deployment directory."""
    ids = list(workspace_ids or (['default'] if preset == 'single' else []))
    if (preset not in ('single', 'multi') or not ids or len(ids) > 100
            or len(set(ids)) != len(ids) or any(not valid_workspace_id(value) for value in ids)
            or (preset == 'single' and len(ids) != 1)):
        raise ValueError('Provide one workspace for single, or distinct workspace IDs for multi.')
    inherited = SettingsStore(legacy_settings, 'server', environment={}).saved_at_start if legacy_settings else {}
    host = inherited.get('RMM_ADMIN_BIND', '127.0.0.1')
    host = {'0.0.0.0': '127.0.0.1', '::': '::1'}.get(host, host)
    host = f'[{host}]' if ':' in host else host
    central_url = central_url or f"http://{host}:{inherited.get('RMM_ADMIN_PORT', 8080)}"
    url = urlsplit(central_url)
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
        raise ValueError('Central URL must be an HTTP(S) origin without credentials.')
    _ = url.port
    if legacy_settings and not Path(legacy_settings).is_file():
        raise ValueError('Legacy settings file does not exist.')
    if inherited.get('RMM_WORKSPACES') or inherited.get('RMM_WORKSPACE_TOKEN'):
        raise ValueError('Existing workspace registrations require explicit reconciliation; use a fresh standalone settings source.')
    output = Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise ValueError('Deployment destination already exists; no configuration was overwritten.')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.rmm-prepare-', dir=output.parent))
    try:
        tokens = {name: secrets.token_urlsafe(32) for name in ids}
        central = {**inherited, 'RMM_API_TOKEN': inherited.get('RMM_API_TOKEN') or secrets.token_urlsafe(32),
                   'RMM_BEACON_SECRET': inherited.get('RMM_BEACON_SECRET') or secrets.token_urlsafe(32),
                   'RMM_WORKSPACES': tokens}
        write_private(temporary / 'central/settings.json', {'schema': SCHEMA, 'scope': 'server', 'settings': central})
        for index, name in enumerate(ids):
            settings = {'RMM_WORKSPACE_ID': name, 'RMM_WORKSPACE_TOKEN': tokens[name],
                        'RMM_C2_URL': central_url.rstrip('/'),
                        'RMM_WORKSPACE_LOCAL_TOKEN': secrets.token_urlsafe(32),
                        'RMM_WORKSPACE_PORT': 8787 + index,
                        'RMM_BLOODHOUND_UI_PORT': str(1031 + index),
                        'RMM_BLOODHOUND_UI_PUBLIC_URL': f'http://127.0.0.1:{1031 + index}',
                        'RMM_WORKSPACE_AUTO_CLAIM': preset == 'single'}
            path = temporary / 'workspaces' / name / 'settings.json'
            write_private(path, {'schema': SCHEMA, 'scope': 'workspace', 'settings': settings})
            SettingsStore(path, 'workspace', environment={})
        SettingsStore(temporary / 'central/settings.json', 'server', environment={})
        write_private(temporary / 'deployment.json', {'schema': 1, 'preset': preset, 'workspaces': ids})
        # Reserve the destination so concurrent preparation cannot replace it.
        output.mkdir(mode=0o700)
        for child in temporary.iterdir():
            child.rename(output / child.name)
    finally:
        shutil.rmtree(temporary)
    return output


def commands(root, component, workspace_id=None):
    """Resolve service commands from validated metadata without loading credentials."""
    root = Path(root).expanduser().resolve()
    metadata = json.loads((root / 'deployment.json').read_text())
    if not isinstance(metadata, dict):
        raise ValueError('Invalid deployment metadata.')
    ids = metadata.get('workspaces')
    if (metadata.get('schema') != 1 or metadata.get('preset') not in ('single', 'multi')
            or not isinstance(ids, list) or not ids
            or any(not isinstance(value, str) or not valid_workspace_id(value) for value in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError('Invalid deployment metadata.')
    if component == 'single' and (metadata['preset'] != 'single' or len(ids) != 1):
        raise ValueError('The single launcher requires a single-workspace preset.')
    selected = workspace_id or (ids[0] if len(ids) == 1 else None)
    if component in ('single', 'workspace') and selected not in ids:
        raise ValueError('Select a registered --workspace-id.')
    inherited_id = os.environ.get('RMM_WORKSPACE_ID', '').strip()
    if component in ('single', 'workspace') and inherited_id and inherited_id != selected:
        raise ValueError('RMM_WORKSPACE_ID override does not match the selected workspace; refusing to reuse another owner\'s data directory.')
    result = []
    if component in ('single', 'control'):
        directory = root / 'central'
        result.append(([sys.executable, str(CODE / 'server_rmm.py'), '--config', str(directory / 'settings.json')],
                       {'RMM_LOG_DIR': str(directory), 'RMM_STORAGE_MODE': 'workspace'}))
    if component in ('single', 'workspace'):
        directory = root / 'workspaces' / selected
        settings = SettingsStore(directory / 'settings.json', 'workspace', environment={}).saved_at_start
        if settings.get('RMM_WORKSPACE_ID') != selected:
            raise ValueError('Workspace settings identity does not match deployment metadata.')
        result.append(([sys.executable, str(CODE / 'workspace_service.py'), '--data-dir', str(directory),
                        '--config', str(directory / 'settings.json')], {}))
    if not result:
        raise ValueError('Invalid component.')
    for argv, _ in result:
        if not Path(argv[argv.index('--config') + 1]).is_file():
            raise ValueError('Missing service settings; prepare the deployment first.')
    return result


def child_environment(argv, overrides):
    """Keep valid scoped overrides without forwarding the other service's credentials."""
    environment = dict(os.environ)
    environment.pop('RMM_CONFIG_FILE', None)
    script = Path(argv[1]).name if len(argv) > 1 else ''
    scope = {'server_rmm.py': 'server', 'workspace_service.py': 'workspace'}.get(script)
    if scope:
        for key in list(environment):
            try:
                # Unknown infrastructure variables are not service settings.
                known = key in FIELDS or key.startswith('RMM_PLUGIN_TOKEN_')
                if known:
                    definition(key, scope)
            except SettingsError:
                environment.pop(key, None)
        for key in ('RMM_INTERNAL_MCP', 'RMM_WORKSPACE_OPERATOR_RELAY'):
            environment.pop(key, None)
        if scope == 'server':
            # Prepared deployments use the registry, not the legacy singleton shortcut.
            for key in ('RMM_WORKSPACE_ID', 'RMM_WORKSPACE_TOKEN', 'RMM_WORKSPACE_DATA'):
                environment.pop(key, None)
        else:
            for key in ('RMM_STORAGE_MODE', 'RMM_LOG_DIR'):
                environment.pop(key, None)
    environment.update(overrides)
    return environment


def run_services(plans):
    """Supervise separate service processes and stop both when either exits."""
    children = []
    stopping = False

    def request_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    handlers = {number: signal.signal(number, request_stop) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        for argv, overrides in plans:
            environment = child_environment(argv, overrides)
            children.append(subprocess.Popen(argv, cwd=CODE, env=environment))
        while not stopping and all(child.poll() is None for child in children):
            time.sleep(0.2)
        return next((child.returncode for child in children if child.returncode is not None), 0)
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        deadline = time.monotonic() + 45
        for child in children:
            try:
                child.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        for number, handler in handlers.items():
            signal.signal(number, handler)


def main():
    """Expose offline preparation and normal workspace-based service startup."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    init = sub.add_parser('prepare')
    init.add_argument('--preset', choices=('single', 'multi'), default='single')
    init.add_argument('--output', required=True)
    init.add_argument('--workspace-id', action='append')
    init.add_argument('--central-url', help='Central origin; defaults to the saved administration listener (wildcard binds use loopback)')
    init.add_argument('--legacy-settings', help='Preserve saved standalone central settings, including agent authentication')
    launch = sub.add_parser('run')
    launch.add_argument('--root', required=True)
    launch.add_argument('--component', choices=('single', 'control', 'workspace'), required=True)
    launch.add_argument('--workspace-id')
    args = parser.parse_args()
    try:
        if args.action == 'prepare':
            root = prepare(args.output, args.preset, args.workspace_id, args.central_url, args.legacy_settings)
            print(f'Prepared {args.preset} deployment in {root}. Credentials remain in private settings files.')
            return 0
        return run_services(commands(args.root, args.component, args.workspace_id))
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    raise SystemExit(main())
