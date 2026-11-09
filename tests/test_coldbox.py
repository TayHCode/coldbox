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
os.environ['COLDBOX_HOST_CHECKS'] = str(Path(tempfile.mkdtemp()) / 'checks.toml')
os.environ['COLDBOX_INCUS_ENV'] = str(Path(tempfile.mkdtemp()) / 'incus.env')
os.environ['COLDBOX_STATE'] = tempfile.mkdtemp()
os.environ['COLDBOX_STANDBY'] = str(Path(tempfile.mkdtemp()) / 'standby')
for inherited_from_a_coldbox_job in ('PYTEST_ADDOPTS', 'COLDBOX_JOB_DIR'):
    os.environ.pop(inherited_from_a_coldbox_job, None)


def load_coldbox():
    loader = importlib.machinery.SourceFileLoader('coldbox', str(ROOT / 'coldbox'))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader('coldbox', loader))
    loader.exec_module(module)
    return module


cb = load_coldbox()


def custom_names():
    path = Path(tempfile.mkdtemp()) / 'incus.env'
    path.write_text('COLDBOX_NAME_PREFIX=cbx\nCOLDBOX_SUBNET=10.205.0.1/24\n')
    return path


def repo(files):
    tmp = Path(tempfile.mkdtemp())
    for name, text in files.items():
        (tmp / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp / name).write_text(text)
    return tmp


def git_repo(files):
    root = repo(files)
    for command in (['init', '-q'], ['add', '.'], ['-c', 'user.name=t', '-c', 'user.email=t@example.com', 'commit', '-qm', 'x']):
        subprocess.run(['git', '-C', str(root), *command], check=True)
    return root


def quiet(fn, *args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        return fn(*args), out.getvalue()


class Version(unittest.TestCase):
    def test_version_prints_0_1_0_and_exits_0(self):
        result = subprocess.run([sys.executable, str(ROOT / 'coldbox'), '--version'], capture_output=True, text=True,
                                timeout=30)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, 'coldbox 0.1.0\n', ''))


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
                                'bin/x.sh': '', '.fleet/verify.sh': ''}))
        self.assertEqual(found['test'], [cb.GO_TEST, cb.CARGO_TEST])
        self.assertTrue(any('shellcheck' in c for c in found['lint']))
        self.assertEqual(found['verify'], ['coldverify --ci'])

    def test_only_the_fleet_recipe_is_a_verify_check(self):
        self.assertNotIn('verify', cb.detect(repo({'verify.sh': ''})))

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


class HostChecks(unittest.TestCase):
    def setUp(self):
        self.host = Path(tempfile.mkdtemp()) / 'checks.toml'
        self.host.write_text('[checks]\nsetup = true\nlint = false\ntest = true\nverify = true\n')
        patcher = mock.patch.object(cb, 'HOST_CHECKS', self.host)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_host_default_switches_a_check_off(self):
        root = repo({'tests/test_x.py': ''})
        self.assertEqual([name for name, _ in cb.plan(root)], ['test'])
        code, output = quiet(cb.detect_cmd, argparse.Namespace(path=str(root)))
        self.assertEqual(code, 0)
        self.assertIn('lint = false', output)
        self.assertIn('test = true', output)

    def test_detect_names_the_recipe_behind_verify(self):
        code, output = quiet(cb.detect_cmd, argparse.Namespace(path=str(repo({'.fleet/verify.sh': ''}))))
        self.assertEqual(code, 0)
        self.assertIn('verify = true  # .fleet/verify.sh: coldverify --ci', output)

    def test_repo_coldbox_toml_overrides_the_host_default(self):
        root = repo({'tests/test_x.py': '', 'coldbox.toml': '[checks]\nlint = true\ntest = false\n'})
        self.assertEqual([name for name, _ in cb.plan(root)], ['lint'])

    def test_host_default_on_needs_no_detected_command(self):
        self.assertEqual([name for name, _ in cb.plan(repo({'app.py': ''}))], [])

    def test_host_file_takes_only_checks(self):
        self.host.write_text('[commands]\ntest = "make t"\n')
        with self.assertRaises(cb.ConfigError):
            cb.plan(repo({'app.py': ''}))

    def test_job_vm_gets_the_host_default(self):
        pushed = []
        with mock.patch.multiple(cb, STATE=Path(tempfile.mkdtemp()), sync_acl=lambda: None, isolated=lambda *a: None,
                                 wait_ready=lambda *a, **k: None, tee=lambda *a: 0, pull=lambda *a: None,
                                 destroy=lambda name: True, ic=lambda *a: pushed.append(a)), \
                mock.patch.object(cb.subprocess, 'run'):
            quiet(cb.job, 'local', 'cbx-1', 60, ['coldbox', 'checks'], 'me/repo')
        self.assertIn(('file', 'push', str(self.host), f'cbx-1{self.host}', '--create-dirs', '--mode', '0644'), pushed)


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


class IncusNames(unittest.TestCase):
    def install(self, *args, env=None):
        return subprocess.run(['bash', str(ROOT / 'install.sh'), *args], capture_output=True, text=True,
                              env={'PATH': os.environ['PATH'], 'COLDBOX_INCUS_ENV': os.environ['COLDBOX_INCUS_ENV'],
                                   **(env or {})})

    def test_defaults_are_the_original_names(self):
        self.assertEqual((cb.PROJECT, cb.POOL, cb.BRIDGE, cb.NETWORK_ACL, cb.GATEWAY),
                         ('coldbox', 'coldbox', 'coldboxbr0', 'coldbox-isolated', '10.204.0.1'))

    def test_cli_follows_the_installed_names(self):
        with mock.patch.dict(os.environ, COLDBOX_INCUS_ENV=str(custom_names())):
            module = load_coldbox()
        self.assertEqual((module.PROJECT, module.POOL, module.BRIDGE, module.NETWORK_ACL, module.GATEWAY),
                         ('cbx', 'cbx', 'cbxbr0', 'cbx-isolated', '10.205.0.1'))
        with mock.patch.object(module, 'query', return_value={
                'type': 'virtual-machine', 'config': {},
                'expanded_devices': {'root': {'type': 'disk', 'path': '/'},
                                     'eth0': {'type': 'nic', 'network': 'coldboxbr0', 'security.port_isolation': 'true'}}}):
            with self.assertRaisesRegex(RuntimeError, 'NIC is not on the isolated cbxbr0 bridge'):
                module.isolated('cbx-1', False)

    def test_acl_command_uses_the_installed_gateway(self):
        env = dict(os.environ, COLDBOX_INCUS_ENV=str(custom_names()))
        doc = json.loads(subprocess.run([sys.executable, str(ROOT / 'coldbox'), 'acl'], env=env, check=True,
                                        capture_output=True, text=True).stdout)
        self.assertIn('10.205.0.1', [rule.get('destination') for rule in doc['egress']])
        self.assertEqual(doc['ingress'][0]['source'], '10.205.0.1')

    def test_bad_names_are_refused_before_anything_is_installed(self):
        for args, message in ((['--name-prefix', 'Cbx'], '--name-prefix must be'),
                              (['--name-prefix', 'waytoolongprefix'], '--name-prefix must be'),
                              (['--subnet', '10.205.0.1'], '--subnet must be'),
                              (['--subnet', '10.205.0.1/99'], '--subnet must be'),
                              (['--subnet', '10.205.999.1/24'], '--subnet must be'),
                              (['--subnet', '8.8.8.1/24'], '--subnet must be'),
                              (['--subnet', '172.32.0.1/24'], '--subnet must be'),
                              (['--subnet', '192.168.0.1/12'], '--subnet must be'),
                              (['--subnet', '10.205.0.0/24'], '--subnet must be'),
                              (['--subnet', '10.205.0.255/24'], '--subnet must be'),
                              (['--subnet', '10.255.255.255/8'], '--subnet must be'),
                              (['--subnet'], '--subnet needs a value')):
            with self.subTest(args):
                result = self.install(*args)
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)

    @unittest.skipIf(os.geteuid() == 0, 'would really install as root')
    def test_re_runs_keep_the_installed_names_and_refuse_others(self):
        env = {'COLDBOX_INCUS_ENV': str(custom_names())}
        for args in ([], ['--name-prefix', 'cbx', '--subnet', '10.205.0.1/24']):
            with self.subTest(args):
                self.assertEqual(self.install(*args, env=env).stderr, 'coldbox install: run with sudo\n')
        for args in (['--name-prefix', 'coldbox'], ['--subnet', '10.204.0.1/24']):
            with self.subTest(args):
                self.assertIn('this host is installed with --name-prefix cbx --subnet 10.205.0.1/24',
                              self.install(*args, env=env).stderr)


    def test_an_install_from_before_names_were_recorded_cannot_switch_names(self):
        text = (ROOT / 'install.sh').read_text()
        start = text.index('if [[ -z $installed_prefix && "$prefix $subnet"')
        guard = text[start:text.index('\nfi\n', start) + 4]
        for prefix, subnet, managed, units, code in (('cbx', '10.204.0.1/24', 'true', 1, 1),
                                                     ('coldbox', '10.205.0.1/24', 'true', 1, 1),
                                                     ('cbx', '10.205.0.1/24', '', 0, 1),
                                                     ('cbx', '10.205.0.1/24', '', 1, 0),
                                                     ('coldbox', '10.204.0.1/24', 'true', 0, 0)):
            with self.subTest(prefix=prefix, subnet=subnet, managed=managed, units=units):
                script = (f'die() {{ echo "$*" >&2; exit 1; }}\nic() {{ echo {managed}; }}\n'
                          f'systemctl() {{ return {units}; }}\n'
                          f'installed_prefix= prefix={prefix} subnet={subnet}\n{guard}')
                result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], capture_output=True, text=True)
                self.assertEqual(result.returncode, code, result.stderr)

    def test_the_recorded_names_are_replaced_whole_and_readable_under_a_strict_umask(self):
        text = (ROOT / 'install.sh').read_text()
        start = text.index("printf 'COLDBOX_NAME_PREFIX")
        record = text[start:text.index('\nif ! claim storage', start) + 1]
        incus_env = Path(tempfile.mkdtemp()) / 'incus.env'
        script = f'umask 077\nincus_env={incus_env} prefix=cbx subnet=10.205.0.1/24\n{record}'
        subprocess.run(['bash', '-euo', 'pipefail', '-c', script], check=True)
        first = incus_env.stat().st_ino
        subprocess.run(['bash', '-euo', 'pipefail', '-c', script], check=True)
        self.assertNotEqual(incus_env.stat().st_ino, first)
        self.assertEqual(incus_env.stat().st_mode & 0o777, 0o644)
        self.assertEqual(incus_env.read_text(), 'COLDBOX_NAME_PREFIX=cbx\nCOLDBOX_SUBNET=10.205.0.1/24\n')
        self.assertEqual([path.name for path in incus_env.parent.iterdir()], ['incus.env'])

    def test_a_real_install_only_records_names_where_the_services_read_them(self):
        text = (ROOT / 'install.sh').read_text()
        start = text.index('[[ $incus_env == ')
        check = text[start:text.index('\n', start) + 1]
        for incus_env, code in (('/etc/coldbox/incus.env', 0), ('/srv/elsewhere/incus.env', 1)):
            with self.subTest(incus_env=incus_env):
                script = f'die() {{ echo "$*" >&2; exit 1; }}\nincus_env={incus_env}\n{check}'
                result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], capture_output=True, text=True)
                self.assertEqual(result.returncode, code, result.stderr)

    def test_uninstall_of_an_install_without_recorded_names_records_the_defaults_first(self):
        text = (ROOT / 'uninstall.sh').read_text()
        start = text.index('if [[ ! -e /etc/coldbox/incus.env ]]')
        block = text[start:text.index('\nfi\n', start) + 4]
        recorded = 'COLDBOX_NAME_PREFIX=cbx\nCOLDBOX_SUBNET=10.205.0.1/24\n'
        for before, after in ((recorded, recorded),
                              (None, 'COLDBOX_NAME_PREFIX=coldbox\nCOLDBOX_SUBNET=10.204.0.1/24\n')):
            with self.subTest(before=before):
                etc = Path(tempfile.mkdtemp()) / 'coldbox'
                incus_env = etc / 'incus.env'
                if before is not None:
                    etc.mkdir()
                    incus_env.write_text(before)
                subprocess.run(['bash', '-euo', 'pipefail', '-c', block.replace('/etc/coldbox', str(etc))], check=True)
                self.assertEqual(incus_env.read_text(), after)
        self.assertEqual(incus_env.stat().st_mode & 0o777, 0o644)

    def test_uninstall_keeps_the_names_until_incus_confirms_nothing_is_left(self):
        text = (ROOT / 'uninstall.sh').read_text()
        helpers_start = text.index('gone()')
        helpers = text[helpers_start:text.index('if [[ ! -e /etc/coldbox/incus.env ]]')]
        start = text.index('if cleared project')
        block = text[start:text.index('\nfi\n', start) + 4]
        acl_show, acl_get = 'network acl show cbx-isolated', 'network acl get cbx-isolated user.coldbox.managed'
        for show, mark, error, removed in (('', None, 'Error: Network not found', True),
                                           (acl_show, None, 'Error: Network not found', False),
                                           (acl_show, 'true', 'Error: Network not found', False),
                                           (acl_show, '', 'Error: Network not found', True),
                                           ('', None, 'Error: connect: connection refused', False),
                                           ('', None, 'bash: incus: command not found', False)):
            with self.subTest(show=show, mark=mark, error=error):
                get = acl_get if mark is not None else ''
                script = (f'ic() {{ case "$*" in "{show}") return 0;; "{get}") echo "{mark}"; return 0;; esac; '
                          f'echo "{error}" >&2; return 1; }}\nrm() {{ echo removed; }}\n{helpers}prefix=cbx\n'
                          f'{block}echo "kept=${{kept-}}"\n')
                result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual('removed' in result.stdout, removed, result.stdout)
                self.assertEqual('kept=coldbox uninstall: FAIL' in result.stdout, not removed, result.stdout)


