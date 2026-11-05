"""Per-step skill selection, dependency resolution, and declarative tool guards."""
import hashlib
import json
import re

CORE_SKILLS = frozenset({'operator-boundaries', 'task-completion-report'})
INTERNAL_TOOLS = frozenset({'load_skill', 'prepare_step', 'read_context'})


def _tool(name, description, properties, required):
    """Build a provider-neutral internal function schema."""
    return {'type': 'function', 'function': {'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': properties, 'required': required,
                           'additionalProperties': False}}}


LOAD_SKILL_TOOL = _tool('load_skill', 'Read one enabled skill before choosing the current step. Does not prepare execution.',
                       {'skill_id': {'type': 'string'}}, ['skill_id'])
PREPARE_STEP_TOOL = _tool('prepare_step',
    'Before EACH operational tool batch, select relevant enabled skills for this immediate step, not just the high-level task. Returns full instructions. Call alone; inspect the result before execution. Explicitly requested enabled skills must be selected. Empty selection requires explaining why no optional skill applies. Do not include secrets or private reasoning.',
    {'summary': {'type': 'string', 'maxLength': 500},
     'skill_ids': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 100},
     'tools': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1},
     'checkpoint': {'type': 'string', 'maxLength': 2000}}, ['summary', 'skill_ids', 'tools', 'checkpoint'])


def backend_enabled(backend, documents, policy):
    """Determine whether enabled metadata requests a backend connection."""
    enabled = {s.get('id') for s in documents or [] if isinstance(s, dict) and s.get('body')}
    rules = [r for s in documents or [] for r in s.get('tool_rules', [])]
    rules += policy or []
    return any(r.get('backend') == backend and set(r.get('requires', [])).issubset(enabled)
               and (not r.get('requires_any') or enabled.intersection(r['requires_any'])) for r in rules)


