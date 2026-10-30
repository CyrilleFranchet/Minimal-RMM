"""Transcript recovery must include events evicted from the live ring."""
import json
import tempfile
import threading
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from server_rmm import RMMServer


class ShellRecoveryTests(unittest.TestCase):
    def test_disk_history_and_live_events_are_merged_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.jsonl'
            events = [{'id': n, 'body': str(n)} for n in range(1, 1206)]
            path.write_text('\n'.join(json.dumps(event) for event in reversed(events[:1200])))
            session = SimpleNamespace(id='session', result_events=deque(events[-500:], maxlen=500))
            server = SimpleNamespace(session_lock=threading.Lock(),
                                     resolve_session=lambda _: session,
                                     get_session=lambda _: session,
                                     resolve_history_session_id=lambda _: session.id,
                                     _history_events_path=lambda _: str(path))
            server.get_history_events = lambda *a, **kw: RMMServer.get_history_events(server, *a, **kw)
            result = RMMServer.get_events(server, session.id, limit=0)
            self.assertEqual(result, events)
            self.assertEqual(RMMServer.get_events(server, session.id, since_id=1199, limit=0), events[1199:])
            self.assertEqual(RMMServer.get_events(server, session.id, limit=2), events[-2:])