class InstallRoutes(unittest.TestCase):
    def check(self, routes, bridge='coldboxbr0', subnet='10.204.0.1/24', ip_exit=0):
        text = (ROOT / 'install.sh').read_text()
        start = text.index('IFS=./ read -r o1')
        block = text[start:text.index('\ndone <<<"$routes"\n', start) + 19]
        self.assertLess(start, text.index('apt-get update'))
        tmp = Path(tempfile.mkdtemp())
        (tmp / 'routes').write_text(''.join(f'{route}\n' for route in routes))
        (tmp / 'ip').write_text(f'#!/bin/sh\n[ "$*" = "-4 route show" ] && cat {tmp}/routes\nexit {ip_exit}\n')
        (tmp / 'ip').chmod(0o755)
        script = (f'die() {{ printf "coldbox install: %s\\n" "$*" >&2; exit 1; }}\n'
                  f'bridge={bridge} subnet={subnet}\n{block}echo no overlap\n')
        return subprocess.run(['bash', '-euo', 'pipefail', '-c', script], capture_output=True, text=True,
                              env={'PATH': f'{tmp}:{os.environ["PATH"]}'})

    host = ['default via 192.168.1.1 dev eth0 proto dhcp src 192.168.1.20 metric 100',
            '192.168.1.0/24 dev eth0 proto kernel scope link src 192.168.1.20 metric 100']

    def test_a_subnet_the_host_already_routes_is_refused_and_names_the_flag(self):
        for route, bridge, subnet in (
                ('10.204.0.0/24 dev eth0 proto kernel scope link src 10.204.0.50', 'coldboxbr0', '10.204.0.1/24'),
                ('10.0.0.0/8 via 192.168.1.9 dev tun0', 'coldboxbr0', '10.204.0.1/24'),
                ('10.204.0.7 dev wg0 scope link', 'coldboxbr0', '10.204.0.1/24'),
                ('blackhole 10.204.0.0/16 proto static', 'coldboxbr0', '10.204.0.1/24'),
                ('10.205.0.0/24 dev coldboxbr0 proto kernel scope link src 10.205.0.1', 'cbxbr0', '10.205.0.1/24'),
                ('172.17.0.0/16 dev docker0 proto kernel scope link src 172.17.0.1', 'cbxbr0', '172.17.5.1/24')):
            with self.subTest(route=route):
                result = self.check([*self.host, route], bridge, subnet)
                self.assertEqual((result.returncode, result.stdout), (1, ''))
                self.assertEqual(result.stderr, f"coldbox install: bridge subnet {subnet} overlaps host route "
                                 f"'{route}', so the bridge would take that range from the host; pick a private "
                                 'range this host does not route (see ip -4 route) with --subnet CIDR\n')

    def test_a_free_subnet_or_coldboxs_own_bridge_route_continues(self):
        for routes, bridge, subnet in (
                (self.host, 'coldboxbr0', '10.204.0.1/24'),
                ([], 'coldboxbr0', '10.204.0.1/24'),
                ([*self.host, '10.204.0.0/24 dev coldboxbr0 proto kernel scope link src 10.204.0.1 linkdown'],
                 'coldboxbr0', '10.204.0.1/24'),
                ([*self.host, '10.205.0.0/24 dev cbxbr0 proto kernel scope link src 10.205.0.1'],
                 'cbxbr0', '10.205.0.1/24'),
                ([*self.host, '10.204.1.0/24 dev eth1 scope link', '10.204.0.0/24 dev coldboxbr0'],
                 'coldboxbr0', '10.204.0.1/24'),
                ([*self.host, '0.0.0.0/1 via 10.8.0.1 dev tun0', '128.0.0.0/1 via 10.8.0.1 dev tun0'],
                 'coldboxbr0', '10.204.0.1/24')):
            with self.subTest(routes=routes, bridge=bridge):
                result = self.check(routes, bridge, subnet)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, 'no overlap\n', ''))

    def test_an_unreadable_route_table_stops_the_install(self):
        result = self.check(self.host, ip_exit=2)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')


class InstallMemory(unittest.TestCase):
    def check(self, mem_kib, memory=None):
        text = (ROOT / 'install.sh').read_text()
        start = text.index('mem_kib=$(awk')
        tail = ' or a smaller --memory"\n'
        end = text.index(tail, start) + len(tail)
        self.assertLess(start, text.index('apt-get update'))
        tmp = Path(tempfile.mkdtemp())
        (tmp / 'meminfo').write_text(f'MemTotal:       {mem_kib} kB\nMemFree:          123456 kB\n')
        block = text[start:end].replace('/proc/meminfo', str(tmp / 'meminfo'))
        script = (f'die() {{ printf "coldbox install: %s\\n" "$*" >&2; exit 1; }}\n'
                  'clamp() { local v=$1; (( v < $2 )) && v=$2; (( v > $3 )) && v=$3; echo "$v"; }\n'
                  f'{block}echo "memory=$memory"\n')
        env = {'PATH': os.environ['PATH'], **({'COLDBOX_MEMORY': memory} if memory else {})}
        return subprocess.run(['bash', '-euo', 'pipefail', '-c', script], capture_output=True, text=True, env=env)

    def test_a_4_gib_vps_is_refused_with_its_real_ram_and_the_minimum(self):
        result = self.check(3933320)
        self.assertEqual((result.returncode, result.stdout), (1, ''))
        self.assertEqual(result.stderr, 'coldbox install: needs at least 4096 MiB (4.00 GiB) of RAM, 2048 MiB for '
                         'the host plus one 2GiB job VM; found 3841 MiB (3.75 GiB). Use a host with more RAM or a '
                         'smaller --memory\n')
        self.assertFalse(cb.fits(1, 2 * GIB, 3933320 * 1024))

    def test_an_8_gib_host_passes_with_half_its_ram_per_vm(self):
        result = self.check(8 * 1048576)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, 'memory=4GiB\n', ''))

    def test_the_floor_matches_admit_host_reserve_plus_one_vm_to_the_byte(self):
        for mem_kib, memory, vm in ((4 * 1048576, None, '2GiB'), (4 * 1048576 - 1, None, '2GiB'),
                                    (14 * 1048576, '12GiB', '12GiB'), (14 * 1048576 - 1, '12GiB', '12GiB'),
                                    (4050277, '2000MB', '2000MB'), (4050276, '2000MB', '2000MB'),
                                    (3 * 1048576, '1024MiB', '1024MiB'), (3 * 1048576 - 1, '1024MiB', '1024MiB')):
            with self.subTest(mem_kib=mem_kib, memory=memory):
                result = self.check(mem_kib, memory)
                admitted = cb.fits(1, cb.size_bytes(vm), mem_kib * 1024)
                self.assertEqual(result.returncode, 0 if admitted else 1, result.stderr)
                self.assertEqual(result.stdout, f'memory={vm}\n' if admitted else '')
                self.assertEqual(admitted, mem_kib in (4 * 1048576, 14 * 1048576, 4050277, 3 * 1048576))

    def test_a_memory_size_admit_cannot_read_is_refused(self):
        for memory in ('50%', '8G', '0GiB'):
            with self.subTest(memory=memory):
                result = self.check(64 * 1048576, memory)
                self.assertEqual((result.returncode, result.stdout), (1, ''))
                self.assertEqual(result.stderr,
                                 f"coldbox install: --memory must be a size such as 8GiB, got '{memory}'\n")


class InstallStorage(unittest.TestCase):
    def check(self, has_zfs, storage=None):
        text = (ROOT / 'install.sh').read_text()
        start = text.index('storage=${COLDBOX_STORAGE:-zfs}')
        tail = 'packages+=(zfsutils-linux)\n'
        end = text.index(tail, start) + len(tail)
        self.assertLess(end, text.index('apt-get update'))
        tmp = Path(tempfile.mkdtemp())
        (tmp / 'modprobe').write_text(f'#!/bin/sh\necho "$*" >> {tmp}/modprobe.log\nexit {0 if has_zfs else 1}\n')
        (tmp / 'uname').write_text('#!/bin/sh\necho 6.12.48+deb13-cloud-amd64\n')
        for fake in ('modprobe', 'uname'):
            (tmp / fake).chmod(0o755)
        script = (f'die() {{ printf "coldbox install: %s\\n" "$*" >&2; exit 1; }}\n'
                  'clamp() { local v=$1; (( v < $2 )) && v=$2; (( v > $3 )) && v=$3; echo "$v"; }\n'
                  f'free_gib=60\n{text[start:end]}echo "storage=$storage packages=${{packages[*]}}"\n')
        env = {'PATH': f'{tmp}:{os.environ["PATH"]}', **({'COLDBOX_STORAGE': storage} if storage else {})}
        result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], capture_output=True, text=True, env=env)
        log = tmp / 'modprobe.log'
        return result, log.read_text() if log.exists() else ''

    packages = 'incus incus-agent qemu-system-x86 ovmf dnsmasq-base python3 git'

    def test_a_missing_zfs_module_falls_back_to_dir_and_says_so(self):
        result, probes = self.check(has_zfs=False)
        self.assertEqual((result.returncode, result.stdout), (0, f'storage=dir packages={self.packages}\n'))
        self.assertEqual(result.stderr, 'coldbox install: kernel 6.12.48+deb13-cloud-amd64 has no zfs module, so VM '
                         'storage uses plain directories (COLDBOX_STORAGE=dir)\n')
        self.assertEqual(probes, '-nq zfs\n')

    def test_a_missing_zfs_module_with_zfs_asked_for_stops_with_the_fix(self):
        result, _ = self.check(has_zfs=False, storage='zfs')
        self.assertEqual((result.returncode, result.stdout), (1, ''))
        self.assertEqual(result.stderr, 'coldbox install: COLDBOX_STORAGE=zfs but kernel 6.12.48+deb13-cloud-amd64 '
                         'has no zfs module; install one (on Debian: zfs-dkms from contrib) or rerun with '
                         'COLDBOX_STORAGE=dir\n')

    def test_a_present_zfs_module_keeps_the_zfs_pool(self):
        for storage in (None, 'zfs'):
            with self.subTest(storage=storage):
                result, probes = self.check(has_zfs=True, storage=storage)
                self.assertEqual((result.returncode, result.stdout, result.stderr),
                                 (0, f'storage=zfs packages={self.packages} zfsutils-linux\n', ''))
                self.assertEqual(probes, '-nq zfs\n')

    def test_dir_storage_never_probes_for_zfs(self):
        result, probes = self.check(has_zfs=False, storage='dir')
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (0, f'storage=dir packages={self.packages}\n', ''))
        self.assertEqual(probes, '')



class InstallDiskFloor(unittest.TestCase):
    def check(self, free_bytes, storage='zfs', pool_size=None, disk=None):
        text = (ROOT / 'install.sh').read_text()

        def line(prefix):
            start = text.index(prefix)
            return text[start:text.index('\n', start) + 1]

        start = text.index('if [[ $storage == zfs ]]; then floor=')
        end = text.index('\n', text.index('floor_bytes=', start)) + 1
        self.assertLess(end, text.index('apt-get update'))
        test = text.index('(( free_bytes >= floor_bytes ))')
        self.assertLess(text.index('if ! claim storage "$prefix"; then'), test)
        self.assertLess(test, text.index('ic storage create'))
        tmp = Path(tempfile.mkdtemp())
        (tmp / 'df').write_text(f'#!/bin/sh\necho "$*" > {tmp}/df.args\nprintf "    Avail\\n%s\\n" {free_bytes}\n')
        (tmp / 'df').chmod(0o755)
        script = (f'die() {{ printf "coldbox install: %s\\n" "$*" >&2; exit 1; }}\n'
                  'clamp() { local v=$1; (( v < $2 )) && v=$2; (( v > $3 )) && v=$3; echo "$v"; }\n'
                  + line('declare -A unit_bytes=') + line('gib() {') + line('free_bytes=$(df') + line('free_gib=')
                  + line('pool_size=${COLDBOX_POOL_SIZE') + line('disk=${COLDBOX_DISK') + f'storage={storage}\n'
                  + text[start:end] + line('(( free_bytes >= floor_bytes ))') + 'echo "pool=$pool_size disk=$disk"\n')
        env = {'PATH': f'{tmp}:{os.environ["PATH"]}', **({'COLDBOX_POOL_SIZE': pool_size} if pool_size else {}),
               **({'COLDBOX_DISK': disk} if disk else {})}
        result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], capture_output=True, text=True, env=env)
        self.assertEqual((tmp / 'df.args').read_text(), '--output=avail -B1 /var/lib\n')
        return result

    def test_a_25_gib_pool_installs_with_30_gib_free(self):
        result = self.check(30 * GIB, pool_size='25GiB', disk='20GiB')
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, 'pool=25GiB disk=20GiB\n', ''))

    def test_less_free_space_than_the_pool_is_refused_with_both_figures(self):
        result = self.check(24 * GIB + GIB // 2, pool_size='25GiB')
        self.assertEqual((result.returncode, result.stdout), (1, ''))
        self.assertEqual(result.stderr, 'coldbox install: needs 25GiB free under /var/lib for the ZFS pool '
                         '(COLDBOX_POOL_SIZE), found 24.50 GiB; free some space or set a smaller COLDBOX_POOL_SIZE\n')

    def test_the_zfs_floor_is_the_pool_size_to_the_byte(self):
        for free, pool, code in ((25 * GIB, '25GiB', 0), (25 * GIB - 1, '25GiB', 1),
                                 (30 * 10**9, '30GB', 0), (30 * 10**9 - 1, '30GB', 1)):
            with self.subTest(free=free, pool=pool):
                self.assertEqual(self.check(free, pool_size=pool).returncode, code)

    def test_the_default_pool_follows_free_space_down_to_40_gib(self):
        result = self.check(45 * GIB)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, 'pool=40GiB disk=40GiB\n', ''))
        result = self.check(30 * GIB)
        self.assertEqual((result.returncode, result.stdout), (1, ''))
        self.assertEqual(result.stderr, 'coldbox install: needs 40GiB free under /var/lib for the ZFS pool '
                         '(COLDBOX_POOL_SIZE), found 30.00 GiB; free some space or set a smaller COLDBOX_POOL_SIZE\n')

    def test_dir_storage_needs_one_job_disk_and_ignores_the_pool_size(self):
        result = self.check(21 * GIB, storage='dir', pool_size='500GiB', disk='20GiB')
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, 'pool=500GiB disk=20GiB\n', ''))
        result = self.check(30 * GIB, storage='dir', pool_size='25GiB')
        self.assertEqual((result.returncode, result.stdout), (1, ''))
        self.assertEqual(result.stderr, "coldbox install: needs 40GiB free under /var/lib for one job VM's disk "
                         '(--disk), found 30.00 GiB; free some space or set a smaller --disk\n')

    def test_a_pool_size_the_floor_cannot_read_is_refused(self):
        for pool in ('25G', '0GiB', 'big'):
            with self.subTest(pool=pool):
                result = self.check(500 * GIB, pool_size=pool)
                self.assertEqual((result.returncode, result.stdout), (1, ''))
                self.assertEqual(result.stderr,
                                 f"coldbox install: COLDBOX_POOL_SIZE must be a size such as 25GiB, got '{pool}'\n")

