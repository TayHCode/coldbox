import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_coldbox import FakeGitHub, GIB, cb, queued, quiet


class RollbackRecovery(unittest.TestCase):
    def test_retry_recovers_images_and_manifests_after_each_update(self):
        for previous in (b'previous', None):
            for interrupted_at in range(1, 7):
                with self.subTest(previous=previous, interrupted_at=interrupted_at), \
                        tempfile.TemporaryDirectory() as folder:
                    state = Path(folder)
                    paths = [state / 'base-packages.txt', state / 'base-packages.prev.txt']
                    paths[0].write_bytes(b'current')
                    if previous is not None:
                        paths[1].write_bytes(previous)
                    instances = {'base-dev': 'current', 'base-prev': 'previous'}
                    changes = 0

                    def changed():
                        nonlocal changes
                        changes += 1
                        if changes == interrupted_at:
                            raise KeyboardInterrupt

                    def incus(*args):
                        if args[0] == 'info':
                            if args[1] not in instances:
                                message = f'Error: Failed to fetch instance "{args[1]}" in project "test": Instance not found'
                                raise cb.subprocess.CalledProcessError(1, args, stderr=message)
                            return ''
                        _, source, target = args
                        if source not in instances or target in instances:
                            raise cb.subprocess.CalledProcessError(1, args)
                        instances[target] = instances.pop(source)
                        changed()

                    write, replace, unlink = Path.write_bytes, os.replace, Path.unlink

                    def write_bytes(path, data):
                        result = write(path, data)
                        if path in paths:
                            changed()
                        return result

                    def replace_file(source, target):
                        replace(source, target)
                        if Path(target) in paths:
                            changed()
                        elif Path(target) == state / 'base-rollback.json' \
                                and json.loads(Path(target).read_text()).get('mode') == 'complete':
                            changed()

                    def unlink_file(path, **kwargs):
                        unlink(path, **kwargs)
                        if path in paths or path == state / 'base-rollback.json':
                            changed()

                    with mock.patch.multiple(cb, STATE=state, ic=incus), \
                            mock.patch.object(Path, 'write_bytes', write_bytes), \
                            mock.patch.object(cb.os, 'replace', replace_file), \
                            mock.patch.object(Path, 'unlink', unlink_file):
                        with self.assertRaises(KeyboardInterrupt):
                            cb.rollback_base()
                        cb.rollback_base()
                    self.assertEqual(instances, {'base-dev': 'previous', 'base-prev': 'current'})
                    self.assertEqual(paths[0].read_bytes() if paths[0].exists() else None, previous)
                    self.assertEqual(paths[1].read_bytes(), b'current')

    def test_probe_failure_retains_the_transaction_and_manifests(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            journal = state / 'base-rollback.json'
            contents = [b'current', b'previous']
            for name, data in zip(('base-packages.txt', 'base-packages.prev.txt'), contents):
                (state / name).write_bytes(data)
            record = dict(mode='swap', step=2, manifests=[data.hex() for data in contents])
            journal.write_text(json.dumps(record))
            error = cb.subprocess.CalledProcessError(1, ['incus', 'info'], stderr='Error: permission denied')
            with mock.patch.object(cb, 'STATE', state), mock.patch.object(cb, 'ic', side_effect=error) as incus:
                with self.assertRaisesRegex(RuntimeError, 'cannot inspect'):
                    cb.rollback_base()
            incus.assert_called_once_with('info', 'base-rollback')
            self.assertEqual(json.loads(journal.read_text()), record)
            self.assertEqual((state / 'base-packages.txt').read_bytes(), b'current')
            self.assertEqual((state / 'base-packages.prev.txt').read_bytes(), b'previous')

    def test_impossible_restore_state_is_rejected_before_any_change(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            journal = state / 'base-rollback.json'
            record = dict(mode='restore', step=3, manifests=[b'current'.hex(), b'previous'.hex()])
            journal.write_text(json.dumps(record))
            with mock.patch.object(cb, 'STATE', state), mock.patch.object(cb, 'ic') as incus:
                with self.assertRaisesRegex(ValueError, 'invalid base rollback transaction'):
                    cb.rollback_base()
            incus.assert_not_called()
            self.assertEqual(json.loads(journal.read_text()), record)
            self.assertFalse((state / 'base-packages.txt').exists())


class WarmCapacity(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.state = Path(self.scratch.name)
        self.workers = [SimpleNamespace(pid=111, target='other/one', name='cbx-1-a', kind='base'),
                        SimpleNamespace(pid=222, target='other/two', name='cbx-1-b', kind='base')]
        self.vms = {worker.name for worker in self.workers}
        patches = dict(STATE=self.state, host_lock=contextlib.nullcontext,
                       live_workers=lambda folder: self.workers, host_vms=lambda: self.vms,
                       registrations=lambda: [], vm_memory=lambda: GIB,
                       host_memory=lambda field='MemTotal': 16 * GIB,
                       additional_vm_memory=lambda workers, memory, vms=None: 0,
                       host_setting=lambda *args: 0, host_cpu_load=lambda: 0, storage_refusal=lambda: None)
        for name, value in patches.items():
            patcher = mock.patch.object(cb, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for patcher in (mock.patch.object(cb.os, 'cpu_count', return_value=12),
                        mock.patch.object(cb.os, 'getloadavg', return_value=(0.0, 0.0, 0.0))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_idle_warm_runners_and_the_bursts_beside_them_fill_the_core_bound(self):
        args = SimpleNamespace(target='me/app', ttl=600, slots=0, allow_public=False, allow_forks=False)
        github = FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(2)})
        bursts = []

        def note_worker(pid, target, name, kind, writers=None, memory=None):
            self.workers.append(SimpleNamespace(pid=pid, target=target, name=name, kind=kind))
            self.vms.add(name)

        with mock.patch.object(cb.subprocess, 'Popen', side_effect=lambda argv: SimpleNamespace(pid=333)), \
                mock.patch.object(cb, 'note_worker', note_worker):
            quiet(cb.autoscale, github, args, bursts)
        receipts = self.state / 'receipts'
        receipts.mkdir()
        for name in ('cbx-1-a', 'cbx-1-b'):
            (receipts / f'{name}.log').write_bytes(b'Running job: verify\n')
        self.assertEqual(len(bursts), 2)
        self.assertEqual(cb.host_load(self.workers), (4, 0))
        self.assertFalse(cb.admit('more/repo', 'cbx-1-c', 'base'))

    def test_idle_warm_runners_fill_the_core_bound_like_busy_ones(self):
        self.assertTrue(cb.admit('more/repo', 'cbx-1-c', 'base'))
        with mock.patch.object(cb.os, 'cpu_count', return_value=6):
            self.assertFalse(cb.admit('more/repo', 'cbx-1-c', 'base'))
