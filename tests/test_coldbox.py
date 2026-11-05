import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('coldbox', str(ROOT / 'coldbox'))
spec = importlib.util.spec_from_loader('coldbox', loader)
cb = importlib.util.module_from_spec(spec)
loader.exec_module(cb)


def repo(files):
    tmp = Path(tempfile.mkdtemp())
    for name, text in files.items():
        (tmp / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp / name).write_text(text)
    return tmp


def quiet(fn, *args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        return fn(*args), out.getvalue()


class Detect(unittest.TestCase):
    def test_python_repo_gets_lint_and_pytest(self):
        found = cb.detect(repo({'pyproject.toml': '', 'tests/test_x.py': ''}))
        self.assertEqual(found['test'], ['python3 -m pytest -q'])
        self.assertIn('compileall', found['lint'][0])
        self.assertNotIn('setup', found)

    def test_python_without_tests_has_no_test_check(self):
        self.assertNotIn('test', cb.detect(repo({'app.py': ''})))

    def test_node_uses_lockfile_and_skips_npm_placeholder_test(self):
        pkg = json.dumps({'scripts': {'lint': 'eslint .', 'test': 'echo "Error: no test specified" && exit 1'}})
        found = cb.detect(repo({'package.json': pkg, 'package-lock.json': '{}'}))
        self.assertEqual(found['setup'], ['npm ci'])
        self.assertEqual(found['lint'], ['npm run lint'])
        self.assertNotIn('test', found)

    def test_go_rust_shell_and_verify(self):
        found = cb.detect(repo({'go.mod': '', 'z_test.go': '', 'Cargo.toml': '', 'src/lib.rs': '#[cfg(test)]\nmod t {\n    #[test]\n    fn a() {}\n}\n',
                                'bin/x.sh': '', 'verify.sh': ''}))
        self.assertEqual(found['test'], [cb.GO_TEST, cb.CARGO_TEST])
        self.assertTrue(any('shellcheck' in c for c in found['lint']))
        self.assertEqual(found['verify'], ['bash verify.sh'])

    def test_go_and_rust_without_tests_have_no_test_check(self):
        found = cb.detect(repo({'go.mod': '', 'z.go': 'package z\n', 'Cargo.toml': '', 'src/lib.rs': 'pub fn f() {}\n'}))
        self.assertNotIn('test', found)
        self.assertEqual(len(found['lint']), 2)

    def test_rust_async_test_attribute_counts_as_a_test(self):
        found = cb.detect(repo({'Cargo.toml': '', 'tests/it.rs': '#[tokio::test]\nasync fn a() {}\n'}))
        self.assertEqual(found['test'], [cb.CARGO_TEST])

    def test_go_and_rust_test_commands_fail_when_no_test_ran(self):
        tmp = Path(tempfile.mkdtemp())
        cases = [(cb.GO_TEST, 'go', 'ok  \tz\t0.01s [no tests to run]', 0, 1),
                 (cb.GO_TEST, 'go', '=== RUN   TestA\n--- PASS: TestA (0.00s)\nok  \tz', 0, 0),
                 (cb.GO_TEST, 'go', '--- FAIL: TestA (0.00s)\n--- PASS: TestB (0.00s)\nFAIL', 1, 1),
                 (cb.CARGO_TEST, 'cargo', 'running 0 tests\ntest result: ok. 0 passed; 0 failed', 0, 1),
                 (cb.CARGO_TEST, 'cargo', 'running 2 tests\ntest result: ok. 2 passed; 0 failed', 0, 0),
                 (cb.CARGO_TEST, 'cargo', 'test result: ok. 1 passed\ntest result: FAILED. 0 passed; 1 failed', 101, 1)]
        for command, tool, output, code, want in cases:
            (tmp / tool).write_text(f'#!/bin/sh\nprintf "%s\\n" "{output}"\nexit {code}\n')
            (tmp / tool).chmod(0o755)
            result = subprocess.run(['bash', '-eo', 'pipefail', '-c', command], capture_output=True, text=True,
                                    env=dict(os.environ, PATH=f'{tmp}:{os.environ["PATH"]}'))
            self.assertEqual(result.returncode != 0, bool(want), (command, output, result.stderr))

    def test_ignores_files_in_dot_and_vendor_dirs(self):
        self.assertEqual(cb.detect(repo({'.git/hooks/x.sh': '', 'node_modules/a/b.py': ''})), {})


class Config(unittest.TestCase):
    def test_switch_off_and_override(self):
        root = repo({'tests/test_x.py': '', 'coldbox.toml': '[checks]\nlint = false\n[commands]\ntest = "make t"\n'})
        self.assertEqual(cb.plan(root), [('test', ['make t'])])

    def test_custom_check_runs_after_builtins(self):
        root = repo({'tests/test_x.py': '', 'coldbox.toml': '[commands]\ntypecheck = "mypy ."\n'})
        self.assertEqual([name for name, _ in cb.plan(root)], ['lint', 'test', 'typecheck'])

    def test_rejects_bad_config(self):
        for text in ('[check]\nlint = true\n', '[checks]\nlint = "yes"\n', '[commands]\ntest = ""\n',
                     '[checks]\nverify = true\n', '[commands]\n"Bad Name" = "x"\n', 'not toml ['):
            with self.subTest(text=text), self.assertRaises(cb.ConfigError):
                cb.plan(repo({'app.py': '', 'coldbox.toml': text}))


class Checks(unittest.TestCase):
    def test_records_pass_fail_and_exit_code(self):
        root = repo({'coldbox.toml': '[commands]\nlint = "true"\ntest = "exit 3"\n'})
        out = root / 'checks.json'
        code, _ = quiet(cb.run_checks, root, out)
        result = json.loads(out.read_text())
        self.assertEqual(code, 1)
        self.assertEqual(result['verdict'], 'fail')
        self.assertEqual([(r['check'], r['status'], r['exit']) for r in result['checks']],
                         [('lint', 'pass', 0), ('test', 'fail', 3)])

    def test_failed_setup_skips_the_rest(self):
        root = repo({'coldbox.toml': '[commands]\nsetup = "false"\ntest = "true"\n'})
        out = root / 'checks.json'
        quiet(cb.run_checks, root, out)
        self.assertEqual([r['status'] for r in json.loads(out.read_text())['checks']], ['fail', 'skipped'])

    def test_nothing_to_run_is_a_failure(self):
        root = repo({'README.md': ''})
        code, output = quiet(cb.run_checks, root, root / 'checks.json')
        self.assertEqual(code, 1)
        self.assertIn('nothing to run', output)

    def test_pipeline_failure_is_not_hidden(self):
        root = repo({'coldbox.toml': '[commands]\ntest = "false | cat"\n'})
        self.assertEqual(quiet(cb.run_checks, root, None)[0], 1)

    def test_on_github_each_failure_and_failed_test_is_an_error_annotation(self):
        fake = ("import os, re, pathlib, sys\n"
                "path = pathlib.Path(re.search(r'--junitxml=(\\S+)', os.environ['PYTEST_ADDOPTS'])[1])\n"
                "path.parent.mkdir(parents=True, exist_ok=True)\n"
                f"path.write_text({JUNIT!r})\n"
                "sys.exit(1)\n")
        root = repo({'fake_pytest.py': fake,
                     'coldbox.toml': '[checks]\nlint = false\n[commands]\nsetup = "true"\ntest = "python3 fake_pytest.py"\n'})
        with mock.patch.dict(os.environ, GITHUB_ACTIONS='true'):
            _, output = quiet(cb.run_checks, root, Path(tempfile.mkdtemp()) / 'checks.json')
        self.assertEqual([line for line in output.splitlines() if line.startswith('::error')],
                         ['::error title=coldbox 2 test(s) failed::tests.test_a.test_bad, tests.test_a.test_boom',
                          '::error title=coldbox test failed::python3 fake_pytest.py exited 1'])
        with mock.patch.dict(os.environ, GITHUB_ACTIONS='true'):
            _, output = quiet(cb.run_checks, repo({'README.md': ''}), None)
        self.assertIn('::error title=coldbox found nothing to run::', output)
        with mock.patch.dict(os.environ):
            os.environ.pop('GITHUB_ACTIONS', None)
            _, output = quiet(cb.run_checks, root, Path(tempfile.mkdtemp()) / 'checks.json')
        self.assertNotIn('::error', output)


    def test_failed_test_annotation_counts_past_the_result_limit_and_ignores_earlier_runs(self):
        cases = '<testcase classname="t" name="ok"/>' * cb.MAX_TEST_RESULTS + '<testcase classname="t" name="late"><failure/></testcase>'
        fake = ("import os, re, pathlib, sys\n"
                "path = pathlib.Path(re.search(r'--junitxml=(\\S+)', os.environ['PYTEST_ADDOPTS'])[1])\n"
                "path.parent.mkdir(parents=True, exist_ok=True)\n"
                f"path.write_text({'<testsuite name=\"s\">' + cases + '</testsuite>'!r})\n"
                "sys.exit(1)\n")
        root = repo({'fake_pytest.py': fake, 'coldbox.toml': '[checks]\nlint = false\n[commands]\ntest = "python3 fake_pytest.py"\n'})
        job = Path(tempfile.mkdtemp())

        def checks():
            env = dict(os.environ, GITHUB_ACTIONS='true', COLDBOX_JOB_DIR=str(job))
            output = subprocess.run([sys.executable, str(ROOT / 'coldbox'), 'checks', str(root)], env=env,
                                    capture_output=True, text=True).stdout
            return [line for line in output.splitlines() if line.startswith('::error')]

        self.assertEqual(checks(), ['::error title=coldbox 1 test(s) failed::...',
                                    '::error title=coldbox test failed::python3 fake_pytest.py exited 1'])
        (root / 'coldbox.toml').write_text('[checks]\nlint = false\n[commands]\ntest = "true"\n')
        self.assertEqual(checks(), [])


class InstallOptions(unittest.TestCase):
    def install(self, *args):
        return subprocess.run(['bash', str(ROOT / 'install.sh'), *args], capture_output=True, text=True,
                              env={'PATH': os.environ['PATH']})

    def test_bad_size_options_are_refused_before_anything_is_installed(self):
        for args, message in ((['--cpus', '0'], '--cpus must be a whole number'),
                              (['--memory', '8'], '--memory must be a size'),
                              (['--disk', 'big'], '--disk must be a size'),
                              (['--memory', ''], '--memory must be a size'),
                              (['--cpus'], '--cpus needs a value'),
                              (['--slots', '2'], 'unknown option --slots')):
            with self.subTest(args):
                result = self.install(*args)
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)

    @unittest.skipIf(os.geteuid() == 0, 'would really install as root')
    def test_good_size_options_and_existing_env_values_are_accepted(self):
        result = self.install('--cpus', '4', '--memory', '8GiB', '--disk', '60GiB')
        self.assertEqual((result.returncode, result.stderr), (1, 'coldbox install: run with sudo\n'))
        result = subprocess.run(['bash', str(ROOT / 'install.sh')], capture_output=True, text=True,
                                env={'PATH': os.environ['PATH'], 'COLDBOX_CPUS': '0-3'})
        self.assertEqual((result.returncode, result.stderr), (1, 'coldbox install: run with sudo\n'))


JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="4">
<testcase classname="tests.test_a" name="test_ok" time="0.010"/>
<testcase classname="tests.test_a" name="test_bad" time="0.020"><failure message="assert 1 == 2">trace</failure></testcase>
<testcase classname="tests.test_a" name="test_boom" time="x"><error message="fixture"/></testcase>
<testcase classname="tests.test_a" name="test_later" time="0"><skipped message="todo"/></testcase>
</testsuite></testsuites>
"""


class ExampleReceipts(unittest.TestCase):
    def test_examples_are_v1_and_their_verdicts_still_hold(self):
        examples = sorted((ROOT / 'examples' / 'receipts').glob('*.json'))
        self.assertGreaterEqual(len(examples), 3)
        for path in examples:
            receipt = json.loads(path.read_text())
            with self.subTest(path.name):
                self.assertEqual(receipt['schema'], 'coldbox.receipt/v1')
                self.assertEqual(cb.verdict(receipt), receipt['verdict'])
                self.assertEqual(receipt['tests']['total'], len(receipt['tests']['results']))


class Tests(unittest.TestCase):
    def test_checks_ask_pytest_for_junit_and_results_are_parsed(self):
        fake = ("import os, re, pathlib\n"
                "path = pathlib.Path(re.search(r'--junitxml=(\\S+)', os.environ['PYTEST_ADDOPTS'])[1])\n"
                "pathlib.Path('addopts.txt').write_text(os.environ['PYTEST_ADDOPTS'])\n"
                "path.parent.mkdir(parents=True, exist_ok=True)\n"
                f"path.write_text({JUNIT!r})\n")
        root = repo({'fake_pytest.py': fake, 'coldbox.toml': '[checks]\nlint = false\n[commands]\ntest = "python3 fake_pytest.py"\n'})
        job = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, PYTEST_ADDOPTS='-p no:cacheprovider'):
            quiet(cb.run_checks, root, job / 'checks.json')
        tests = cb.junit_summary([job / 'junit'])
        self.assertEqual({k: tests[k] for k in ('total', 'passed', 'failed', 'error', 'skipped')},
                         dict(total=4, passed=1, failed=1, error=1, skipped=1))
        self.assertEqual(tests['results'][1], dict(suite='pytest', classname='tests.test_a', name='test_bad',
                                                   status='failed', seconds=0.02))
        self.assertIsNone(tests['results'][2]['seconds'])
        self.assertEqual((root / 'addopts.txt').read_text(), f"-p no:cacheprovider --junitxml={job / 'junit' / str(os.getpid()) / '0.xml'}")

    def test_junit_command_finds_reports_and_ignores_other_xml(self):
        top = repo({'build/report.xml': JUNIT, 'pom.xml': '<project/>', 'bad.xml': '<testsuite><testcase',
                    'node_modules/x/junit.xml': JUNIT, '.cache/junit.xml': JUNIT})
        out = subprocess.run([sys.executable, str(ROOT / 'coldbox'), 'junit', str(top), str(top / 'missing')],
                             capture_output=True, text=True, check=True).stdout
        tests = json.loads(out)
        self.assertEqual(tests['files'], [str(top / 'build' / 'report.xml')])
        self.assertEqual(tests['total'], 4)
        self.assertFalse(tests['truncated'])

    def test_projects_own_junitxml_setting_is_left_alone(self):
        fake = "import os, pathlib\npathlib.Path('addopts.txt').write_text(os.environ.get('PYTEST_ADDOPTS', 'unset'))\n"
        root = repo({'fake_pytest.py': fake, 'pyproject.toml': '[tool.pytest.ini_options]\naddopts = "--junitxml=build/r.xml"\n',
                     'coldbox.toml': '[checks]\nlint = false\n[commands]\ntest = "python3 fake_pytest.py"\n'})
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('PYTEST_ADDOPTS', None)
            quiet(cb.run_checks, root, Path(tempfile.mkdtemp()) / 'checks.json')
        self.assertEqual((root / 'addopts.txt').read_text(), 'unset')

    def test_a_junitxml_setting_written_by_an_earlier_command_wins(self):
        gen = "import pathlib\npathlib.Path('pyproject.toml').write_text('[tool.pytest.ini_options]\\naddopts = \"--junitxml=own.xml\"\\n')\n"
        fake = "import os, pathlib\npathlib.Path('addopts.txt').write_text(os.environ.get('PYTEST_ADDOPTS', 'unset'))\n"
        root = repo({'gen.py': gen, 'fake_pytest.py': fake,
                     'coldbox.toml': '[checks]\nlint = false\n[commands]\nsetup = "python3 gen.py"\ntest = "python3 fake_pytest.py"\n'})
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('PYTEST_ADDOPTS', None)
            quiet(cb.run_checks, root, Path(tempfile.mkdtemp()) / 'checks.json')
        self.assertEqual((root / 'addopts.txt').read_text(), 'unset')

    def test_files_copied_in_from_git_archive_are_not_reports(self):
        top = repo({'fixtures/report.xml': JUNIT, 'build/report.xml': JUNIT})
        (top / 'archived').write_text(f"{top / 'fixtures' / 'report.xml'}\n{top / 'fixtures'}/\n")
        self.assertEqual(cb.junit_summary([top], archived=top / 'archived')['files'], [str(top / 'build' / 'report.xml')])

    def test_only_reports_written_during_the_job_count(self):
        top = repo({'committed/report.xml': JUNIT, 'old/report.xml': JUNIT})
        git = ['git', '-C', str(top), '-c', 'user.name=t', '-c', 'user.email=t@example.invalid']
        subprocess.run(git[:3] + ['init', '-q'], check=True)
        subprocess.run(git + ['add', 'committed'], check=True)
        subprocess.run(git + ['commit', '-qm', 'fixture'], check=True)
        os.utime(top / 'old' / 'report.xml', (time.time() - 60, time.time() - 60))
        marker = top / '.started'
        marker.touch()
        (top / 'new').mkdir()
        (top / 'new' / 'report.xml').write_text(JUNIT)
        os.utime(top / 'committed' / 'report.xml')
        out = subprocess.run([sys.executable, str(ROOT / 'coldbox'), 'junit', '--newer', str(marker), str(top)],
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(out)['files'], [str(top / 'new' / 'report.xml')])

    def test_no_reports_means_no_tests_key(self):
        self.assertEqual(cb.junit_summary([Path(tempfile.mkdtemp())])['total'], 0)


class ForkGuard(unittest.TestCase):
    def run_hook(self, head, allow=False):
        event = {'pull_request': {'head': {'repo': head and {'full_name': head}},
                                  'base': {'repo': {'full_name': 'me/app'}}}}
        tmp = Path(tempfile.mkdtemp())
        (tmp / 'event.json').write_text(json.dumps(event))
        env = {'GITHUB_EVENT_PATH': str(tmp / 'event.json'), 'GITHUB_REPOSITORY': 'me/app', 'GITHUB_SHA': 'abc',
               'GITHUB_EVENT_NAME': 'pull_request', 'COLDBOX_JOB_DIR': str(tmp)}
        if allow:
            env['COLDBOX_ALLOW_FORKS'] = '1'
        code, _ = quiet(cb.job_started, env)
        return code, json.loads((tmp / 'job.json').read_text())

    def test_same_repo_pr_runs_and_is_recorded(self):
        code, job = self.run_hook('me/app')
        self.assertEqual((code, job['repo'], job['sha'], job['fork_pr']), (0, 'me/app', 'abc', False))

    def test_fork_pr_is_refused(self):
        code, job = self.run_hook('stranger/app')
        self.assertEqual((code, job['refused']), (1, 'fork pull request'))

    def test_deleted_fork_is_refused(self):
        self.assertEqual(self.run_hook(None)[0], 1)

    def test_fork_allowed_only_with_opt_in(self):
        self.assertEqual(self.run_hook('stranger/app', allow=True)[0], 0)

    def event(self, name, payload, allow=False):
        tmp = Path(tempfile.mkdtemp())
        (tmp / 'event.json').write_text(json.dumps(payload))
        env = {'GITHUB_EVENT_PATH': str(tmp / 'event.json'), 'GITHUB_EVENT_NAME': name, 'COLDBOX_JOB_DIR': str(tmp)}
        if allow:
            env['COLDBOX_ALLOW_FORKS'] = '1'
        code, _ = quiet(cb.job_started, env)
        return code, json.loads((tmp / 'job.json').read_text())

    def test_push_event_runs(self):
        self.assertEqual(self.event('push', {'ref': 'refs/heads/main'})[0], 0)

    def test_merge_queue_is_refused_without_opt_in(self):
        payload = {'merge_group': {'head_ref': 'refs/heads/gh-readonly-queue/main/pr-1'}}
        code, job = self.event('merge_group', payload)
        self.assertEqual(code, 1)
        self.assertIsNone(job['fork_pr'])
        self.assertIn('merge_group', job['refused'])
        self.assertEqual(self.event('merge_group', payload, allow=True)[0], 0)

    def test_workflow_run_checks_the_head_repository(self):
        def payload(head):
            return {'repository': {'full_name': 'me/app'}, 'workflow_run': {'head_repository': {'full_name': head}}}
        self.assertEqual(self.event('workflow_run', payload('me/app'))[0], 0)
        code, job = self.event('workflow_run', payload('stranger/app'))
        self.assertEqual((code, job['fork_pr']), (1, True))

    def test_unknown_or_missing_event_fails_closed(self):
        self.assertEqual(self.event('issue_comment', {'issue': {'number': 1}})[0], 1)
        self.assertEqual(self.event('', {})[0], 1)


class Acl(unittest.TestCase):
    def test_blocks_lan_tailnet_and_host_but_allows_internet(self):
        import ipaddress
        doc = cb.acl_document([{'addr_info': [{'family': 'inet', 'local': '8.8.4.4'}]}])
        allowed = [ipaddress.ip_network(r['destination']) for r in doc['egress']
                   if r['action'] == 'allow' and 'protocol' not in r]

        def reachable(ip):
            return any(ipaddress.ip_address(ip) in net for net in allowed)

        for ip in ('192.168.1.10', '10.204.0.1', '100.64.0.5', '172.16.0.1', '169.254.169.254', '127.0.0.1'):
            self.assertFalse(reachable(ip), ip)
        self.assertTrue(reachable('140.82.112.3'))
        self.assertIn({'action': 'reject', 'state': 'enabled', 'destination': '8.8.4.4'}, doc['egress'])

    def test_sync_applies_the_hosts_current_addresses(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / 'ip').write_text("#!/bin/sh\necho '[{\"addr_info\": [{\"family\": \"inet\", \"local\": \"1.2.3.4\"}]}]'\n")
        (tmp / 'incus').write_text(f'#!/bin/sh\necho "$@" >> {tmp}/calls\ncat > {tmp}/acl.json\n')
        for fake in ('ip', 'incus'):
            (tmp / fake).chmod(0o755)
        old_path = os.environ['PATH']
        os.environ['PATH'] = f'{tmp}:{old_path}'
        try:
            cb.sync_acl()
        finally:
            os.environ['PATH'] = old_path
        self.assertEqual((tmp / 'calls').read_text().split(), ['--force-local', 'network', 'acl', 'edit', 'coldbox-isolated'])
        egress = json.loads((tmp / 'acl.json').read_text())['egress']
        self.assertEqual(egress[0], {'action': 'reject', 'state': 'enabled', 'destination': '1.2.3.4'})


class Guard(unittest.TestCase):
    def setUp(self):
        self.real = cb.gh

    def tearDown(self):
        cb.gh = self.real

    def test_public_repo_refused_without_flag(self):
        cb.gh = lambda path: {'visibility': 'public'}
        with self.assertRaises(SystemExit) as caught:
            cb.guard('me/app', False)
        self.assertIn('--allow-public', str(caught.exception))
        cb.guard('me/app', True)

    def test_internal_repo_counts_as_not_private(self):
        cb.gh = lambda path: {'visibility': 'internal'}
        with self.assertRaises(SystemExit):
            cb.guard('org/app', False)

    def test_private_repo_and_closed_org_allowed(self):
        cb.gh = lambda path: {'visibility': 'private', 'allows_public_repositories': False}
        cb.guard('me/app', False)
        cb.guard('myorg', False)

    def test_org_group_open_to_public_refused(self):
        cb.gh = lambda path: {'allows_public_repositories': True}
        with self.assertRaises(SystemExit):
            cb.guard('myorg', False)

    def test_bad_target_rejected(self):
        with self.assertRaises(SystemExit):
            cb.scope('../etc')


class Verdict(unittest.TestCase):
    def receipt(self, **kw):
        return dict(mode='runner', vm={'destroyed': True}, **kw)

    def test_cases(self):
        self.assertEqual(cb.verdict(dict(mode='local', vm={'destroyed': True}, checks_verdict='pass', exit=0)), 'pass')
        self.assertEqual(cb.verdict(dict(mode='local', vm={'destroyed': True}, checks_verdict='pass', exit=137,
                                         error='TTL reached')), 'incomplete')
        self.assertEqual(cb.verdict(dict(mode='local', vm={'destroyed': True}, checks_verdict='pass')), 'incomplete')
        self.assertEqual(cb.verdict(self.receipt(checks_verdict='pass')), 'incomplete')
        self.assertEqual(cb.verdict(self.receipt(checks_verdict='fail')), 'fail')
        self.assertEqual(cb.verdict(dict(mode='local', vm={'destroyed': False}, checks_verdict='pass')), 'incomplete')
        self.assertEqual(cb.verdict(self.receipt(github={'refused': 'fork pull request'})), 'refused')
        self.assertEqual(cb.verdict(self.receipt(error='TTL reached')), 'idle')
        self.assertEqual(cb.verdict(self.receipt(github={'repo': 'me/app'})), 'incomplete')
        self.assertEqual(cb.verdict(self.receipt(github={'repo': 'me/app'}, job_result='Succeeded')), 'pass')
        self.assertEqual(cb.verdict(self.receipt(checks_verdict='pass', job_result='Failed')), 'fail')
        self.assertEqual(cb.verdict(self.receipt(checks_verdict='pass', job_result='Canceled')), 'incomplete')
        self.assertEqual(cb.verdict(self.receipt(github={'refused': 'fork pull request'}, job_result='Failed')),
                         'refused')

    def test_runner_success_without_hook_record_or_with_collection_error_is_incomplete(self):
        self.assertEqual(cb.verdict(self.receipt(job_result='Succeeded')), 'incomplete')
        self.assertEqual(cb.verdict(self.receipt(github={'repo': 'me/app'}, job_result='Succeeded',
                                                 error='Expecting value: line 1 column 1 (char 0)')), 'incomplete')

    def test_job_name_and_result_read_from_runner_log(self):
        log = b'Running job: checks\n2026-11-05 01:19:03Z: Job checks completed with result: Succeeded\n'
        self.assertEqual(cb.job_outcome(log), ('checks', 'Succeeded'))
        self.assertEqual(cb.job_outcome(b'Listening for Jobs\n'), (None, None))
        self.assertEqual(cb.job_outcome(b'2026-11-05 01:19:03Z: Job test (3.12) completed with result: Failed\n'),
                         ('test (3.12)', 'Failed'))


class Destroy(unittest.TestCase):
    def destroyed(self, info_code, info_stderr):
        calls = []

        def fake_run(command, **kw):
            calls.append(command)
            code, stderr = (info_code, info_stderr) if 'info' in command else (0, b'')
            return subprocess.CompletedProcess(command, code, b'', stderr)

        original, cb.subprocess.run = cb.subprocess.run, fake_run
        try:
            return cb.destroy('cbx-1')
        finally:
            cb.subprocess.run = original

    def test_only_instance_not_found_counts_as_destroyed(self):
        self.assertTrue(self.destroyed(1, b'Error: Failed to fetch instance "cbx-1" in project "coldbox": Instance not found\n'))
        self.assertFalse(self.destroyed(1, b'Error: Get "http://unix.socket/1.0": dial unix: connect: connection refused\n'))
        self.assertFalse(self.destroyed(0, b''))


class RunLocal(unittest.TestCase):
    def git_repo(self, files):
        root = repo(files)
        for command in (['init', '-q'], ['add', '.'], ['-c', 'user.name=t', '-c', 'user.email=t@example.com', 'commit', '-qm', 'x']):
            subprocess.run(['git', '-C', str(root), *command], check=True)
        return root

    def run_local(self, root):
        def no_job(*args, **kw):
            raise AssertionError('job started')
        original, cb.job = cb.job, no_job
        try:
            return cb.run_local(argparse.Namespace(path=str(root), ttl=60, offline=False))
        finally:
            cb.job = original

    def test_refuses_uncommitted_coldbox_toml(self):
        root = self.git_repo({'app.py': '', 'coldbox.toml': '[checks]\nlint = true\n'})
        (root / 'coldbox.toml').write_text('[checks]\nlint = false\n')
        with self.assertRaises(SystemExit) as raised:
            self.run_local(root)
        self.assertIn('coldbox.toml', str(raised.exception))

    def test_refuses_untracked_coldbox_toml(self):
        root = self.git_repo({'app.py': ''})
        (root / 'coldbox.toml').write_text('[checks]\nlint = false\n')
        with self.assertRaises(SystemExit):
            self.run_local(root)


class Reap(unittest.TestCase):
    def reap(self, ip_script):
        tmp = Path(tempfile.mkdtemp())
        now = int(time.time())
        receipts = tmp / 'state' / 'receipts'
        receipts.mkdir(parents=True)
        for name in ('cbx-old.json', 'cbx-old.log', 'cbx-new.json'):
            (receipts / name).write_text('{}')
        for name in ('cbx-old.json', 'cbx-old.log'):
            os.utime(receipts / name, (now - 31 * 86400, now - 31 * 86400))
        rows = [{'name': 'cbx-1-a', 'config': {'user.coldbox.expires': str(now - 5)}},
                {'name': 'cbx-2-b', 'config': {'user.coldbox.expires': str(now + 600)}},
                {'name': 'base-dev', 'config': {}},
                {'name': 'other', 'config': {'user.coldbox.expires': str(now - 5)}}]
        (tmp / 'rows.json').write_text(json.dumps(rows))
        fake = tmp / 'incus'
        fake.write_text('#!/bin/sh\n'
                        f'echo "$@" >> {tmp}/calls\n'
                        'case "$*" in\n'
                        f'  *list*) cat {tmp}/rows.json ;;\n'
                        '  *info*) echo "Error: Failed to fetch instance: Instance not found" >&2; exit 1 ;;\n'
                        '  *acl*) cat > /dev/null ;;\n'
                        'esac\n')
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        (tmp / 'ip').write_text(ip_script)
        (tmp / 'ip').chmod(0o755)
        env = dict(os.environ, PATH=f'{tmp}:{os.environ["PATH"]}', COLDBOX_STATE=str(tmp / 'state'))
        result = subprocess.run([sys.executable, str(ROOT / 'coldbox'), 'reap'], env=env, capture_output=True,
                                text=True)
        calls = (tmp / 'calls').read_text().splitlines()
        deletes = [line for line in calls if ' delete ' in line]
        self.assertEqual(deletes, ['--force-local --project coldbox delete cbx-1-a --force'])
        self.assertEqual(sorted(p.name for p in receipts.iterdir()), ['cbx-new.json'])
        return result, calls

    def test_incus_never_reads_the_callers_stdin(self):
        tmp = Path(tempfile.mkdtemp())
        fake = tmp / 'incus'
        fake.write_text(f'#!/bin/sh\ncase "$*" in *acl*) cat > /dev/null ;; *) cat >> {tmp}/stdin ;; esac\necho "[]"\n')
        fake.chmod(0o755)
        env = dict(os.environ, PATH=f'{tmp}:{os.environ["PATH"]}', COLDBOX_STATE=str(tmp / 'state'))
        result = subprocess.run([sys.executable, str(ROOT / 'coldbox'), 'reap'], env=env, input='x: y: z\n',
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((tmp / 'stdin').read_text(), '')

    def test_deletes_expired_coldbox_vms_and_old_receipts_only(self):
        result, calls = self.reap('#!/bin/sh\necho "[]"\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--force-local network acl edit coldbox-isolated', calls)

    def test_still_cleans_up_when_acl_refresh_fails(self):
        result, calls = self.reap('#!/bin/sh\necho "ip broke" >&2; exit 1\n')
        self.assertEqual(result.returncode, 1)
        self.assertIn('FAILED to refresh network ACL', result.stderr)


class BuildBase(unittest.TestCase):
    def test_waits_for_a_running_build(self):
        import fcntl
        import threading
        state = Path(tempfile.mkdtemp())
        calls = []
        with mock.patch.multiple(cb, STATE=state, GUEST_FILES=state / 'no-guest-files', fetch_json=lambda url: None,
                                 runner_release=lambda _: ('2.340.0', 'u', 'd'), wait_ready=lambda *a, **k: None,
                                 destroy=lambda name: calls.append(('destroy', name)) or True,
                                 ic=lambda *a, **k: calls.append(a)), \
                mock.patch.object(cb.os, 'geteuid', return_value=0), \
                mock.patch.object(cb.subprocess, 'run'), \
                open(state / 'build-base.lock', 'w') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            build = threading.Thread(target=quiet, args=(cb.build_base, None))
            build.start()
            build.join(0.5)
            self.assertTrue(build.is_alive())
            self.assertEqual(calls, [])
            fcntl.flock(held, fcntl.LOCK_UN)
            build.join(5)
        self.assertFalse(build.is_alive())
        self.assertIn(('move', 'base-new', 'base-dev'), calls)


    def test_copies_guest_files_into_the_base_as_root(self):
        import tarfile
        tmp = Path(tempfile.mkdtemp())
        files = tmp / 'guest'
        (files / 'etc/coldbox').mkdir(parents=True)
        (files / 'etc/coldbox/host').write_text('build-host\n')
        (files / 'opt/tool').mkdir(parents=True)
        (files / 'opt/tool/run').write_text('#!/bin/sh\n')
        (files / 'opt/tool/run').chmod(0o755)
        seen = []

        def guest(name, *command, user):
            seen.append((name, command, user))
            return ['sh', '-c', f'cat > {tmp}/archive']
        with mock.patch.multiple(cb, GUEST_FILES=files, ic_exec=guest):
            cb.push_guest_files('base-new')
        self.assertEqual(seen, [('base-new', ('tar', '-C', '/', '-x', '--no-overwrite-dir', '--keep-directory-symlink'), None)])
        with tarfile.open(tmp / 'archive') as archive:
            members = {m.name: m for m in archive.getmembers()}
            self.assertEqual(archive.extractfile('./etc/coldbox/host').read(), b'build-host\n')
        self.assertEqual({(m.uid, m.gid) for m in members.values()}, {(0, 0)})
        self.assertEqual(members['./opt/tool/run'].mode & 0o777, 0o755)

    def test_guest_files_keep_the_vms_directory_symlinks(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / 'guest/bin').mkdir(parents=True)
        (tmp / 'guest/bin/tool').write_text('#!/bin/sh\n')
        vm = tmp / 'vm'
        (vm / 'usr/bin').mkdir(parents=True)
        (vm / 'usr/bin/sh').write_text('')
        (vm / 'bin').symlink_to('usr/bin')
        with mock.patch.multiple(cb, GUEST_FILES=tmp / 'guest',
                                 ic_exec=lambda name, *command, user: [str(vm) if c == '/' else c for c in command]):
            cb.push_guest_files('base-new')
        self.assertTrue((vm / 'bin').is_symlink())
        self.assertEqual(sorted(p.name for p in (vm / 'usr/bin').iterdir()), ['sh', 'tool'])

    @unittest.skipIf(os.geteuid() == 0, 'root can read any file')
    def test_unreadable_guest_files_fail_the_build(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / 'guest').mkdir()
        (tmp / 'guest/secret').write_text('x')
        (tmp / 'guest/secret').chmod(0)
        with mock.patch.multiple(cb, GUEST_FILES=tmp / 'guest',
                                 ic_exec=lambda name, *command, user: ['sh', '-c', f'cat > {tmp}/archive']):
            with self.assertRaises(subprocess.CalledProcessError):
                quiet(cb.push_guest_files, 'base-new')

    def test_no_guest_files_means_no_copy(self):
        with mock.patch.multiple(cb, GUEST_FILES=Path(tempfile.mkdtemp()) / 'missing'), \
                mock.patch.object(cb.subprocess, 'run') as run:
            cb.push_guest_files('base-new')
        run.assert_not_called()

    def test_guest_files_land_before_the_snapshot(self):
        calls = []
        with mock.patch.multiple(cb, fetch_json=lambda url: None, runner_release=lambda _: ('2.340.0', 'u', 'd'),
                                 wait_ready=lambda *a, **k: None, destroy=lambda name: True,
                                 push_guest_files=lambda name: calls.append(('guest-files', name)),
                                 ic=lambda *a, **k: calls.append(a)), \
                mock.patch.object(cb.subprocess, 'run'):
            cb.build_new_base()
        self.assertLess(calls.index(('guest-files', 'base-new')), calls.index(('stop', 'base-new')))


class Unregister(unittest.TestCase):
    def test_deletes_coldbox_runners_on_every_page(self):
        tmp = Path(tempfile.mkdtemp())
        page1 = {'runners': [{'id': i, 'name': f'cbx-{i}'} for i in range(100)]}
        page2 = {'runners': [{'id': 100, 'name': 'cbx-100'}, {'id': 101, 'name': 'build-box'}]}
        (tmp / 'pages').write_text(json.dumps(page1) + json.dumps(page2))
        (tmp / 'page1').write_text(json.dumps(page1))
        (tmp / 'gh').write_text('#!/bin/sh\n'
                                f'echo "$@" >> {tmp}/calls\n'
                                'case "$*" in\n'
                                '  *DELETE*) ;;\n'
                                f'  *--paginate*) python3 -c "import json,sys; d=json.JSONDecoder(); t=open(sys.argv[1]).read(); i=0\n'
                                'while i < len(t):\n'
                                '    o, i = d.raw_decode(t, i)\n'
                                '    [print(r[\\"id\\"]) for r in o[\\"runners\\"] if r[\\"name\\"].startswith(\\"cbx-\\")]" '
                                f'{tmp}/pages ;;\n'
                                f'  *) cat {tmp}/page1 ;;\n'
                                'esac\n')
        (tmp / 'gh').chmod(0o755)
        (tmp / 'sudo').write_text('#!/bin/sh\nexit 0\n')
        (tmp / 'sudo').chmod(0o755)
        env = dict(os.environ, PATH=f'{tmp}:{os.environ["PATH"]}')
        result = subprocess.run([sys.executable, str(ROOT / 'coldbox'), 'unregister', 'me/app'], env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        deleted = [line.split('/')[-1] for line in (tmp / 'calls').read_text().splitlines() if 'DELETE' in line]
        self.assertEqual(deleted, [str(i) for i in range(101)])


class Status(unittest.TestCase):
    def test_a_host_without_gh_or_incus_gets_failures_not_a_crash(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / 'systemctl').write_text('#!/bin/sh\nexit 3\n')
        (tmp / 'systemctl').chmod(0o755)
        with mock.patch.dict(os.environ, PATH=str(tmp)), mock.patch.object(cb, 'STATE', tmp):
            code, output = quiet(cb.status, None)
        self.assertEqual(code, 1)
        self.assertIn('FAIL  gh is logged in', output)
        self.assertIn('FAIL  Coldbox is installed', output)


class Sizes(unittest.TestCase):
    def test_reads_incus_sizes(self):
        self.assertEqual(cb.size_bytes('8GiB'), 8 * 2**30)
        self.assertEqual(cb.size_bytes('512MiB'), 512 * 2**20)
        self.assertEqual(cb.size_bytes('2GB'), 2 * 10**9)
        self.assertEqual(cb.size_bytes('1073741824'), 2**30)
        with self.assertRaises(ValueError):
            cb.size_bytes('50%')

    def test_slots_must_leave_host_memory(self):
        self.assertTrue(cb.fits(2, 16 * 2**30, 45 * 2**30))
        self.assertFalse(cb.fits(3, 16 * 2**30, 45 * 2**30))
        self.assertFalse(cb.fits(1, 8 * 2**30, 9 * 2**30))


class RunnerRelease(unittest.TestCase):
    def release(self, **asset):
        name = 'actions-runner-linux-x64-2.340.0.tar.gz'
        return dict(tag_name='v2.340.0', body='', assets=[
            dict(name='actions-runner-linux-arm64-2.340.0.tar.gz', browser_download_url='arm', digest='sha256:' + 'b' * 64),
            dict(name=name, browser_download_url='https://example.test/x64.tgz', **asset)])

    def test_uses_asset_digest(self):
        self.assertEqual(cb.runner_release(self.release(digest='sha256:' + 'a' * 64)),
                         ('2.340.0', 'https://example.test/x64.tgz', 'a' * 64))

    def test_falls_back_to_release_notes(self):
        release = self.release()
        release['body'] = '<!-- BEGIN SHA linux-x64 -->' + 'c' * 64 + '<!-- END SHA linux-x64 -->'
        self.assertEqual(cb.runner_release(release)[2], 'c' * 64)

    def test_refuses_without_checksum_or_asset(self):
        with self.assertRaises(RuntimeError):
            cb.runner_release(self.release())
        with self.assertRaises(RuntimeError):
            cb.runner_release(dict(tag_name='v2.340.0', body='', assets=[]))


class Slots(unittest.TestCase):
    def test_serve_starts_one_single_slot_loop_per_slot_with_same_flags(self):
        started = []

        class Child:
            def __init__(self, argv):
                started.append(argv)

            def wait(self):
                return 0

        args = cb.argparse.Namespace(target='me/repo', slots=3, ttl=60, allow_public=False, allow_forks=True,
                                     once=True)
        original, handlers = cb.subprocess.Popen, (cb.signal.getsignal(cb.signal.SIGINT),
                                                  cb.signal.getsignal(cb.signal.SIGTERM))
        cb.subprocess.Popen = Child
        try:
            self.assertEqual(cb.serve(args), 0)
        finally:
            cb.subprocess.Popen = original
            cb.signal.signal(cb.signal.SIGINT, handlers[0])
            cb.signal.signal(cb.signal.SIGTERM, handlers[1])
        self.assertEqual(len(started), 3)
        self.assertEqual(started[0][2:], ['serve', 'me/repo', '--slots', '1', '--ttl', '60', '--allow-forks', '--once'])

    def test_a_dead_slot_stops_the_service_so_systemd_restarts_every_slot(self):
        children = []

        class Child:
            def __init__(self, argv):
                self.code = 1 if not children else None
                children.append(self)

            def poll(self):
                return self.code

            def send_signal(self, sig):
                self.code = 143

            def wait(self):
                return self.code

        args = cb.argparse.Namespace(target='me/repo', slots=2, ttl=60, allow_public=False, allow_forks=False,
                                     once=False)
        handlers = cb.signal.getsignal(cb.signal.SIGINT), cb.signal.getsignal(cb.signal.SIGTERM)
        try:
            with mock.patch.object(cb.subprocess, 'Popen', Child), mock.patch.object(cb.time, 'sleep'):
                code = cb.serve(args)
        finally:
            cb.signal.signal(cb.signal.SIGINT, handlers[0])
            cb.signal.signal(cb.signal.SIGTERM, handlers[1])
        self.assertNotEqual(code, 0)
        self.assertEqual(children[1].code, 143)


class ServeOnce(unittest.TestCase):
    def test_exit_status_follows_the_receipt_verdict(self):
        runner = {'runner': {'id': 7}, 'encoded_jit_config': 'c'}
        args = cb.argparse.Namespace(target='me/repo', slots=1, ttl=60, allow_public=False, allow_forks=False,
                                     once=True)
        handler = cb.signal.getsignal(cb.signal.SIGTERM)
        codes = {}
        try:
            for verdict in ('pass', 'fail', 'incomplete', 'refused'):
                with mock.patch.multiple(cb, guard=lambda *a: None, gh=lambda *a, **k: runner,
                                         job=lambda *a, **k: {'verdict': verdict}):
                    codes[verdict] = quiet(cb.serve, args)[0]
        finally:
            cb.signal.signal(cb.signal.SIGTERM, handler)
        self.assertEqual(codes, {'pass': 0, 'fail': 1, 'incomplete': 125, 'refused': 125})


class SwapBase(unittest.TestCase):
    def swap(self, instances, failing_move=None):
        def ic(*args, **kwargs):
            _, source, target = args
            if (source, target) == failing_move or source not in instances or target in instances:
                raise cb.subprocess.CalledProcessError(1, args)
            instances[target] = instances.pop(source)

        with mock.patch.multiple(cb, ic=ic, destroy=lambda name: instances.pop(name, None)):
            cb.swap_base()
        return instances

    def test_replaces_the_base(self):
        self.assertEqual(self.swap({'base-dev': 'old', 'base-new': 'new'}), {'base-dev': 'new'})

    def test_first_install_has_no_old_base(self):
        self.assertEqual(self.swap({'base-new': 'new'}), {'base-dev': 'new'})

    def test_failed_swap_keeps_the_old_base(self):
        instances = {'base-dev': 'old', 'base-new': 'new'}
        with self.assertRaises(cb.subprocess.CalledProcessError):
            self.swap(instances, failing_move=('base-new', 'base-dev'))
        self.assertEqual(instances['base-dev'], 'old')


if __name__ == '__main__':
    unittest.main()