class InstallChecks(unittest.TestCase):
    def install(self, *args, env=None, tty_input=None):
        env = {'PATH': os.environ['PATH'], 'COLDBOX_HOST_CHECKS': str(cb.HOST_CHECKS), **(env or {})}
        if tty_input is None:
            result = subprocess.run(['bash', str(ROOT / 'install.sh'), *args], input='2\n\n', capture_output=True,
                                    text=True, env=env)
            return result.returncode, result.stdout + result.stderr
        primary, secondary = os.openpty()
        os.write(primary, tty_input.encode())
        child = subprocess.Popen(['bash', str(ROOT / 'install.sh'), *args], stdin=secondary, stdout=secondary,
                                 stderr=secondary, env=env)
        os.close(secondary)
        output = b''
        with contextlib.suppress(OSError):
            while chunk := os.read(primary, 4096):
                output += chunk
        os.close(primary)
        return child.wait(), output.decode()

    def test_bad_checks_are_refused(self):
        for args, env in ((['--checks', 'lint,typo'], None), (['--checks', ''], None), ([], {'COLDBOX_CHECKS': 'all'})):
            with self.subTest(args=args, env=env):
                code, output = self.install(*args, env=env)
                self.assertEqual(code, 1)
                self.assertIn('--checks must be', output)

    def write_host_checks(self, checks, host):
        text = (ROOT / 'install.sh').read_text()
        start = text.index('if [[ -n $checks ]]; then\n    install -d')
        block = text[start:text.index('\nfi\n', start) + 4]
        script = f'all_checks=(setup lint test verify); host_checks={host}; checks={checks}\n{block}'
        subprocess.run(['bash', '-euo', 'pipefail', '-c', script], check=True)
        return host.read_text()

    @unittest.skipIf(os.geteuid() == 0, 'would really install as root')
    def test_non_tty_install_without_checks_is_unchanged(self):
        self.assertEqual(self.install(), (1, 'coldbox install: run with sudo\n'))
        host = Path(tempfile.mkdtemp()) / 'etc' / 'checks.toml'
        host.parent.mkdir()
        host.write_text('[checks]\nlint = false\n')
        self.assertEqual(self.write_host_checks("''", host), '[checks]\nlint = false\n')
        self.assertEqual(self.write_host_checks('lint,test', host),
                         '[checks]\nsetup = false\nlint = true\ntest = true\nverify = false\n')

    @unittest.skipIf(os.geteuid() == 0, 'would really install as root')
    def test_checks_flag_and_env_set_the_choice_without_a_menu(self):
        for args, env in ((['--checks', 'lint,test'], None), ([], {'COLDBOX_CHECKS': 'lint,test'})):
            with self.subTest(args=args, env=env):
                code, output = self.install(*args, env=env, tty_input='2\n\n')
                self.assertEqual(code, 1)
                self.assertNotIn('Number to switch', output)
                self.assertIn('checks on by default: lint test\r\n', output)

    def test_missing_kvm_points_a_vm_or_container_at_nested_virtualization_and_bare_metal_at_the_bios(self):
        text = (ROOT / 'install.sh').read_text()
        start = text.index('if [[ ! -c /dev/kvm ]]; then')
        block = text[start:text.index('\nfi\n', start) + 4].replace('/dev/kvm ]]', '/nonexistent/kvm ]]', 1)
        for virt, message in (('kvm', 'this host is a kvm VM or container, so Coldbox needs nested virtualization '
                                      'from its provider, see docs/vps.md'),
                              ('lxc', 'this host is a lxc VM or container'),
                              ('none', 'enable virtualization (VT-x/AMD-V) in the BIOS'),
                              (None, 'enable virtualization (VT-x/AMD-V) in the BIOS')):
            with self.subTest(virt=virt):
                tmp = Path(tempfile.mkdtemp())
                if virt:
                    (tmp / 'systemd-detect-virt').write_text(f'#!/bin/sh\necho {virt}\n[ {virt} != none ]\n')
                    (tmp / 'systemd-detect-virt').chmod(0o755)
                script = f'die() {{ echo "$*" >&2; exit 1; }}\n{block}'
                result = subprocess.run(['/bin/bash', '-euo', 'pipefail', '-c', script], capture_output=True,
                                        text=True, env={'PATH': str(tmp)})
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)
                self.assertEqual('BIOS' in result.stderr, virt in ('none', None))

    def incus_guard(self, candidate):
        text = (ROOT / 'install.sh').read_text()
        start = text.index('incus_crashes() {')
        block = text[start:text.index('\nfi\n', start) + 4]
        tmp = Path(tempfile.mkdtemp())
        (tmp / 'apt-cache').write_text(f'#!/bin/sh\nprintf "incus:\\n  Installed: (none)\\n  Candidate: {candidate}\\n"\n')
        (tmp / 'apt-cache').chmod(0o755)
        script = f'die() {{ printf "coldbox install: %s\\n" "$*" >&2; exit 1; }}\n{block}echo "$zabbly_lts"\n'
        return subprocess.run(['bash', '-euo', 'pipefail', '-c', script], capture_output=True, text=True,
                              env={'PATH': f'{tmp}:{os.environ["PATH"]}'})

    def test_install_refuses_an_incus_candidate_with_the_operation_metadata_crash(self):
        for candidate in ('6.0.0-1ubuntu0.3', '6.0.4-2', '6.0.5-8', '1:6.21.0-ubuntu24.04-202601011200'):
            with self.subTest(candidate=candidate):
                result = self.incus_guard(candidate)
                self.assertEqual(result.returncode, 1)
                version = candidate.split(':')[-1].split('-')[0]
                self.assertEqual(result.stderr, f'coldbox install: Incus {version} crashes under parallel jobs and kills '
                                 'every running one (lxc/incus#2865, fixed in 6.0.6). Install the Zabbly Incus 6.0 LTS, '
                                 f'then rerun with COLDBOX_REBUILD=1: {cb.ZABBLY_LTS} && sudo COLDBOX_REBUILD=1 '
                                 './install.sh\n')

    def test_install_continues_on_a_fixed_or_unknown_incus_candidate(self):
        for candidate in ('1:6.0.7-ubuntu26.04-202610301755', '6.0.6-1', '1:6.22.0-debian13-202602011200', '7.0.0-1',
                          '(none)'):
            with self.subTest(candidate=candidate):
                result = self.incus_guard(candidate)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, cb.ZABBLY_LTS + '\n', ''))

    @unittest.skipIf(os.geteuid() == 0, 'would really install as root')
    def test_tty_menu_starts_all_on_and_toggles(self):
        code, output = self.install(tty_input='2\n9\n\n')
        self.assertEqual(code, 1)
        self.assertIn('[x] lint', output)
        self.assertIn('[ ] lint', output)
        self.assertIn('checks on by default: setup test verify\r\n', output)
        self.assertIn('run with sudo', output)


class InstallProfile(unittest.TestCase):
    def apply_profile(self, incus_error):
        text = (ROOT / 'install.sh').read_text()
        start = text.index('if ! profile_error=')
        block = text[start:text.index('\nfi\n', start) + 4]
        script = (f'die() {{ printf "coldbox install: %s\\n" "$*" >&2; exit 1; }}\n'
                  f'icp() {{ cat >/dev/null; printf %s "$INCUS_ERROR" >&2; [[ -z $INCUS_ERROR ]]; }}\n'
                  f'cpus=8 memory=16GiB disk=40GiB prefix=coldbox bridge=coldboxbr0\n{block}echo copied CLI\n')
        return subprocess.run(['bash', '-euo', 'pipefail', '-c', script], capture_output=True, text=True,
                              env={'PATH': os.environ['PATH'], 'INCUS_ERROR': incus_error})

    def test_busy_running_vms_do_not_stop_the_install(self):
        busy = ('Error: The following instances failed to update (profile change still saved):\n'
                ' - Project: coldbox, Instance: cbx-1: Failed setting memory: taking too long\n')
        result = self.apply_profile(busy)
        self.assertEqual((result.returncode, result.stdout), (0, 'copied CLI\n'))
        self.assertIn('Failed setting memory', result.stderr)
        self.assertIn('running job VMs keep their old limits', result.stderr)

    def test_a_profile_incus_did_not_save_still_stops_the_install(self):
        result = self.apply_profile('Error: Invalid value for limits.memory\n')
        self.assertEqual((result.returncode, result.stdout), (1, ''))
        self.assertEqual(result.stderr, 'coldbox install: Error: Invalid value for limits.memory\n')

    def test_a_clean_apply_is_quiet(self):
        result = self.apply_profile('')
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, 'copied CLI\n', ''))


class InstallRefreshUnit(unittest.TestCase):
    def test_a_failed_refresh_runs_again_a_bounded_number_of_times(self):
        import configparser
        text = (ROOT / 'install.sh').read_text()
        start = text.index('cat >/etc/systemd/system/coldbox-refresh.service <<EOF')
        block = text[start:text.index('\nEOF\n', start) + 5]
        unit = Path(tempfile.mkdtemp()) / 'coldbox-refresh.service'
        script = block.replace('/etc/systemd/system/coldbox-refresh.service', str(unit), 1)
        subprocess.run(['bash', '-euo', 'pipefail', '-c', script], check=True)
        parsed = configparser.ConfigParser(interpolation=None)
        parsed.optionxform = str
        parsed.read_string(unit.read_text())
        seconds = {'min': 60, 'h': 3600}
        span = {key: int(value.rstrip('minh')) * seconds[value.lstrip('0123456789')]
                for key, value in (('restart', parsed['Service']['RestartSec']),
                                   ('window', parsed['Unit']['StartLimitIntervalSec']))}
        burst = int(parsed['Unit']['StartLimitBurst'])
        self.assertEqual((parsed['Service']['Type'], parsed['Service']['Restart']), ('oneshot', 'on-failure'))
        self.assertEqual((span['restart'], burst), (30 * 60, 3))
        self.assertGreater(span['window'], (burst - 1) * span['restart'])


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
        receipts = {path.name: json.loads(path.read_text()) for path in examples}
        self.assertGreaterEqual(sum('tests' in receipt for receipt in receipts.values()), 3)
        self.assertLessEqual({'pass', 'fail', 'idle', 'canceled'}, {r['verdict'] for r in receipts.values()})
        for name, receipt in receipts.items():
            with self.subTest(name):
                self.assertEqual(receipt['schema'], 'coldbox.receipt/v1')
                self.assertEqual(cb.verdict(receipt), receipt['verdict'])
                if 'tests' in receipt:
                    self.assertEqual(receipt['tests']['total'], len(receipt['tests']['results']))