class StepSkills:
    """Keep enabled documents and active instructions outside mutable history."""

    def __init__(self, documents, tools, delegate, progress=None, external_tool_names=(), policy=None):
        self.skills = {s['id']: dict(s) for s in documents or [] if isinstance(s, dict)
                       and isinstance(s.get('id'), str) and str(s.get('body') or '').strip()}
        self.core = [s for sid, s in self.skills.items() if sid in CORE_SKILLS]
        self.active = []
        self.loaded = [s['id'] for s in self.core]
        self.delegate, self.progress = delegate, progress
        self.external = set(external_tool_names)
        self.rules = list(policy or []) + [r for s in self.skills.values() for r in s.get('tool_rules', [])]
        self.tool_names = {t.get('function', {}).get('name') for t in tools}
        self.action_defaults = {t['function']['name']: t['function'].get('parameters', {}).get('properties', {}).get('action', {}).get('default')
                                for t in tools}
        self.action_enums = {t['function']['name']: t['function'].get('parameters', {}).get('properties', {}).get('action', {}).get('enum')
                             for t in tools}
        if self.tool_names.intersection(INTERNAL_TOOLS):
            raise ValueError('Reserved internal tool name')
        self.prepared = None
        self.batch_tools = set()
        self.step = 0
        self.summary = self.checkpoint = ''
        self.required = []
        self.versions = {sid: hashlib.sha256(s['body'].encode()).hexdigest() for sid, s in self.skills.items()}

    def begin_batch(self, calls):
        """Consume preparation once and reject mixed preparation/execution batches."""
        operational = [c for c in calls if c['name'] not in INTERNAL_TOOLS]
        self.batch_tools = set()
        if any(c['name'] == 'prepare_step' for c in calls) and len(calls) != 1:
            self.prepared = None
            self.batch_tools = {'invalid_preparation_batch'}
            return
        if operational:
            names = {c['name'] for c in operational}
            if (len(operational) == len(calls) and self.prepared is not None
                    and names.issubset(self.prepared)):
                self.batch_tools = names
            self.prepared = None
        elif any(c['name'] == 'prepare_step' for c in calls):
            self.prepared = None
            if len(calls) != 1:
                self.batch_tools = {'invalid_preparation_batch'}

    def _resolve(self, ids):
        """Resolve enabled dependencies atomically, rejecting cycles and missing IDs."""
        selected, visiting = set(), set()
        def visit(sid):
            if sid not in self.skills:
                raise ValueError('skill_disabled_or_unknown')
            if sid in visiting:
                raise ValueError('skill_dependency_cycle')
            if sid in selected:
                return
            visiting.add(sid)
            for dependency in self.skills[sid].get('requires', []):
                visit(dependency)
            visiting.remove(sid)
            selected.add(sid)
        for sid in ids:
            visit(sid)
        if any(tool not in self.tool_names or not self.available(tool)
               for sid in selected for tool in self.skills[sid].get('required_tools', [])):
            raise ValueError('required_tool_unavailable')
        return sorted(selected, key=lambda sid: (self.skills[sid].get('priority', 0), sid))

    def require_from_messages(self, messages):
        """Honor an explicit leading /skill-id invocation without enabling disabled IDs."""
        latest = next((m.get('content', '') for m in reversed(messages) if m.get('role') == 'user'), '')
        match = re.match(r'^/([a-z0-9][a-z0-9_-]{0,63})(?:\s|$)', str(latest).strip())
        self.required = [match.group(1)] if match else []

    def activate_explicit(self):
        """Load explicitly invoked instructions before even a tool-free model response."""
        if not self.required:
            return
        resolved = self._resolve([*(s['id'] for s in self.core), *self.required])
        self.active = [sid for sid in resolved if sid not in CORE_SKILLS]
        self.loaded = list(dict.fromkeys([*self.loaded, *resolved]))
        if self.progress:
            self.progress({'phase': 'skill', 'status': 'loaded', 'skills': self.loaded,
                           'active_skills': self.active})

    def instructions(self):
        """Reattach complete active instructions and checkpoint after history compaction."""
        text = '\n\n## Current step (authoritative active skill set)\n'
        text += json.dumps({'step': self.step, 'summary': self.summary,
                            'checkpoint': self.checkpoint, 'active_skills': self.active})
        for sid in self.active:
            text += f'\n### {sid} (version {self.versions[sid]})\n{self.skills[sid]["body"]}\n'
        return text

    def available(self, name):
        """Hide tools with unconditional prerequisites outside the enabled snapshot."""
        enabled = set(self.skills)
        for rule in self.rules:
            if rule.get('actions'):
                continue
            if rule.get('tool') == name or (rule.get('backend') == 'external' and name in self.external):
                if (not set(rule.get('requires', [])).issubset(enabled)
                        or (rule.get('requires_any') and not enabled.intersection(rule['requires_any']))):
                    return False
        return True

    async def __call__(self, name, arguments):
        """Load instructions, prepare a single step, or dispatch guarded operations."""
        args = arguments if isinstance(arguments, dict) else {}
        if name not in INTERNAL_TOOLS:
            if not isinstance(arguments, dict):
                return json.dumps({'ok': False, 'error': 'invalid_tool_arguments'})
            allowed_actions = self.action_enums.get(name)
            action = args.get('action', self.action_defaults.get(name))
            if allowed_actions is not None and ('action' in args or action is not None) and action not in allowed_actions:
                return json.dumps({'ok': False, 'error': 'invalid_tool_arguments'})
        if name not in INTERNAL_TOOLS and not self.available(name):
            return json.dumps({'ok': False, 'error': 'required_skill_disabled'})
        if name == 'load_skill':
            sid = args.get('skill_id')
            if not isinstance(sid, str) or sid not in self.skills:
                return json.dumps({'ok': False, 'error': 'unknown_skill'})
            if sid not in self.loaded:
                self.loaded.append(sid)
            return json.dumps({'ok': True, 'skill_id': sid, 'version': self.versions[sid],
                               'instructions': self.skills[sid]['body']})
        if name == 'prepare_step':
            self.prepared = None
            if self.batch_tools == {'invalid_preparation_batch'}:
                return json.dumps({'ok': False, 'error': 'prepare_step_must_be_alone'})
            ids, names = args.get('skill_ids'), args.get('tools')
            summary, checkpoint = args.get('summary'), args.get('checkpoint')
            if (not isinstance(ids, list) or len(ids) > 100 or not all(isinstance(x, str) for x in ids)
                    or not isinstance(names, list) or not names or not all(isinstance(x, str) and x in self.tool_names for x in names)
                    or not isinstance(summary, str) or not summary.strip() or len(summary) > 500
                    or not isinstance(checkpoint, str) or len(checkpoint) > 2000):
                return json.dumps({'ok': False, 'error': 'invalid_step'})
            try:
                resolved = self._resolve([*(s['id'] for s in self.core), *ids, *self.required])
                active = [sid for sid in resolved if sid not in CORE_SKILLS]
            except ValueError as error:
                return json.dumps({'ok': False, 'error': str(error)})
            self.active = active
            self.summary, self.checkpoint = summary, checkpoint
            self.step += 1
            self.prepared = set(names)
            for sid in active:
                if sid not in self.loaded:
                    self.loaded.append(sid)
            if self.progress:
                self.progress({'phase': 'step', 'status': 'prepared', 'step': self.step,
                               'step_summary': summary, 'step_checkpoint': checkpoint,
                               'skills': self.loaded, 'active_skills': active})
            return json.dumps({'ok': True, 'step': self.step, 'instructions': self.instructions()})
        if name not in self.batch_tools:
            return json.dumps({'ok': False, 'error': 'step_preparation_required',
                               'next': 'Call prepare_step alone for this immediate step, inspect its instructions, then execute.'})
        for rule in self.rules:
            if rule.get('tool') != name and not (rule.get('backend') == 'external' and name in self.external):
                continue
            if rule.get('actions'):
                action = args.get('action', self.action_defaults.get(name))
                if not isinstance(action, str):
                    return json.dumps({'ok': False, 'error': 'explicit_action_required'})
                if action.strip().casefold() not in {value.strip().casefold() for value in rule['actions']}:
                    continue
            active = set(self.active) | CORE_SKILLS.intersection(self.skills)
            if (not set(rule.get('requires', [])).issubset(active)
                    or (rule.get('requires_any') and not active.intersection(rule['requires_any']))):
                return json.dumps({'ok': False, 'error': 'required_skill_not_active'})
        return await self.delegate(name, arguments)


