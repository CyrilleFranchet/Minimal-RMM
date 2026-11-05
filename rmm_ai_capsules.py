"""Code-owned v3 specialist capabilities; models cannot grant themselves tools."""
import asyncio
import ipaddress
import json
import re
import secrets
import shlex
import time
import xml.etree.ElementTree as ET

from rmm_ai_kernel import KernelError
from rmm_ai_skill_context import CORE_SKILLS, StepSkills, _tool
from rmm_exegol_runtime import execution_arguments, output_strings

READS = frozenset({'health', 'list_sessions', 'get_session', 'get_events', 'list_plugins',
                   'list_history', 'get_history_session', 'get_history_events'})
CAPSULES = {
    'OBSERVE': ((), READS, 4),
    'BASELINE': (('local-recon-before-network',), READS | {'agent_baseline'}, 5),
    'NETWORK_SCAN': (('exegol-rmm-pivot',), {'list_socks', 'list_tun', 'start_socks', 'stop_socks',
                      'start_tun', 'stop_tun', 'verify_network_route', 'network_scan'}, 8),
    'DIRECTORY_QUERY': (('bloodhound-analysis',), {'bloodhound'}, 7),
    'DIRECTORY_COLLECT': (('bloodhound-ce-python-collection', 'exegol-rmm-pivot'),
                          {'bloodhound', 'list_socks', 'list_tun', 'start_socks', 'start_tun'}, 9),
    'FILES': (('windows-user-profile-path',), {'list_session_downloads', 'get_events', 'get_session'}, 6),
    'EMBEDDINGS': (('document-embeddings',), {'embedding_profiles', 'embedding_results'}, 6),
    'AGENT_COMMAND': (('agent-command-dispatch',), {'exec_command', 'get_events', 'get_session'}, 6),
}
DIRECTORY_ACTIONS = {
    'DIRECTORY_QUERY': {'health', 'preflight', 'status', 'query', 'queries', 'report', 'library_search', 'library_get', 'result'},
    'DIRECTORY_COLLECT': {'health', 'ensure', 'preflight', 'create', 'collect', 'import', 'status', 'verify', 'report', 'result'},
}
BASELINES = {
    'identity': "[pscustomobject]@{Name=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name;SID=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value}",
    'token': "$out=(& whoami.exe /all 2>&1 | Out-String); if($LASTEXITCODE -ne 0){throw 'whoami_failed'}; $out",
    'host': "Get-CimInstance Win32_ComputerSystem -ErrorAction Stop | Select-Object Name,Domain,PartOfDomain,DomainRole",
    'os': "Get-CimInstance Win32_OperatingSystem -ErrorAction Stop | Select-Object Caption,Version,BuildNumber,OSArchitecture,LastBootUpTime",
    'network': "$ip=(& ipconfig.exe /all 2>&1 | Out-String); if($LASTEXITCODE -ne 0){throw 'ipconfig_failed'}; $routes=(& route.exe print 2>&1 | Out-String); if($LASTEXITCODE -ne 0){throw 'route_failed'}; [pscustomobject]@{IPConfiguration=$ip;Routes=$routes}",
    'process': "[pscustomobject]@{Is64BitProcess=[Environment]::Is64BitProcess;Profile=[Environment]::GetFolderPath('UserProfile');Temp=[IO.Path]::GetTempPath();WorkingDirectory=(Get-Location).Path;UserDomain=$env:USERDOMAIN;UserDnsDomain=$env:USERDNSDOMAIN;LogonServer=$env:LOGONSERVER}",
}
FULL_BASELINE = "PS: $ErrorActionPreference='Stop'; $facts=[ordered]@{ObservedAt=[DateTime]::UtcNow.ToString('o')}; " + '; '.join(
    "try {$value=(& { " + command + " }); $facts['" + name + "']=@{status='known';value=$value}} catch {$facts['" + name + "']=@{status='unknown';reason='local_query_failed'}}"
    for name, command in BASELINES.items()) + '; $facts | ConvertTo-Json -Compress -Depth 8'
