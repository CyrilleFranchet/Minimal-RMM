"""Prepare and run single- or multi-workspace deployments using the same services."""
import argparse
import ipaddress
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
    ids = list(workspace_ids or (['default'] if preset in ('single', 'distributed') else []))
    if (preset not in ('single', 'multi', 'distributed') or not ids or len(ids) > 100
            or len(set(ids)) != len(ids) or any(not valid_workspace_id(value) for value in ids)
            or (preset == 'single' and len(ids) != 1)):
        raise ValueError('Provide one workspace for single, or distinct workspace IDs for multi.')
    if preset == 'distributed' and not central_url:
        raise ValueError('Distributed deployment requires --central-url reachable from Exegol.')
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
        if preset == 'distributed':
            central.setdefault('RMM_ADMIN_BIND', '0.0.0.0')
            central.setdefault('RMM_BEACON_BIND', '0.0.0.0')
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
            for kind in ('plugins', 'skills', 'rclone'):
                (path.parent / 'assets' / kind).mkdir(parents=True, mode=0o700)
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
    if (metadata.get('schema') != 1 or metadata.get('preset') not in ('single', 'multi', 'distributed')
            or not isinstance(ids, list) or not ids
            or any(not isinstance(value, str) or not valid_workspace_id(value) for value in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError('Invalid deployment metadata.')
    if component == 'single' and (metadata['preset'] != 'single' or len(ids) != 1):
        raise ValueError('The single launcher requires a single-workspace preset.')
    selected = workspace_id or (ids[0] if len(ids) == 1 else None)
    if component in ('single', 'workspace') and selected not in ids:
        raise ValueError('Select a registered --workspace-id.')
    result = []
    if component in ('single', 'control'):
        directory = root / 'central'
        result.append(([sys.executable, str(CODE / 'server_rmm.py'), '--config', str(directory / 'settings.json')],
                       owner_paths(directory, 'server')))
    if component in ('single', 'workspace'):
        directory = root / 'workspaces' / selected
        settings = SettingsStore(directory / 'settings.json', 'workspace', environment={}).saved_at_start
        if settings.get('RMM_WORKSPACE_ID') != selected:
            raise ValueError('Workspace settings identity does not match deployment metadata.')
        result.append(([sys.executable, str(CODE / 'workspace_service.py'), '--data-dir', str(directory),
                        '--config', str(directory / 'settings.json')], owner_paths(directory, 'workspace')))
    if not result:
        raise ValueError('Invalid component.')
    for argv, _ in result:
        if not Path(argv[argv.index('--config') + 1]).is_file():
            raise ValueError('Missing service settings; prepare the deployment first.')
    return result


def owner_paths(directory, scope):
    """Pin mutable storage to its owner root and resolve saved relative tool paths there."""
    directory = Path(directory).resolve()
    saved = SettingsStore(directory / 'settings.json', scope, environment={}).saved_at_start
    paths = {'RMM_LOG_DIR': str(directory), 'RMM_AI_SKILLS_DIR': str(directory / 'assets' / 'skills')}
    if scope == 'server':
        paths.update(RMM_STORAGE_MODE='workspace', RMM_ASSETS_DIR=str(directory / 'assets'),
                     RMM_PLUGIN_CACHE_DIR=str(directory / 'plugin-cache'), RMM_PROFILES_DIR=str(directory / 'profiles'),
                     RMM_UI_STATE_DB=str(directory / 'ui-state.db'))
    else:
        paths.update(RMM_WORKSPACE_DATA=str(directory))
    for key, expected in paths.items():
        if key == 'RMM_STORAGE_MODE' or not saved.get(key):
            continue
        configured = Path(saved[key]).expanduser()
        configured = configured if configured.is_absolute() else directory / configured
        if configured.resolve() != Path(expected):
            raise ValueError(f'{key} points outside its canonical location; migrate it under the selected service directory before starting.')
    for key in ('RMM_RCLONE_BIN', 'RMM_TUN_HELPER', 'RMM_WORKSPACE_TUN_HELPER', 'RMM_BLOODHOUND_COLLECTOR', 'RMM_CLOUDFLARE_BINARY', 'CLOUDFLARED_BIN'):
        value = saved.get(key)
        if value:
            expanded = Path(value).expanduser()
            if expanded.is_absolute():
                paths[key] = str(expanded)
            elif '/' in value or key in ('RMM_RCLONE_BIN', 'RMM_TUN_HELPER', 'RMM_WORKSPACE_TUN_HELPER'):
                paths[key] = str((directory / expanded).resolve())
    if scope == 'workspace' and not saved.get('RMM_WORKSPACE_TUN_HELPER'):
        paths['RMM_WORKSPACE_TUN_HELPER'] = str(CODE / 'tools' / 'rmm-tun')
    return paths


def child_environment(argv, overrides, allow_env_overrides=False):
    """Use saved settings by default; optionally retain valid same-scope compatibility overrides."""
    environment = dict(os.environ)
    environment.pop('RMM_CONFIG_FILE', None)
    script = Path(argv[1]).name if len(argv) > 1 else ''
    scope = {'server_rmm.py': 'server', 'workspace_service.py': 'workspace'}.get(script)
    if scope or not allow_env_overrides:
        for key in list(environment):
            try:
                # Unknown infrastructure variables are not service settings.
                known = key in FIELDS or key.startswith('RMM_PLUGIN_TOKEN_')
                if known:
                    if not allow_env_overrides:
                        environment.pop(key, None)
                        continue
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
    if scope == 'workspace' and environment.get('RMM_WORKSPACE_ID'):
        config = Path(argv[argv.index('--config') + 1]) if '--config' in argv else None
        if config and environment['RMM_WORKSPACE_ID'] != SettingsStore(config, scope, environment={}).saved_at_start.get('RMM_WORKSPACE_ID'):
            raise ValueError('RMM_WORKSPACE_ID override does not match the selected workspace.')
    return environment


def run_services(plans, allow_env_overrides=False):
    """Supervise separate service processes and stop both when either exits."""
    children = []
    stopping = False

    def request_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    handlers = {number: signal.signal(number, request_stop) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        for argv, overrides in plans:
            environment = child_environment(argv, overrides, allow_env_overrides)
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


def deployment_paths(root, workspace_id=None, component='workspace'):
    """Describe one local owner's effective storage locations without credentials."""
    argv, overrides = commands(root, component, workspace_id)[0]
    config = Path(argv[argv.index('--config') + 1])
    directory = config.parent
    return {'deployment_root': str(Path(root).expanduser().resolve()),
            'component': component, 'settings_file': str(config), 'data_directory': str(directory),
            'assets_directory': str(directory / 'assets'),
            'rclone_profiles_file': str(directory / 'assets/rclone/profiles.json') if component == 'workspace' else None,
            'transport_profiles_directory': overrides.get('RMM_PROFILES_DIR'),
            'configuration_policy': 'saved owner settings; inherited service settings ignored unless explicitly enabled'}


def import_rclone(root, workspace_id, source):
    """Register a validated profile asset without replacing different existing content."""
    from rmm_rclone import validate_profiles, RcloneConfigError
    from rmm_asset_store import AssetStore, AssetError
    paths = deployment_paths(root, workspace_id)
    try:
        document = validate_profiles(json.loads(Path(source).expanduser().read_text(encoding='utf-8')))
    except (OSError, UnicodeError, ValueError, RcloneConfigError):
        raise ValueError('Cannot import rclone profiles; expected a readable JSON object of named profiles.') from None
    target = Path(paths['rclone_profiles_file'])
    if target.exists():
        raise FileExistsError('The destination profiles.json already exists; use the workspace asset upload API to replace it explicitly.')
    store = AssetStore(paths['assets_directory'])
    try:
        store.add_bytes('rclone', 'profiles.json', json.dumps(document).encode('utf-8'))
    except AssetError:
        raise ValueError('Cannot register rclone profiles; check the workspace asset inventory.') from None
    return target


def central_compose(root, admin_bind='127.0.0.1', beacon_bind='0.0.0.0'):
    """Derive a central-only container specification from local saved settings and owner mounts."""
    root = Path(root).expanduser().resolve()
    commands(root, 'control')
    metadata = json.loads((root / 'deployment.json').read_text())
    if metadata['preset'] == 'single':
        raise ValueError('Docker is supported only for distributed central deployments.')
    if root.is_relative_to(CODE):
        raise ValueError('Keep deployment data outside the source checkout before building a Docker image.')
    bindings = [ipaddress.ip_address(admin_bind), ipaddress.ip_address(beacon_bind)]
    saved = SettingsStore(root / 'central/settings.json', 'server', environment={}).saved_at_start
    for key in ('RMM_LOG_DIR', 'RMM_ASSETS_DIR', 'RMM_PLUGIN_CACHE_DIR', 'RMM_PROFILES_DIR', 'RMM_UI_STATE_DB', 'RMM_AI_SKILLS_DIR'):
        if saved.get(key) and Path(saved[key]).expanduser().is_absolute():
            raise ValueError(f'{key} is an absolute host storage path; remove this redundant setting or use an owner-relative path before container startup.')
    if any(saved.get(key, '127.0.0.1') != '0.0.0.0' for key in ('RMM_ADMIN_BIND', 'RMM_BEACON_BIND')):
        raise ValueError('Distributed Docker listeners must bind 0.0.0.0 in central/settings.json; publishing ports cannot expose a loopback-only container listener.')
    admin = saved.get('RMM_ADMIN_PORT', 8080)
    ports = (admin, saved.get('RMM_BEACON_PORT', admin + 1))
    mappings = [f'{"[" + str(bind) + "]" if bind.version == 6 else bind}:{port}:{port}' for bind, port in zip(bindings, ports)]
    spec = {'services': {'rmm-control': {
        'build': {'context': str(CODE)}, 'stop_grace_period': '30s',
        'command': ['python3', 'rmm_deployment.py', 'run', '--root', '/deployment', '--component', 'control'],
        'ports': mappings,
        'volumes': [{'type': 'bind', 'source': str(root / 'central'), 'target': '/deployment/central'},
                    {'type': 'bind', 'source': str(root / 'deployment.json'), 'target': '/deployment/deployment.json', 'read_only': True}],
    }}}
    path = root / 'compose.generated.json'
    fd, temporary = tempfile.mkstemp(prefix='.compose-', dir=root)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(spec, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def main():
    """Expose offline preparation and normal workspace-based service startup."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    init = sub.add_parser('prepare')
    init.add_argument('--preset', choices=('single', 'distributed', 'multi'), default='single')
    init.add_argument('--output', required=True)
    init.add_argument('--workspace-id', action='append')
    init.add_argument('--central-url', help='Central origin; defaults to the saved administration listener (wildcard binds use loopback)')
    init.add_argument('--legacy-settings', help='Preserve saved standalone central settings, including agent authentication')
    launch = sub.add_parser('run')
    launch.add_argument('--root', required=True)
    launch.add_argument('--component', choices=('single', 'control', 'workspace'), required=True)
    launch.add_argument('--workspace-id')
    launch.add_argument('--allow-env-overrides', action='store_true', help='Compatibility only: allow same-scope service setting overrides from the parent environment')
    launch.add_argument('--docker', action='store_true', help='Run only the distributed central service in a dedicated Docker container')
    launch.add_argument('--admin-publish-bind', default='127.0.0.1')
    launch.add_argument('--beacon-publish-bind', default='0.0.0.0')
    for action in ('paths', 'import-rclone'):
        command = sub.add_parser(action)
        command.add_argument('--root', required=True)
        command.add_argument('--workspace-id')
        if action == 'import-rclone':
            command.add_argument('--file', required=True)
        else:
            command.add_argument('--component', choices=('control', 'workspace'), default='workspace')
    args = parser.parse_args()
    try:
        if args.action == 'prepare':
            root = prepare(args.output, args.preset, args.workspace_id, args.central_url, args.legacy_settings)
            print(f'Prepared {args.preset} deployment in {root}. Credentials remain in private settings files.')
            return 0
        if args.action == 'paths':
            print(json.dumps(deployment_paths(args.root, args.workspace_id, args.component), indent=2))
            return 0
        if args.action == 'import-rclone':
            target = import_rclone(args.root, args.workspace_id, args.file)
            print(f'Imported rclone profiles into {target}. Existing files were not overwritten.')
            return 0
        if args.docker:
            if args.component != 'control' or args.allow_env_overrides:
                raise ValueError('--docker supports only --component control with saved settings.')
            spec = central_compose(args.root, args.admin_publish_bind, args.beacon_publish_bind)
            plan = [(['docker', 'compose', '-f', str(spec), 'up', '--build', '--abort-on-container-exit'], {})]
        else:
            plan = commands(args.root, args.component, args.workspace_id)
        return run_services(plan, args.allow_env_overrides)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    raise SystemExit(main())
