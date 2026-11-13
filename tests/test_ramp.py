import argparse
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


ROOT = Path(__file__).resolve().parents[1]
cb = load('ramp_coldbox', ROOT / 'coldbox')


class Cap(unittest.TestCase):
    def test_cap_only_restricts_admission_and_expires(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(cb, 'STATE', Path(folder)):
            self.assertEqual(cb.ramp_room(7, 3, now=10), 7)
            path = Path(folder) / 'ramp-cap.json'
            path.write_text(json.dumps({'limit': 5, 'expires': 20}))
            self.assertEqual(cb.ramp_room(7, 3, now=10), 2)
            self.assertEqual(cb.ramp_room(1, 3, now=10), 1)
            self.assertEqual(cb.ramp_room(7, 6, now=10), 0)
            self.assertEqual(cb.ramp_room(7, 3, now=20), 7)

    def test_invalid_cap_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(cb, 'STATE', Path(folder)):
            path = Path(folder) / 'ramp-cap.json'
            for value in ('broken', '{}', '{"limit":-1,"expires":20}', '{"limit":true,"expires":20}'):
                path.write_text(value)
                with self.subTest(value=value), self.assertRaises(ValueError):
                    cb.ramp_room(7, 3, now=10)


ramp = load('capacity_ramp', ROOT / 'scripts' / 'capacity-ramp.py')


class Ramp(unittest.TestCase):
    def test_high_cpu_and_load_do_not_stop_measurement(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            args = argparse.Namespace(output=root, seconds=1800)
            def sample(at):
                return dict(at=at, busy=0, load=[100, 100, 100], cpu=[at*99+1, 0, 0, at+1],
                            available=100, reserve=10, cpu_psi={'some': 0}, memory_psi={'full': 0},
                            oom=0, active=[], cli='abc', cpus=4, total=100, memory=10, receipts=[])
            with mock.patch.object(ramp, 'call', side_effect=[sample(0), sample(900), sample(1800)]), \
                    mock.patch.object(ramp.time, 'sleep'):
                result = ramp.measure(args, 'test-host', root / 'step')
            self.assertFalse(result['unsafe'])
            self.assertEqual(result['seconds'], 1800)

    def test_memory_pressure_and_oom_stop_measurement(self):
        for changes in (dict(available=9), dict(memory_psi={'full': 1}), dict(oom=1)):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                args = argparse.Namespace(output=root, seconds=1800)
                sample = dict(at=0, busy=0, cpu=[1, 0, 0, 1], available=100, reserve=10,
                              cpu_psi={'some': 0}, memory_psi={'full': 0}, oom=0, active=[],
                              cli='abc', cpus=4, total=100, memory=10, receipts=[])
                second = dict(sample, at=30, cpu=[2, 0, 0, 2])
                second.update(changes)
                with mock.patch.object(ramp, 'call', side_effect=[sample, second]), \
                        mock.patch.object(ramp.time, 'sleep'):
                    result = ramp.measure(args, 'test-host', root / 'step')
                self.assertTrue(result['unsafe'])
                self.assertEqual(result['seconds'], 30)

    def test_sustained_cpu_pressure_stops_measurement(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            args = argparse.Namespace(output=root, seconds=1800)
            def sample(at, pressure):
                return dict(at=at, busy=0, cpu=[at+1, 0, 0, at*9+1], available=100,
                            reserve=10, cpu_psi={'some': pressure}, memory_psi={'full': 0},
                            oom=0, active=[], cli='abc', cpus=4, total=100, memory=10, receipts=[])
            with mock.patch.object(ramp, 'call', side_effect=[sample(0, 60), sample(30, 60)]), \
                    mock.patch.object(ramp.time, 'sleep'):
                result = ramp.measure(args, 'test-host', root / 'step')
            self.assertTrue(result['unsafe'])
            self.assertEqual(result['seconds'], 30)

    def test_default_measurement_never_writes_cap(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            args = argparse.Namespace(output=root, host=['test-host'], seconds=1800)
            def measured(args, host, target):
                target.mkdir()
                (target / 'samples.jsonl').write_text(
                    json.dumps(dict(at=0, busy=2)) + '\n' + json.dumps(dict(at=1800, busy=2)))
                (target / 'matched.json').write_text(json.dumps({
                    '1': dict(started_at=10, completed_at=40, wait=20, duration=30, outcome='success')}))
                return dict(count=1, cutoff=1800)
            with mock.patch.object(ramp, 'call', return_value={'load': [0, 0, 0], 'cpus': 4}) as call, \
                    mock.patch.object(ramp, 'measure', side_effect=measured):
                ramp.run(args)
            self.assertEqual(call.call_args_list, [])
            report = json.loads((root / 'test-host-report.json').read_text())
            self.assertEqual(report['levels']['2']['queue_p90'], 20)
            self.assertEqual(report['levels']['2']['exposure_seconds'], 1800)

    def test_drain_does_not_inflate_level_exposure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            args = argparse.Namespace(output=root, host=['test-host'], seconds=1800)
            def measured(args, host, target):
                target.mkdir()
                (target / 'samples.jsonl').write_text(
                    json.dumps(dict(at=0, busy=2)) + '\n' + json.dumps(dict(at=1800, busy=2)) + '\n' +
                    json.dumps(dict(at=2700, busy=3)) + '\n' +
                    json.dumps(dict(at=3600, busy=3)))
                (target / 'matched.json').write_text(json.dumps({
                    '1': dict(started_at=10, completed_at=40, wait=20, duration=30, outcome='success')}))
                return dict(count=1, cutoff=1800)
            with mock.patch.object(ramp, 'call', return_value={'load': [0, 0, 0], 'cpus': 4}) as call, \
                    mock.patch.object(ramp, 'measure', side_effect=measured):
                ramp.run(args)
            self.assertEqual(call.call_args_list, [])
            report = json.loads((root / 'test-host-report.json').read_text())
            self.assertEqual(report['levels']['2']['completions_per_hour'], 2)
            self.assertNotIn('3', report['levels'])
            self.assertEqual(report['levels']['2']['exposure_seconds'], 1800)

    def test_removed_write_options_are_rejected(self):
        for argv in (['run', '--apply'], ['cap', '--limit', '2', '--owner', 'ours', '--evidence', 'a'*64]):
            with self.subTest(argv=argv), mock.patch('sys.argv', ['capacity-ramp.py', *argv]), mock.patch.object(ramp, 'run') as run:
                with self.assertRaises(SystemExit) as error:
                    ramp.main()
                self.assertEqual(error.exception.code, 2)
                run.assert_not_called()

    def test_remote_cap_action_preserves_state(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'ramp-cap.json'
            path.write_text('{"limit":2}')
            with mock.patch.object(ramp.runpy, 'run_path', return_value={'STATE': Path(folder)}):
                with self.assertRaisesRegex(ValueError, 'only sample and jobs'):
                    ramp.remote(argparse.Namespace(action='cap', coldbox='unused'))
            self.assertEqual(path.read_text(), '{"limit":2}')

    def window(self, **changes):
        result = dict(count=30, seconds=1800, peak_busy=5, unsafe=False, failures=0,
                      duration_p90=100, wait_p90=80, at_limit_samples=3, saturated_seconds=1800, job_mix={'workflow/job': 1}, cli='abc')
        result.update(changes)
        if 'saturated_seconds' not in changes:
            result['saturated_seconds'] = result['seconds']
        result.setdefault('successful_completions', result['count'])
        return result

    def test_accepts_only_observed_improvement(self):
        base = self.window(wait_p90=100, count=30)
        self.assertEqual(ramp.decision(base, self.window(count=33), 5), 'PASS')
        for changes in (dict(count=29), dict(seconds=1799), dict(at_limit_samples=2)):
            self.assertEqual(ramp.decision(base, self.window(**changes), 5), 'INCOMPLETE')
        for changes in (dict(unsafe=True), dict(failures=1), dict(duration_p90=121), dict(count=30)):
            self.assertEqual(ramp.decision(base, self.window(**changes), 5), 'STOP')

    def test_unsuccessful_completions_cannot_hide_plateau(self):
        base = self.window(count=40, successful_completions=30, failures=10)
        for outcome in ('failure', 'cancelled', 'skipped'):
            with self.subTest(outcome=outcome):
                current = self.window(count=50, successful_completions=30,
                                      failures=20 if outcome == 'failure' else 10)
                self.assertEqual(ramp.decision(base, current, 5), 'STOP')

    def test_successful_sample_is_required(self):
        for successes in (0, 1, 29):
            window = self.window(successful_completions=successes)
            self.assertEqual(ramp.decision(window, window, 3), 'INCOMPLETE')

    def test_passive_levels_count_success_only(self):
        with tempfile.TemporaryDirectory() as folder:
            args = argparse.Namespace(output=Path(folder), host=['test-host'], seconds=1800)
            def measured(args, host, target):
                target.mkdir()
                (target / 'samples.jsonl').write_text(
                    json.dumps(dict(at=0, busy=2)) + '\n' + json.dumps(dict(at=1800, busy=2)))
                (target / 'matched.json').write_text(json.dumps({
                    str(i): dict(started_at=10, completed_at=40, wait=20, duration=30, outcome=outcome)
                    for i, outcome in enumerate(('success', 'failure', 'cancelled', 'skipped'))}))
                return dict(count=4, cutoff=1800)
            with mock.patch.object(ramp, 'call', return_value={'load': [0], 'cpus': 4}), \
                    mock.patch.object(ramp, 'measure', side_effect=measured):
                ramp.run(args)
            level = json.loads((args.output / 'test-host-report.json').read_text())['levels']['2']
            self.assertEqual(level['completions_per_hour'], 2)
            self.assertEqual(len(level['jobs']), 4)

    def test_nearest_rank_matches_literal_expected_values(self):
        self.assertEqual(ramp.percentile([7, 2, 9, 3, 1, 5, 8, 6, 4, 10], .9), 9)
        self.assertEqual(ramp.percentile([7, 2, 9, 3, 1, 5, 8, 6, 4, 10], .5), 5)

    def test_measure_waits_for_long_job_and_uses_completed_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            args = mock.Mock(output=root, seconds=1800, owner='ours', apply=True)
            clock = [ramp.datetime(2026, 11, 10, 12, tzinfo=ramp.timezone.utc).timestamp()]
            start = clock[0]
            receipt = dict(id='cbx-job', mode='runner', verdict='pass', vm={'destroyed': True, 'destroyed_at': 'finished'}, github=dict(repo='owner/repo', run_id=1, run_attempt=None))
            def iso(seconds):
                return ramp.datetime.fromtimestamp(seconds, ramp.timezone.utc).isoformat()
            job = dict(id=1, runner_name='cbx-job', status='completed', conclusion='success',
                       created_at=iso(start - 20), started_at=iso(start+10), completed_at=iso(start+1900))
            def call(a, host, action, *extra):
                self.assertIn(action, ('sample', 'jobs'))
                if action == 'jobs':
                    return dict(jobs=[job, dict(job, id=2, runner_name='cbx-job2'), {'status': 'in_progress'}], requests=1)
                return dict(at=clock[0], cpu=[int(clock[0]-start)+1, 0, 0, int(clock[0]-start)*9+1, 0, 0, 0, 0],
                            cap=dict(limit=2, owner='ours', expires=clock[0]+180),
                            active=['cbx-job'] if clock[0] < start+1920 else [], busy=2, warm=0,
                            available=100, reserve=10, cpu_psi={'some': 0}, memory_psi={'full': 0}, oom=0, cpus=4, total=100, memory=10,
                            cli='abc', receipts=[receipt, dict(receipt, id='cbx-job2')] if clock[0] >= start+1920 else [
                                dict(id='cbx-job', mode='runner', verdict='incomplete', vm={'destroyed': False})])
            with mock.patch.object(ramp.time, 'time', side_effect=lambda: clock[0]), \
                    mock.patch.object(ramp.time, 'sleep', side_effect=lambda n: clock.__setitem__(0, clock[0]+min(n, 30))), \
                    mock.patch.object(ramp, 'call', side_effect=call):
                result = ramp.measure(args, 'test-host', root / 'step', 2)
            self.assertFalse(result['unsafe'])
            self.assertEqual(result['count'], 2)
            self.assertEqual(result['wait_p90'], 30)
            self.assertEqual(result['duration_p90'], 1890)
            self.assertEqual(result['seconds'], 1800)
            self.assertEqual(result['requests'], 1)

    def test_measure_counts_only_successful_completions_in_throughput(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            args = mock.Mock(output=root, seconds=1800, owner='ours', apply=True)
            clock = [ramp.datetime(2026, 11, 10, 12, tzinfo=ramp.timezone.utc).timestamp()]
            start = clock[0]
            receipt = dict(id='cbx-job', mode='runner', verdict='pass', vm={'destroyed': True, 'destroyed_at': 'finished'}, github=dict(repo='owner/repo', run_id=1, run_attempt=None))
            def iso(seconds):
                return ramp.datetime.fromtimestamp(seconds, ramp.timezone.utc).isoformat()
            job = dict(id=1, runner_name='cbx-job', status='completed', conclusion='success',
                       created_at=iso(start - 20), started_at=iso(start+10), completed_at=iso(start+1700))
            def call(a, host, action, *extra):
                self.assertIn(action, ('sample', 'jobs'))
                if action == 'jobs':
                    return dict(jobs=[job, *[dict(job, id=i, runner_name=f'cbx-job{i}', conclusion=outcome) for i, outcome in enumerate(('failure', 'cancelled', 'skipped'), 2)], {'status': 'in_progress'}], requests=1)
                return dict(at=clock[0], cpu=[int(clock[0]-start)+1, 0, 0, int(clock[0]-start)*9+1, 0, 0, 0, 0],
                            cap=dict(limit=2, owner='ours', expires=clock[0]+180),
                            active=['cbx-job'] if clock[0] < start+1920 else [], busy=2, warm=0,
                            available=100, reserve=10, cpu_psi={'some': 0}, memory_psi={'full': 0}, oom=0, cpus=4, total=100, memory=10,
                            cli='abc', receipts=[receipt, *[dict(receipt, id=f'cbx-job{i}') for i in range(2, 5)]] if clock[0] >= start+1920 else [
                                dict(id='cbx-job', mode='runner', verdict='incomplete', vm={'destroyed': False})])
            with mock.patch.object(ramp.time, 'time', side_effect=lambda: clock[0]), \
                    mock.patch.object(ramp.time, 'sleep', side_effect=lambda n: clock.__setitem__(0, clock[0]+min(n, 30))), \
                    mock.patch.object(ramp, 'call', side_effect=call):
                result = ramp.measure(args, 'test-host', root / 'step', 2)
            self.assertFalse(result['unsafe'])
            self.assertEqual(result['count'], 4)
            self.assertEqual(result['successful_completions'], 1)
            self.assertEqual(result['completions_per_hour'], 2)
            self.assertEqual(result['failures'], 1)
            self.assertEqual(result['wait_p90'], 30)
            self.assertEqual(result['duration_p90'], 1690)
            self.assertEqual(result['seconds'], 1800)
            self.assertEqual(result['requests'], 1)

    def test_drain_success_is_excluded_from_window_throughput(self):
        for fields in (dict(verdict='pass'), dict(verdict='incomplete', reason='no tests ran', tests_ran=0)):
            with self.subTest(**fields):
                with tempfile.TemporaryDirectory() as folder:
                    root = Path(folder)
                    args = mock.Mock(output=root, seconds=1800, owner='ours', apply=True)
                    clock = [ramp.datetime(2026, 11, 10, 12, tzinfo=ramp.timezone.utc).timestamp()]
                    start = clock[0]
                    receipt = dict(id='cbx-job', mode='runner', **fields, vm={'destroyed': True, 'destroyed_at': 'finished'}, github=dict(repo='owner/repo', run_id=1, run_attempt=None))
                    def iso(seconds):
                        return ramp.datetime.fromtimestamp(seconds, ramp.timezone.utc).isoformat()
                    job = dict(id=1, runner_name='cbx-job', status='completed', conclusion='success',
                               created_at=iso(start - 20), started_at=iso(start+10), completed_at=iso(start+1900))
                    def call(a, host, action, *extra):
                        self.assertIn(action, ('sample', 'jobs'))
                        if action == 'jobs':
                            return dict(jobs=[job, *[dict(job, id=i, runner_name=f'cbx-job{i}', conclusion=outcome) for i, outcome in enumerate(('failure', 'cancelled', 'skipped'), 2)], {'status': 'in_progress'}], requests=1)
                        return dict(at=clock[0], cpu=[int(clock[0]-start)+1, 0, 0, int(clock[0]-start)*9+1, 0, 0, 0, 0],
                                    cap=dict(limit=2, owner='ours', expires=clock[0]+180),
                                    active=['cbx-job'] if clock[0] < start+1920 else [], busy=2, warm=0,
                                    available=100, reserve=10, cpu_psi={'some': 0}, memory_psi={'full': 0}, oom=0, cpus=4, total=100, memory=10,
                                    cli='abc', receipts=[receipt, *[dict(receipt, id=f'cbx-job{i}') for i in range(2, 5)]] if clock[0] >= start+1920 else [
                                        dict(id='cbx-job', mode='runner', verdict='incomplete', vm={'destroyed': False})])
                    with mock.patch.object(ramp.time, 'time', side_effect=lambda: clock[0]), \
                            mock.patch.object(ramp.time, 'sleep', side_effect=lambda n: clock.__setitem__(0, clock[0]+min(n, 30))), \
                            mock.patch.object(ramp, 'call', side_effect=call):
                        result = ramp.measure(args, 'test-host', root / 'step', 2)
                    self.assertFalse(result['unsafe'])
                    self.assertEqual(result['count'], 4)
                    self.assertEqual(result['successful_completions'], 0)
                    self.assertEqual(result['completions_per_hour'], 0)
                    self.assertEqual(result['failures'], 1)
                    self.assertEqual(result['wait_p90'], 30)
                    self.assertEqual(result['duration_p90'], 1890)
                    self.assertEqual(result['seconds'], 1800)
                    self.assertEqual(result['requests'], 1)

    def test_inactive_unfinished_receipt_is_unsafe(self):
        for changes in ({'verdict': 'incomplete'}, {'verdict': 'pass', 'error': 'cleanup failed'},
                        {'verdict': 'incomplete', 'reason': 'the test count could not be read'}):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                args = argparse.Namespace(output=root, seconds=1800)
                receipt = dict(id='gone', vm={}, **changes)
                sample = dict(cli='abc', cpus=4, total=100, memory=10, at=1800, busy=0, cpu=[1, 0, 0, 9], available=100,
                              reserve=10, cpu_psi={'some': 0}, memory_psi={'full': 0}, oom=0, active=[],
                              receipts=[receipt])
                with mock.patch.object(ramp, 'call', side_effect=[dict(sample, at=0), dict(sample, at=1800, cpu=[2, 0, 0, 18])]) as call, \
                        mock.patch.object(ramp.time, 'sleep') as sleep:
                    result = ramp.measure(args, 'test-host', root / 'step')
                self.assertTrue(result['unsafe'])
                self.assertEqual(call.call_count, 1)
                sleep.assert_not_called()

    def test_successful_completion_window_table(self):
        cases = [
            ('started before window', 5, 100, 'success', 29, 30, True),
            ('finished after window', 20, 1811, 'success', 29, 29, False),
            ('failed inside window', 20, 100, 'failure', 29, 29, False),
            ('at window start', 5, 10, 'success', 29, 30, True),
            ('at window end', 20, 1810, 'success', 29, 30, True),
            ('before window start', 0, 9, 'success', 29, 29, False),
            ('cancelled inside', 20, 100, 'cancelled', 29, 29, False),
            ('skipped inside', 20, 100, 'skipped', 29, 29, False),
            ('31 versus 30', 5, 100, 'success', 30, 31, True),
        ]
        for name, started, ended, outcome, baseline, expected, qualifies in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                args = argparse.Namespace(output=root, seconds=1800)
                def iso(value):
                    return ramp.datetime.fromtimestamp(value, ramp.timezone.utc).isoformat()
                jobs = [dict(id=i, runner_name=f'worker-{i}', status='completed',
                             conclusion='success', created_at=iso(0), started_at=iso(20),
                             completed_at=iso(100)) for i in range(baseline)]
                jobs.append(dict(jobs[0], id=baseline, runner_name=f'worker-{baseline}',
                                 started_at=iso(started), completed_at=iso(ended), conclusion=outcome))
                receipts = [dict(id=job['runner_name'], vm={'destroyed_at': 'done'},
                                 github=dict(repo='owner/repo', run_id=1)) for job in jobs]
                def sample(at):
                    return dict(at=at, busy=2, cpu=[at, 0, 0, at*9], available=100,
                                reserve=10, cpu_psi={'some': 0}, memory_psi={'full': 0}, oom=0,
                                active=['worker-0'] if at == 1810 else [],
                                cli='abc', cpus=4, total=100, memory=10,
                                receipts=receipts if at == 1840 else [])
                samples = iter([sample(at) for at in range(10, 1841, 30)])
                def call(args, host, action, *extra):
                    return next(samples) if action == 'sample' else dict(jobs=jobs, requests=1)
                with mock.patch.object(ramp.time, 'time', return_value=0), \
                        mock.patch.object(ramp.time, 'sleep'), \
                        mock.patch.object(ramp, 'call', side_effect=call):
                    result = ramp.measure(args, 'test-host', root / 'step', 2)
                self.assertEqual(result['successful_completions'], expected)
                self.assertEqual(result['completions_per_hour'], expected * 2)
                self.assertEqual(result['seconds'], 1800)
                self.assertEqual(ramp.qualified(result), qualifies)

    def test_first_sample_sets_measurement_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            args = argparse.Namespace(output=root, host=['test-host'], seconds=1800)
            receipt = dict(id='worker', vm={'destroyed_at': 'done'},
                           github=dict(repo='owner/repo', run_id=1))
            def iso(value):
                return ramp.datetime.fromtimestamp(value, ramp.timezone.utc).isoformat()
            jobs = [dict(id=i, runner_name='worker', status='completed', conclusion='success',
                         created_at=iso(0), started_at=iso(start), completed_at=iso(100))
                    for i, start in [(1, 5), (2, 10)]]
            def sample(at, receipts):
                return dict(at=at, busy=2, cpu=[at, 0, 0, at*9], available=100,
                            reserve=10, cpu_psi={'some': 0}, memory_psi={'full': 0}, oom=0, active=[],
                            cli='abc', cpus=4, total=100, memory=10, receipts=receipts)
            responses = [sample(10, []),
                         sample(1810, [receipt]), dict(jobs=[jobs[0]], requests=1)]
            with mock.patch.object(ramp.time, 'time', return_value=0), \
                    mock.patch.object(ramp.time, 'sleep'), \
                    mock.patch.object(ramp, 'call', side_effect=responses):
                ramp.run(args)
            report = json.loads((root / 'test-host-report.json').read_text())
            self.assertEqual(report['count'], 1)
            self.assertEqual(report['successful_completions'], 1)
            self.assertEqual(report['seconds'], 1800)
            self.assertEqual(report['levels']['2']['exposure_seconds'], 1800)
            self.assertEqual(len(report['levels']['2']['jobs']), 1)
            self.assertEqual(report['levels']['2']['completions_per_hour'], 2)

    def test_passive_level_counts_only_success(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            args = argparse.Namespace(output=root, host=['primary'], seconds=1800)
            def measure(*unused):
                step = root / 'primary-measurement'
                step.mkdir()
                (step / 'samples.jsonl').write_text(' {"at":0,"busy":2}\n{"at":1800,"busy":2}\n')
                (step / 'matched.json').write_text(json.dumps({str(i): dict(started_at=10, completed_at=12, wait=1, duration=2, outcome=outcome)
                    for i, outcome in enumerate(('success', 'failure', 'cancelled', 'skipped'))}))
                return dict(cutoff=1800)
            with mock.patch.object(ramp, 'call', return_value=dict(load=[0], cpus=4)), \
                    mock.patch.object(ramp, 'measure', side_effect=measure):
                ramp.measure_only(args)
            report = json.loads((root / 'primary-report.json').read_text())
            self.assertEqual(report['levels']['2']['completions_per_hour'], 2)
            self.assertEqual(len(report['levels']['2']['jobs']), 4)

    def test_measurement_cohort_includes_finish_boundaries(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            args = argparse.Namespace(output=root, host=['test-host'], seconds=1800)
            starts = [9, 10, 1809, 1810, 1811]
            receipts = [dict(id=f'worker-{i}', vm={'destroyed_at': 'done'},
                             github=dict(repo='owner/repo', run_id=1)) for i in range(5)]
            def iso(value):
                return ramp.datetime.fromtimestamp(value, ramp.timezone.utc).isoformat()
            jobs = [dict(id=i, runner_name=f'worker-{i}', status='completed', conclusion='success',
                         created_at=iso(start-5), started_at=iso(start), completed_at=iso(start))
                    for i, start in enumerate(starts)]
            def sample(at, busy, entries, active):
                return dict(at=at, busy=busy, cpu=[at, 0, 0, at*9], available=100,
                            reserve=10, cpu_psi={'some': 0}, memory_psi={'full': 0}, oom=0, active=active,
                            cli='abc', cpus=4, total=100, memory=10, receipts=entries)
            responses = [sample(10, 2, [], []),
                         sample(1810, 3, [], ['worker-0']), sample(1840, 4, receipts, []),
                         dict(jobs=jobs, requests=1)]
            with mock.patch.object(ramp.time, 'time', return_value=0), \
                    mock.patch.object(ramp.time, 'sleep'), \
                    mock.patch.object(ramp, 'call', side_effect=responses):
                ramp.run(args)
            report = json.loads((root / 'test-host-report.json').read_text())
            self.assertEqual(report['count'], 3)
            self.assertEqual(report['successful_completions'], 3)
            self.assertEqual(report['levels']['2']['completions_per_hour'], 6)
            self.assertEqual(set(report['levels']), {'2'})
            self.assertEqual(report['levels']['2']['exposure_seconds'], 1800)
            self.assertEqual([job['started_at'] for job in report['levels']['2']['jobs']], [10, 1809, 1810])
            self.assertEqual(sum(len(level['jobs']) for level in report['levels'].values()), report['count'])

    def test_throughput_compares_rates_and_requires_more_than_five_percent(self):
        base = self.window(count=40)
        self.assertEqual(ramp.decision(base, self.window(count=84, seconds=3600), 5), 'STOP')
        self.assertEqual(ramp.decision(base, self.window(count=85, seconds=3600, wait_p90=100), 5), 'PASS')
        self.assertEqual(ramp.decision(base, self.window(count=39, wait_p90=0), 5), 'STOP')
        self.assertEqual(ramp.decision(base, self.window(count=50, cli='changed'), 5), 'INCOMPLETE')
        self.assertEqual(ramp.decision(base, self.window(count=0, unsafe=True), 5), 'STOP')

    def test_sample_uses_real_cpu_counters_and_admission_inputs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'coldbox'
            source.write_text('payload')
            (root / 'receipts').mkdir()
            state = dict(STATE=root, live_workers=lambda p: [], host_load=lambda workers: (2, 1),
                         host_setting=lambda *a: 2, RESERVE=root / 'reserve',
                         additional_vm_memory=lambda *a: 10, vm_memory=lambda: 5,
                         host_memory=lambda field='MemTotal': 100 if field == 'MemTotal' else 50,
                         HOST_RESERVE=5, ramp_room=lambda *a: 0)
            with mock.patch.object(ramp.runpy, 'run_path', return_value=state):
                result = ramp.remote(mock.Mock(action='sample', coldbox=str(source), since=0))
            self.assertEqual(result['cpus'], ramp.os.cpu_count()-2)
            self.assertEqual(result['total'], 90)
            self.assertEqual((result['busy'], result['warm'], result['active'], result['cap']), (2, 1, [], None))
            self.assertEqual(len(result['cpu']), 8)
            self.assertTrue(result['cap_supported'])


class Trials(unittest.TestCase):
    def window(self, count=40, **changes):
        return dict(count=count, successful_completions=count, seconds=1800, at_limit_samples=3, unsafe=False,
                    failures=0, duration_p90=100, cli='abc', saturated_seconds=1800, job_mix={'workflow/job': 1}, **changes)

    def run_trials(self, windows, responses=None):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        args = argparse.Namespace(output=Path(folder.name), host=['primary'], seconds=1800,
                                  trials=True, limits=[2, 3, 4], max_load=None)
        initial = dict(load=[0], cpus=8, total=100, reserve=10, memory=10,
                       cap=None, cap_supported=True)
        with mock.patch.object(ramp, 'call', side_effect=responses or [initial, {'applied': True},
                               {'applied': True}, {'applied': True}, {'applied': True}]) as call, \
                mock.patch.object(ramp, 'measure', side_effect=windows):
            ramp.run(args)
        return json.loads((args.output / 'primary-report.json').read_text()), call

    def test_sparse_demand_and_changed_mix_are_inconclusive(self):
        for changes in ({'saturated_seconds': 90}, {'job_mix': {'different/job': 1}}):
            with self.subTest(changes=changes):
                report, _ = self.run_trials([self.window(), dict(self.window(), **changes)])
                self.assertEqual(report['status'], 'INCOMPLETE')
                self.assertIsNone(report['measured_cap'])

    def test_trial_preflight_ignores_expired_lease(self):
        initial = dict(at=100, load=[0], cpus=8, total=100, reserve=10, memory=10,
                       cap=dict(owner='old', expires=99), cap_supported=True)
        report, call = self.run_trials([self.window(40), self.window(50), self.window(51)],
                                      [initial, {'applied': True}, {'applied': True},
                                       {'applied': True}, {'applied': True}])
        self.assertEqual(report['measured_cap'], 3)
        self.assertIn('--acquire', call.call_args_list[1].args)

    def test_expired_foreign_lease_can_be_replaced_when_idle(self):
        from contextlib import nullcontext
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'ramp-cap.json'
            path.write_text(json.dumps(dict(owner='old', limit=2, expires=99)))
            state = dict(STATE=root, host_lock=nullcontext, live_workers=lambda _: [],
                         host_load=lambda _: (1, 0), RESERVE=root / 'reserve', host_setting=lambda *a: 0)
            args = argparse.Namespace(action='trial-cap', coldbox='unused', owner='a'*32,
                                      limit=2, seconds=1800, acquire=True, max_load=None)
            with mock.patch.object(ramp.runpy, 'run_path', return_value=state), \
                    mock.patch.object(ramp.os, 'getloadavg', return_value=(0, 0, 0)), \
                    mock.patch.object(ramp.time, 'time', return_value=100):
                self.assertFalse(ramp.remote(args)['applied'])
                self.assertEqual(json.loads(path.read_text())['owner'], 'old')
                state['host_load'] = lambda _: (0, 0)
                self.assertTrue(ramp.remote(args)['applied'])
                self.assertEqual(json.loads(path.read_text())['owner'], 'a'*32)

    def test_zero_success_windows_cannot_select_cap(self):
        for successes in (0, 1, 29):
            report, _ = self.run_trials([dict(self.window(), successful_completions=successes),
                                         dict(self.window(), successful_completions=successes)])
            self.assertEqual(report['status'], 'INCOMPLETE')
            self.assertIsNone(report['measured_cap'])

    def test_plateau_selects_previous_measured_limit(self):
        report, call = self.run_trials([self.window(40), self.window(50), self.window(51)])
        self.assertEqual(report['measured_cap'], 3)
        self.assertEqual(report['status'], 'STOP')
        self.assertEqual([t['limit'] for t in report['trials']], [2, 3, 4])
        self.assertFalse(report['configured'])
        self.assertEqual(call.call_args_list[-1].args[2:], ('trial-cap',))

    def test_degradation_selects_control(self):
        report, _ = self.run_trials([self.window(40), self.window(35)])
        self.assertEqual(report['measured_cap'], 2)

    def test_improving_windows_do_not_guess_a_cap(self):
        report, _ = self.run_trials([self.window(40), self.window(50), self.window(60)])
        self.assertEqual(report['status'], 'INCOMPLETE')
        self.assertIsNone(report['measured_cap'])

    def test_incomplete_and_unsafe_windows_do_not_select(self):
        for window in (dict(self.window(), count=29), dict(self.window(), unsafe=True)):
            with self.subTest(window=window):
                report, _ = self.run_trials([self.window(), window])
                self.assertIsNone(report['measured_cap'])

    def test_measurement_error_cleans_up(self):
        report, _ = self.run_trials([ValueError('lease expired')])
        self.assertEqual(report['status'], 'INCOMPLETE')
        self.assertEqual(report['cleanup'], {'applied': True})

    def test_busy_cleanup_is_reported_without_force(self):
        initial = dict(load=[0], cpus=8, total=100, reserve=10, memory=10,
                       cap=None, cap_supported=True)
        report, _ = self.run_trials([ValueError('lost sample')],
                                   [initial, {'applied': True}, {'applied': False, 'reason': 'runner busy'}])
        self.assertEqual(report['cleanup'], {'applied': False, 'reason': 'runner busy'})
        self.assertIsNone(report['measured_cap'])

    def test_remote_busy_preserves_cap_and_idle_acquires_removes(self):
        from contextlib import nullcontext
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'receipts').mkdir()
            path = root / 'ramp-cap.json'
            args = argparse.Namespace(action='trial-cap', coldbox='unused', owner='a'*32,
                                      limit=2, seconds=1800, acquire=True, max_load=None)
            state = dict(STATE=root, host_lock=nullcontext, live_workers=lambda _: [],
                         host_load=lambda _: (1, 0), RESERVE=root / 'reserve', host_setting=lambda *a: 0)
            with mock.patch.object(ramp.runpy, 'run_path', return_value=state), \
                    mock.patch.object(ramp.os, 'getloadavg', return_value=(0, 0, 0)), \
                    mock.patch.object(ramp.time, 'time', return_value=100):
                self.assertFalse(ramp.remote(args)['applied'])
                self.assertFalse(path.exists())
                state['host_load'] = lambda _: (0, 0)
                self.assertEqual(ramp.remote(args), {'applied': True})
                self.assertEqual(json.loads(path.read_text()), dict(limit=2, owner='a'*32, expires=23800))
                original = path.read_text()
                state['host_load'] = lambda _: (1, 0)
                args.limit = None
                self.assertFalse(ramp.remote(args)['applied'])
                self.assertEqual(path.read_text(), original)
                state['host_load'] = lambda _: (0, 0)
                self.assertEqual(ramp.remote(args), {'applied': True})
                self.assertFalse(path.exists())

    def test_accept_between_idle_check_and_cap_mutation(self):
        from contextlib import nullcontext
        for limit in (2, 3, None):
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / 'receipts').mkdir()
                log = root / 'receipts/warm.log'
                log.write_text('Listening for Jobs\n')
                path = root / 'ramp-cap.json'
                original = dict(limit=2, owner='a'*32, expires=9999999999)
                if limit != 2:
                    path.write_text(json.dumps(original))
                worker = argparse.Namespace(name='warm', kind='base', target='owner/repo')
                args = argparse.Namespace(action='trial-cap', coldbox='unused', owner='a'*32,
                                          limit=limit, seconds=1800, acquire=True, max_load=None)
                def load(workers):
                    result = cb.host_load(workers)
                    return result
                with mock.patch.object(cb, 'STATE', root), \
                        mock.patch.object(cb, 'host_vms', return_value={'warm'}), \
                        mock.patch.object(cb, 'registrations', return_value=[]), \
                        mock.patch.object(ramp.runpy, 'run_path', return_value=dict(
                            STATE=root, host_lock=nullcontext, live_workers=lambda _: [worker],
                            host_load=load, RESERVE=root/'reserve', host_setting=lambda *a: 0)), \
                        mock.patch.object(ramp.os, 'getloadavg', return_value=(0, 0, 0)):
                    result = ramp.remote(args)
                    log.write_text('Running job: test\n')
                    self.assertFalse(result['applied'])
                self.assertEqual(json.loads(path.read_text()) if path.exists() else None,
                                 original if limit != 2 else None)

    def test_configured_warm_slots_transition_to_idle(self):
        from contextlib import nullcontext
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'ramp-cap.json'
            worker = argparse.Namespace(name='warm', kind='base', target='owner/repo')
            args = argparse.Namespace(action='trial-cap', coldbox='unused', owner='a'*32,
                                      limit=2, seconds=1800, acquire=True, max_load=None)
            with mock.patch.object(cb, 'STATE', root), \
                    mock.patch.object(cb, 'host_vms', return_value={'warm'}), \
                    mock.patch.object(cb, 'registrations', return_value=[dict(
                        target='owner/repo', slots=4, configured=True)]), \
                    mock.patch.object(ramp.runpy, 'run_path', return_value=dict(
                        STATE=root, host_lock=nullcontext, live_workers=lambda _: [worker],
                        host_load=cb.host_load, RESERVE=root/'reserve', host_setting=lambda *a: 0)), \
                    mock.patch.object(ramp.os, 'getloadavg', return_value=(0, 0, 0)):
                self.assertEqual(cb.host_load([worker]), (0, 1))
                self.assertFalse(ramp.remote(args)['applied'])
                self.assertFalse(path.exists())
                path.write_text(json.dumps(dict(limit=2, owner='a'*32, expires=9999999999)))
                (root / 'receipts').mkdir()
                log = root / 'receipts/warm.log'
                log.write_text('Running job: test\n')
                args.acquire = False
                args.limit = 3
                self.assertFalse(ramp.remote(args)['applied'])
                self.assertEqual(json.loads(path.read_text())['limit'], 2)
                log.write_text('Listening for Jobs\n')
                self.assertEqual(cb.host_load([worker]), (0, 1))
                self.assertFalse(ramp.remote(args)['applied'])
                self.assertEqual(json.loads(path.read_text())['limit'], 2)
                state = ramp.runpy.run_path('unused')
                state['live_workers'] = lambda _: []
                cb.host_vms.return_value = set()
                self.assertTrue(ramp.remote(args)['applied'])
                self.assertEqual(json.loads(path.read_text())['limit'], 3)
                args.limit = None
                self.assertTrue(ramp.remote(args)['applied'])
                self.assertFalse(path.exists())

    def test_lost_response_after_write_attempts_owned_cleanup(self):
        for busy in (False, True):
            with self.subTest(busy=busy), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                path = root / 'ramp-cap.json'
                args = argparse.Namespace(output=root, host=['primary'], seconds=1800,
                                          trials=True, limits=[2, 3], max_load=None)
                def call(a, host, action, *extra):
                    if action == 'sample':
                        return dict(load=[0], cpus=8, total=100, reserve=10,
                                    memory=10, cap=None, cap_supported=True)
                    if extra:
                        path.write_text(json.dumps(dict(owner=a.owner, limit=2)))
                        raise ramp.subprocess.TimeoutExpired('ssh', 120)
                    self.assertEqual(json.loads(path.read_text())['owner'], a.owner)
                    if busy:
                        return dict(applied=False, reason='runner busy; cap preserved')
                    path.unlink()
                    return dict(applied=True)
                with mock.patch.object(ramp, 'call', side_effect=call):
                    ramp.run(args)
                report = json.loads((root/'primary-report.json').read_text())
                self.assertEqual(report['cleanup']['applied'], not busy)
                self.assertEqual(path.exists(), busy)
                if busy:
                    self.assertIn('busy', report['cleanup']['reason'])

    def test_zero_success_trials_never_select_cap(self):
        report, _ = self.run_trials([dict(self.window(), successful_completions=0)] * 2)
        self.assertEqual(report['status'], 'INCOMPLETE')
        self.assertIsNone(report['measured_cap'])

    def test_foreign_cap_is_preserved(self):
        from contextlib import nullcontext
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'ramp-cap.json'
            path.write_text('{"owner":"other","limit":2}')
            with mock.patch.object(ramp.runpy, 'run_path', return_value=dict(STATE=Path(folder), host_lock=nullcontext)):
                with self.assertRaisesRegex(ValueError, 'another run'):
                    ramp.remote(argparse.Namespace(action='trial-cap', coldbox='unused', owner='ours'))
            self.assertEqual(path.read_text(), '{"owner":"other","limit":2}')

    def test_busy_transition_waits_then_changes_limit(self):
        initial = dict(load=[0], cpus=8, total=100, reserve=10, memory=10,
                       cap=None, cap_supported=True)
        with mock.patch.object(ramp.time, 'sleep') as sleep:
            report, call = self.run_trials([self.window(40), self.window(35)],
                [initial, {'applied': True}, {'applied': False, 'reason': 'runner busy'},
                 {'applied': True}, {'applied': True}])
        self.assertEqual(report['measured_cap'], 2)
        self.assertEqual(sleep.call_args.args, (30,))
        self.assertEqual(call.call_args_list[2].args[3:5], ('--limit', 3))

    def test_busy_deadline_leaves_no_recommendation(self):
        initial = dict(load=[0], cpus=8, total=100, reserve=10, memory=10,
                       cap=None, cap_supported=True)
        with mock.patch.object(ramp.time, 'monotonic', side_effect=[0, 901]):
            report, _ = self.run_trials([], [initial, {'applied': False, 'reason': 'runner busy'}])
        self.assertEqual(report['status'], 'INCOMPLETE')
        self.assertIsNone(report['measured_cap'])
        self.assertEqual(report['reason'], 'runner busy')

    def test_expired_lease_aborts_real_measurement(self):
        with tempfile.TemporaryDirectory() as folder:
            args = argparse.Namespace(output=Path(folder), seconds=1800, owner='ours', trials=True)
            with mock.patch.object(ramp, 'call', return_value=dict(at=100,
                    cap=dict(limit=2, owner='ours', expires=100))):
                with self.assertRaisesRegex(ValueError, 'expired'):
                    ramp.measure(args, 'primary', Path(folder) / 'trial', 2)

    def test_trial_validation_rejects_duplicate_and_descending_limits(self):
        with tempfile.TemporaryDirectory() as folder:
            for limits in ([2, 2], [3, 2], [0, 1], [2]):
                args = argparse.Namespace(output=Path(folder), seconds=1800, trials=True, limits=limits)
                with self.subTest(limits=limits), self.assertRaisesRegex(ValueError, 'increasing'):
                    ramp.run(args)
