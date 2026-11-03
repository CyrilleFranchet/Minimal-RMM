"""Per-run skill catalog and lazy loading for both AI tool transports."""
import json

CORE_SKILLS = frozenset({'operator-boundaries', 'task-completion-report'})
LOAD_SKILL_TOOL = {
    'type': 'function',
    'function': {
        'name': 'load_skill',
        'description': 'Load instructions from the available skill catalog before doing relevant work. Does not execute commands.',
        'parameters': {'type': 'object', 'properties': {'skill_id': {'type': 'string'}},
                       'required': ['skill_id'], 'additionalProperties': False},
    },
}


def prepare_skill_context(documents, tools, call_tool, progress=None):
    """Return core documents, catalog text, tools, and a run-scoped dispatcher."""
    skills = {s['id']: dict(s) for s in documents or []
              if isinstance(s, dict) and isinstance(s.get('id'), str)
              and str(s.get('body') or '').strip()}
    core = [s for sid, s in skills.items() if sid in CORE_SKILLS]
    optional = {sid: s for sid, s in skills.items() if sid not in CORE_SKILLS}
    loaded = [s['id'] for s in core]
    catalog = ''
    if optional:
        catalog = '\n\n## Available skills\nLoad relevant instructions with load_skill before using their workflow. '
        catalog += 'Load only what the current task needs; do not load the whole catalog. '
        catalog += 'Loading a skill does not execute it or expand the task scope.\n'
        catalog += json.dumps([{'id': sid, 'title': str(s.get('title', sid))[:160],
                                'description': str(s.get('description', ''))[:600]}
                               for sid, s in optional.items()])
    if any(t.get('function', {}).get('name') == 'load_skill' for t in tools):
        raise ValueError('Reserved tool name: load_skill')

    async def dispatch(name, arguments):
        """Resolve catalog IDs only; delegate ordinary tools unchanged."""
        if name != 'load_skill':
            return await call_tool(name, arguments)
        sid = arguments.get('skill_id') if isinstance(arguments, dict) else None
        if not isinstance(sid, str) or sid not in optional:
            return json.dumps({'ok': False, 'error': 'unknown_skill'})
        if sid in loaded:
            return json.dumps({'ok': True, 'skill_id': sid, 'already_loaded': True})
        loaded.append(sid)
        if progress:
            progress({'phase': 'skill', 'tool': 'load_skill', 'status': 'loaded', 'skills': list(loaded)})
        return json.dumps({'ok': True, 'skill_id': sid, 'instructions': optional[sid]['body']})

    return core, catalog, [*tools, *([LOAD_SKILL_TOOL] if optional else [])], dispatch