class ExampleWorkflow(unittest.TestCase):
    FORK = "github.event_name == 'pull_request' && github.event.pull_request.head.repo.full_name != github.repository"

    def setUp(self):
        self.text = (ROOT / 'examples' / 'coldbox.yml').read_text()

    def test_fork_pull_requests_go_to_a_hosted_runner_and_fail_before_checkout(self):
        route = f"runs-on: ${{{{ {self.FORK} && 'ubuntu-latest' || fromJSON('[\"self-hosted\", \"coldbox\"]') }}}}"
        refuse = f'if: {self.FORK}\n        run: |\n          echo "Fork pull requests are not verified on Coldbox." >&2\n' \
                 '          exit 1\n'
        self.assertIn(route, self.text)
        self.assertEqual(self.text.count('self-hosted'), 1)
        self.assertLess(self.text.index(refuse), self.text.index('uses: actions/checkout'))
        self.assertNotIn('pull_request_target', self.text)

    def test_job_is_bounded_and_checkout_keeps_no_token(self):
        self.assertIn('    timeout-minutes: 60\n', self.text)
        self.assertIn('          persist-credentials: false\n', self.text)

    def test_a_new_push_cancels_only_the_same_pull_requests_older_run(self):
        block = ('concurrency:\n'
                 '  group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.run_id }}\n'
                 '  cancel-in-progress: true\n')
        self.assertIn(block, self.text)
        self.assertEqual(self.text.count('concurrency'), 1)
        self.assertLess(self.text.index(block), self.text.index('jobs:'))


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
        import hashlib
        import ipaddress
        import re
        sources = [Path(__file__).read_text()]
        if (ROOT / '.git').exists():
            sources.append(subprocess.check_output(['git', 'show', 'HEAD:tests/test_coldbox.py'],
                                                   cwd=ROOT, text=True))
        for source in sources:
            addresses = re.findall(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', source)
            self.assertNotIn('d72676dfa714edb2b89b8f62620535cb7fd76551ff25b25c48b2f8523ca7a6df',
                             [hashlib.sha256(address.encode()).hexdigest() for address in addresses])
        doc = cb.acl_document([{'addr_info': [{'family': 'inet', 'local': '8.8.4.4'}]}])
        allowed = [ipaddress.ip_network(r['destination']) for r in doc['egress']
                   if r['action'] == 'allow' and 'protocol' not in r]

        def reachable(ip):
            return any(ipaddress.ip_address(ip) in net for net in allowed)

        for ip in ('192.168.1.10', '10.204.0.1', '172.16.0.1', '169.254.169.254', '127.0.0.1'):
            self.assertFalse(reachable(ip), ip)
        shared = ipaddress.ip_network('100.64.0.0/10')
        self.assertFalse(any(shared.overlaps(net) for net in allowed))
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
        cb.gh = lambda path: {'visibility': 'public', 'full_name': 'me/app'}
        with self.assertRaises(SystemExit) as caught:
            cb.guard('me/app', False)
        self.assertIn('--allow-public', str(caught.exception))
        cb.guard('me/app', True)

    def test_internal_repo_counts_as_not_private(self):
        cb.gh = lambda path: {'visibility': 'internal', 'full_name': 'org/app'}
        with self.assertRaises(SystemExit):
            cb.guard('org/app', False)

    def test_private_repo_and_closed_org_allowed(self):
        cb.gh = lambda path: {'visibility': 'private', 'full_name': 'Me/App', 'allows_public_repositories': False}
        cb.guard('me/app', False)
        cb.guard('myorg', False)

    def test_repo_that_now_resolves_to_another_name_is_refused(self):
        cb.gh = lambda path: {'visibility': 'private', 'full_name': 'someone/app'}
        with self.assertRaises(SystemExit) as caught:
            cb.guard('me/app', False)
        self.assertIn('someone/app', str(caught.exception))

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

    def test_a_runner_stopped_before_it_got_a_job_is_idle(self):
        receipt = self.receipt(repo='me/app', sha=None, checks=[], runner_id=93, error='stopped', seconds=163.771)
        self.assertEqual(cb.verdict(receipt), 'idle')

    def test_a_runner_stopped_while_running_a_job_stays_incomplete(self):
        self.assertEqual(cb.verdict(self.receipt(error='stopped', job_name='verify')), 'incomplete')
        self.assertEqual(cb.verdict(self.receipt(error='stopped', github={'repo': 'me/app', 'refused': None})),
                         'incomplete')
        self.assertEqual(cb.verdict(dict(mode='local', vm={'destroyed': True}, error='stopped')), 'incomplete')
        self.assertEqual(cb.verdict(dict(mode='runner', vm={'destroyed': False}, error='stopped')), 'incomplete')

    def test_a_job_github_canceled_is_canceled(self):
        github = {'repo': 'me/app', 'sha': 'f' * 40, 'event': 'pull_request', 'refused': None}
        receipt = self.receipt(exit=0, job_name='verify', job_result='Canceled', github=github)
        self.assertEqual(cb.verdict(receipt), 'canceled')

    def test_a_real_failure_is_never_idle_or_canceled(self):
        github = {'repo': 'me/app', 'refused': None}
        cases = [dict(error='stopped', job_result='Failed'), dict(error='stopped', checks_verdict='fail'),
                 dict(github=github, job_result='Failed', exit=1),
                 dict(github=github, job_result='Canceled', checks_verdict='fail'),
                 dict(error='TTL reached', job_result='Failed')]
        for case in cases:
            with self.subTest(case):
                self.assertEqual(cb.verdict(self.receipt(**case)), 'fail')
        self.assertEqual(cb.verdict(self.receipt(github=github, exit=1)), 'incomplete')
        self.assertEqual(cb.verdict(self.receipt(github=github, job_result='Canceled', error='TTL reached')),
                         'incomplete')

    def stopped_runner(self, *lines):
        def runner(command, log, data=None):
            log.write(b''.join(line + b'\n' for line in lines))
            log.flush()
            raise SystemExit(0)

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addCleanup(cb.CLEANUP.clear)
        receipts = Path(folder.name)
        with mock.patch.multiple(cb, boot=mock.DEFAULT, ic=mock.DEFAULT, destroy=lambda name: True, tee=runner), \
                mock.patch.object(cb.subprocess, 'run'), self.assertRaises(SystemExit):
            cb.job('runner', 'cbx-1-test', 60, ['run.sh'], repo='me/app', receipts=receipts)
        return json.loads((receipts / 'cbx-1-test.json').read_text())

    def test_a_service_stop_reads_the_runner_log_to_tell_idle_from_interrupted(self):
        idle = self.stopped_runner(b'Current runner version: 2.338.2', b'2026-11-06 18:52:13Z: Listening for Jobs')
        self.assertEqual((idle['error'], idle['verdict'], 'job_name' in idle), ('stopped', 'idle', False))
        busy = self.stopped_runner(b'2026-11-08 16:52:19Z: Listening for Jobs',
                                   b'2026-11-08 16:52:21Z: Running job: verify')
        self.assertEqual((busy['error'], busy['verdict'], busy['job_name']), ('stopped', 'incomplete', 'verify'))

    def test_job_name_and_result_read_from_runner_log(self):
        log = b'Running job: checks\n2026-11-05 01:19:03Z: Job checks completed with result: Succeeded\n'
        self.assertEqual(cb.job_outcome(log), ('checks', 'Succeeded'))
        self.assertEqual(cb.job_outcome(b'Listening for Jobs\n'), (None, None))
        self.assertEqual(cb.job_outcome(b'2026-11-05 01:19:03Z: Job test (3.12) completed with result: Failed\n'),
                         ('test (3.12)', 'Failed'))


class Destroy(unittest.TestCase):
    def destroyed(self, *infos):
        calls = []
        infos = list(infos)

        def fake_run(command, **kw):
            calls.append(command)
            code, stderr = (infos.pop(0) if len(infos) > 1 else infos[0]) if 'info' in command else (0, b'')
            return subprocess.CompletedProcess(command, code, b'', stderr)

        with mock.patch.object(cb.subprocess, 'run', fake_run), mock.patch.object(cb.time, 'sleep', lambda _: None):
            return cb.destroy('cbx-1'), sum('delete' in command for command in calls)

    gone = (1, b'Error: Failed to fetch instance "cbx-1" in project "coldbox": Instance not found\n')

    def test_only_instance_not_found_counts_as_destroyed(self):
        self.assertTrue(self.destroyed(self.gone)[0])
        self.assertFalse(self.destroyed((1, b'Error: Get "http://unix.socket/1.0": dial unix: connect: connection refused\n'))[0])
        self.assertFalse(self.destroyed((0, b''))[0])

    def test_retries_a_delete_that_left_the_vm_behind(self):
        self.assertEqual(self.destroyed((0, b''), self.gone), (True, 2))
        self.assertEqual(self.destroyed((0, b'')), (False, 3))


class RunLocal(unittest.TestCase):
    def run_local(self, root):
        def no_job(*args, **kw):
            raise AssertionError('job started')
        original, cb.job = cb.job, no_job
        try:
            return cb.run_local(argparse.Namespace(path=str(root), ttl=60, offline=False, receipts=None))
        finally:
            cb.job = original

    def test_refuses_uncommitted_coldbox_toml(self):
        root = git_repo({'app.py': '', 'coldbox.toml': '[checks]\nlint = true\n'})
        (root / 'coldbox.toml').write_text('[checks]\nlint = false\n')
        with self.assertRaises(SystemExit) as raised:
            self.run_local(root)
        self.assertIn('coldbox.toml', str(raised.exception))

    def test_refuses_untracked_coldbox_toml(self):
        root = git_repo({'app.py': ''})
        (root / 'coldbox.toml').write_text('[checks]\nlint = false\n')
        with self.assertRaises(SystemExit):
            self.run_local(root)


class Reap(unittest.TestCase):
    def reap(self, ip_script, project='coldbox', extra=(), **env):
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
                {'name': 'other', 'config': {'user.coldbox.expires': str(now - 5)}}, *extra]
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
        env = dict(os.environ, PATH=f'{tmp}:{os.environ["PATH"]}', COLDBOX_STATE=str(tmp / 'state'), **env)
        result = subprocess.run([sys.executable, str(ROOT / 'coldbox'), 'reap'], env=env, capture_output=True,
                                text=True)
        calls = (tmp / 'calls').read_text().splitlines()
        deletes = [line for line in calls if ' delete ' in line]
        self.assertEqual(deletes, [f'--force-local --project {project} delete cbx-1-a --force'])
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

    def test_follows_the_installed_names(self):
        result, calls = self.reap('#!/bin/sh\necho "[]"\n', 'cbx', COLDBOX_INCUS_ENV=str(custom_names()))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--force-local network acl edit cbx-isolated', calls)
        self.assertIn('--force-local --project cbx list --format json', calls)

    def test_an_instance_without_a_readable_expiry_is_skipped_and_the_rest_are_reaped(self):
        result, calls = self.reap('#!/bin/sh\necho "[]"\n', extra=[
            {'name': 'cbx-3-null', 'config': None},
            {'name': 'cbx-4-none'},
            {'name': 'cbx-5-bad', 'config': {'user.coldbox.expires': 'soon'}}])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('cbx-1-a deleted', result.stdout)
        for name in ('cbx-3-null', 'cbx-4-none', 'cbx-5-bad'):
            self.assertIn(f'coldbox reap: skipped {name}: no readable user.coldbox.expires', result.stderr)

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
                                 seed_cloud_init_on_create_only=lambda name: None,
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
                                 seed_cloud_init_on_create_only=lambda name: calls.append(('seed', name)),
                                 ic=lambda *a, **k: calls.append(a)), \
                mock.patch.object(cb.subprocess, 'run'):
            cb.build_new_base()
        self.assertLess(calls.index(('guest-files', 'base-new')), calls.index(('stop', 'base-new')))
        self.assertLess(calls.index(('seed', 'base-new')), calls.index(('snapshot', 'create', 'base-new', 'warm')))

    def test_a_failed_template_edit_fails_the_build_and_keeps_the_live_base(self):
        for failing in ('query', 'put'):
            calls, destroyed = [], []

            def run(command, **kwargs):
                if failing == 'put' and command[:5] == ['incus', '--force-local', 'query', '-X', 'PUT']:
                    if kwargs.get('check'):
                        raise subprocess.CalledProcessError(1, command)
                    return mock.Mock(returncode=1, stdout='')
                return mock.Mock(returncode=0, stdout='')

            def query(path):
                if failing == 'query':
                    raise subprocess.CalledProcessError(1, ['incus', 'query', path])
                return {'templates': {'/var/lib/cloud/seed/nocloud-net/meta-data': {'when': ['create', 'copy']}}}
            with self.subTest(failing=failing), \
                    mock.patch.multiple(cb, fetch_json=lambda url: None, runner_release=lambda _: ('2.340.0', 'u', 'd'),
                                        wait_ready=lambda *a, **k: None, push_guest_files=lambda name: None,
                                        destroy=lambda name: destroyed.append(name) or True, query=query,
                                        ic=lambda *a, **k: calls.append(a)), \
                    mock.patch.object(cb.subprocess, 'run', side_effect=run):
                with self.assertRaises(subprocess.CalledProcessError):
                    cb.build_new_base()
                self.assertEqual(destroyed, ['base-new', 'base-new'])
                self.assertNotIn(('snapshot', 'create', 'base-new', 'warm'), calls)
                self.assertFalse([c for c in calls if c[0] == 'move'])

    def test_clones_keep_the_cloud_init_seed_so_the_agent_does_not_reboot_them(self):
        both = ['create', 'copy']
        metadata = {'templates': {
            '/etc/hostname': {'when': list(both), 'template': 'hostname.tpl'},
            '/etc/hosts': {'when': list(both), 'template': 'hosts.tpl'},
            '/var/lib/cloud/seed/nocloud-net/meta-data': {'when': list(both), 'template': 'meta.tpl'},
            '/var/lib/cloud/seed/nocloud-net/user-data': {'when': list(both), 'template': 'user.tpl'}}}
        with mock.patch.object(cb, 'query', return_value=metadata) as query, \
                mock.patch.object(cb.subprocess, 'run') as run:
            cb.seed_cloud_init_on_create_only('base-new')
        path = f'/1.0/instances/base-new/metadata?project={cb.PROJECT}'
        query.assert_called_once_with(path)
        command = run.call_args.args[0]
        self.assertEqual(command[:6] + command[7:], ['incus', '--force-local', 'query', '-X', 'PUT', '--data', path])
        self.assertEqual({target: t['when'] for target, t in json.loads(command[6])['templates'].items()}, {
            '/etc/hostname': both, '/etc/hosts': both,
            '/var/lib/cloud/seed/nocloud-net/meta-data': ['create'],
            '/var/lib/cloud/seed/nocloud-net/user-data': ['create']})
        self.assertTrue(run.call_args.kwargs['check'])


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

    def test_missing_kvm_fix_depends_on_what_systemd_detect_virt_reports(self):
        for virt, fix in (('kvm', 'this host is a kvm VM or container, so Coldbox needs nested virtualization '
                                  'from its provider, see docs/vps.md'),
                          ('lxc', 'this host is a lxc VM or container, so Coldbox needs nested virtualization '
                                  'from its provider, see docs/vps.md'),
                          ('none', 'enable VT-x/AMD-V in the BIOS'),
                          (None, 'enable VT-x/AMD-V in the BIOS')):
            with self.subTest(virt=virt):
                tmp = Path(tempfile.mkdtemp())
                (tmp / 'systemctl').write_text('#!/bin/sh\nexit 3\n')
                (tmp / 'systemctl').chmod(0o755)
                if virt:
                    (tmp / 'systemd-detect-virt').write_text(f'#!/bin/sh\necho {virt}\n[ {virt} != none ]\n')
                    (tmp / 'systemd-detect-virt').chmod(0o755)
                no_kvm = lambda path: tmp / 'no-kvm' if str(path) == '/dev/kvm' else Path(path)
                with mock.patch.dict(os.environ, PATH=str(tmp)), mock.patch.object(cb, 'STATE', tmp), \
                        mock.patch.object(cb, 'Path', no_kvm):
                    output = quiet(cb.status, None)[1]
                self.assertIn(f'FAIL  hardware virtualization (/dev/kvm)\n      fix: {fix}\n', output)

    def test_incus_with_the_operation_metadata_crash_fails_with_the_zabbly_fix(self):
        for version, crashes in (('6.0.0', True), ('6.0.4', True), ('6.0.5', True), ('6.21', True), ('6.21.0', True),
                                 ('6.0.6', False), ('6.0.7', False), ('6.22.0', False), ('7.0.0', False)):
            with self.subTest(version=version):
                self.assertEqual(cb.incus_crashes(version), crashes)
                tmp = Path(tempfile.mkdtemp())
                (tmp / 'systemctl').write_text('#!/bin/sh\nexit 3\n')
                (tmp / 'systemctl').chmod(0o755)
                def server(path, version=version):
                    if path != '/1.0':
                        raise OSError('no incus')
                    return {'environment': {'server_version': version}}
                with mock.patch.dict(os.environ, PATH=str(tmp)), mock.patch.object(cb, 'STATE', tmp), \
                        mock.patch.object(cb, 'query', server):
                    output = quiet(cb.status, None)[1]
                line = f'Incus {version} has the fix for crash lxc/incus#2865 (6.0.6+)\n'
                if crashes:
                    self.assertIn(f'FAIL  {line}      fix: {cb.ZABBLY_LTS} && sudo coldbox build-base\n', output)
                else:
                    self.assertIn(f'PASS  {line}', output)

    def test_a_burst_only_repo_is_listed_as_burst_only(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / 'systemctl').write_text('#!/bin/sh\nexit 3\n')
        (tmp / 'systemctl').chmod(0o755)
        with mock.patch.dict(os.environ, PATH=str(tmp)), mock.patch.object(cb, 'STATE', tmp), \
                mock.patch.object(cb, 'registrations', lambda: [dict(target='me/app', active='active', slots=0)]):
            output = quiet(cb.status, None)[1]
        self.assertRegex(output, r'me/app +active +burst only')

    def test_a_job_vm_with_null_config_is_listed_not_a_crash(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / 'systemctl').write_text('#!/bin/sh\nexit 3\n')
        (tmp / 'systemctl').chmod(0o755)
        rows = json.dumps([{'name': 'cbx-3-c', 'status': 'Stopping', 'config': None}])
        incus = {'environment': {'server_version': '6.0.7'}, 'created_at': '2026-11-08T00:00:00', 'config': {},
                 'space': {'used': 0, 'total': GIB}}
        with mock.patch.dict(os.environ, PATH=str(tmp)), mock.patch.object(cb, 'STATE', tmp), \
                mock.patch.multiple(cb, ic=lambda *a, **k: rows, query=lambda path: incus,
                                    vm_memory=lambda: GIB, host_memory=lambda: 8 * GIB):
            output = quiet(cb.status, None)[1]
        self.assertRegex(output, r'cbx-3-c +Stopping +expires in 0 min')

    def test_a_standby_host_is_listed_as_standby(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / 'systemctl').write_text('#!/bin/sh\nexit 3\n')
        (tmp / 'systemctl').chmod(0o755)
        (tmp / 'standby').write_text('90\n')
        with mock.patch.dict(os.environ, PATH=str(tmp)), mock.patch.object(cb, 'STATE', tmp), \
                mock.patch.object(cb, 'STANDBY', tmp / 'standby'), mock.patch.object(cb, 'registrations', lambda: []):
            output = quiet(cb.status, None)[1]
        self.assertIn('standby host: bursts start only for jobs queued over 90 s', output)


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