def prepare_skill_context(documents, tools, call_tool, progress=None, external_tool_names=(), policy=None):
    """Build a bounded catalog and one step-aware dispatcher for either transport."""
    runtime = StepSkills(documents, tools, call_tool, progress, external_tool_names, policy)
    optional = [s for sid, s in runtime.skills.items() if sid not in CORE_SKILLS]
    catalog = '\n\n## Skill selection for every immediate step\n'
    catalog += ('Before each operational tool batch, call prepare_step ALONE. Select relevant enabled skills '
                'for the immediate action, including explicit operator selections/invocations and enabled dependencies. '
                'Read returned instructions before executing. Reevaluate after every operational batch. '
                'Enabled skills govern relevant work; they do not authorize unrelated actions. '
                'Disabled skills must never be loaded or reconstructed. Missing dependencies are blockers. '
                'Higher numeric priority wins conflicting workflow preferences; operator instructions and core boundaries prevail. '
                'If same-priority instructions conflict, report the conflict. Never silently substitute another workflow on failure. '
                'Use summary for a brief public operational rationale, never secrets or private reasoning. '
                'Use checkpoint for completed actions, pending operations and evidence IDs; never repeat an uncertain operation.\n')
    catalog += json.dumps([{'id': s['id'], 'title': s.get('title', s['id']),
                            'description': s.get('description', ''), 'requires': s.get('requires', []),
                            'required_tools': s.get('required_tools', []),
                            'priority': s.get('priority', 0)} for s in optional])
    visible = [tool for tool in tools if runtime.available(tool['function']['name'])]
    return runtime.core, catalog, [*visible, LOAD_SKILL_TOOL, PREPARE_STEP_TOOL], runtime
