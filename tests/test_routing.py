import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import inbox


class InboxRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = inbox

    def setUp(self):
        self.inbox = object.__new__(self.module.Inbox)
        # Two different checkouts, plus a shell and Claude in the target checkout.
        self.snapshot = ('%94\t23119\tOnns\t6\t3\twindow6\tcodex\t/work/vas-6\tvas-6\n'
                         '%100\t24607\tOnns\t8\t1\twindow8\tzsh\t/work/vas-8\tvas-8\n'
                         '%102\t25094\tOnns\t8\t3\twindow8\tcodex\t/work/vas-8\tvas-8\n'
                         '%103\t25358\tOnns\t8\t4\twindow8\tclaude\t/work/vas-8\tvas-8\n')
        self.inbox.call = Mock(return_value=self.snapshot)
        self.inbox.push = Mock()
        self.inbox.store = Mock()
        self.env = patch.dict(os.environ, TMUX_PANE='%94')
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_shared_codex_daemon_routes_stop_to_window8(self):
        self.module.event(self.inbox, 'Codex', 'Stop',
                          {'cwd': '/work/vas-8', 'last_assistant_message': 'done'})
        self.inbox.push.assert_called_once_with('%102', 'Codex', 'done', 'done', '/work/vas-8')
        self.assertEqual(self.inbox.panes()['%102']['location'], 'Onns:8.3')

    def test_shared_daemon_routes_permission_input_and_legacy_notify(self):
        for name, payload, kind in [
            ('PermissionRequest', {}, 'permission'),
            ('PreToolUse', {'tool_name': 'request_user_input'}, 'input'),
            ('agent-turn-complete', {'last-assistant-message': 'done'}, 'done'),
        ]:
            with self.subTest(name=name):
                self.inbox.push.reset_mock()
                self.module.event(self.inbox, 'Codex', name, dict(payload, cwd='/work/vas-8'))
                self.assertEqual(self.inbox.push.call_args.args[:3], ('%102', 'Codex', kind))

    def test_same_checkout_is_ambiguous_even_when_inherited_pane_matches(self):
        self.inbox.call.return_value = self.snapshot + self.snapshot.splitlines()[2].replace('%102', '%110') + '\n'
        with patch.dict(os.environ, TMUX_PANE='%102'), self.assertRaises(RuntimeError):
            self.module.event(self.inbox, 'Codex', 'Stop', {'cwd': '/work/vas-8'})
        self.inbox.push.assert_not_called()

    def test_title_routes_codex_sessions_sharing_the_same_checkout(self):
        session_id = '01a1f31b-b9cc-7632-9ed5-9f8be0000000'
        self.inbox.call.return_value = (
            self.snapshot.replace('codex\t/work/vas-6\tvas-6',
                                  'codex\t/work/vas-8\tvas-8 | codex | 01a1f31b-b9d2-78b0-957b-5909a...')
            .replace('codex\t/work/vas-8\tvas-8\n', f'codex\t/work/vas-8\tvas-8 | codex | {session_id[:29]}...\n')
        )
        self.module.event(self.inbox, 'Codex', 'Stop',
                          {'session_id': session_id, 'cwd': '/work/other', 'last_assistant_message': 'done'})
        self.assertEqual(self.module.codex_session_panes(self.inbox.panes(), session_id), ['%102'])
        self.inbox.store.assert_called_once_with(f'thread:{session_id}', '', 'Codex', 'done', 'done', '/work/other')
        self.inbox.push.assert_not_called()

    def test_cwd_does_not_override_a_different_session_in_the_pane_title(self):
        session_id = '01a1f31b-b9cc-7632-9ed5-9f8be0000000'
        self.inbox.call.return_value = self.snapshot.replace(
            'codex\t/work/vas-8\tvas-8', 'codex\t/work/vas-8\tvas-8 | codex | 01a1f31b-b9d2-78b0-957b-5909a...')
        self.module.event(self.inbox, 'Codex', 'Stop', {'session_id': session_id, 'cwd': '/work/vas-8'})
        self.inbox.store.assert_called_once_with(f'thread:{session_id}', '', 'Codex', 'done', '任务已完成', '/work/vas-8')
        self.inbox.push.assert_not_called()

    def test_missing_codex_pane_does_not_fall_back_to_shell_or_daemon(self):
        self.inbox.call.return_value = self.snapshot.replace('\tcodex\t/work/vas-8', '\tzsh\t/work/vas-8')
        with self.assertRaises(RuntimeError):
            self.module.event(self.inbox, 'Codex', 'Stop', {'cwd': '/work/vas-8'})
        self.inbox.push.assert_not_called()

    def test_invalid_cwd_does_not_use_daemon_pane(self):
        for cwd in (None, '', 'relative', 42):
            with self.subTest(cwd=cwd), self.assertRaises(RuntimeError):
                self.module.event(self.inbox, 'Codex', 'Stop', {'cwd': cwd})
        self.inbox.push.assert_not_called()

    def test_linked_window_is_one_pane(self):
        self.inbox.call.return_value = self.snapshot + self.snapshot.replace('Onns\t', 'linked\t')
        self.assertEqual(self.inbox.hook_pane('Codex', {'cwd': '/work/vas-8'}), '%102')

    def test_symlinked_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'checkout'
            target.mkdir()
            alias = Path(directory) / 'alias'
            alias.symlink_to(target, target_is_directory=True)
            self.inbox.call.return_value = self.snapshot.replace('/work/vas-8', str(target))
            self.assertEqual(self.inbox.hook_pane('Codex', {'cwd': str(alias)}), '%102')

    def test_claude_uses_its_own_inherited_pane(self):
        self.module.event(self.inbox, 'Claude', 'Stop', {'cwd': '/work/vas-8'})
        self.assertEqual(self.inbox.push.call_args.args[0], '%94')
        self.inbox.call.assert_not_called()

    def test_ignored_events_do_not_resolve_panes(self):
        for name, payload in [('SubagentStop', {}), ('Stop', {'agent_id': 'child'}),
                              ('Notification', {'notification_type': 'idle_prompt'})]:
            self.module.event(self.inbox, 'Codex', name, payload)
        self.inbox.call.assert_not_called()
        self.inbox.push.assert_not_called()


if __name__ == '__main__':
    unittest.main()