GIB = 2**30
RUNNER_01 = dict(cpus=40, cores=20, load=7.0, available=210 * GIB, total=247 * GIB, vm_memory=16 * GIB)


class ScalingDecision(unittest.TestCase):
    def test_one_burst_per_queued_job_up_to_the_memory_ceiling(self):
        self.assertEqual(cb.burst_count(3, 3, **RUNNER_01), 3)
        self.assertEqual(cb.burst_count(30, 3, **RUNNER_01), 12)
        self.assertEqual(cb.burst_count(30, 15, **RUNNER_01), 0)
        self.assertEqual(cb.burst_count(0, 3, **RUNNER_01), 0)

    def test_busy_cores_stop_scaling(self):
        self.assertEqual(cb.burst_count(5, 3, **dict(RUNNER_01, load=18.5)), 1)
        self.assertEqual(cb.burst_count(5, 3, **dict(RUNNER_01, load=20.2)), 0)

    def test_hyperthreads_do_not_count_as_free_cores(self):
        self.assertEqual(cb.burst_count(5, 3, **dict(RUNNER_01, load=25.0)), 0)
        self.assertEqual(cb.burst_count(5, 3, **dict(RUNNER_01, cores=40, load=25.0)), 5)

    def test_physical_cores_count_each_online_cores_threads_once(self):
        root = Path(tempfile.mkdtemp())
        for cpu, siblings in enumerate(['0,20', '1,21', '0,20', '1,21', '2', '5']):
            (root / f'cpu{cpu}' / 'topology').mkdir(parents=True)
            (root / f'cpu{cpu}' / 'topology' / 'core_cpus_list').write_text(siblings + '\n')
        (root / 'online').write_text('0-2,3,4\n')
        with mock.patch.object(cb, 'CPUS', root):
            self.assertEqual(cb.physical_cores(), 3)
        (root / 'online').write_text('0-2,6\n')
        with mock.patch.object(cb, 'CPUS', root):
            self.assertEqual(cb.physical_cores(), 4)
        with mock.patch.object(cb, 'CPUS', root / 'missing'), mock.patch.object(cb.os, 'cpu_count', lambda: 6):
            self.assertEqual(cb.physical_cores(), 6)

    def test_unreadable_cpu_topology_raises_instead_of_counting_threads(self):
        root = Path(tempfile.mkdtemp())
        (root / 'cpu0' / 'topology' / 'core_cpus_list').mkdir(parents=True)
        (root / 'online').write_text('0\n')
        with mock.patch.object(cb, 'CPUS', root), self.assertRaises(OSError):
            cb.physical_cores()
        for malformed in ('zero', '0-1,3-2', '0-', '', '0,,1'):
            (root / 'online').write_text(malformed + '\n')
            with mock.patch.object(cb, 'CPUS', root), self.assertRaises(ValueError, msg=malformed):
                cb.physical_cores()

    def test_ceiling_is_one_vm_per_core_on_a_host_with_little_cpu(self):
        self.assertEqual(cb.burst_count(30, 0, **dict(RUNNER_01, cpus=4, load=0)), 4)

    def test_low_available_memory_stops_scaling(self):
        self.assertEqual(cb.burst_count(5, 3, **dict(RUNNER_01, available=17 * GIB)), 0)
        self.assertEqual(cb.burst_count(5, 3, **dict(RUNNER_01, available=34 * GIB)), 2)

    def test_only_jobs_a_coldbox_runner_can_take_count(self):
        self.assertTrue(cb.runs_here(['self-hosted', 'coldbox']))
        self.assertTrue(cb.runs_here(['self-hosted', 'Linux', 'X64', 'coldbox']))
        self.assertFalse(cb.runs_here(['ubuntu-latest']))
        self.assertFalse(cb.runs_here(['self-hosted', 'agentfleet-linux', 'other-host']))
        self.assertFalse(cb.runs_here([]))


class FakeGitHub:
    def __init__(self, runs, jobs, runners=(), repo=None):
        self.runs, self.jobs, self.runners = runs, jobs, list(runners)
        self.repo = repo or {'full_name': 'me/app', 'visibility': 'private'}

    def get(self, path):
        page = int(path.rsplit('page=', 1)[1]) if 'page=' in path else 1
        chunk = slice((page - 1) * 100, page * 100)
        if path == 'repos/me/app':
            return self.repo
        if '/runners?' in path:
            return {'runners': self.runners[chunk]}
        if '/jobs?' in path:
            return {'jobs': self.jobs[int(path.split('/runs/')[1].split('/')[0])][chunk]}
        status = path.split('status=')[1].split('&')[0]
        return {'workflow_runs': [{'id': run} for run in self.runs[status]][chunk]}

    def forget_unused(self):
        pass


def queued(n, labels=('self-hosted', 'coldbox')):
    return [{'status': 'queued', 'labels': list(labels)}] * n