LOCAL_BASELINE = _tool('agent_baseline', 'Collect fixed read-only Windows identity, token, OS and network context. No arguments, arbitrary commands or network discovery.', {}, [])

VERIFY_ROUTE = _tool('verify_network_route', 'Verify one scoped target connection from the verified Exegol container through an existing session-owned SOCKS/TUN. Closed/open TCP responses establish reachability; filtered or timed-out responses do not.',
    {'probe_ip': {'type': 'string'}, 'port': {'type': 'integer', 'minimum': 1, 'maximum': 65535},
     'transport': {'type': 'string', 'enum': ['socks', 'tun']}}, ['probe_ip', 'port', 'transport'])
NETWORK_SCAN = _tool('network_scan', 'Run a structured Nmap TCP-connect scan of exactly this task target after matching route verification. No arbitrary shell arguments, installation or alternate container.',
    {'ports': {'type': 'string', 'description': 'Comma-separated numeric ports/ranges, or empty for Nmap defaults'},
     'transport': {'type': 'string', 'enum': ['socks', 'tun']}}, ['transport'])


def decode(raw):
    """Parse a tool receipt without interpreting its prose as instructions."""
    try:
        result = json.loads(raw)
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


WORKFLOWS = {
    'legacy': set(),
    'auto': set(), 'explain': set(), 'observe': {'OBSERVE', 'BASELINE'},
    'network': {'OBSERVE', 'BASELINE', 'NETWORK_SCAN'},
    'directory': {'OBSERVE', 'BASELINE', 'DIRECTORY_QUERY'},
    'collect': {'OBSERVE', 'BASELINE', 'DIRECTORY_COLLECT'},
    'files': {'OBSERVE', 'FILES'}, 'embeddings': {'EMBEDDINGS'},
    'agent_commands': {'OBSERVE', 'AGENT_COMMAND'},
}


