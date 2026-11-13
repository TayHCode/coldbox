import contextlib
import signal
import unittest
from types import SimpleNamespace
from unittest import mock

from test_coldbox import cb


class RepositoryPolicyChanges(unittest.TestCase):
    def assert_only_idle_runner_is_withdrawn(self, repo):
        github = mock.Mock()
        github.get.return_value = repo
        args = SimpleNamespace(target='owner/repo', allow_public=False)
        workers = [SimpleNamespace(target='owner/repo', name='idle', pid=111),
                   SimpleNamespace(target='owner/repo', name='busy', pid=222)]
        runners = [{'name': 'idle', 'id': 1, 'busy': False},
                   {'name': 'busy', 'id': 2, 'busy': True}]
        with mock.patch.object(cb, 'host_lock', return_value=contextlib.nullcontext()), \
                mock.patch.object(cb, 'live_workers', return_value=workers), \
                mock.patch.object(cb, 'listing', return_value=runners), \
                mock.patch.object(cb, 'gh') as request, \
                mock.patch.object(cb.os, 'kill') as kill:
            with self.assertRaises(ValueError):
                cb.autoscale(github, args, [])
            request.assert_called_once_with('-X', 'DELETE', 'repos/owner/repo/actions/runners/1')
            kill.assert_called_once_with(111, signal.SIGUSR1)

    def test_private_to_public_withdraws_idle_runner_and_keeps_busy_job(self):
        self.assert_only_idle_runner_is_withdrawn({'full_name': 'owner/repo', 'visibility': 'public'})

    def test_changed_repository_identity_withdraws_idle_runner_and_keeps_busy_job(self):
        self.assert_only_idle_runner_is_withdrawn({'full_name': 'other/repo', 'visibility': 'private'})