def queued_ago(*seconds):
    return [{'status': 'queued', 'labels': ['coldbox'], 'created_at': time.strftime(
        '%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - ago))} for ago in seconds]


class QueuedJobs(unittest.TestCase):
    def test_queued_jobs_in_started_runs_and_past_the_first_page_count(self):
        github = FakeGitHub({'queued': [1], 'in_progress': [2]},
                            {1: queued(1), 2: [{'status': 'in_progress', 'labels': ['coldbox']}] * 100 + queued(4)
                             + queued(2, ['ubuntu-latest'])})
        self.assertEqual(cb.queued_jobs(github, 'me/app'), 5)

    def test_only_jobs_queued_longer_than_the_delay_count(self):
        github = FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued_ago(30, 200, 32)})
        self.assertEqual(cb.queued_jobs(github, 'me/app', 31), 2)
        self.assertEqual(cb.queued_jobs(github, 'me/app'), 3)


class Autoscale(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.started = []
        test = self

        class Child:
            returncode = None

            def __init__(self, argv):
                self.argv, self.pid = argv, os.getpid()
                test.started.append(argv)

            def poll(self):
                return None

            def send_signal(self, sig):
                pass

            def wait(self):
                return 143

        for name, value in dict(STATE=self.state, registrations=lambda: [dict(active='active', slots=1, target='x/y')] * 3,
                                vm_memory=lambda: 16 * GIB, host_vms=lambda: set(), query=lambda path: [],
                                host_memory=lambda field='MemTotal': (247 if field == 'MemTotal' else 210) * GIB).items():
            patcher = mock.patch.object(cb, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for patcher in (mock.patch.object(cb.subprocess, 'Popen', Child), mock.patch.object(cb.os, 'cpu_count', lambda: 40),
                        mock.patch.object(cb, 'physical_cores', lambda: 20),
                        mock.patch.object(cb.os, 'getloadavg', lambda: (7.0, 7.0, 7.0))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.args = argparse.Namespace(target='me/app', ttl=21600, allow_public=False, allow_forks=False)

    def scale(self, github, bursts=None):
        bursts = [] if bursts is None else bursts
        quiet(cb.autoscale, github, self.args, bursts)
        return bursts

    def test_backed_up_repo_gets_a_one_job_burst_runner_per_queued_job_beyond_idle_runners(self):
        idle = {'name': 'cbx-1-a', 'busy': False, 'status': 'online'}
        github = FakeGitHub({'queued': [1, 2, 3], 'in_progress': []}, {1: queued(2), 2: queued(2), 3: queued(1)},
                            [idle, {'name': 'cbx-2-b', 'busy': True, 'status': 'online'},
                             {'name': 'cbx-3-stale', 'busy': False, 'status': 'offline'},
                             {'name': 'other', 'busy': False, 'status': 'online'}])
        bursts = self.scale(github)
        self.assertEqual(len(bursts), 4)
        self.assertEqual(self.started[0][2:5], ['serve', 'me/app', '--worker'])
        self.assertEqual(self.started[0][-5:-1], ['--once', '--idle', str(cb.BURST_IDLE_SECONDS), '--name'])
        self.assertEqual(len({argv[-1] for argv in self.started}), 4)
        self.assertEqual((self.state / 'workers' / str(os.getpid())).read_text(), json.dumps(['me/app', cb.process_start(os.getpid()), bursts[-1].name, 'burst']))

    def test_foreign_vm_commitments_block_bursts_even_before_they_use_ram(self):
        rows = [dict(name='bootstrap', project='default', type='virtual-machine',
                     status='Running', expanded_config={'limits.memory': '240GiB'})]
        with mock.patch.object(cb, 'query', return_value=rows):
            bursts = self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(1)}))
        self.assertEqual(bursts, [])
        self.assertEqual(self.started, [])

    def test_bursts_from_every_repo_count_against_one_host_ceiling(self):
        folder = self.state / 'workers'
        folder.mkdir()
        (folder / str(os.getppid())).write_text(json.dumps(['other/repo', cb.process_start(os.getppid()), 'cbx-o', 'burst']))
        (folder / '999999999').write_text(json.dumps(['gone/repo', '1', 'cbx-g', 'burst']))
        (folder / str(os.getpid())).write_text(json.dumps(['reused/pid', '1', 'cbx-r', 'burst']))
        with mock.patch.object(cb, 'burst_count', wraps=cb.burst_count) as planned:
            self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(30)}))
        self.assertEqual(planned.call_args.args[:2], (30, 4))
        self.assertEqual(len(self.started), 11)
        self.assertFalse((folder / '999999999').exists())
        self.assertNotEqual((folder / str(os.getpid())).read_text(), 'reused/pid 1')

    def test_a_host_whose_cores_are_busy_leaves_queued_jobs_to_other_hosts(self):
        bursts, log = [], io.StringIO()
        with mock.patch.object(cb.os, 'getloadavg', lambda: (24.0, 24.0, 24.0)), contextlib.redirect_stderr(log):
            cb.autoscale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(3)}), self.args, bursts)
        self.assertEqual(bursts, [])
        self.assertEqual(self.started, [])
        self.assertEqual(log.getvalue(), 'coldbox: 3 queued job(s) and 0 idle runner(s) on me/app; started 0 burst '
                                         'runner(s); host has 3 job VM(s) and load 24.0 on 20 cores\n')

    def test_bursts_still_booting_count_as_load_on_every_repo(self):
        folder = self.state / 'workers'
        folder.mkdir()
        record = folder / str(os.getppid())
        for name, expected in ((f'cbx-{int(time.time())}-b', 1), ('cbx-1-b', 2)):
            for leftover in folder.iterdir():
                leftover.unlink()
            record.write_text(json.dumps(['other/repo', cb.process_start(os.getppid()), name, 'burst']))
            self.started.clear()
            with mock.patch.object(cb.os, 'getloadavg', lambda: (18.0, 18.0, 18.0)):
                self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(5)}))
            self.assertEqual(len(self.started), expected, name)

    def test_unreadable_cpu_topology_starts_no_bursts(self):
        bursts = []
        with mock.patch.object(cb, 'physical_cores', side_effect=PermissionError('core_cpus_list')), \
                self.assertRaises(OSError):
            quiet(cb.autoscale, FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(3)}), self.args, bursts)
        self.assertEqual(bursts, [])
        self.assertEqual(self.started, [])

    def test_bursts_still_starting_are_not_started_again_on_the_next_pass(self):
        github = FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(3)})
        bursts = self.scale(github)
        self.assertEqual(len(bursts), 3)
        self.scale(github, bursts)
        self.assertEqual(len(self.started), 3)

    def test_running_bursts_reserve_their_memory_however_long_they_boot(self):
        folder = self.state / 'workers'
        folder.mkdir()
        record = folder / str(os.getppid())
        record.write_text(json.dumps(['other/repo', cb.process_start(os.getppid()), 'cbx-o', 'burst']))
        os.utime(record, (0, 0))
        with mock.patch.object(cb, 'host_memory', lambda field='MemTotal': (247 if field == 'MemTotal' else 50) * GIB):
            self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(5)}))
        self.assertEqual(len(self.started), 2)

    def test_a_finished_burst_drops_its_pid_file(self):
        folder = self.state / 'workers'
        folder.mkdir()
        (folder / '4242').write_text(json.dumps(['me/app', '1', 'cbx-d', 'burst']))
        done = argparse.Namespace(pid=4242, poll=lambda: 0, returncode=0)
        bursts = self.scale(FakeGitHub({'queued': [], 'in_progress': []}, {}), [done])
        self.assertEqual(bursts, [])
        self.assertFalse((folder / '4242').exists())

    def test_register_counts_running_bursts_against_the_ceiling(self):
        folder = self.state / 'workers'
        folder.mkdir()
        (folder / str(os.getpid())).write_text(json.dumps(['other/repo', cb.process_start(os.getpid()), 'cbx-o', 'burst']))
        args = argparse.Namespace(target='me/new', slots=12, allow_public=False, allow_forks=False)
        with mock.patch.object(cb, 'guard') as guard, self.assertRaises(SystemExit) as refused:
            cb.register(args)
        self.assertIn('counting running burst VMs', str(refused.exception))
        guard.assert_not_called()

    def test_register_refuses_more_vms_than_cpu_threads(self):
        args = argparse.Namespace(target='me/new', slots=5, allow_public=False, allow_forks=False)
        with mock.patch.object(cb.os, 'cpu_count', lambda: 4), \
                mock.patch.object(cb, 'host_memory', lambda field='MemTotal': 1024 * GIB), \
                mock.patch.object(cb, 'guard') as guard, self.assertRaises(SystemExit) as refused:
            cb.register(args)
        self.assertIn('4 CPU threads', str(refused.exception))
        guard.assert_not_called()

    def test_vms_still_on_the_host_count_against_the_ceiling(self):
        with mock.patch.object(cb, 'host_vms', lambda: {f'cbx-{n}' for n in range(10)}):
            self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(5)}))
        self.assertEqual(len(self.started), 2)

    def test_a_burst_still_booting_counts_on_top_of_vms_already_on_the_host(self):
        folder = self.state / 'workers'
        folder.mkdir()
        (folder / str(os.getppid())).write_text(json.dumps(['other/repo', cb.process_start(os.getppid()), 'cbx-booting', 'burst']))
        with mock.patch.object(cb, 'host_vms', lambda: {f'cbx-{n}' for n in range(10)}):
            self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(5)}))
        self.assertEqual(len(self.started), 1)

    def test_a_base_worker_and_its_vm_count_once(self):
        folder = self.state / 'workers'
        folder.mkdir()
        (folder / str(os.getpid())).write_text(json.dumps(['x/y', cb.process_start(os.getpid()), 'cbx-base', 'base']))
        with mock.patch.object(cb, 'host_vms', lambda: {'cbx-base'}), \
                mock.patch.object(cb, 'registrations', lambda: [dict(active='active', slots=3, target='x/y')]), \
                mock.patch.object(cb, 'burst_count', wraps=cb.burst_count) as planned:
            self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(1)}))
        self.assertEqual(planned.call_args.args[1], 3)

    def test_re_registering_a_repo_at_the_ceiling_does_not_count_its_own_vms_twice(self):
        folder = self.state / 'workers'
        folder.mkdir()
        (folder / str(os.getpid())).write_text(json.dumps(['me/app', cb.process_start(os.getpid()), 'cbx-mine', 'base']))
        args = argparse.Namespace(target='me/app', slots=1, allow_public=False, allow_forks=False)
        with mock.patch.object(cb, 'registrations', lambda: [dict(active='active', slots=1, target='me/app')]), \
                mock.patch.object(cb, 'host_vms', lambda: {'cbx-mine'} | {f'cbx-{n}' for n in range(14)}), \
                mock.patch.object(cb, 'guard'), mock.patch.object(cb, 'instance', lambda target: 'me-app'), \
                mock.patch.object(cb.subprocess, 'run'):
            quiet(cb.register, args)

    def test_supervisor_and_worker_can_write_the_same_record_at_once(self):
        folder = self.state / 'workers'
        real = cb.os.replace

        pid = os.getpid()

        def racing(src, dst):
            with mock.patch.object(cb.os, 'getpid', lambda: 1), mock.patch.object(cb.os, 'replace', real):
                cb.note_worker(pid, 'me/app', 'cbx-1', 'burst')
            real(src, dst)

        with mock.patch.object(cb.os, 'replace', racing):
            cb.note_worker(pid, 'me/app', 'cbx-1', 'burst')
        self.assertEqual([w.name for w in cb.live_workers(folder)], ['cbx-1'])

    def test_register_counts_vms_still_on_the_host(self):
        args = argparse.Namespace(target='me/new', slots=1, allow_public=False, allow_forks=False)
        with mock.patch.object(cb, 'host_vms', lambda: {f'cbx-{n}' for n in range(15)}), mock.patch.object(cb, 'guard') as guard, \
                self.assertRaises(SystemExit):
            cb.register(args)
        guard.assert_not_called()

    def test_the_lock_works_when_another_user_created_it_read_only(self):
        lock = self.state / 'autoscale.lock'
        lock.touch(mode=0o444)
        with cb.host_lock():
            pass

    def test_register_never_waits_for_a_restart_while_holding_the_lock(self):
        args = argparse.Namespace(target='me/new', slots=1, allow_public=False, allow_forks=False)
        commands = []
        with mock.patch.object(cb, 'guard'), mock.patch.object(cb, 'instance', lambda target: 'me-new'), \
                mock.patch.object(cb.subprocess, 'run', lambda command, **_: commands.append(command)):
            quiet(cb.register, args)
        self.assertIn(['sudo', 'systemctl', 'restart', '--no-block', 'coldbox-runner@me-new.service'], commands)

    def test_starting_and_stopping_units_still_hold_their_slots(self):
        with mock.patch.object(cb, 'registrations', lambda: [dict(active=state, slots=4, target='x/y') for state in
                                                              ('active', 'activating', 'deactivating', 'inactive', 'failed')]), \
                mock.patch.object(cb, 'burst_count', wraps=cb.burst_count) as counted:
            self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(1)}))
        self.assertEqual(counted.call_args.args[1], 12)

    def test_a_registered_unit_not_yet_started_holds_its_slots(self):
        with mock.patch.object(cb, 'registrations', lambda: [dict(active='inactive', slots=4, target='x/y', configured=True),
                                                              dict(active='inactive', slots=4, target='gone/repo')]), \
                mock.patch.object(cb, 'burst_count', wraps=cb.burst_count) as counted:
            self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(1)}))
        self.assertEqual(counted.call_args.args[1], 4)

    def test_a_target_with_spaces_keeps_its_record(self):
        cb.note_worker(os.getpid(), 'my checkout', 'cbx-1', 'once')
        self.assertEqual([w.target for w in cb.live_workers(self.state / 'workers')], ['my checkout'])

    def test_a_worker_record_is_never_seen_half_written(self):
        written = []
        real = cb.os.replace
        with mock.patch.object(cb.os, 'replace', lambda src, dst: (written.append(cb.live_workers(dst.parent)), real(src, dst))):
            cb.note_worker(os.getpid(), 'me/app', 'cbx-1', 'base')
        self.assertEqual(written, [[]])
        self.assertEqual([w.name for w in cb.live_workers(self.state / 'workers')], ['cbx-1'])

    def test_a_vm_start_outside_the_autoscaler_is_admitted_only_below_the_ceiling(self):
        with mock.patch.object(cb, 'host_vms', lambda: {f'cbx-{n}' for n in range(12)}):
            self.assertFalse(cb.admit('me/app', 'cbx-solo', 'once'))
        self.assertFalse((self.state / 'workers' / str(os.getpid())).exists())
        self.assertTrue(cb.admit('me/app', 'cbx-solo', 'once'))
        self.assertEqual([w.kind for w in cb.live_workers(self.state / 'workers')], ['once'])

    def test_a_registered_base_worker_fills_its_own_reserved_slot(self):
        with mock.patch.object(cb, 'host_vms', lambda: {f'cbx-{n}' for n in range(14)}), \
                mock.patch.object(cb, 'registrations', lambda: [dict(active='active', slots=1, target='x/y')]):
            self.assertTrue(cb.admit('x/y', 'cbx-base', 'base'))
            self.assertFalse(cb.admit('x/y', 'cbx-extra', 'once'))

    def test_every_finished_burst_is_reaped_in_one_pass(self):
        done = [argparse.Namespace(pid=n, poll=lambda: 0, returncode=None) for n in (1, 2, 3)]
        self.assertEqual(self.scale(FakeGitHub({'queued': [], 'in_progress': []}, {}), done), [])

    def serve_burst_only(self, github):
        args = argparse.Namespace(**vars(self.args), slots=0, once=False, worker=False, idle=0)
        handlers = cb.signal.getsignal(cb.signal.SIGINT), cb.signal.getsignal(cb.signal.SIGTERM)
        self.addCleanup(cb.signal.signal, cb.signal.SIGINT, handlers[0])
        self.addCleanup(cb.signal.signal, cb.signal.SIGTERM, handlers[1])
        self.addCleanup(cb.STOPPING.clear)
        stop = lambda _: cb.signal.getsignal(cb.signal.SIGTERM)(cb.signal.SIGTERM, None)
        with mock.patch.object(cb, 'GitHub', lambda: github), mock.patch.object(cb.time, 'sleep', stop):
            return quiet(cb.serve, args)[0]

    def test_a_burst_only_repo_starts_a_burst_for_a_queued_job_and_no_base_worker(self):
        self.assertEqual(self.serve_burst_only(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(1)})), 143)
        self.assertEqual(len(self.started), 1)
        self.assertEqual(self.started[0][-5:-1], ['--once', '--idle', str(cb.BURST_IDLE_SECONDS), '--name'])

    def test_an_idle_burst_only_repo_starts_nothing_and_stops_cleanly(self):
        self.assertEqual(self.serve_burst_only(FakeGitHub({'queued': [], 'in_progress': []}, {})), 143)
        self.assertEqual(self.started, [])

    def test_a_supervisor_told_to_stop_mid_pass_starts_no_burst(self):
        cb.STOPPING.set()
        self.addCleanup(cb.STOPPING.clear)
        self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(3)}))
        self.assertEqual(self.started, [])

    def test_a_burst_only_repo_holds_no_ceiling_capacity(self):
        with mock.patch.object(cb, 'registrations', lambda: [dict(active='active', slots=0, target='x/y')] * 15), \
                mock.patch.object(cb, 'burst_count', wraps=cb.burst_count) as counted:
            self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(1)}))
        self.assertEqual(counted.call_args.args[1], 0)
        self.assertEqual(len(self.started), 1)

    def test_register_accepts_zero_slots_for_a_repo_even_on_a_full_host(self):
        args = argparse.Namespace(target='me/new', slots=0, allow_public=False, allow_forks=False)
        commands = []
        with mock.patch.object(cb, 'host_vms', lambda: {f'cbx-{n}' for n in range(15)}), \
                mock.patch.object(cb, 'guard') as guard, mock.patch.object(cb, 'instance', lambda target: 'me-new'), \
                mock.patch.object(cb.subprocess, 'run', lambda command, **kw: commands.append((command, kw))):
            code, output = quiet(cb.register, args)
        self.assertEqual(code, 0)
        guard.assert_called_once()
        self.assertIn('Environment=COLDBOX_SLOTS=0', next(kw['input'] for c, kw in commands if c[1] == 'tee'))
        self.assertIn('burst only', output)

    def test_register_refuses_negative_slots_and_zero_slots_for_an_org(self):
        for target, slots in (('me/new', -1), ('myorg', 0)):
            with mock.patch.object(cb, 'guard') as guard, self.assertRaises(SystemExit):
                cb.register(argparse.Namespace(target=target, slots=slots, allow_public=False, allow_forks=False))
            guard.assert_not_called()

    def standby(self, seconds):
        path = self.state / 'standby'
        path.write_text(f'{seconds}\n')
        patcher = mock.patch.object(cb, 'STANDBY', path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_standby_host_leaves_a_freshly_queued_job_to_the_primary_host(self):
        self.standby(90)
        output = quiet(cb.autoscale, FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued_ago(30)}), self.args, [])[1]
        self.assertEqual(self.started, [])
        self.assertIn('standby host: 1 job(s) on me/app queued under 90 s are left to the primary host', output)

    def test_a_standby_host_bursts_for_a_job_queued_longer_than_its_delay(self):
        self.standby(90)
        self.assertEqual(len(self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued_ago(30, 120, 300)}))), 2)

    def test_without_standby_a_freshly_queued_job_gets_a_burst_at_once(self):
        self.assertEqual(len(self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued_ago(1)}))), 1)

    def test_a_malformed_standby_file_fails_the_pass_instead_of_bursting(self):
        self.standby('soon')
        with self.assertRaises(ValueError):
            self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued_ago(300)}))
        self.assertEqual(self.started, [])

    def test_nothing_queued_starts_nothing(self):
        self.scale(FakeGitHub({'queued': [], 'in_progress': [1]}, {1: [{'status': 'in_progress', 'labels': ['coldbox']}]}))
        self.assertEqual(self.started, [])

    def test_a_renamed_or_public_repository_is_not_scaled(self):
        for repo in ({'full_name': 'someone/app', 'visibility': 'private'}, {'full_name': 'me/app', 'visibility': 'public'}):
            with self.assertRaises(ValueError):
                cb.autoscale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(3)}, repo=repo), self.args, [])
        self.assertEqual(self.started, [])


def headers(etag=None, remaining='4000'):
    from email.message import Message
    result = Message()
    result['X-RateLimit-Remaining'] = remaining
    result['X-RateLimit-Reset'] = '9999999999'
    if etag:
        result['ETag'] = etag
    return result


class Body(io.BytesIO):
    def __enter__(self):
        return self