def validate_execution_scope(value):
    """Compile operator-provided allow/exclude lists; prose never grants execution scope."""
    if not isinstance(value, dict) or set(value) - {'targets', 'exclude', 'commands', 'directory', 'proxy_host', 'tun_device'}:
        raise KernelError('invalid_execution_scope')
    result = {}
    for key in ('targets', 'exclude', 'commands'):
        entries = value.get(key, [])
        if not isinstance(entries, list) or len(entries) > 32 or not all(isinstance(x, str) and 0 < len(x) <= (8192 if key == 'commands' else 64) for x in entries):
            raise KernelError('invalid_execution_scope')
        if key != 'commands':
            try:
                nets = [ipaddress.ip_network(x, strict=False) for x in entries]
                if any(net.version != 4 for net in nets):
                    raise ValueError()
            except ValueError as error:
                raise KernelError('invalid_ipv4_scope') from error
            entries = [str(net) for net in nets]
        result[key] = entries
    directory = value.get('directory', '')
    if not isinstance(directory, str) or len(directory) > 253 or (directory and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', directory)):
        raise KernelError('invalid_directory_scope')
    result['directory'] = directory.lower()
    for key, pattern in [('proxy_host', r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}'), ('tun_device', r'[A-Za-z0-9_.-]{1,32}')]:
        field = value.get(key, '')
        if not isinstance(field, str) or (field and not re.fullmatch(pattern, field)):
            raise KernelError('invalid_pivot_endpoint')
        result[key] = field
    return result


class CapsuleRegistry:
    """Compile tasks into static tools and mandatory enabled method instructions."""

    def __init__(self, documents, tools, delegate, session_id, container, external_tools, workflow, objective, kernel, archive, policy=None, execution_scope=None, progress=None):
        self.progress = progress
        self.documents = {s['id']: s for s in documents}
        self.tools = {t['function']['name']: t for t in tools}
        self.gate = StepSkills(documents, tools, delegate, external_tool_names=external_tools, policy=policy)
        self.session_id, self.container = session_id, container
        self.external_tools = set(external_tools)
        self.workflow, self.objective = workflow, objective
        self.kernel, self.archive = kernel, archive
        self.execution_scope = validate_execution_scope(execution_scope or {})
        self.authorized_networks = [ipaddress.ip_network(x) for x in self.execution_scope['targets']]
        self.excluded_networks = [ipaddress.ip_network(x) for x in self.execution_scope['exclude']]

    def compile(self, proposal):
        """Reject incompatible methods, disabled requirements and unscoped network targets."""
        if not isinstance(proposal, dict) or set(proposal) - {'kind', 'target', 'objective', 'basis', 'skill_ids'} or not {'kind', 'target', 'objective', 'basis'}.issubset(proposal):
            raise KernelError('invalid_task_contract')
        kind = proposal['kind']
        if not isinstance(kind, str) or kind not in CAPSULES:
            raise KernelError('unknown_task_kind')
        if (not isinstance(proposal['target'], str) or len(proposal['target']) > 256
                or not isinstance(proposal['objective'], str) or not 1 <= len(proposal['objective']) <= 1000
                or not isinstance(proposal['basis'], list) or len(proposal['basis']) > 6
                or not all(isinstance(x, str) for x in proposal['basis'])):
            raise KernelError('invalid_task_contract')
        if kind == 'AGENT_COMMAND' and self.workflow != 'agent_commands':
            raise KernelError('agent_command_workflow_not_selected')
        if kind not in WORKFLOWS.get(self.workflow, set()):
            raise KernelError('task_outside_selected_workflow')
        if kind in {'BASELINE', 'AGENT_COMMAND', 'FILES', 'EMBEDDINGS'}:
            if not self.session_id or proposal['target'] != self.session_id:
                raise KernelError('selected_session_required')
        if kind == 'AGENT_COMMAND' and not self.execution_scope['commands']:
            raise KernelError('exact_command_grant_required')
        if kind == 'DIRECTORY_COLLECT' and (not self.session_id or not proposal['target'] or proposal['target'].casefold() != self.execution_scope['directory']):
            raise KernelError('explicit_directory_target_and_session_required')
        if kind == 'DIRECTORY_COLLECT' and not self.kernel.db.execute("SELECT 1 FROM tasks WHERE kind='BASELINE' AND target=? AND state='done'", (self.session_id,)).fetchone():
            raise KernelError('completed_baseline_required')
        if kind == 'NETWORK_SCAN':
            try:
                network = ipaddress.ip_network(proposal['target'], strict=False)
            except ValueError as error:
                raise KernelError('ipv4_scan_target_required') from error
            if network.version != 4 or network.num_addresses > 256 or not any(network.subnet_of(scope) for scope in self.authorized_networks) or any(network.overlaps(excluded) for excluded in self.excluded_networks):
                raise KernelError('scan_target_outside_operator_scope')
            if not self.session_id or not self.container:
                raise KernelError('verified_workspace_and_session_required')
            if 'execute_command_in_container' not in self.external_tools:
                raise KernelError('exegol_execution_unavailable')
            if not self.kernel.db.execute("SELECT 1 FROM tasks WHERE kind='BASELINE' AND target=? AND state='done'", (self.session_id,)).fetchone():
                raise KernelError('completed_baseline_required')
            proposal = {**proposal, 'target': str(network)}
        optional = proposal.get('skill_ids', [])
        if not isinstance(optional, list) or len(optional) > 4 or not all(isinstance(x, str) for x in optional):
            raise KernelError('invalid_task_skills')
        proposal = {**proposal, 'skill_ids': optional}
        required, allowed, budget = CAPSULES[kind]
        required = (*required, *optional)
        match = re.match(r'^/([a-z0-9][a-z0-9_-]{0,63})(?:\s|$)', self.objective.strip())
        if match:
            required = (*required, match.group(1))
        try:
            resolved = self.gate._resolve([*CORE_SKILLS.intersection(self.documents), *required])
        except ValueError as error:
            raise KernelError(str(error)) from error
        # Unknown custom methods are not silently activated: a capsule owns its
        # fixed method bundle, and checkbox-disabled requirements block it.
        self.gate.active = resolved
        instructions = '\n\n'.join(f"## {sid}\n{self.documents[sid]['body']}" for sid in resolved)
        missing = set(allowed) - self.tools.keys() - {'agent_baseline', 'verify_network_route', 'network_scan'}
        if missing == set(allowed):
            raise KernelError('capsule_tools_unavailable')
        schemas = [self.tools[name] for name in sorted(allowed) if name in self.tools and name not in {'agent_baseline', 'verify_network_route', 'network_scan'}]
        schemas += [tool for tool in (LOCAL_BASELINE, VERIFY_ROUTE, NETWORK_SCAN) if tool['function']['name'] in allowed]
        return proposal, instructions, schemas, budget

    async def backend(self, name, args):
        """Apply existing declarative metadata guards to code-owned backend operations."""
        self.gate.prepared = {name}
        self.gate.begin_batch([{'name': name, 'arguments': args}])
        return await self.gate(name, args)

    async def owner_read(self, arguments):
        """Resolve one bounded owner-broker read without replaying its originating request."""
        result = decode(await self.backend('bloodhound', arguments))
        for _ in range(20):
            data = result.get('data', {})
            request_id = data.get('workspace_request_id') if isinstance(data, dict) else None
            if not request_id:
                return result
            await asyncio.sleep(0.25)
            result = decode(await self.backend('bloodhound', {'action': 'result', 'run_id': request_id}))
        raise KernelError('owner_read_pending_retry_later')

    async def _relay(self, transport, target):
        """Read an authoritative, live relay owned by the selected session."""
        if not isinstance(transport, str) or transport not in {'socks', 'tun'}:
            raise KernelError('invalid_transport')
        data = decode(await self.backend('list_socks' if transport == 'socks' else 'list_tun', {}))
        for row in data.get('relays', []):
            if row.get('session_id') != self.session_id:
                continue
            if transport == 'socks' and row.get('listener_running') is True:
                if row.get('bind_host') not in {'127.0.0.1', 'localhost'} or type(row.get('port')) is not int or not 1 <= row['port'] <= 65535:
                    continue
                return row
            if transport == 'tun' and row.get('helper_alive') is True:
                try:
                    if any(ipaddress.ip_network(target).subnet_of(ipaddress.ip_network(route)) for route in row.get('routes', [])):
                        return row
                except ValueError:
                    continue
        raise KernelError('matching_live_pivot_required')

    async def _nmap(self, task, target, ports, transport, marker):
        """Build a bounded command from validated fields and the trusted container."""
        if not isinstance(ports, str):
            raise KernelError('invalid_scan_ports')
        if not re.fullmatch(r'(?:\d{1,5}(?:-\d{1,5})?)(?:,\d{1,5}(?:-\d{1,5})?)*', ports or '1') or len(ports) > 512:
            raise KernelError('invalid_scan_ports')
        for part in (ports or '1').split(','):
            values = [int(x) for x in part.split('-')]
            if any(not 1 <= x <= 65535 for x in values) or values[0] > values[-1]:
                raise KernelError('invalid_scan_ports')
        relay = await self._relay(transport, task['target'])
        if transport == 'tun':
            device = str(relay.get('device', '')).removeprefix('tun://')
            if not re.fullmatch(r'[A-Za-z0-9_.-]{1,32}', device) or (self.execution_scope['tun_device'] and self.execution_scope['tun_device'] != device):
                raise KernelError('selected_relay_tun_device_required')
            # A more-specific route can override a subnet route for any host.
            # Check every destination, including network/broadcast addresses that
            # Nmap may probe, in one fixed, bounded command.
            destinations = list(ipaddress.ip_network(str(target), strict=False))
            if len(destinations) > 256:
                raise KernelError('scan_target_too_large')
            route_command = 'for _rmm_ip in ' + ' '.join(shlex.quote(str(ip)) for ip in destinations) + '; do ip -j route get "$_rmm_ip" || exit 1; done'
            route_args = execution_arguments(list(self.tools.values()), self.container, route_command)
            route_raw = await self.backend('execute_command_in_container', route_args)
            matching_route = False
            for output in output_strings(route_raw):
                try:
                    pages = [json.loads(line) for line in output.splitlines() if line.strip()]
                    matching_route = len(pages) == len(destinations) and all(isinstance(routes, list) and bool(routes) and all(isinstance(row, dict) and row.get('dev') == device for row in routes) for routes in pages)
                except (ValueError, TypeError):
                    continue
                if matching_route:
                    break
            if not matching_route:
                raise KernelError('exegol_tun_route_unverified')
        command = 'nmap -sT -Pn -n --unprivileged --max-retries 1 --host-timeout 120s -oX - '
        if ports:
            command += '-p ' + shlex.quote(ports) + ' '
        command += '-- ' + shlex.quote(str(target))
        if transport == 'socks':
            if self.execution_scope['proxy_host'] not in {'', '127.0.0.1', 'localhost'}:
                raise KernelError('unverified_cross_namespace_proxy')
            # The owner runtime verifies the pivot owner and Exegol share a namespace.
            proxy = 'socks5 127.0.0.1 ' + str(relay['port'])
            command = ("_rmm_proxy=$(mktemp) || exit 1; trap 'rm -f \"$_rmm_proxy\"' EXIT; "
                       "printf '%s\\n' strict_chain proxy_dns '[ProxyList]' " + shlex.quote(proxy)
                       + ' > "$_rmm_proxy"; proxychains -q -f "$_rmm_proxy" ' + command)
        command += '; _rmm_rc=$?; printf "\\n' + marker + ':%s\\n" "$_rmm_rc"'
        arguments = execution_arguments(list(self.tools.values()), self.container, command)
        raw = await self.backend('execute_command_in_container', arguments)
        for text in output_strings(raw):
            if len(text) > 2_000_000 or not re.search(re.escape(marker) + r':0\s*$', text):
                continue
            start, end = text.find('<?xml'), text.rfind('</nmaprun>')
            if start < 0 or end < start:
                continue
            xml = text[start:end + len('</nmaprun>')]
            xml = xml.replace('<!DOCTYPE nmaprun>', '')
            if '<!DOCTYPE' in xml or '<!ENTITY' in xml:
                continue
            try:
                root = ET.fromstring(xml)
            except ET.ParseError:
                continue
            expected_ip = str(ipaddress.ip_network(str(target), strict=False).network_address)
            hosts = [host for host in root.findall('host') if any(addr.get('addr') == expected_ip for addr in host.findall('address'))]
            reachable = any(state.get('state') in {'open', 'closed'} for host in hosts for state in host.findall('.//port/state'))
            reachable = reachable or any(extra.get('state') == 'closed' and any(reason.get('reason') in {'conn-refused', 'reset'} for reason in extra.findall('extrareasons')) for host in hosts for extra in host.findall('.//extraports'))
            return {'ok': True, 'completed': True, 'verified': reachable,
                    'target': task['target'], 'session_id': self.session_id, 'container': self.container,
                    'transport': transport, 'relay_identity': {k: v for k, v in relay.items() if k not in {'active_tunnels', 'agent_websocket', 'agent_channel'}}, 'raw_artifact_id': self.archive.archive(text, 'nmap_xml'),
                    'port_states': [state.get('state') for state in root.findall('.//port/state')][:64],
                    'output_excerpt': text[-2000:]}
        return {'ok': False, 'completed': False, 'error': 'nmap_completion_unconfirmed', 'output': raw[:4000]}

    async def dispatch(self, task, name, args):
        """Apply capsule/action/target boundaries before the authenticated backend call."""
        kind = task['kind']
        if name not in CAPSULES[kind][1] or not isinstance(args, dict):
            raise KernelError('tool_outside_capsule')
        args = dict(args)
        if name == 'agent_baseline':
            if args:
                raise KernelError('invalid_baseline_arguments')
            session = decode(await self.backend('get_session', {'session_ref': self.session_id})).get('session') or {}
            if str(session.get('os_type', '')).lower() != 'windows':
                raise KernelError('windows_baseline_required')
            raw = await self.backend('exec_command', {'session_ref': self.session_id,
                'command': FULL_BASELINE, 'timeout': 60})
            result = decode(raw)
            try:
                baseline = json.loads(str(result.get('output', '')).lstrip('\ufeff').strip())
            except ValueError:
                baseline = None
            if (result.get('ok') is not True or not isinstance(baseline, dict) or not set(BASELINES).issubset(baseline)
                    or not all(isinstance(baseline[key], dict) and baseline[key].get('status') in ('known', 'unknown')
                               and ('value' in baseline[key] if baseline[key]['status'] == 'known' else bool(baseline[key].get('reason')))
                               for key in BASELINES)):
                return json.dumps({'ok': False, 'error': 'baseline_output_unverified', 'output': raw[:4000]})
            return json.dumps({'ok': True, 'completed': True, 'session_id': self.session_id,
                               'sections': list(BASELINES), 'raw_artifact_id': self.archive.archive(raw, 'local_baseline'),
                               'baseline': {key: {field: value for field, value in baseline[key].items() if field in {'status', 'reason'}} for key in BASELINES},
                               'next': 'Use read_evidence part=source for full baseline facts.'})
        if name in {'verify_network_route', 'network_scan'}:
            transport = args.get('transport')
            if name == 'verify_network_route':
                if set(args) != {'probe_ip', 'port', 'transport'} or type(args['port']) is not int or not 1 <= args['port'] <= 65535:
                    raise KernelError('invalid_route_probe')
                try:
                    probe = ipaddress.ip_address(args['probe_ip'])
                    if probe not in ipaddress.ip_network(task['target']):
                        raise ValueError()
                except (ValueError, TypeError) as error:
                    raise KernelError('route_probe_outside_task_scope') from error
                result = await self._nmap(task, probe, str(args['port']), transport, 'RMM_VERIFY_' + secrets.token_hex(16))
                result['ok'] = result.get('verified') is True
                return json.dumps(result)
            if set(args) - {'ports', 'transport'}:
                raise KernelError('invalid_scan_arguments')
            verified = False
            current_relay = None
            for row in self.kernel.db.execute("SELECT i.artifact_id FROM invocations i JOIN attempts a ON a.id=i.attempt_id WHERE i.tool='verify_network_route' AND i.state='returned' AND a.task_id=? AND a.state='running' ORDER BY i.rowid DESC", (task.get('id', ''),)):
                proof = decode(self.archive.read(row[0])['content'])
                if current_relay is None:
                    current_relay = await self._relay(transport, task['target'])
                if (proof.get('verified') is True and proof.get('target') == task['target']
                        and proof.get('session_id') == self.session_id and proof.get('container') == self.container
                        and proof.get('transport') == transport and proof.get('relay_identity') == {k: v for k, v in current_relay.items() if k not in {'active_tunnels', 'agent_websocket', 'agent_channel'}}):
                    verified = True
                    break
            if not verified:
                raise KernelError('verified_pivot_receipt_required')
            return json.dumps(await self._nmap(task, task['target'], args.get('ports', ''), transport, 'RMM_SCAN_' + secrets.token_hex(16)))
        if name == 'exec_command' and args.get('command') not in self.execution_scope['commands']:
            raise KernelError('command_not_operator_approved')
        if name in {'embedding_profiles', 'embedding_results'}:
            allowed = {'list', 'get', 'defaults'} if name == 'embedding_profiles' else {'list', 'get'}
            if not isinstance(args.get('action', 'list'), str) or args.get('action', 'list') not in allowed:
                raise KernelError('read_only_embedding_capsule')
        if name == 'bloodhound':
            if not isinstance(args.get('action', 'preflight'), str) or not isinstance(args.get('options', {}), dict):
                raise KernelError('invalid_directory_arguments')
            if kind == 'DIRECTORY_COLLECT' and args.get('action') in {'collect', 'import', 'verify', 'status', 'report'}:
                run_id = args.get('run_id')
                if not isinstance(run_id, str) or not re.fullmatch(r'[a-f0-9]{32}', run_id):
                    raise KernelError('directory_run_id_required')
                status = await self.owner_read({'action': 'status', 'run_id': run_id})
                data = status.get('data', {})
                run = data.get('run', data) if isinstance(data, dict) else {}
                if not isinstance(run, dict) or run.get('domain', '').casefold() != task['target'].casefold():
                    raise KernelError('directory_run_outside_task_scope')
            if args.get('action') == 'create' and str((args.get('options') or {}).get('domain', '')).casefold() != task['target'].casefold():
                raise KernelError('directory_target_outside_task')
            if args.get('action', 'preflight') not in DIRECTORY_ACTIONS[kind]:
                raise KernelError('directory_action_outside_capsule')
            if args.get('action') == 'collect':
                if not self.session_id:
                    raise KernelError('selected_session_required')
                await self._relay('socks', '0.0.0.0/32')
                args['options'] = {'network': {'transport': 'socks', 'session_id': self.session_id}}
        if 'session_ref' in self.tools.get(name, {}).get('function', {}).get('parameters', {}).get('properties', {}):
            if self.session_id:
                if args.get('session_ref', self.session_id) != self.session_id:
                    raise KernelError('session_outside_task_scope')
                args['session_ref'] = self.session_id
        if name == 'start_socks':
            args['bind_host'] = '127.0.0.1'
        if name == 'start_tun':
            if kind != 'NETWORK_SCAN':
                raise KernelError('scoped_tun_task_required')
            args['routes'] = [task['target']]
            args.pop('route', None)
        if kind == 'DIRECTORY_COLLECT' and name == 'bloodhound' and args.get('action') == 'status':
            # Keep asynchronous waiting in code, not rapid model polling. This is
            # one journaled read; no collect/import request is resubmitted.
            deadline = time.monotonic() + 1850
            while time.monotonic() < deadline:
                if self.progress:
                    self.progress({'phase': 'tool', 'tool': 'bloodhound', 'status': 'waiting', 'role': kind})
                try:
                    result = await asyncio.wait_for(self.owner_read(args), timeout=max(0.001, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    break
                data = result.get('data', {})
                run = data.get('run', data) if isinstance(data, dict) else {}
                if not isinstance(run, dict) or run.get('state') not in {'collecting', 'uploading', 'ingesting'}:
                    return json.dumps(result)
                await asyncio.sleep(min(5, max(0, deadline - time.monotonic())))
            return json.dumps({'ok': False, 'error': 'directory_wait_budget_exceeded_inspect_run', 'run_id': args.get('run_id')})
        return await self.backend(name, args)


def receipt_class(name, raw):
    """Distinguish received data, queued work, explicit terminal receipts and errors."""
    data = decode(raw)
    if len(raw) > 16000:
        return 'truncated'
    if data.get('ok') is False or data.get('error'):
        return 'error'
    if data.get('completed') is True and data.get('ok') is True:
        return 'completed'
    if name in READS or name in {'list_socks', 'list_tun', 'list_session_downloads', 'list_artifacts', 'embedding_results', 'embedding_profiles'}:
        return 'read' if data.get('ok') is True else 'unknown'
    if name in {'agent_baseline', 'exec_command'} and data.get('ok') is True and 'output' in data:
        return 'completed'
    if name == 'bloodhound':
        payload = data.get('data', {})
        if isinstance(payload, dict) and isinstance(payload.get('evidence'), dict) and isinstance(payload.get('graph_preview'), dict) and data.get('ok') is True:
            return 'graph_query'
        run = payload.get('run', payload) if isinstance(payload, dict) else {}
        if data.get('ok') is True and run.get('state') == 'verified':
            return 'completed'
        return 'read' if data.get('ok') is True else 'unknown'
    return 'accepted' if data.get('ok') is True else 'unknown'