class FreeRunners(unittest.TestCase):
    def test_online_idle_and_booting_runners_are_free_but_stale_offline_ones_are_not(self):
        now = 1_000_000
        runners = [{'name': f'cbx-{now - 10}-a', 'busy': False, 'status': 'offline'},
                   {'name': f'cbx-{now - 4000}-b', 'busy': False, 'status': 'online'},
                   {'name': f'cbx-{now - 4000}-c', 'busy': False, 'status': 'offline'},
                   {'name': f'cbx-{now - 10}-d', 'busy': True, 'status': 'online'},
                   {'name': 'other-fleet-x', 'busy': False, 'status': 'online'}]
        bursts = [argparse.Namespace(name=f'cbx-{now - 5}-new'), argparse.Namespace(name=f'cbx-{now - 10}-a'),
                  argparse.Namespace(name=f'cbx-{now - 4000}-gone')]
        self.assertEqual(cb.free_runners(runners, bursts, now), 3)


class ConditionalRequests(unittest.TestCase):
    def setUp(self):
        token = subprocess.CompletedProcess([], 0, stdout='test-token\n')
        patcher = mock.patch.object(cb.subprocess, 'run', return_value=token)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_unchanged_queue_is_read_from_cache_with_if_none_match(self):
        github, sent = cb.GitHub(), []

        def urlopen(request, timeout):
            sent.append(request.get_header('If-none-match'))
            if len(sent) == 1:
                body = Body(b'{"workflow_runs": []}')
                body.headers = headers('W/"abc"')
                return body
            raise cb.urllib.error.HTTPError(request.full_url, 304, 'Not Modified', headers(), io.BytesIO())

        with mock.patch.object(cb.urllib.request, 'urlopen', urlopen):
            first = github.get('repos/me/app/actions/runs?status=queued')
            second = github.get('repos/me/app/actions/runs?status=queued')
        self.assertEqual(sent, [None, 'W/"abc"'])
        self.assertEqual(first, second)

    def test_every_request_stops_while_quota_is_low_until_the_reset(self):
        github = cb.GitHub()
        github.note(headers(remaining='120'))
        with mock.patch.object(cb.urllib.request, 'urlopen') as urlopen:
            with self.assertRaises(RuntimeError):
                github.get('repos/me/app')
        urlopen.assert_not_called()
        github.note({'X-RateLimit-Remaining': '120', 'X-RateLimit-Reset': '1'})
        self.assertEqual(github.remaining, 120)
        self.assertLess(github.reset, time.time())


class BurstIdle(unittest.TestCase):
    def test_idle_burst_is_released_but_a_busy_one_is_kept(self):
        def busy(*args):
            raise RuntimeError('HTTP 422: Runner is still running a job')

        with mock.patch.object(cb, 'gh', busy), mock.patch.object(cb.os, 'kill') as kill:
            cb.release_idle('me/app', 7)
        kill.assert_not_called()
        with mock.patch.object(cb, 'gh', lambda *args: None), mock.patch.object(cb.os, 'kill') as kill:
            cb.release_idle('me/app', 7)
        kill.assert_called_once_with(os.getpid(), cb.signal.SIGUSR1)

    def test_a_failed_release_is_retried_unless_the_runner_is_busy(self):
        for message, retried in (('HTTP 502: Bad gateway', True), ('HTTP 422: Runner is still running a job', False)):
            def fails(*args):
                raise RuntimeError(message)

            with mock.patch.object(cb, 'gh', fails), mock.patch.object(cb.threading, 'Timer') as timer:
                cb.release_idle('me/app', 7)
            self.assertEqual(timer.called, retried)

    def test_idle_release_never_interrupts_vm_cleanup(self):
        cb.CLEANUP.set()
        self.addCleanup(cb.CLEANUP.clear)
        with mock.patch.object(cb, 'gh') as gh, mock.patch.object(cb.os, 'kill') as kill:
            cb.release_idle('me/app', 7)
        gh.assert_not_called()
        kill.assert_not_called()
        cb.idle_released()

    def test_released_burst_receipt_reads_idle(self):
        receipt = dict(mode='runner', error=cb.IDLE_RELEASED, vm=dict(destroyed=True))
        self.assertEqual(cb.verdict(receipt), 'idle')


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
                                     once=True, worker=False, idle=0)
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
        self.assertEqual(started[0][2:], ['serve', 'me/repo', '--worker', '--ttl', '60', '--allow-forks', '--once'])

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
                                     once=False, worker=False, idle=0)
        handlers = cb.signal.getsignal(cb.signal.SIGINT), cb.signal.getsignal(cb.signal.SIGTERM)
        try:
            with mock.patch.object(cb.subprocess, 'Popen', Child), mock.patch.object(cb.time, 'sleep'), \
                    mock.patch.object(cb, 'autoscale'):
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
                                     once=True, worker=False, idle=0, name=None)
        handler = cb.signal.getsignal(cb.signal.SIGTERM)
        self.addCleanup(cb.signal.signal, cb.signal.SIGTERM, handler)
        codes = {}
        try:
            for verdict in ('pass', 'fail', 'incomplete', 'refused'):
                with mock.patch.multiple(cb, guard=lambda *a: None, gh=lambda *a, **k: runner, note_worker=mock.DEFAULT,
                                         admit=mock.DEFAULT,
                                         job=lambda *a, **k: {'verdict': verdict}) as patched:
                    codes[verdict] = quiet(cb.serve, args)[0]
        finally:
            cb.signal.signal(cb.signal.SIGTERM, handler)
        self.assertEqual(codes, {'pass': 0, 'fail': 1, 'incomplete': 125, 'refused': 125})
        patched['note_worker'].assert_not_called()
        patched['admit'].assert_called_with('me/repo', mock.ANY, 'once')
        args.worker = True
        with mock.patch.multiple(cb, guard=lambda *a: None, gh=lambda *a, **k: runner, note_worker=mock.DEFAULT,
                                 admit=mock.DEFAULT, job=lambda *a, **k: {'verdict': 'pass'}) as patched:
            quiet(cb.serve, args)
        patched['note_worker'].assert_not_called()
        patched['admit'].assert_called_once_with('me/repo', mock.ANY, 'once')


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


class HostMemoryAdmission(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.rows = []
        self.owned = set()
        patches = dict(STATE=self.state, vm_memory=lambda: 4 * GIB,
                       host_memory=lambda: 10 * GIB, host_vms=lambda: self.owned,
                       registrations=lambda: [])
        for name, value in patches.items():
            patcher = mock.patch.object(cb, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(cb, 'query', side_effect=self.instances)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(cb.os, 'cpu_count', return_value=8)
        patcher.start()
        self.addCleanup(patcher.stop)

    def instances(self, path):
        self.assertEqual(path, '/1.0/instances?recursion=1&all-projects=true')
        return self.rows

    def vm(self, name='bootstrap', project='default', memory='8GiB', status='Running', **extra):
        return dict(name=name, project=project, type='virtual-machine', status=status,
                    config={}, expanded_config={'limits.memory': memory}, **extra)

    def test_foreign_vm_blocks_admission(self):
        self.rows = [self.vm()]
        self.assertFalse(cb.admit('repo', 'cbx-new', 'once'))
        self.assertFalse((self.state / 'workers' / str(os.getpid())).exists())

    def test_tracked_vm_is_not_counted_twice(self):
        cb.note_worker(os.getppid(), 'repo', 'cbx-worker', 'once')
        self.owned = {'cbx-worker'}
        self.rows = [self.vm('cbx-worker', cb.PROJECT, '4GiB')]
        self.assertTrue(cb.admit('repo', 'cbx-new', 'once'))

    def test_untracked_coldbox_vm_is_already_in_host_load(self):
        self.owned = {'cbx-orphan'}
        self.rows = [self.vm('cbx-orphan', cb.PROJECT, '4GiB')]
        self.assertTrue(cb.admit('repo', 'cbx-new', 'once'))

    def test_owned_vm_with_a_larger_limit_reserves_the_difference(self):
        self.owned = {'cbx-orphan'}
        self.rows = [self.vm('cbx-orphan', cb.PROJECT, '8GiB')]
        self.assertFalse(cb.admit('repo', 'cbx-new', 'once'))

    def test_same_name_in_another_project_is_foreign(self):
        self.owned = {'cbx-worker'}
        self.rows = [self.vm('cbx-worker', 'other', '4GiB')]
        self.assertFalse(cb.admit('repo', 'cbx-new', 'once'))

    def test_stopped_vms_and_containers_do_not_reserve_memory(self):
        container = self.vm()
        container['type'] = 'container'
        self.rows = [self.vm(status='Stopped'), container]
        self.assertTrue(cb.admit('repo', 'cbx-new', 'once'))

    def test_profile_memory_is_used_without_an_instance_override(self):
        self.rows = [self.vm(memory='6GiB')]
        self.assertFalse(cb.admit('repo', 'cbx-new', 'once'))

    def test_missing_limit_uses_coldbox_memory(self):
        self.rows = [self.vm(memory=''), self.vm(name='second', memory='')]
        self.assertFalse(cb.admit('repo', 'cbx-new', 'once'))

    def test_an_owned_vm_with_null_config_is_counted_at_coldbox_memory(self):
        self.owned = {'cbx-gone'}
        self.rows = [dict(self.vm('cbx-gone', cb.PROJECT), config=None, expanded_config=None)]
        self.assertTrue(cb.admit('repo', 'cbx-new', 'once'))

    def test_vm_up_is_refused_when_foreign_vms_fill_host(self):
        self.rows = [self.vm()]
        with mock.patch.object(cb, 'boot') as boot:
            with self.assertRaisesRegex(SystemExit, 'host is at its job VM ceiling'):
                cb.main(['vm', 'up'])
        boot.assert_not_called()


class AdhocVm(unittest.TestCase):
    def fake_incus(self, rows):
        tmp = Path(tempfile.mkdtemp())
        (tmp / 'rows.json').write_text(json.dumps(rows))
        fake = tmp / 'incus'
        fake.write_text('#!/bin/sh\n'
                        f'echo "$@" >> {tmp}/calls\n'
                        'case "$*" in\n'
                        f'  *" list "*) cat {tmp}/rows.json ;;\n'
                        '  *" exec "*) cat; exit 7 ;;\n'
                        '  *info*) echo "Error: Failed to fetch instance: Instance not found" >&2; exit 1 ;;\n'
                        '  *acl*) cat > /dev/null ;;\n'
                        'esac\n')
        fake.chmod(0o755)
        (tmp / 'ip').write_text('#!/bin/sh\necho "[]"\n')
        (tmp / 'ip').chmod(0o755)
        env = dict(os.environ, PATH=f'{tmp}:{os.environ["PATH"]}', COLDBOX_STATE=str(tmp / 'state'))
        return tmp, env

    def coldbox(self, env, *args, stdin=''):
        return subprocess.run([sys.executable, str(ROOT / 'coldbox'), *args], env=env, input=stdin,
                              capture_output=True, text=True)

    def rows(self, expires):
        return [{'name': 'cbx-1-a', 'status': 'Running',
                 'config': {'user.coldbox.expires': str(expires), cb.ADHOC: 'true'}},
                {'name': 'cbx-2-b', 'status': 'Running', 'config': {'user.coldbox.expires': str(expires)}}]

    def test_up_boots_a_marked_vm_with_the_ttl_and_prints_its_name(self):
        booted = []
        with mock.patch.multiple(cb, admit=lambda *a: True, new_id=lambda: 'cbx-9-z',
                                 boot=lambda *a: booted.append(a)):
            code, out = quiet(cb.main, ['vm', 'up', '--ttl', '60'])
        self.assertEqual((code, out), (0, 'cbx-9-z\n'))
        name, expires, offline, *config = booted[0]
        self.assertEqual((name, offline, config), ('cbx-9-z', False, ['--config', f'{cb.ADHOC}=true']))
        self.assertAlmostEqual(expires, time.time() + 60, delta=5)

    def test_up_default_ttl_is_two_hours(self):
        booted = []
        with mock.patch.multiple(cb, admit=lambda *a: True, boot=lambda *a: booted.append(a)):
            quiet(cb.main, ['vm', 'up', '--offline'])
        self.assertAlmostEqual(booted[0][1], time.time() + 7200, delta=5)
        self.assertTrue(booted[0][2])

    def test_up_that_fails_to_boot_destroys_the_vm(self):
        destroyed = []

        def fail(*_):
            raise RuntimeError('Refusing unsafe VM: raw.* config present')
        with mock.patch.multiple(cb, admit=lambda *a: True, new_id=lambda: 'cbx-9-z', boot=fail,
                                 destroy=destroyed.append):
            code, out = quiet(cb.main, ['vm', 'up'])
        self.assertEqual((code, destroyed), (125, ['cbx-9-z']))
        self.assertIn('Refusing unsafe VM', out)

    def test_up_is_refused_at_the_host_ceiling(self):
        with mock.patch.multiple(cb, admit=lambda *a: False, boot=mock.DEFAULT) as mocks:
            with self.assertRaises(SystemExit):
                cb.main(['vm', 'up'])
        mocks['boot'].assert_not_called()

    def test_exec_passes_stdin_stdout_and_exit_code_through(self):
        tmp, env = self.fake_incus(self.rows(int(time.time()) + 600))
        result = self.coldbox(env, 'vm', 'exec', 'cbx-1-a', '--', 'bash', '-s', stdin='echo hi\n')
        self.assertEqual((result.returncode, result.stdout), (7, 'echo hi\n'), result.stderr)
        line = [c for c in (tmp / 'calls').read_text().splitlines() if ' exec ' in c][0]
        self.assertIn('exec cbx-1-a --mode non-interactive', line)
        self.assertTrue(line.endswith('-- runuser -u runner -- bash -s'), line)

    def test_exec_refuses_a_vm_past_its_ttl_that_is_not_reaped_yet(self):
        tmp, env = self.fake_incus(self.rows(int(time.time()) - 5))
        result = self.coldbox(env, 'vm', 'exec', 'cbx-1-a', '--', 'true')
        self.assertEqual(result.returncode, 1)
        self.assertIn('cbx-1-a has expired', result.stderr)
        self.assertNotIn(' exec ', (tmp / 'calls').read_text())

    def test_exec_and_down_refuse_a_vm_that_is_not_ad_hoc(self):
        tmp, env = self.fake_incus(self.rows(int(time.time()) + 600))
        for args in (('exec', 'cbx-2-b', '--', 'true'), ('down', 'cbx-2-b')):
            result = self.coldbox(env, 'vm', *args)
            self.assertEqual(result.returncode, 1)
            self.assertIn('no ad-hoc VM named cbx-2-b', result.stderr)
        calls = (tmp / 'calls').read_text()
        self.assertNotIn(' exec ', calls)
        self.assertNotIn(' delete ', calls)

    def test_down_deletes_and_list_shows_only_ad_hoc_vms(self):
        tmp, env = self.fake_incus(self.rows(int(time.time()) + 600))
        listed = self.coldbox(env, 'vm', 'list')
        self.assertEqual(listed.stdout.split('\t')[:2], ['cbx-1-a', 'Running'])
        self.assertNotIn('cbx-2-b', listed.stdout)
        result = self.coldbox(env, 'vm', 'down', 'cbx-1-a')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--force-local --project coldbox delete cbx-1-a --force', (tmp / 'calls').read_text())

    def test_down_and_list_ignore_a_vm_with_null_config(self):
        rows = self.rows(int(time.time()) + 600) + [{'name': 'cbx-3-c', 'status': 'Stopping', 'config': None}]
        tmp, env = self.fake_incus(rows)
        listed = self.coldbox(env, 'vm', 'list')
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertNotIn('cbx-3-c', listed.stdout)
        result = self.coldbox(env, 'vm', 'down', 'cbx-1-a')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--force-local --project coldbox delete cbx-1-a --force', (tmp / 'calls').read_text())

    def test_reaper_deletes_an_expired_ad_hoc_vm(self):
        tmp, env = self.fake_incus(self.rows(int(time.time()) - 5)[:1])
        result = self.coldbox(env, 'reap')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('cbx-1-a deleted', result.stdout)


class WaitReady(unittest.TestCase):
    def wait(self, states):
        calls = []

        def run(args, **_kwargs):
            calls.append(args)
            ready = ' exec ' in f' {" ".join(args)} ' and states[0] == 'Ready'
            return subprocess.CompletedProcess(args, 0 if ready else 1)

        def query(_path):
            state = states.pop(0) if len(states) > 1 else states[0]
            return {'status': 'Stopped' if state == 'Stopped' else 'Running'}

        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds

        with mock.patch.object(cb.subprocess, 'run', run), mock.patch.object(cb, 'query', query), \
                mock.patch.object(cb.time, 'sleep', sleep), mock.patch.object(cb.time, 'time', lambda: clock[0]):
            cb.wait_ready('cbx-1-a', 3600, network=False)
        return [args for args in calls if 'start' in args]

    def test_restarts_a_vm_that_stops_before_its_agent_is_ready(self):
        starts = self.wait(['Running'] + ['Stopped'] * 32 + ['Running', 'Ready'])
        self.assertEqual(starts, [['incus', '--force-local', '--project', 'coldbox', 'start', 'cbx-1-a']])

    def test_gives_up_on_a_vm_that_keeps_stopping_instead_of_waiting_for_the_ttl(self):
        with self.assertRaisesRegex(RuntimeError, 'VM agent not ready after restarts'):
            self.wait(['Stopped'])

    def test_restarts_a_running_vm_whose_agent_never_comes_up(self):
        with mock.patch.object(cb, 'BOOT_SECONDS', -1):
            starts = self.wait(['Running', 'Ready'])
        self.assertEqual(starts, [['incus', '--force-local', '--project', 'coldbox', 'start', 'cbx-1-a']])

    def test_leaves_a_vm_alone_while_incus_briefly_reports_it_stopped(self):
        self.assertEqual(self.wait(['Running'] + ['Stopped'] * 5 + ['Running', 'Ready']), [])


class AdhocRun(unittest.TestCase):
    VM = json.dumps({'type': 'virtual-machine', 'status': 'Running', 'expanded_config': {},
                     'expanded_devices': {'root': {'type': 'disk', 'path': '/'},
                                          'eth0': {'type': 'nic', 'network': 'coldboxbr0',
                                                   'security.port_isolation': 'true'}}})
    ROOMY_HOST = ('import importlib.machinery, importlib.util, sys\n'
                  'loader = importlib.machinery.SourceFileLoader("coldbox", sys.argv[1])\n'
                  'cb = importlib.util.module_from_spec(importlib.util.spec_from_loader("coldbox", loader))\n'
                  'loader.exec_module(cb)\n'
                  'cb.host_memory = lambda field="MemTotal": 64 * 2**30\n'
                  'sys.exit(cb.main(sys.argv[2:]))\n')

    def argv(self, *args):
        return [sys.executable, '-c', self.ROOMY_HOST, str(ROOT / 'coldbox'), *args]

    def fake_incus(self, fail_start=False, undeletable=False):
        tmp = Path(tempfile.mkdtemp())
        (tmp / 'vm.json').write_text(self.VM)
        fake = tmp / 'incus'
        fake.write_text('#!/bin/sh\n'
                        f'echo "$@" >> {tmp}/calls\n'
                        'case "$*" in\n'
                        '  *" exec "*)\n'
                        '    while [ "$1" != "--" ]; do shift; done; shift\n'
                        '    [ "$1" = runuser ] && shift 4\n'
                        '    case "$1" in touch|cloud-init) exit 0 ;; coldbox) exit 1 ;; esac\n'
                        '    exec "$@" ;;\n'
                        '  *all-projects*) echo "[]" ;;\n'
                        f'  *" query "*) cat {tmp}/vm.json ;;\n'
                        '  *" profile get "*) echo 1GiB ;;\n'
                        '  *" list "*) echo "[]" ;;\n'
                        '  *" file pull "*) exit 1 ;;\n'
                        f'  *" start "*) exit {int(fail_start)} ;;\n'
                        + ('  *" info "*) exit 0 ;;\n' if undeletable else
                           '  *" info "*) echo "Error: Failed to fetch instance: Instance not found" >&2; exit 1 ;;\n') +
                        '  *acl*) cat > /dev/null ;;\n'
                        'esac\n')
        fake.chmod(0o755)
        (tmp / 'ip').write_text('#!/bin/sh\necho "[]"\n')
        (tmp / 'ip').chmod(0o755)
        env = dict(os.environ, PATH=f'{tmp}:{os.environ["PATH"]}', COLDBOX_STATE=str(tmp / 'state'))
        return tmp, env

    def coldbox(self, env, *args):
        return subprocess.run(self.argv(*args), env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def receipt(self, tmp):
        return json.loads(next((tmp / 'state' / 'receipts').glob('cbx-*.json')).read_text())

    def deleted(self, tmp):
        return [c for c in (tmp / 'calls').read_text().splitlines() if ' delete cbx-' in c and c.endswith('--force')]

    def test_guest_exit_code_and_output_pass_through_and_the_vm_is_destroyed(self):
        tmp, env = self.fake_incus()
        result = self.coldbox(env, 'run', '--ttl', '60', '--', 'sh', '-c', 'echo out; echo err >&2; exit 3')
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(result.stdout, 'out\nerr\n')
        receipt = self.receipt(tmp)
        self.assertEqual((receipt['mode'], receipt['exit'], receipt['verdict'], receipt['vm']['destroyed']),
                         ('adhoc', 3, 'fail', True))
        self.assertEqual(receipt['command'], ['sh', '-c', 'echo out; echo err >&2; exit 3'])
        self.assertEqual(len(self.deleted(tmp)), 1)
        line = [c for c in (tmp / 'calls').read_text().splitlines() if 'echo out' in c][0]
        self.assertIn('--cwd /home/runner', line)
        self.assertIn('-- runuser -u runner -- sh -c', line)

    def test_success_exits_0_and_destroys_the_vm(self):
        tmp, env = self.fake_incus()
        result = self.coldbox(env, 'run', '--', 'true')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.receipt(tmp)['verdict'], 'pass')
        self.assertEqual(len(self.deleted(tmp)), 1)

    def test_the_receipt_names_the_host_that_ran_it(self):
        tmp, env = self.fake_incus()
        self.assertEqual(self.coldbox(env, 'run', '--', 'true').returncode, 0)
        self.assertEqual(self.receipt(tmp)['host'], os.uname().nodename)

    def test_a_vm_that_fails_to_boot_is_destroyed_and_exits_125(self):
        tmp, env = self.fake_incus(fail_start=True)
        result = self.coldbox(env, 'run', '--', 'true')
        self.assertEqual(result.returncode, 125, result.stderr)
        receipt = self.receipt(tmp)
        self.assertEqual((receipt['verdict'], receipt['vm']['destroyed']), ('incomplete', True))
        self.assertIn('start', receipt['error'])
        self.assertEqual(len(self.deleted(tmp)), 1)

    def test_a_vm_that_cannot_be_deleted_exits_125_even_when_the_command_succeeds(self):
        tmp, env = self.fake_incus(undeletable=True)
        result = self.coldbox(env, 'run', '--', 'true')
        self.assertEqual(result.returncode, 125, result.stderr)
        receipt = self.receipt(tmp)
        self.assertEqual((receipt['exit'], receipt['verdict'], receipt['vm']['destroyed']), (0, 'incomplete', False))
        self.assertNotIn('error', receipt)
        self.assertEqual(len(self.deleted(tmp)), 3)

    def test_receipts_dir_keeps_a_test_run_out_of_the_receipt_folder(self):
        tmp, env = self.fake_incus()
        root = git_repo({'coldbox.toml': '[checks]\nlint = false\n[commands]\nstress = "true"\n'})
        for mode, args in (('adhoc', ['--', 'true']), ('local', [str(root)])):
            with self.subTest(mode):
                elsewhere = tmp / f'{mode}-receipts'
                result = self.coldbox(env, 'run', '--receipts', str(elsewhere), *args)
                self.assertIn(f'receipt {elsewhere}/cbx-', result.stderr)
                receipt = json.loads(next(elsewhere.glob('cbx-*.json')).read_text())
                self.assertEqual((receipt['mode'], receipt['vm']['destroyed']), (mode, True))
                self.assertEqual(receipt['log'], str(elsewhere / f"{receipt['id']}.log"))
                self.assertEqual(list((tmp / 'state' / 'receipts').glob('*')), [])

    def test_output_streams_while_the_command_still_runs(self):
        tmp, env = self.fake_incus()
        go = tmp / 'go'
        child = subprocess.Popen(self.argv('run', '--', 'sh', '-c',
                                           f'echo first; while [ ! -e {go} ]; do sleep 0.1; done; echo second'),
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        self.addCleanup(child.kill)
        self.addCleanup(child.stdout.close)
        self.assertEqual(child.stdout.readline(), b'first\n')
        self.assertIsNone(child.poll())
        go.touch()
        self.assertEqual(child.stdout.read(), b'second\n')
        self.assertEqual(child.wait(timeout=30), 0)

    def test_an_empty_command_or_a_path_with_a_command_is_refused_before_any_vm(self):
        tmp, env = self.fake_incus()
        for args, message in ((['run', '--'], 'give a command to run'),
                              (['run', 'some/repo', '--', 'true'], 'not both')):
            with self.subTest(args):
                result = self.coldbox(env, *args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
        self.assertFalse((tmp / 'calls').exists())

    def test_vm_exec_still_gets_its_own_command(self):
        vms = {'cbx-1-a': {'config': {'user.coldbox.expires': str(int(time.time()) + 600)}}}
        with mock.patch.object(cb, 'adhoc_vms', lambda: vms), \
                mock.patch.object(cb.subprocess, 'run', return_value=subprocess.CompletedProcess([], 4)) as run:
            self.assertEqual(cb.main(['vm', 'exec', 'cbx-1-a', '--', 'echo', 'hi']), 4)
        self.assertEqual(run.call_args[0][0][-2:], ['echo', 'hi'])


class InstallPoolAdoption(unittest.TestCase):
    def claim(self, pool_mark, project_mark, used_by):
        text = (ROOT / 'install.sh').read_text()
        start = text.index('only_coldbox_uses_pool() {')
        block = text[start:text.index('\n}\n', text.index('claim() {', start)) + 3]
        tmp = Path(tempfile.mkdtemp())
        (tmp / 'pool.json').write_text(json.dumps({'used_by': used_by}))
        script = (f'die() {{ echo "$*" >&2; exit 1; }}\n'
                  f'ic() {{ case "$*" in\n'
                  f'  "storage show cbx") ;;\n'
                  f'  "storage get cbx user.coldbox.managed") echo {pool_mark} ;;\n'
                  f'  "project get cbx user.coldbox.managed") echo {project_mark} ;;\n'
                  f'  "query /1.0/storage-pools/cbx") cat {tmp}/pool.json ;;\n'
                  f'  *) echo "ic $*" >> {tmp}/writes ;;\n'
                  f'esac; }}\n'
                  f'prefix=cbx mark=user.coldbox.managed=true\n{block}claim storage cbx\n')
        result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], capture_output=True, text=True)
        writes = (tmp / 'writes').read_text() if (tmp / 'writes').exists() else ''
        return result.returncode, writes, result.stderr

    coldbox_only = ['/1.0/instances/base-dev?project=cbx', '/1.0/profiles/coldbox?project=cbx']

    def test_an_unmarked_pool_only_the_coldbox_project_uses_is_marked_not_recreated(self):
        code, writes, stderr = self.claim('', 'true', self.coldbox_only)
        self.assertEqual((code, writes), (0, 'ic storage set cbx user.coldbox.managed=true\n'), stderr)
        self.assertIn('adopted storage pool cbx', stderr)

    def test_a_marked_pool_is_kept_untouched(self):
        self.assertEqual(self.claim('true', '', [])[:2], (0, ''))

    def test_a_pool_anything_else_uses_is_refused_untouched(self):
        for project_mark, used_by in (('true', self.coldbox_only + ['/1.0/instances/day-one-box']),
                                      ('true', self.coldbox_only + ['/1.0/images/abc?project=cbxother']),
                                      ('', self.coldbox_only),
                                      ('true', [])):
            with self.subTest(project_mark=project_mark, used_by=used_by):
                code, writes, stderr = self.claim('', project_mark, used_by)
                self.assertEqual((code, writes), (1, ''))
                self.assertIn('Coldbox did not create it', stderr)


if __name__ == '__main__':
    unittest.main()
