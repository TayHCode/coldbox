import argparse
import contextlib
import fcntl
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ['COLDBOX_HOST_CHECKS'] = str(Path(tempfile.mkdtemp()) / 'checks.toml')
os.environ['COLDBOX_INCUS_ENV'] = str(Path(tempfile.mkdtemp()) / 'incus.env')
os.environ['COLDBOX_STATE'] = tempfile.mkdtemp()
os.environ['COLDBOX_GITHUB_APP_ENV'] = str(Path(tempfile.mkdtemp()) / 'github-app.env')
os.environ['COLDBOX_RESERVE'] = str(Path(tempfile.mkdtemp()) / 'reserve')
for inherited_from_a_coldbox_job in ('PYTEST_ADDOPTS', 'COLDBOX_JOB_DIR', 'COLDBOX_JUNIT'):
    os.environ.pop(inherited_from_a_coldbox_job, None)


def load_coldbox():
    loader = importlib.machinery.SourceFileLoader('coldbox', str(ROOT / 'coldbox'))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader('coldbox', loader))
    loader.exec_module(module)
    return module


cb = load_coldbox()
cb.CI_HOSTS = Path(tempfile.mkdtemp()) / 'ci-hosts.json'
cb.CI_HOSTS.write_text(json.dumps(dict(primary=os.uname().nodename, overflow=[])))
cb.VM_MAX = Path(tempfile.mkdtemp()) / 'vm-max'
cb.APP_ENV = Path(os.environ['COLDBOX_STATE']) / 'github-app.env'
cb.APP_KEY = Path(os.environ['COLDBOX_STATE']) / 'github-app.pem'
os.environ.pop('COLDBOX_GITHUB_APP_ID', None)
cb.GUEST_FILES = Path(tempfile.mkdtemp()) / 'missing'


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
    def test_version_prints_version_and_file_build_and_exits_0(self):
        result = subprocess.run([sys.executable, str(ROOT / 'coldbox'), '--version'], capture_output=True, text=True,
                                timeout=30)
        build = hashlib.sha256((ROOT / 'coldbox').read_bytes()).hexdigest()[:12]
        self.assertEqual((result.returncode, result.stderr), (0, ''))
        self.assertRegex(result.stdout, r'^coldbox 0\.2\.0 \(build [0-9a-f]{12}\)\n$')
        self.assertEqual(result.stdout, f'coldbox 0.2.0 (build {build})\n')


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

    def test_python_runs_ruff_and_mypy_only_when_configured(self):
        ruff = cb.PY_VENV + 'pip install -q ruff && ruff check .'
        found = cb.detect(repo({'pyproject.toml': '[tool.ruff]\nline-length = 120\n[tool.mypy]\nstrict = true\n'}))
        self.assertEqual(found['lint'][1:], [ruff, cb.PY_VENV + 'pip install -q mypy && mypy .'])
        found = cb.detect(repo({'pyproject.toml': '[project]\nname = "x"\n[tool.mypy]\nfiles = ["src"]\n', 'ruff.toml': ''}))
        self.assertEqual(found['lint'][1:], [ruff, cb.PY_VENV + 'pip install -q mypy . && mypy'])
        found = cb.detect(repo({'app.py': '', 'mypy.ini': '[mypy]\n'}))
        self.assertEqual(found['lint'][1:], [cb.PY_VENV + 'pip install -q mypy && mypy .'])
        found = cb.detect(repo({'app.py': '', '.mypy.ini': '[mypy]\nfiles = src\n'}))
        self.assertEqual(found['lint'][1:], [cb.PY_VENV + 'pip install -q mypy && mypy'])
        found = cb.detect(repo({'app.py': '', 'mypy.ini': '[mypy]\nfiles = %(MYPY_CONFIG_FILE_DIR)s/src\n'}))
        self.assertEqual(found['lint'][1:], [cb.PY_VENV + 'pip install -q mypy && mypy'])
        found = cb.detect(repo({'app.py': '', 'mypy.ini': 'no section\n'}))
        self.assertEqual(found['lint'][1:], [cb.PY_VENV + 'pip install -q mypy && mypy .'])
        found = cb.detect(repo({'mypy.ini': '[mypy]\nstrict = True\n', 'pyproject.toml': '[tool.mypy]\nfiles = ["src"]\n',
                                'requirements.txt': 'pydantic\n'}))
        self.assertEqual(found['lint'][1:], [cb.PY_VENV + 'pip install -q mypy -r requirements.txt && mypy .'])
        self.assertEqual(cb.detect(repo({'ruff.toml': ''}))['lint'][1:], [ruff])
        for text in ('[project]\nname = "x"\n', 'not toml [', 'tool = "ruff mypy"\n', '[tool]\nmypy = 1\n'):
            self.assertEqual(len(cb.detect(repo({'pyproject.toml': text}))['lint']), 1, text)

    def test_node_package_manager_follows_the_lockfile(self):
        for lock, setup in (('pnpm-lock.yaml', 'npx --yes pnpm install --frozen-lockfile'),
                            ('yarn.lock', 'npx --yes yarn install --frozen-lockfile'),
                            ('package-lock.json', 'npm ci'), (None, 'npm install')):
            files = {'package.json': '{}'} | ({lock: ''} if lock else {})
            self.assertEqual(cb.detect(repo(files))['setup'], [setup], lock)

    def test_pnpm_and_yarn_use_the_pinned_package_manager_only_when_it_is_a_plain_version(self):
        for manager, lock, setup in (('pnpm@9.15.0+sha512.abc', 'pnpm-lock.yaml', 'npx --yes pnpm@9.15.0 install --frozen-lockfile'),
                                     ('yarn@1.22.22', 'yarn.lock', 'npx --yes yarn@1.22.22 install --frozen-lockfile'),
                                     ('yarn@4.5.1', 'yarn.lock', 'npx --yes -p @yarnpkg/cli-dist@4.5.1 yarn install --immutable'),
                                     ('yarn@4.5.1', 'pnpm-lock.yaml', 'npx --yes pnpm install --frozen-lockfile'),
                                     ('pnpm@9; rm -rf /', 'pnpm-lock.yaml', 'npx --yes pnpm install --frozen-lockfile'),
                                     ('pnpm@latest', 'pnpm-lock.yaml', 'npx --yes pnpm install --frozen-lockfile'),
                                     (['pnpm@9'], 'pnpm-lock.yaml', 'npx --yes pnpm install --frozen-lockfile')):
            files = {'package.json': json.dumps({'packageManager': manager}), lock: ''}
            self.assertEqual(cb.detect(repo(files))['setup'], [setup], manager)

    def test_yarn_2_plus_runs_detected_tools_through_its_own_yarn(self):
        pkg = json.dumps({'packageManager': 'yarn@4.5.1', 'devDependencies': {'eslint': '^9', 'jest': '^29'}})
        found = cb.detect(repo({'package.json': pkg, 'yarn.lock': '', 'tsconfig.json': '{}'}))
        yarn = 'npx --yes -p @yarnpkg/cli-dist@4.5.1 yarn'
        self.assertEqual(found, {'setup': [f'{yarn} install --immutable'], 'lint': [f'{yarn} eslint .', f'{yarn} tsc --noEmit'],
                                 'test': [f'{yarn} jest']})

    def test_tsconfig_gets_a_typecheck(self):
        found = cb.detect(repo({'package.json': '{}', 'tsconfig.json': '{}'}))
        self.assertEqual(found['lint'], ['npx --yes -p typescript tsc --noEmit'])
        self.assertNotIn('lint', cb.detect(repo({'package.json': '{}'})))

    def test_node_tools_from_dev_dependencies_or_config_without_scripts(self):
        pkg = json.dumps({'devDependencies': {'eslint': '^9', 'vitest': '^2'}})
        found = cb.detect(repo({'package.json': pkg}))
        self.assertEqual((found['lint'], found['test']), (['npx --yes eslint .'], ['npx --yes vitest run']))
        found = cb.detect(repo({'package.json': '{}', 'eslint.config.mjs': '', 'jest.config.js': ''}))
        self.assertEqual((found['lint'], found['test']), (['npx --yes eslint .'], ['npx --yes jest']))
        found = cb.detect(repo({'package.json': json.dumps({'devDependencies': {'jest': '^29'}}), '.eslintrc.json': '{}'}))
        self.assertEqual((found['lint'], found['test']), (['npx --yes eslint .'], ['npx --yes jest']))
        found = cb.detect(repo({'package.json': json.dumps({'eslintConfig': {}, 'jest': {}})}))
        self.assertEqual((found['lint'], found['test']), (['npx --yes eslint .'], ['npx --yes jest']))

    def test_node_scripts_win_over_detected_tools(self):
        pkg = json.dumps({'scripts': {'lint': 'eslint src', 'test': 'vitest run'},
                          'devDependencies': {'eslint': '^9', 'vitest': '^2', 'jest': '^29'}})
        found = cb.detect(repo({'package.json': pkg}))
        self.assertEqual((found['lint'], found['test']), (['npm run lint'], ['npm test']))
        pkg = json.dumps({'scripts': {'test': 'echo "Error: no test specified" && exit 1'}, 'devDependencies': {'jest': '^29'}})
        self.assertEqual(cb.detect(repo({'package.json': pkg}))['test'], ['npx --yes jest'])

    def test_go_rust_and_shell(self):
        found = cb.detect(repo({'go.mod': '', 'z_test.go': '', 'Cargo.toml': '', 'src/lib.rs': '#[cfg(test)]\nmod t {\n    #[test]\n    fn a() {}\n}\n',
                                'bin/x.sh': ''}))
        self.assertEqual(found['test'], [cb.GO_TEST, cb.CARGO_TEST])
        self.assertTrue(any('shellcheck' in c for c in found['lint']))

    def test_no_file_makes_a_verify_check(self):
        self.assertNotIn('verify', cb.detect(repo({'.fleet/verify.sh': '', 'verify.sh': ''})))

    def test_go_and_rust_without_tests_have_no_test_check(self):
        found = cb.detect(repo({'go.mod': '', 'z.go': 'package z\n', 'Cargo.toml': '', 'src/lib.rs': 'pub fn f() {}\n'}))
        self.assertNotIn('test', found)
        self.assertEqual(len(found['lint']), 2)

    def test_rust_async_test_attribute_counts_as_a_test(self):
        found = cb.detect(repo({'Cargo.toml': '', 'tests/it.rs': '#[tokio::test]\nasync fn a() {}\n'}))
        self.assertEqual(found['test'], [cb.CARGO_TEST])

    def test_go_and_rust_test_commands_fail_when_no_test_ran(self):
        tmp = Path(tempfile.mkdtemp())
        cases = [(cb.GO_TEST, 'go', 'ok  \tz\t0.01s [no tests to run]', 0, 1, 0),
                 (cb.GO_TEST, 'go', '=== RUN   TestA\n--- PASS: TestA (0.00s)\n    --- PASS: TestA/b (0.00s)\nok  \tz', 0, 0, 2),
                 (cb.GO_TEST, 'go', '--- FAIL: TestA (0.00s)\n--- PASS: TestB (0.00s)\nFAIL', 1, 1, 1),
                 (cb.CARGO_TEST, 'cargo', 'running 0 tests\ntest result: ok. 0 passed; 0 failed', 0, 1, 0),
                 (cb.CARGO_TEST, 'cargo', 'running 2 tests\ntest a ... ok\ntest src/lib.rs - f (line 3) ... ok\n'
                                          'test result: ok. 2 passed; 0 failed', 0, 0, 2),
                 (cb.CARGO_TEST, 'cargo', 'test result: ok. 1 passed\ntest result: FAILED. 0 passed; 1 failed', 101, 1, 0)]
        for command, tool, output, code, want, count in cases:
            (tmp / tool).write_text(f'#!/bin/sh\nprintf "%s\\n" "{output}"\nexit {code}\n')
            (tmp / tool).chmod(0o755)
            report = tmp / 'reports' / f'{tool}-{count}-{want}.xml'
            report.parent.mkdir(exist_ok=True)
            result = subprocess.run(['bash', '-eo', 'pipefail', '-c', command], capture_output=True, text=True,
                                    env=dict(os.environ, PATH=f'{tmp}:{os.environ["PATH"]}', COLDBOX_JUNIT=str(report)))
            self.assertEqual(result.returncode != 0, bool(want), (command, output, result.stderr))
            self.assertEqual(cb.junit_summary([report.parent])['total'], count, (command, output))
            report.unlink()

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


class VmSizes(unittest.TestCase):
    def test_coldbox_toml_vm_size_is_one_of_three_names(self):
        for size in cb.SIZES:
            path = repo({'coldbox.toml': f'[vm]\nsize = "{size}"\n'}) / 'coldbox.toml'
            self.assertEqual(cb.load_config(path)[2], size)
        self.assertIsNone(cb.load_config(repo({'coldbox.toml': '[checks]\nlint = true\n'}) / 'coldbox.toml')[2])
        for text in ('[vm]\nsize = "huge"\n', '[vm]\nsize = 2\n', '[vm]\n', '[vm]\nsize = "large"\ncpus = 4\n',
                     'vm = "large"\n'):
            with self.subTest(text=text), self.assertRaisesRegex(cb.ConfigError, r"\[vm\] holds only size"):
                cb.plan(repo({'app.py': '', 'coldbox.toml': text}))

    def test_small_halves_and_large_doubles_the_profile_up_to_the_host(self):
        def shape(size, host_gib, threads):
            with mock.patch.multiple(cb, vm_memory=lambda: 12 * GIB, ic=lambda *a: '8\n',
                                     host_memory=lambda field='MemTotal': host_gib * GIB), \
                    mock.patch.object(cb.os, 'cpu_count', lambda: threads):
                return cb.vm_shape(size)
        self.assertEqual(shape('default', 64, 32), (None, {}))
        self.assertEqual(shape('small', 64, 32), (6 * GIB, {'limits.cpu': '4', 'limits.memory': '6144MiB'}))
        self.assertEqual(shape('large', 64, 32), (24 * GIB, {'limits.cpu': '16', 'limits.memory': '24576MiB'}))
        self.assertEqual(shape('large', 20, 12), (16 * GIB, {'limits.cpu': '12', 'limits.memory': '16384MiB'}))
        self.assertEqual(shape('large', 14, 8), (12 * GIB, {'limits.cpu': '8', 'limits.memory': '12288MiB'}))

    def test_a_workflow_label_picks_the_size_and_sized_runners_take_only_their_size(self):
        self.assertEqual(cb.runner_labels('default'), cb.LABELS)
        self.assertEqual(cb.runner_labels('large'), ['self-hosted', 'linux', 'x64', 'coldbox-large'])
        for labels, size in ((['self-hosted', 'coldbox'], 'default'), (['self-hosted', 'Coldbox-Large'], 'large'),
                             (['coldbox-small'], 'small'), (['self-hosted', 'coldbox', 'coldbox-large'], None),
                             (['coldbox-small', 'coldbox-large'], None), (['coldbox-huge'], None), ([], None)):
            with self.subTest(labels=labels):
                self.assertEqual(cb.job_size(labels), size)
                self.assertEqual(cb.runs_here(labels), size is not None)

    def test_coldbox_run_boots_and_admits_the_size_coldbox_toml_asks_for(self):
        root = git_repo({'app.py': '', 'coldbox.toml': '[vm]\nsize = "small"\n'})
        booted = {}
        args = argparse.Namespace(path=str(root), cpus=None, memory=None, ttl=60, offline=False, receipts=None)
        with mock.patch.multiple(cb, HOST_CHECKS=root / 'none.toml',
                                 vm_shape=lambda size: (2 * GIB, {'limits.memory': f'{size}-2048MiB'}),
                                 admit=lambda *a, **k: booted.update(memory=k['memory']) or True,
                                 job=lambda *a, **k: booted.update(config=k['config']) or {'verdict': 'pass'}):
            self.assertEqual(cb.run_local(args), 0)
        self.assertEqual(booted, dict(memory=2 * GIB, config=['--config', 'limits.memory=small-2048MiB']))


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
        code, output = quiet(cb.detect_cmd, argparse.Namespace(path=str(root), workflow=False))
        self.assertEqual(code, 0)
        self.assertIn('lint = false', output)
        self.assertIn('test = true', output)

    def test_a_fleet_recipe_without_a_verify_entry_fails_checks_loudly(self):
        root = repo({'.fleet/verify.sh': '', 'tests/test_x.py': ''})
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(cb.main(['checks', str(root)]), 2)
        self.assertIn('add verify = "coldverify --ci" under [commands], or verify = false under [checks]', stderr.getvalue())

    def test_a_fleet_recipe_with_verify_switched_off_runs_the_rest(self):
        root = repo({'.fleet/verify.sh': '', 'tests/test_x.py': '', 'coldbox.toml': '[checks]\nverify = false\n'})
        self.assertEqual([name for name, _ in cb.plan(root)], ['test'])
        self.host.write_text('[checks]\nlint = false\nverify = false\n')
        self.assertEqual([name for name, _ in cb.plan(repo({'.fleet/verify.sh': '', 'tests/test_x.py': ''}))], ['test'])

    def test_verify_runs_the_command_coldbox_toml_names(self):
        root = repo({'.fleet/verify.sh': '', 'coldbox.toml': '[checks]\nverify = true\n[commands]\nverify = "coldverify --ci"\n'})
        self.assertEqual(cb.plan(root), [('verify', ['coldverify --ci'])])

    def test_verify_switched_on_without_a_command_is_an_error(self):
        root = repo({'.fleet/verify.sh': '', 'coldbox.toml': '[checks]\nverify = true\n'})
        with self.assertRaisesRegex(cb.ConfigError, r"check 'verify' is on but nothing was detected; add it under \[commands\]"):
            cb.plan(root)

    def test_detect_workflow_proposes_the_example_workflow_then_the_checks(self):
        import yaml
        root = repo({'pyproject.toml': '', 'tests/test_x.py': ''})
        code, output = quiet(cb.detect_cmd, argparse.Namespace(path=str(root), workflow=True))
        example = (ROOT / 'examples' / 'coldbox.yml').read_text()
        self.assertEqual(code, 0)
        self.assertTrue(output.startswith(example))
        self.assertIn('#   test: python3 -m pytest -q', output[len(example):].splitlines())
        self.assertNotIn('lint', output[len(example):])
        workflow = yaml.safe_load(output)
        job = workflow['jobs']['verify']
        self.assertIn('coldbox', job['runs-on'])
        self.assertIn('coldbox checks', [step.get('run') for step in job['steps']])

    def test_detect_workflow_prints_none_when_a_workflow_already_runs_coldbox_checks(self):
        root = repo({'pyproject.toml': '', 'tests/test_x.py': '',
                     '.github/workflows/ci.yaml': 'jobs:\n  t:\n    steps:\n      - run: coldbox checks\n'})
        code, output = quiet(cb.detect_cmd, argparse.Namespace(path=str(root), workflow=True))
        self.assertEqual(code, 1)
        self.assertNotIn('runs-on', output)
        self.assertIn('.github/workflows/ci.yaml already runs coldbox checks', output)

    def test_detect_workflow_lists_only_the_checks_the_repo_config_runs(self):
        root = repo({'pyproject.toml': '', 'tests/test_x.py': '', 'coldbox.toml': '[checks]\ntest = false\n',
                     '.github/workflows/ci.yml': '# TODO: add coldbox checks\njobs: {}\n'})
        self.assertEqual(quiet(cb.detect_cmd, argparse.Namespace(path=str(root), workflow=True)),
                         (1, 'coldbox: no checks would run; write [commands] in coldbox.toml yourself\n'))

    def test_detect_workflow_comments_out_every_line_of_a_multiline_command(self):
        import yaml
        root = repo({'coldbox.toml': '[commands]\ntest = """\nmake a\nmake b"""\n'})
        code, output = quiet(cb.detect_cmd, argparse.Namespace(path=str(root), workflow=True))
        self.assertEqual(code, 0)
        self.assertEqual(yaml.safe_load(output), yaml.safe_load((ROOT / 'examples' / 'coldbox.yml').read_text()))

    def test_detect_workflow_sees_the_run_not_the_step_name(self):
        for text, code in (('steps:\n  - name: Run coldbox checks\n    run: make\n', 0),
                           ('steps:\n  - run: make test # previously ran coldbox checks\n', 0),
                           ('steps:\n  - run: coldbox   checks\n', 1)):
            root = repo({'tests/test_x.py': '', '.github/workflows/ci.yml': text})
            with self.subTest(text=text):
                self.assertEqual(quiet(cb.detect_cmd, argparse.Namespace(path=str(root), workflow=True))[0], code)

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


class JobGuestFiles(unittest.TestCase):
    def run_job(self, files, fail=False, mode='adhoc'):
        calls = []
        state = Path(self.enterContext(tempfile.TemporaryDirectory()))

        def in_vm(name, *command, **kwargs):
            calls.append(command)
            if command[0] == 'tar':
                return ['sh', '-c', 'cat >/dev/null; exit ' + ('1' if fail else '0')]
            return ['false'] if command[0] == 'coldbox' else ['true']

        def command(*args):
            calls.append(('job-command',))
            return 0

        with mock.patch.multiple(cb, STATE=state, GUEST_FILES=files, boot=lambda *a: None,
                                 ic=lambda *a: None, ic_exec=in_vm, tee=command, pull=lambda *a: None,
                                 destroy=lambda name: True):
            quiet(cb.job, mode, 'cbx-overlay', 60, ['true'], 'me/repo')
        return calls, json.loads((state / 'receipts/cbx-overlay.json').read_text())

    def test_every_job_mode_pushes_guest_files_before_the_command(self):
        files = repo({'opt/tool/marker': 'new'})
        for mode in ('adhoc', 'local', 'runner'):
            with self.subTest(mode=mode):
                calls, receipt = self.run_job(files, mode=mode)
                extract = ('tar', '-C', '/', '-x', '--no-overwrite-dir', '--keep-directory-symlink')
                self.assertIn(extract, calls)
                self.assertLess(calls.index(extract), calls.index(('job-command',)))
                self.assertNotIn('error', receipt)

    def test_missing_guest_files_still_passes_without_an_extract(self):
        calls, receipt = self.run_job(Path(tempfile.mkdtemp()) / 'missing')
        self.assertFalse(any(call[0] == 'tar' for call in calls))
        self.assertIn(('job-command',), calls)
        self.assertEqual(receipt['verdict'], 'pass')

    def test_a_vm_that_never_becomes_ready_is_incomplete_with_a_cause(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addCleanup(cb.CLEANUP.clear)
        with mock.patch.multiple(cb, boot=mock.Mock(side_effect=RuntimeError()), ic=mock.DEFAULT,
                                 destroy=lambda name: True, vm_resources=lambda name: {}), \
                contextlib.redirect_stderr(io.StringIO()):
            cb.job('local', 'cbx-1-test', 60, ['job-command'], repo='me/app', receipts=folder.name)
        receipt = json.loads((Path(folder.name) / 'cbx-1-test.json').read_text())
        self.assertEqual((receipt['verdict'], receipt['cause'], receipt['error']),
                         ('incomplete', 'vm_not_ready', 'RuntimeError'))

    def test_readiness_is_on_disk_before_post_boot_work(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addCleanup(cb.CLEANUP.clear)
        path = Path(folder.name) / 'cbx-1-test.json'
        seen = []
        with mock.patch.multiple(cb, boot=mock.DEFAULT, ic=mock.DEFAULT, destroy=lambda name: True,
                                 vm_resources=lambda name: {},
                                 push_guest_files=lambda name: seen.append(json.loads(path.read_text())['vm'])), \
                mock.patch.object(cb.subprocess, 'run', side_effect=OSError('pull failed')), \
                contextlib.redirect_stderr(io.StringIO()):
            cb.job('local', 'cbx-1-test', 60, ['job-command'], repo='me/app', receipts=folder.name)
        killed = dict(seen[0] and json.loads(path.read_text()), error=None, verdict='incomplete')
        killed['vm'] = dict(seen[0], destroyed=True)
        self.assertEqual(cb.cause(killed), 'no_result')

    def test_failed_guest_push_stops_the_command_and_records_incomplete(self):
        calls, receipt = self.run_job(repo({'opt/tool/marker': 'new'}), fail=True)
        self.assertNotIn(('job-command',), calls)
        self.assertEqual(receipt['verdict'], 'incomplete')
        self.assertIn('failed', receipt['error'])
        self.assertTrue(receipt['vm']['destroyed'])


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

    def test_failed_setup_leaves_the_rest_not_run(self):
        root = repo({'coldbox.toml': '[commands]\nsetup = "false"\ntest = "true"\n'})
        out = root / 'checks.json'
        quiet(cb.run_checks, root, out)
        result = json.loads(out.read_text())
        self.assertEqual([(r['status'], r['exit']) for r in result['checks']], [('fail', 1), ('not_run', None)])
        receipt = dict(mode='local', vm={'destroyed': True, 'ready_seconds': 1}, exit=1,
                       checks=result['checks'], checks_verdict=result['verdict'])
        cb.settle(receipt)
        self.assertEqual((receipt['verdict'], receipt['cause']), ('fail', 'setup'))

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
        self.assertEqual(result.stderr, 'coldbox install: needs at least 5632 MiB (5.50 GiB) of RAM, 3584 MiB for '
                         'the host plus one 2GiB job VM; found 3841 MiB (3.75 GiB). Use a host with more RAM or a '
                         'smaller --memory\n')
        self.assertFalse(cb.fits(1, 2 * GIB, 3933320 * 1024))

    def test_an_8_gib_host_passes_with_half_its_ram_per_vm(self):
        result = self.check(8 * 1048576)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, 'memory=4GiB\n', ''))

    def test_the_floor_matches_admit_host_reserve_plus_one_vm_to_the_byte(self):
        for mem_kib, memory, vm in ((5767168, None, '2GiB'), (5767167, None, '2GiB'),
                                    (16252928, '12GiB', '12GiB'), (16252927, '12GiB', '12GiB'),
                                    (5623141, '2000MB', '2000MB'), (5623140, '2000MB', '2000MB'),
                                    (4718592, '1024MiB', '1024MiB'), (4718591, '1024MiB', '1024MiB'),
                                    (32 * 1048576, '26GiB', '26GiB'), (32 * 1048576, '27GiB', '27GiB')):
            with self.subTest(mem_kib=mem_kib, memory=memory):
                result = self.check(mem_kib, memory)
                admitted = cb.fits(1, cb.size_bytes(vm), mem_kib * 1024)
                self.assertEqual(result.returncode, 0 if admitted else 1, result.stderr)
                self.assertEqual(result.stdout, f'memory={vm}\n' if admitted else '')
                self.assertEqual(admitted, mem_kib in (5767168, 16252928, 5623141, 4718592) or vm == '26GiB')

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

    def test_the_default_pool_follows_free_space_down_to_56_gib(self):
        result = self.check(80 * GIB)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, 'pool=64GiB disk=40GiB\n', ''))
        result = self.check(60 * GIB)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, 'pool=56GiB disk=40GiB\n', ''))
        result = self.check(45 * GIB)
        self.assertEqual((result.returncode, result.stdout), (1, ''))
        self.assertEqual(result.stderr, 'coldbox install: needs 56GiB free under /var/lib for the ZFS pool '
                         '(COLDBOX_POOL_SIZE), found 45.00 GiB; free some space or set a smaller COLDBOX_POOL_SIZE\n')

    def test_dir_storage_needs_one_job_disk_and_ignores_the_pool_size(self):
        result = self.check(21 * GIB, storage='dir', pool_size='500GiB', disk='20GiB')
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, 'pool=500GiB disk=20GiB\n', ''))
        result = self.check(30 * GIB, storage='dir', pool_size='25GiB')
        self.assertEqual((result.returncode, result.stdout), (1, ''))
        self.assertEqual(result.stderr, "coldbox install: needs 40GiB free under /var/lib for one job VM's disk "
                         '(--disk), found 30.00 GiB; free some space or set a smaller --disk\n')

    def test_a_zfs_pool_must_hold_one_disk_and_the_base_images_past_the_reserve_admission_keeps(self):
        text = (ROOT / 'install.sh').read_text()
        start = text.index('    [[ $disk =~')
        check = text[start:text.index('\n', text.index('[[ $storage != zfs ]]', start)) + 1]
        script = ('die() { printf "coldbox install: %s\\n" "$*" >&2; exit 1; }\n'
                  + text[text.index('declare -A unit_bytes='):text.index('\n', text.index('declare -A unit_bytes=')) + 1]
                  + 'floor_bytes=$1 pool_size=$2 disk=$3 storage=$4\n' + check + 'echo ok\n')
        need = 50 * GIB * 10 // 9
        for pool, disk, storage, ok in ((need, '40GiB', 'zfs', True), (need - 1, '40GiB', 'zfs', False),
                                        (40 * GIB, '40GiB', 'dir', True), (34 * GIB, '20GiB', 'zfs', True)):
            with self.subTest(pool=pool, disk=disk, storage=storage):
                result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script, 'install', str(pool), 'P', disk,
                                         storage], capture_output=True, text=True)
                self.assertEqual(result.stdout, 'ok\n' if ok else '', result.stderr)
        result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script, 'install', str(40 * GIB), '40GiB', '40GiB',
                                 'zfs'], capture_output=True, text=True)
        self.assertEqual((result.returncode, result.stderr), (1, (
            'coldbox install: a 40GiB ZFS pool cannot start a 40GiB job VM: admission needs one --disk plus a tenth of '
            'the pool free, after about 10 GiB of base images; set COLDBOX_POOL_SIZE to at least 56GiB or a smaller '
            '--disk\n')))

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
        self.assertIn('\n### WSL2\n', (ROOT / 'README.md').read_text())
        text = (ROOT / 'install.sh').read_text()
        start = text.index('if [[ ! -c /dev/kvm ]]; then')
        block = text[start:text.index('\nfi\n', start) + 4].replace('/dev/kvm ]]', '/nonexistent/kvm ]]', 1)
        for virt, message in (('kvm', 'this host is a kvm VM or container, so Coldbox needs nested virtualization '
                                      'from its provider, see docs/vps.md'),
                              ('lxc', 'this host is a lxc VM or container'),
                              ('wsl', 'WSL2 gives Coldbox no KVM, so run it on a Linux machine, see README.md#wsl2'),
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

    def incus_guard(self, candidate, install_incus=0, tty_input=None, zabbly='1:6.0.7-ubuntu24.04-202610301755',
                    update_fails=False):
        text = (ROOT / 'install.sh').read_text()
        start = text.index('incus_crashes() {')
        tmp = Path(tempfile.mkdtemp())
        block = text[start:text.index('\nDEBIAN_FRONTEND=noninteractive apt-get install -y -q "${packages[@]}"', start)]
        head, tail = block.split('\nincus_candidate() {', 1)
        block = head + '\nincus_candidate() {' + tail.replace('/etc/apt/', f'{tmp}/etc/apt/').replace('/etc/coldbox', f'{tmp}/etc/coldbox')
        (tmp / 'etc' / 'apt' / 'sources.list.d').mkdir(parents=True)
        sources = tmp / 'etc' / 'apt' / 'sources.list.d' / 'zabbly-incus-lts-6.0.sources'
        stubs = {'apt-cache': f'if [ -e {sources} ]; then c="{zabbly}"; else c="{candidate}"; fi\n'
                              'printf "incus:\\n  Installed: (none)\\n  Candidate: %s\\n" "$c"\n',
                 'apt-get': f'echo "apt-get $*" >>{tmp}/calls\n' + ('[ "$1" != update ]\n' if update_fails else ''),
                 'curl': f'echo "curl $*" >>{tmp}/calls\necho key >"$2"\n'}
        for name, body in stubs.items():
            (tmp / name).write_text(f'#!/bin/sh\n{body}')
            (tmp / name).chmod(0o755)
        script = (f'die() {{ printf "coldbox install: %s\\n" "$*" >&2; exit 1; }}\ninstall_incus={install_incus}\n'
                  f'VERSION_CODENAME=noble\n{block}\necho "incus=$incus_version"\n')
        args = ['bash', '-euo', 'pipefail', '-c', script]
        env = {'PATH': f'{tmp}:{os.environ["PATH"]}'}
        if tty_input is None:
            result = subprocess.run(args, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
            code, output = result.returncode, result.stdout + result.stderr
        else:
            primary, secondary = os.openpty()
            os.write(primary, tty_input.encode())
            child = subprocess.Popen(args, stdin=secondary, stdout=secondary, stderr=secondary, env=env)
            os.close(secondary)
            output = b''
            with contextlib.suppress(OSError):
                while chunk := os.read(primary, 4096):
                    output += chunk
            os.close(primary)
            code, output = child.wait(), output.decode().replace('\r\n', '\n')
        calls = (tmp / 'calls').read_text() if (tmp / 'calls').exists() else ''
        apt = tmp / 'etc' / 'apt'
        written = sorted(str(f.relative_to(tmp / 'etc')) for f in (tmp / 'etc').rglob('*') if f.is_file())
        return code, output, calls, written, sources

    def refusal(self, version):
        return (f'coldbox install: Incus {version} crashes under parallel jobs and kills every running one '
                '(lxc/incus#2865, fixed in 6.0.6). No apt repository was added. Rerun with --install-incus to add the Zabbly '
                f'Incus 6.0 LTS repo and install it, or do it yourself: {cb.ZABBLY_LTS} && sudo COLDBOX_REBUILD=1 '
                './install.sh\n')

    def test_without_consent_install_refuses_an_incus_with_the_operation_metadata_crash_and_changes_nothing(self):
        for candidate in ('6.0.0-1ubuntu0.3', '6.0.4-2', '6.0.5-8', '1:6.21.0-ubuntu24.04-202601011200'):
            with self.subTest(candidate=candidate):
                code, output, calls, files, _ = self.incus_guard(candidate)
                version = candidate.split(':')[-1].split('-')[0]
                self.assertEqual((code, output, calls, files), (1, self.refusal(version), '', []))

    def test_a_declined_prompt_refuses_and_changes_nothing(self):
        for answer in ('n\n', '\n', 'no\n', 'x\n', 'yuck\n', 'yes please\n'):
            with self.subTest(answer=answer):
                code, output, calls, files, _ = self.incus_guard('6.0.0-1ubuntu0.3', tty_input=answer)
                self.assertEqual((code, calls, files), (1, '', []))
                self.assertIn('and install Incus from it? [y/N] ', output)
                self.assertTrue(output.endswith(self.refusal('6.0.0')), output)

    def test_consent_adds_the_zabbly_lts_repo_installs_from_it_and_rebuilds_the_base(self):
        for consent in ({'install_incus': 1}, {'tty_input': 'y\n'}, {'tty_input': 'Y\n'}, {'tty_input': 'yes\n'}):
            with self.subTest(**consent):
                code, output, calls, files, sources = self.incus_guard('6.0.0-1ubuntu0.3', **consent)
                self.assertEqual(code, 0, output)
                self.assertTrue(output.endswith('coldbox install: adding the Zabbly Incus 6.0 LTS repo for noble\n'
                                                'incus=6.0.7\n'), output)
                self.assertEqual(calls.splitlines(), [
                    'apt-get install -y -q curl ca-certificates',
                    f'curl -fsSLo {sources.parent.parent}/keyrings/zabbly.asc https://pkgs.zabbly.com/key.asc',
                    'apt-get update -q'])
                self.assertEqual(files, ['apt/keyrings/zabbly.asc', 'apt/sources.list.d/zabbly-incus-lts-6.0.sources',
                                         'coldbox/rebuild-base'])
                self.assertEqual(sources.read_text(), 'Types: deb\nURIs: https://pkgs.zabbly.com/incus/lts-6.0\n'
                                 'Suites: noble\nComponents: main\nSigned-By: /etc/apt/keyrings/zabbly.asc\n'.replace(
                                     '/etc/apt/', f'{sources.parent.parent}/'))

    def test_consent_stops_when_the_zabbly_repo_has_no_fixed_incus(self):
        for zabbly, shown in (('6.0.5-1', '6.0.5'), ('(none)', 'none')):
            with self.subTest(zabbly=zabbly):
                code, output, _, _, _ = self.incus_guard('6.0.0-1ubuntu0.3', install_incus=1, zabbly=zabbly)
                self.assertEqual(code, 1)
                self.assertTrue(output.endswith(f'coldbox install: the Zabbly repo offers Incus {shown} for noble, '
                                                'not 6.0.6 or newer; see https://github.com/zabbly/incus\n'), output)

    def test_a_rebuild_owed_by_the_incus_switch_survives_a_failed_run_until_build_base_succeeds(self):
        text = (ROOT / 'install.sh').read_text()
        block = text[text.index('if [[ ${COLDBOX_REBUILD:-} == 1 || -e /etc/coldbox/rebuild-base ]]'):]
        for marked, build_ok, expected in ((True, True, (0, 'built\n', False)), (True, False, (1, '', True)),
                                           (False, True, (0, 'coldbox install: PASS base-dev/warm already exists '
                                                          '(COLDBOX_REBUILD=1 rebuilds it)\n', False))):
            with self.subTest(marked=marked, build_ok=build_ok):
                tmp = Path(tempfile.mkdtemp())
                mark = tmp / 'rebuild-base'
                if marked:
                    mark.touch()
                stub = tmp / 'coldbox'
                stub.write_text('#!/bin/sh\necho built\n' if build_ok else '#!/bin/sh\nexit 1\n')
                stub.chmod(0o755)
                script = 'icp() { true; }\n' + block.replace('/etc/coldbox/rebuild-base', str(mark)).replace(
                    '/usr/local/bin/coldbox', str(stub))
                result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], capture_output=True, text=True,
                                        env={'PATH': os.environ['PATH']})
                self.assertEqual((result.returncode, result.stdout, mark.exists()), expected)

    def test_a_zabbly_repo_apt_cannot_read_is_removed_again_and_named(self):
        code, output, _, files, _ = self.incus_guard('6.0.0-1ubuntu0.3', install_incus=1, update_fails=True)
        self.assertEqual(code, 1)
        self.assertNotIn('apt/sources.list.d/zabbly-incus-lts-6.0.sources', files)
        self.assertTrue(output.endswith('coldbox install: apt-get update failed with the Zabbly repo for noble, so it '
                                        'was removed again; see https://github.com/zabbly/incus\n'), output)

    def test_consent_leaves_an_existing_zabbly_source_alone(self):
        text = (ROOT / 'install.sh').read_text()
        start = text.index('incus_crashes() {')
        tmp = Path(tempfile.mkdtemp())
        sources = tmp / 'zabbly-incus-lts-6.0.sources'
        sources.write_text('operator config\n')
        block = text[start:text.index('\nDEBIAN_FRONTEND=noninteractive apt-get install -y -q "${packages[@]}"', start)]
        head, tail = block.split('\nincus_candidate() {', 1)
        block = head + '\nincus_candidate() {' + tail.replace(
            '/etc/apt/sources.list.d/zabbly-incus-lts-6.0.sources', str(sources))
        (tmp / 'apt-cache').write_text('#!/bin/sh\nprintf "incus:\\n  Candidate: 6.0.0-1\\n"\n')
        (tmp / 'apt-cache').chmod(0o755)
        script = f'die() {{ printf "coldbox install: %s\\n" "$*" >&2; exit 1; }}\ninstall_incus=1\n{block}\n'
        result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], capture_output=True, text=True,
                                env={'PATH': f'{tmp}:{os.environ["PATH"]}'}, stdin=subprocess.DEVNULL)
        self.assertEqual((result.returncode, result.stdout, sources.read_text()), (1, '', 'operator config\n'))
        self.assertEqual(result.stderr, f'coldbox install: Incus 6.0.0 is the apt candidate although {sources} exists; '
                         'check that repo and any apt pinning of incus\n')

    def test_install_continues_on_a_fixed_or_unknown_incus_candidate_without_asking_or_adding_a_repo(self):
        for candidate in ('1:6.0.7-ubuntu26.04-202610301755', '6.0.6-1', '1:6.22.0-debian13-202602011200', '7.0.0-1',
                          '(none)'):
            for consent in ({}, {'install_incus': 1}, {'tty_input': 'y\n'}):
                with self.subTest(candidate=candidate, **consent):
                    code, output, calls, files, _ = self.incus_guard(candidate, **consent)
                    version = candidate.split(':')[-1].split('-')[0].strip('()').replace('none', '')
                    self.assertEqual((code, calls, files), (0, '', []))
                    self.assertTrue(output.endswith(f'incus={version}\n'), output)
                    self.assertNotIn('[y/N]', output)

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


class IntegrationContract(unittest.TestCase):
    DOC = (ROOT / 'docs' / 'integration.md').read_text()
    VALUES = {'TTL': '600', 'NAME': 'cbx-1-a', 'CMD': 'true', 'OWNER/REPO': 'o/r', 'SHA': 'a' * 40, 'DIR': '/r', 'PATH': '.'}
    HANDLERS = ('run_repo', 'run_local', 'run_adhoc', 'vm_cmd', 'run_checks')

    def section(self, heading):
        return self.DOC.split(f'\n## {heading}\n')[1].split('\n## ')[0]

    def consumer_calls(self):
        text = self.section('Calls other tools make')
        lines = [line for block in re.findall(r'```sh\n(.*?)```', text, re.S) for line in block.splitlines()]
        lines += re.findall(r'^run: (.*)$', text, re.M)
        return [[self.VALUES.get(word, word) for word in line.split()] for line in lines]

    def expected_handler(self, argv):
        if argv[0] == 'checks':
            return 'run_checks'
        if argv[0] == 'vm':
            return 'vm_cmd'
        return 'run_repo' if '--repo' in argv else 'run_adhoc' if '--' in argv else 'run_local'

    def test_every_consumer_call_parses_and_reaches_its_handler(self):
        calls = self.consumer_calls()
        self.assertGreaterEqual(len(calls), 10)
        for call in calls:
            with self.subTest(' '.join(call)):
                self.assertEqual(call[0], 'coldbox')
                argv = call[1:]
                mocks = {name: mock.Mock(return_value=7) for name in self.HANDLERS}
                with mock.patch.multiple(cb, incus_group_exec=mock.DEFAULT, **mocks):
                    self.assertEqual(cb.main(argv), 7)
                called = [name for name, handler in mocks.items() if handler.called]
                self.assertEqual(called, [self.expected_handler(argv)])
                if called == ['vm_cmd'] and argv[1] == 'exec':
                    self.assertEqual(mocks['vm_cmd'].call_args.args[0].command, argv[argv.index('--') + 1:])
                if called == ['vm_cmd'] and argv[1] == 'up':
                    self.assertEqual(mocks['vm_cmd'].call_args.args[0].offline, '--offline' in argv)

    def test_the_template_runs_on_coldbox_runner_labels(self):
        labels = re.search(r'^runs-on: \[(.*)\]$', self.section('Calls other tools make'), re.M)[1].split(', ')
        self.assertLessEqual(set(labels), set(cb.LABELS))
        self.assertIn('coldbox', labels)

    def test_exit_codes_match_the_receipt_verdict(self):
        rows = {row.split('|')[1].strip(): row for row in self.section('Exit codes').splitlines()[2:] if row.startswith('|')}
        for verdict in ('pass', 'fail', 'incomplete', 'refused', 'canceled', 'idle'):
            with self.subTest(verdict):
                code = cb.exit_code({'verdict': verdict})
                self.assertIn(f'`{verdict}`', rows[str(code)])

    def test_malformed_arguments_exit_2(self):
        for argv in (['run', '--repo', 'o/r', '--sha', 'ABC'], ['run', '--repo', 'not-a-repo', '--sha', 'a' * 40],
                     ['vm', 'exec'], ['vm', 'up', '--ttl', 'soon']):
            with self.subTest(argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    cb.main(argv)
                self.assertEqual(raised.exception.code, 2)

    def test_the_receipt_schema_is_the_one_receipts_carry(self):
        self.assertIn('`coldbox.receipt/v1`', self.section('Receipt'))
        self.assertIn('# Receipt format: `coldbox.receipt/v1`', (ROOT / 'docs' / 'receipt.md').read_text())


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

    def test_checks_count_the_tests_of_the_built_in_go_command(self):
        bin_dir = Path(tempfile.mkdtemp())
        (bin_dir / 'go').write_text('#!/bin/sh\nprintf -- "--- PASS: TestA (0.00s)\\n--- PASS: TestB (0.00s)\\nok\\n"\n')
        (bin_dir / 'go').chmod(0o755)
        root = repo({'go.mod': 'module z\n', 'z_test.go': 'package z\n', 'coldbox.toml': '[checks]\nlint = false\nverify = false\n'})
        job = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, PATH=f'{bin_dir}:{os.environ["PATH"]}'):
            quiet(cb.run_checks, root, job / 'checks.json')
        self.assertEqual(cb.junit_summary([job / 'junit'])['total'], 2)

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

    def test_junit_command_reads_cold_verify_results_written_during_the_job(self):
        top = repo({'old/.cold/verify-result.json': json.dumps(dict(tests_ran=9))})
        os.utime(top / 'old/.cold/verify-result.json', (time.time() - 60, time.time() - 60))
        marker = top / 'started'
        marker.touch()
        for name, text in (('_work/app/app/.cold/verify-result.json', json.dumps(dict(tests_ran=412, tests_source='junit'))),
                           ('_work/lib/lib/.cold/verify-result.json', json.dumps(dict(tests_ran=5, tests_source='output'))),
                           ('_work/bad/bad/.cold/verify-result.json', '{"tests_ran": "many"}'),
                           ('_work/cut/cut/.cold/verify-result.json', '{"tests_ran"'),
                           ('_work/app/app/.cache/.cold/verify-result.json', json.dumps(dict(tests_ran=8))),
                           ('_work/app/app/node_modules/x/.cold/verify-result.json', json.dumps(dict(tests_ran=7)))):
            (top / name).parent.mkdir(parents=True)
            (top / name).write_text(text)
        (top / 'archived').write_text(f"{top / '_work/lib/lib/.cold/verify-result.json'}\n")
        out = subprocess.run([sys.executable, str(ROOT / 'coldbox'), 'junit', '--newer', str(marker), str(top)],
                             capture_output=True, text=True, check=True).stdout
        self.assertCountEqual(json.loads(out)['cold_verify'], [5, 412, None, None])
        out = subprocess.run([sys.executable, str(ROOT / 'coldbox'), 'junit', '--newer', str(marker),
                              '--archived', str(top / 'archived'), str(top)],
                             capture_output=True, text=True, check=True).stdout
        self.assertCountEqual(json.loads(out)['cold_verify'], [412, None, None])

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

    def test_refusal_kills_the_runner_worker_so_always_steps_cannot_run(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / 'bin').mkdir()
        (tmp / 'bin' / 'pkill').write_text(f'#!/bin/sh\necho "$@" >> {tmp}/pkill.args\n')
        (tmp / 'bin' / 'pkill').chmod(0o755)
        for head, killed in (('me/app', False), ('stranger/app', True)):
            (tmp / 'event.json').write_text(json.dumps({'pull_request': {'head': {'repo': {'full_name': head}},
                                                                         'base': {'repo': {'full_name': 'me/app'}}}}))
            env = {'PATH': f"{tmp / 'bin'}:{os.environ['PATH']}", 'GITHUB_EVENT_PATH': str(tmp / 'event.json'),
                   'GITHUB_EVENT_NAME': 'pull_request', 'GITHUB_REPOSITORY': 'me/app', 'COLDBOX_JOB_DIR': str(tmp)}
            result = subprocess.run([sys.executable, str(ROOT / 'coldbox'), 'job-started'], env=env,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, int(killed), result.stderr)
            self.assertEqual((tmp / 'pkill.args').exists(), killed, head)
        self.assertEqual((tmp / 'pkill.args').read_text(), '-KILL -x Runner.Worker\n')

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


class PublicJobGuard(unittest.TestCase):
    def hook(self, event, actor, head='me/app', payload=None, rerun_by=None, **extra):
        tmp = Path(tempfile.mkdtemp())
        pr = {'head': {'repo': head and {'full_name': head}}, 'base': {'repo': {'full_name': 'me/app'}}}
        (tmp / 'event.json').write_text(json.dumps(payload if payload is not None else {'pull_request': pr}))
        env = {'GITHUB_EVENT_PATH': str(tmp / 'event.json'), 'GITHUB_EVENT_NAME': event, 'GITHUB_REPOSITORY': 'me/app',
               'GITHUB_ACTOR': actor, 'GITHUB_TRIGGERING_ACTOR': rerun_by or actor, 'COLDBOX_JOB_DIR': str(tmp),
               'COLDBOX_PUBLIC_WRITERS': 'me,pal', **extra}
        code, out = quiet(cb.job_started, env)
        return code, json.loads((tmp / 'job.json').read_text())['refused'], out

    def test_owner_push_and_pull_request_run(self):
        self.assertEqual(self.hook('push', 'me', payload={'ref': 'refs/heads/main'})[:2], (0, None))
        self.assertEqual(self.hook('pull_request', 'Pal')[:2], (0, None))
        self.assertEqual(self.hook('merge_group', 'me', payload={'merge_group': {}})[:2], (0, None))

    def test_approved_outside_fork_pull_request_runs(self):
        self.assertEqual(self.hook('pull_request', 'stranger', head='stranger/app')[:2], (0, None))

    def test_outside_jobs_github_does_not_hold_for_approval_are_refused(self):
        for event, head in (('pull_request_target', 'stranger/app'), ('issue_comment', 'me/app'),
                            ('workflow_run', 'stranger/app'), ('pull_request', 'me/app')):
            code, refused, out = self.hook(event, 'stranger', head=head)
            self.assertEqual(code, 1, event)
            self.assertEqual(refused, f'{event} event triggered by stranger, who has no write access')
            self.assertNotIn('--allow-forks', out)

    def test_allow_forks_does_not_open_a_public_repo(self):
        code, refused, _ = self.hook('merge_group', 'stranger', payload={'merge_group': {}}, COLDBOX_ALLOW_FORKS='1')
        self.assertEqual(code, 1)

    def test_a_writer_re_running_counts_and_a_missing_actor_fails_closed(self):
        self.assertEqual(self.hook('issue_comment', 'stranger', rerun_by='me')[0], 0)
        self.assertEqual(self.hook('push', '', payload={'ref': 'refs/heads/main'})[0], 1)


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

    def public(self, policy=cb.APPROVAL, owner='User'):
        answers = {'repos/me/app': {'visibility': 'public', 'full_name': 'me/app', 'owner': {'type': owner}},
                   'repos/me/app/actions/permissions/fork-pr-contributor-approval': {'approval_policy': policy},
                   'repos/me/app/collaborators?permission=push&per_page=100&page=1': [{'login': 'me'},
                                                                                      {'login': 'pal'}]}
        cb.gh = answers.__getitem__

    def test_public_repo_refused_without_flag(self):
        self.public()
        with self.assertRaises(SystemExit) as caught:
            cb.guard('me/app', False)
        self.assertIn('--allow-public', str(caught.exception))
        self.assertEqual(cb.guard('me/app', True), ['me', 'pal'])

    def test_public_repo_without_github_approval_for_all_outside_users_is_refused_with_the_fix(self):
        for policy in ('first_time_contributors', 'first_time_contributors_new_to_github', None):
            self.public(policy)
            with self.assertRaises(SystemExit) as caught:
                cb.guard('me/app', True)
            self.assertIn('gh api -X PUT repos/me/app/actions/permissions/fork-pr-contributor-approval '
                          '-f approval_policy=all_external_contributors', str(caught.exception))

    def test_public_repo_owned_by_an_org_is_refused_even_with_the_flag(self):
        self.public(owner='Organization')
        with self.assertRaisesRegex(SystemExit, 'owned by an organization'):
            cb.guard('me/app', True)

    def test_every_page_of_writers_counts(self):
        self.public()
        pages = {1: [{'login': f'w{n:03}'} for n in range(100)], 2: [{'login': 'last'}]}
        answers = cb.gh
        cb.gh = lambda path: pages[int(path.rsplit('page=', 1)[1])] if '/collaborators?' in path else answers(path)
        writers = cb.guard('me/app', True)
        self.assertEqual((len(writers), writers[0], writers[-1]), (101, 'last', 'w099'))

    def test_private_repo_has_no_writer_list(self):
        cb.gh = lambda path: {'visibility': 'private', 'full_name': 'me/app'}
        self.assertIsNone(cb.guard('me/app', True))

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
        with self.assertRaisesRegex(SystemExit, 'per repository'):
            cb.guard('myorg', True)

    def test_bad_target_rejected(self):
        with self.assertRaises(SystemExit):
            cb.scope('../etc')


class Verdict(unittest.TestCase):
    def receipt(self, **kw):
        return dict(mode='runner', vm={'destroyed': True}, **kw)

    def test_cases(self):
        self.assertEqual(cb.verdict(dict(mode='local', vm={'destroyed': True}, checks_verdict='pass', exit=0,
                                         tests_ran=3)), 'pass')
        self.assertEqual(cb.verdict(dict(mode='local', vm={'destroyed': True}, checks_verdict='pass', exit=137,
                                         error='TTL reached')), 'incomplete')
        self.assertEqual(cb.verdict(dict(mode='local', vm={'destroyed': True}, checks_verdict='pass')), 'incomplete')
        self.assertEqual(cb.verdict(self.receipt(checks_verdict='pass')), 'incomplete')
        self.assertEqual(cb.verdict(self.receipt(checks_verdict='fail')), 'fail')
        self.assertEqual(cb.verdict(dict(mode='local', vm={'destroyed': False}, checks_verdict='pass')), 'incomplete')
        self.assertEqual(cb.verdict(self.receipt(github={'refused': 'fork pull request'})), 'refused')
        self.assertEqual(cb.verdict(self.receipt(error='TTL reached')), 'idle')
        self.assertEqual(cb.verdict(self.receipt(github={'repo': 'me/app'})), 'incomplete')
        self.assertEqual(cb.verdict(self.receipt(github={'repo': 'me/app'}, job_result='Succeeded', tests_ran=1)), 'pass')
        self.assertEqual(cb.verdict(self.receipt(checks_verdict='pass', job_result='Failed')), 'fail')
        self.assertEqual(cb.verdict(self.receipt(checks_verdict='pass', job_result='Canceled')), 'incomplete')
        self.assertEqual(cb.verdict(self.receipt(github={'refused': 'fork pull request'}, job_result='Failed')),
                         'refused')

    def test_every_fail_and_incomplete_names_a_cause(self):
        ready = {'destroyed': True, 'ready_seconds': 1}
        cases = [
            (dict(mode='local', vm=ready, checks_verdict='fail',
                  checks=[dict(check='lint', status='pass'), dict(check='test', status='fail')]), 'fail', 'test'),
            (dict(mode='local', vm=ready, checks_verdict='fail', checks=[]), 'fail', 'checks'),
            (dict(mode='adhoc', vm=ready, exit=2), 'fail', 'job'),
            (dict(mode='runner', vm=ready, github={'repo': 'me/app'}, job_result='Failed'), 'fail', 'job'),
            (dict(mode='local', vm={'destroyed': True}, error='incus start failed: '), 'incomplete', 'vm_not_ready'),
            (dict(mode='local', vm={'destroyed': True}, error='stopped'), 'incomplete', 'stopped'),
            (dict(mode='local', vm=ready, error='TTL reached'), 'incomplete', 'ttl'),
            (dict(mode='local', vm=ready, error='incus file push failed: x'), 'incomplete', 'infra_error'),
            (dict(mode='local', vm={'destroyed': False, 'ready_seconds': 1}, checks_verdict='pass', exit=0,
                  tests_ran=3), 'incomplete', 'vm_not_destroyed'),
            (dict(mode='local', vm=ready, checks_verdict='pass', exit=0, tests_ran=0), 'incomplete', 'no_tests'),
            (dict(mode='runner', vm=ready, github={'repo': 'me/app'}), 'incomplete', 'no_result'),
        ]
        for receipt, expected, cause in cases:
            with self.subTest(receipt):
                cb.settle(receipt)
                self.assertEqual((receipt['verdict'], receipt['cause']), (expected, cause))
        reaped = dict(mode='local', vm={'destroyed': False, 'ready_seconds': 1}, checks_verdict='pass', exit=0,
                      tests_ran=0)
        cb.settle(reaped)
        self.assertEqual((reaped['cause'], 'reason' in reaped), ('vm_not_destroyed', False))
        reaped['vm']['destroyed'] = True
        cb.settle(reaped)
        self.assertEqual((reaped['cause'], reaped['reason']), ('no_tests', cb.NO_TESTS))
        passed = dict(mode='adhoc', vm=ready, exit=0, cause='job')
        cb.settle(passed)
        self.assertEqual(passed['verdict'], 'pass')
        self.assertNotIn('cause', passed)

    def test_a_job_where_no_tests_ran_is_never_a_pass(self):
        local = dict(mode='local', vm={'destroyed': True}, checks_verdict='pass', exit=0)
        runner = self.receipt(github={'repo': 'me/app'}, job_result='Succeeded')
        for receipt in (local, runner):
            for count in ({}, dict(tests_ran=0)):
                with self.subTest(mode=receipt['mode'], **count):
                    self.assertEqual(cb.verdict(dict(receipt, **count)), 'incomplete')

    def run_job(self, junit, cold_verify, **result):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addCleanup(cb.CLEANUP.clear)
        summary = json.dumps(dict(cb.junit_summary([Path(folder.name) / 'none']), total=junit or 0, skipped=0,
                                  files=[] if junit is None else ['report.xml'], cold_verify=cold_verify))

        def run(command, *args, **kwargs):
            return subprocess.CompletedProcess(command, 0, summary if 'junit' in command else '', '')

        def runner(command, out, data=None):
            out.write(b'Running job: verify\nJob verify completed with result: Succeeded\n')
            out.flush()
            return 0

        github = dict(repo='me/app', sha='f' * 40, refused=None)
        with mock.patch.multiple(cb, boot=mock.DEFAULT, ic=mock.DEFAULT, destroy=lambda name: True,
                                 runner_admission=lambda *a: dict(host='h'), record_queue_wait=mock.DEFAULT,
                                 pull=lambda name, path: github if path.endswith('job.json') else None,
                                 tee=runner), \
                mock.patch.object(cb.subprocess, 'run', run):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                cb.job('runner', 'cbx-1-test', 60, ['run.sh'], repo='me/app', receipts=folder.name)
        return json.loads((Path(folder.name) / 'cbx-1-test.json').read_text())

    def test_a_succeeded_job_with_no_tests_is_incomplete_and_says_why(self):
        receipt = self.run_job(None, [])
        self.assertEqual((receipt['verdict'], receipt['tests_ran'], receipt['tests_source'], receipt['reason']),
                         ('incomplete', 0, None, cb.NO_TESTS))
        self.assertEqual(self.run_job(None, [0])['verdict'], 'incomplete')

    def test_an_unreadable_cold_verify_result_is_incomplete_not_zero(self):
        receipt = self.run_job(None, [412, None])
        self.assertEqual(receipt['verdict'], 'incomplete')
        self.assertNotIn('tests_ran', receipt)
        self.assertEqual(receipt['reason'], 'the test count could not be read')

    def test_a_junit_report_wins_over_cold_verify_even_with_zero_tests(self):
        receipt = self.run_job(0, [412])
        self.assertEqual((receipt['verdict'], receipt['tests_ran'], receipt['tests_source']), ('incomplete', 0, 'junit'))

    def test_unittest_and_pytest_style_counts_reach_the_receipt(self):
        receipt = self.run_job(None, [412])
        self.assertEqual((receipt['verdict'], receipt['tests_ran'], receipt['tests_source']), ('pass', 412, 'cold-verify'))
        self.assertNotIn('reason', receipt)
        receipt = self.run_job(5, [])
        self.assertEqual((receipt['verdict'], receipt['tests_ran'], receipt['tests_source']), ('pass', 5, 'junit'))

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
        self.assertIsNone(cb.ACTIVE_LOG)
        return json.loads((receipts / 'cbx-1-test.json').read_text())

    def test_a_service_stop_reads_the_runner_log_to_tell_idle_from_interrupted(self):
        idle = self.stopped_runner(b'Current runner version: 2.338.2', b'2026-11-06 18:52:13Z: Listening for Jobs')
        self.assertEqual((idle['error'], idle['verdict'], 'job_name' in idle), ('stopped', 'idle', False))
        busy = self.stopped_runner(b'2026-11-08 16:52:19Z: Listening for Jobs',
                                   b'2026-11-08 16:52:21Z: Running job: verify')
        self.assertEqual((busy['error'], busy['verdict'], busy['job_name']), ('stopped', 'incomplete', 'verify'))

    def test_forced_stop_preserves_github_run_identity(self):
        github = dict(repo='me/app', sha='abc', run_id=123)
        with mock.patch.object(cb, 'pull', return_value=github):
            receipt = self.stopped_runner(b'Running job: verify')
        self.assertEqual(receipt['github']['run_id'], 123)
        self.assertEqual(receipt['verdict'], 'incomplete')

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


def live_worker(test, state, name):
    process = subprocess.Popen(['sleep', '600'])
    test.addCleanup(process.wait)
    test.addCleanup(process.kill)
    (state / 'workers').mkdir(parents=True, exist_ok=True)
    (state / 'workers' / str(process.pid)).write_text(json.dumps(['me/app', cb.process_start(process.pid), name, 'base']))


def unfinished_receipt(name, error='stopped'):
    return dict(schema='coldbox.receipt/v1', id=name, mode='runner', repo='me/app', verdict='incomplete',
                started_at='2026-11-11T12:00:00Z', **({'error': error} if error else {}),
                vm=dict(name=name, ttl_seconds=21600, destroyed=False, destroyed_at='2026-11-11T12:10:00Z'))


class Reap(unittest.TestCase):
    def test_host_vms_lists_names_only_so_a_loaded_host_answers_fast(self):
        calls = []
        rows = [{'name': 'cbx-1-a'}, {'name': 'base-dev'}]
        with mock.patch.object(cb, 'ic', lambda *a: calls.append(a) or json.dumps(rows)):
            self.assertEqual(cb.host_vms(), {'cbx-1-a'})
        self.assertEqual(calls, [('list', '--format', 'json', '--columns', 'n')])

    def reap(self, ip_script, project='coldbox', extra=(), delete_fails=None, expected_deletes=(), orphans=(),
             receipt_files=None, build_running=False, unreadable=None, rollback=None, **env):
        tmp = Path(tempfile.mkdtemp())
        now = int(time.time())
        receipts = tmp / 'state' / 'receipts'
        receipts.mkdir(parents=True)
        for name in ('cbx-old.json', 'cbx-old.log', 'cbx-new.json'):
            (receipts / name).write_text('{}')
        for name in ('cbx-old', 'cbx-new', 'cbx-gone'):
            (receipts / f'{name}.artifacts' / 'out').mkdir(parents=True)
            (receipts / f'{name}.artifacts' / 'out' / 'report.txt').write_text('saved')
        for name in ('cbx-old.json', 'cbx-old.log', 'cbx-old.artifacts', 'cbx-new.artifacts', 'cbx-gone.artifacts'):
            os.utime(receipts / name, (now - 31 * 86400, now - 31 * 86400))
        for name, receipt in (receipt_files or {}).items():
            cb.write_json(receipts / f'{name}.json', receipt)
        if rollback:
            cb.write_json(tmp / 'state' / 'base-rollback.json', rollback)
        rows = [{'name': 'cbx-1-a', 'config': {'user.coldbox.expires': str(now - 5)}},
                {'name': 'cbx-2-b', 'config': {'user.coldbox.expires': str(now + 600)}},
                {'name': 'base-dev', 'config': {}},
                {'name': 'other', 'config': {'user.coldbox.expires': str(now - 5)}}, *extra]
        for row in rows:
            if row['name'].startswith('cbx-') and row['name'] not in orphans:
                live_worker(self, tmp / 'state', row['name'])
        if unreadable:
            live_worker(self, tmp / 'state', unreadable)
            for record in (tmp / 'state' / 'workers').iterdir():
                if unreadable in record.read_text():
                    target, start, name, kind = json.loads(record.read_text())
                    record.write_text(json.dumps(dict(target=target, start=start, name=name, kind=kind)))
        (tmp / 'rows.json').write_text(json.dumps(rows))
        fake = tmp / 'incus'
        fake.write_text('#!/bin/sh\n'
                        f'echo "$@" >> {tmp}/calls\n'
                        'case "$*" in\n'
                        f'  *list*) python3 -c "import json, pathlib, sys; gone = pathlib.Path(sys.argv[2]); '
                        f'gone = gone.read_text().split() if gone.exists() else []; '
                        f'print(json.dumps([r for r in json.load(open(sys.argv[1])) if r[\'name\'] not in gone]))" '
                        f'{tmp}/rows.json {tmp}/deleted ;;\n'
                        f'  *delete*{delete_fails}*) exit 1 ;;\n'
                        f'  *delete*) echo "$5" >> {tmp}/deleted ;;\n'
                        f'  *info*{delete_fails}*) exit 0 ;;\n'
                        '  *info*) echo "Error: Failed to fetch instance: Instance not found" >&2; exit 1 ;;\n'
                        '  *acl*) cat > /dev/null ;;\n'
                        'esac\n')
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        (tmp / 'ip').write_text(ip_script)
        (tmp / 'ip').chmod(0o755)
        env = dict(os.environ, PATH=f'{tmp}:{os.environ["PATH"]}', COLDBOX_STATE=str(tmp / 'state'), **env)
        with open(tmp / 'state' / 'build-base.lock', 'w') as build:
            (tmp / 'state' / 'build-base.lock').chmod(0o444)
            if build_running:
                fcntl.flock(build, fcntl.LOCK_EX)
            result = subprocess.run([sys.executable, str(ROOT / 'coldbox'), 'reap'], env=env, capture_output=True,
                                    text=True)
        calls = (tmp / 'calls').read_text().splitlines()
        deletes = [line for line in calls if ' delete ' in line]
        self.assertEqual(deletes, [f'--force-local --project {project} delete {name} --force'
                                   for name in ('cbx-1-a', *expected_deletes)])
        self.assertEqual(sorted(p.name for p in receipts.iterdir()),
                         sorted(['cbx-new.json', 'cbx-new.artifacts', *(f'{name}.json' for name in receipt_files or {})]))
        self.assertEqual((receipts / 'cbx-new.artifacts' / 'out' / 'report.txt').read_text(), 'saved')
        return result, calls, receipts

    def test_a_smoke_vm_left_by_an_interrupted_build_is_deleted_but_not_while_a_build_runs(self):
        smoke = [{'name': 'base-smoke', 'config': {'user.coldbox.expires': '0'}}]
        result, _, _ = self.reap('#!/bin/sh\necho "[]"\n', extra=smoke, expected_deletes=['base-smoke'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('base-smoke left by an interrupted build-base; deleted', result.stdout)
        result, _, _ = self.reap('#!/bin/sh\necho "[]"\n', extra=smoke, build_running=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('base-smoke', result.stdout)
        self.assertIn('coldbox reap: skipped base-smoke: build-base lock not free', result.stderr)

    def test_bases_get_a_lifecycle_and_only_managed_expired_ones_are_deleted(self):
        now = int(time.time())
        mark = {'user.coldbox.managed': 'true'}
        result, calls, _ = self.reap('#!/bin/sh\necho "[]"\n', expected_deletes=['base-old'], extra=[
            {'name': 'base-prev', 'config': {**mark, 'user.coldbox.expires': 'never'}},
            {'name': 'base-old', 'config': {**mark, 'user.coldbox.expires': str(now - 5)}},
            {'name': 'base-rollback', 'config': {**mark, 'user.coldbox.expires': str(now - 5)}},
            {'name': 'base-new', 'config': {**mark, 'user.coldbox.expires': str(now + 600)}},
            {'name': 'base-dev-pre62', 'config': {'user.coldbox.expires': str(now - 5)}}])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('coldbox reap: base-old expired; deleted', result.stdout)
        self.assertIn('--force-local --project coldbox config set base-dev user.coldbox.managed=true '
                      'user.coldbox.expires=never', calls)
        retire = [line.split('=')[-1] for line in calls if line.startswith(
            '--force-local --project coldbox config set base-prev user.coldbox.managed=true user.coldbox.expires=')]
        self.assertEqual(len(retire), 1, calls)
        self.assertAlmostEqual(int(retire[0]), now + cb.ROLLBACK_WINDOW, delta=60)
        self.assertEqual(len([line for line in calls if line.endswith(' list --format json')]), 2, calls)
        result, calls, _ = self.reap('#!/bin/sh\necho "[]"\n', extra=[
            {'name': 'base-prev', 'config': {'user.coldbox.expires': 'never'}}])
        self.assertFalse([line for line in calls if ' config set base-prev ' in line], calls)
        result, calls, _ = self.reap('#!/bin/sh\necho "[]"\n', rollback={'mode': 'swap'}, extra=[
            {'name': 'base-prev', 'config': {**mark, 'user.coldbox.expires': str(now - 5)}}])
        self.assertEqual(result.returncode, 0, result.stderr)
        result, calls, _ = self.reap('#!/bin/sh\necho "[]"\n', build_running=True, extra=[
            {'name': 'base-old', 'config': {**mark, 'user.coldbox.expires': str(now - 5)}}])
        self.assertIn('coldbox reap: skipped bases: build-base lock not free', result.stderr)
        self.assertFalse([line for line in calls if ' config set ' in line], calls)

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
        result, calls, _ = self.reap('#!/bin/sh\necho "[]"\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--force-local network acl edit coldbox-isolated', calls)

    def test_follows_the_installed_names(self):
        result, calls, _ = self.reap('#!/bin/sh\necho "[]"\n', 'cbx', COLDBOX_INCUS_ENV=str(custom_names()))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--force-local network acl edit cbx-isolated', calls)
        self.assertIn('--force-local --project cbx list --format json', calls)

    def test_an_instance_without_a_readable_expiry_is_skipped_and_the_rest_are_reaped(self):
        now = int(time.time())
        names = [f'cbx-{now}-{suffix}' for suffix in ('null', 'none', 'bad')]
        result, calls, _ = self.reap('#!/bin/sh\necho "[]"\n', extra=[
            {'name': names[0], 'config': None},
            {'name': names[1]},
            {'name': names[2], 'config': {'user.coldbox.expires': 'soon'}}])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('cbx-1-a deleted', result.stdout)
        for name in names:
            self.assertIn(f'coldbox reap: skipped {name}: no readable user.coldbox.expires', result.stderr)

    def test_deletes_an_old_instance_without_a_readable_expiry(self):
        name = f'cbx-{int(time.time()) - 2 * 86400}-aa'
        result, calls, _ = self.reap('#!/bin/sh\necho "[]"\n',
                                  extra=[{'name': name, 'config': None, 'status': 'Error'}],
                                  expected_deletes=[name])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'{name} deleted', result.stdout)

    def test_reports_failure_to_delete_an_old_instance_without_a_readable_expiry(self):
        name = f'cbx-{int(time.time()) - 2 * 86400}-aa'
        result, calls, _ = self.reap('#!/bin/sh\necho "[]"\n',
                                  extra=[{'name': name, 'config': None, 'status': 'Error'}],
                                  delete_fails=name, expected_deletes=[name] * 3)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(f'{name} FAILED to delete', result.stdout)

    def test_skips_a_young_instance_without_a_readable_expiry(self):
        name = f'cbx-{int(time.time()) - 60}-bb'
        result, calls, _ = self.reap('#!/bin/sh\necho "[]"\n', extra=[{'name': name, 'config': None}])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'coldbox reap: skipped {name}: no readable user.coldbox.expires', result.stderr)

    def test_skips_an_instance_without_a_readable_creation_time(self):
        for name in ('cbx-unknown-aa', 'cbx-'):
            with self.subTest(name=name):
                result, calls, _ = self.reap('#!/bin/sh\necho "[]"\n', extra=[{'name': name, 'config': None}])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'coldbox reap: skipped {name}: no readable user.coldbox.expires', result.stderr)

    def test_after_a_reboot_a_stopped_vm_without_a_live_worker_is_deleted_before_its_ttl_and_its_receipt_finalized(self):
        now = int(time.time())
        orphan, adhoc, gone, busy, killed = (f'cbx-{now - 7000}-{suffix}' for suffix in ('dead', 'adhoc', 'gone', 'busy', 'kill'))
        result, calls, receipts = self.reap(
            '#!/bin/sh\necho "[]"\n', orphans=[orphan, adhoc], expected_deletes=[orphan], extra=[
                {'name': orphan, 'status': 'Stopped', 'config': {'user.coldbox.expires': str(now + 14600)}},
                {'name': adhoc, 'status': 'Running',
                 'config': {'user.coldbox.expires': str(now + 600), 'user.coldbox.adhoc': 'true'}},
                {'name': busy, 'status': 'Running', 'config': {'user.coldbox.expires': str(now + 14600)}}],
            receipt_files={orphan: unfinished_receipt(orphan), gone: unfinished_receipt(gone),
                           busy: unfinished_receipt(busy), killed: unfinished_receipt(killed, error=None)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'{orphan} has no live worker; deleted', result.stdout)
        for name in (orphan, gone):
            receipt = json.loads((receipts / f'{name}.json').read_text())
            self.assertEqual((receipt['vm']['destroyed'], receipt['verdict'], receipt['reconciled_by']),
                             (True, 'idle', 'coldbox reap'))
            self.assertNotEqual(receipt['vm']['destroyed_at'], '2026-11-11T12:10:00Z')
        receipt = json.loads((receipts / f'{killed}.json').read_text())
        self.assertEqual((receipt['vm']['destroyed'], receipt['verdict'], receipt['reconciled_by']),
                         (True, 'incomplete', 'coldbox reap'))
        busy_receipt = json.loads((receipts / f'{busy}.json').read_text())
        self.assertEqual((busy_receipt['vm']['destroyed'], busy_receipt['verdict']), (False, 'incomplete'))
        self.assertNotIn('reconciled_by', busy_receipt)

    def test_a_worker_less_vm_stays_while_a_live_workers_record_cannot_be_read(self):
        vm = f'cbx-{int(time.time())}-newer'
        result, _, _ = self.reap('#!/bin/sh\necho "[]"\n', orphans=[vm], unreadable=vm,
                                 extra=[{'name': vm, 'status': 'Running', 'config': None}])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('kept worker-less VMs: cannot read the records of live worker(s)', result.stderr)
        self.assertNotIn(f'{vm} has no live worker', result.stdout)

    def test_a_receipt_whose_orphan_vm_could_not_be_deleted_stays_unfinished(self):
        orphan = f'cbx-{int(time.time())}-stuck'
        result, calls, receipts = self.reap(
            '#!/bin/sh\necho "[]"\n', orphans=[orphan], delete_fails=orphan, expected_deletes=[orphan] * 3,
            extra=[{'name': orphan, 'status': 'Stopped', 'config': None}],
            receipt_files={orphan: unfinished_receipt(orphan)})
        self.assertEqual(result.returncode, 1)
        self.assertIn(f'{orphan} has no live worker; FAILED to delete', result.stdout)
        receipt = json.loads((receipts / f'{orphan}.json').read_text())
        self.assertEqual((receipt['vm']['destroyed'], receipt['verdict']), (False, 'incomplete'))

    def test_still_cleans_up_when_acl_refresh_fails(self):
        result, calls, _ = self.reap('#!/bin/sh\necho "ip broke" >&2; exit 1\n')
        self.assertEqual(result.returncode, 1)
        self.assertIn('FAILED to refresh network ACL', result.stderr)


class BuildBase(unittest.TestCase):
    def test_waits_for_a_running_build(self):
        import threading
        state = Path(tempfile.mkdtemp())
        calls = []
        with mock.patch.multiple(cb, STATE=state, GUEST_FILES=state / 'no-guest-files', fetch_json=lambda url: None,
                                 runner_release=lambda _: ('2.340.0', 'u', 'd'), wait_ready=lambda *a, **k: None,
                                 seed_cloud_init_on_create_only=lambda name: None, smoke_base=lambda: None,
                                 destroy=lambda name: calls.append(('destroy', name)) or True,
                                 ic=lambda *a, **k: calls.append(a)), \
                mock.patch.object(cb.os, 'geteuid', return_value=0), \
                mock.patch.object(cb.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout='')), \
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


    def test_records_guest_packages_before_stopping_the_base(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.multiple(
                cb, STATE=Path(tmp), fetch_json=lambda url: None,
                runner_release=lambda _: ('2.340.0', 'url', 'digest'), wait_ready=mock.DEFAULT,
                push_guest_files=mock.DEFAULT, seed_cloud_init_on_create_only=mock.DEFAULT,
                destroy=mock.DEFAULT), mock.patch.object(cb.subprocess, 'run') as run:
            (Path(tmp) / 'base-packages.txt').write_text('gh 1\n')
            run.return_value = subprocess.CompletedProcess([], 0, stdout='gh 2\n')
            def ic(*args):
                if args == ('stop', 'base-new'):
                    self.assertEqual((Path(tmp) / 'base-packages.txt').read_text(), 'gh 2\n')
                    self.assertEqual((Path(tmp) / 'base-packages.prev.txt').read_text(), 'gh 1\n')
            with mock.patch.object(cb, 'ic', side_effect=ic):
                self.assertEqual(cb.build_new_base(), '2.340.0')
            self.assertIn(mock.call(cb.ic_exec('base-new', 'dpkg-query', '-W', '-f',
                                              '${Package} ${Version}\\n', user=None),
                                    check=True, capture_output=True, text=True), run.call_args_list)

    def test_failed_build_or_swap_restores_package_manifests(self):
        for failure in ('build', 'swap'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.object(cb, 'STATE', Path(tmp)), \
                    mock.patch.object(cb.os, 'geteuid', return_value=0), \
                    mock.patch.object(cb, 'ic', return_value='2.338.1'), \
                    mock.patch.object(cb, 'smoke_base'):
                current, previous = Path(tmp) / 'base-packages.txt', Path(tmp) / 'base-packages.prev.txt'
                current.write_text('gh 2\n')
                previous.write_text('gh 1\n')
                def build():
                    current.replace(previous)
                    current.write_text('gh 3\n')
                    if failure == 'build':
                        raise RuntimeError('build failed')
                    return '2.338.2'
                with mock.patch.object(cb, 'build_new_base', side_effect=build), \
                        mock.patch.object(cb, 'swap_base', side_effect=RuntimeError('swap failed')):
                    with self.assertRaises(RuntimeError):
                        cb.build_base(argparse.Namespace(rollback=False))
                self.assertEqual(current.read_text(), 'gh 2\n')
                self.assertEqual(previous.read_text(), 'gh 1\n')

    def test_a_candidate_base_that_cannot_run_a_job_is_not_swapped_in_and_status_fails(self):
        state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        instances = {'base-dev': 'old'}
        booted = []

        calls = []

        def ic(*args, **kwargs):
            calls.append(args)
            if args[0] == 'move':
                instances[args[2]] = instances.pop(args[1])
            return ''

        def build():
            instances['base-new'] = 'candidate'
            (state / 'base-packages.txt').write_text('gh 2\n')
            return '2.340.0'

        def boot(name, expires, offline, *config, base=cb.BASE):
            booted.append((name, base, offline))
            instances[name] = 'clone'

        def build_base(exit_code):
            job = subprocess.CompletedProcess([], exit_code, stdout='', stderr='runuser: user runner does not exist\n')
            with mock.patch.multiple(cb, STATE=state, ic=ic, build_new_base=build, boot=boot,
                                     destroy=lambda name: (instances.pop(name, None), True)[1]), \
                    mock.patch.object(cb.os, 'geteuid', return_value=0), \
                    mock.patch.object(cb.subprocess, 'run', return_value=job) as run:
                result = quiet(cb.build_base, argparse.Namespace(rollback=False))
            self.assertIn(mock.call(cb.ic_exec('base-smoke', 'true', cwd=f'/home/{cb.RUNNER}', timeout=60),
                                    capture_output=True, text=True), run.call_args_list)
            return result

        with self.assertRaises(SystemExit) as failed:
            build_base(1)
        self.assertEqual(booted, [('base-smoke', 'base-new/warm', False)])
        self.assertIn('FAIL the new base did not run a job, kept the current base', str(failed.exception))
        self.assertEqual(instances, {'base-dev': 'old'})
        self.assertFalse([c for c in calls if c[:2] == ('config', 'set')], calls)
        code, output = Status().healthy_status(state, [])
        self.assertEqual(code, 1, output)
        self.assertRegex(output, r'FAIL  base build at \S+ \(actions-runner 2\.340\.0\) failed its smoke job, so it was '
                         r'not swapped in: `true` exited 1 in a VM cloned from base-new/warm: '
                         r'runuser: user runner does not exist\n      fix: fix the cause, then sudo coldbox build-base\n')
        self.assertEqual(output.count('FAIL'), 1, output)

        self.assertEqual(build_base(0)[0], 0)
        self.assertEqual(instances, {'base-dev': 'candidate', 'base-prev': 'old'})
        retire = [c for c in calls if c[:3] == ('config', 'set', 'base-dev')]
        self.assertEqual(len(retire), 1, calls)
        self.assertLess(calls.index(retire[0]), calls.index(('move', 'base-dev', 'base-prev')))
        self.assertAlmostEqual(int(retire[0][4].split('=')[1]), time.time() + cb.ROLLBACK_WINDOW, delta=60)
        self.assertFalse((state / 'base-smoke-failed.json').exists())
        self.assertEqual(Status().healthy_status(state, [])[0], 0)

    def test_sigterm_during_build_base_exits_through_its_cleanup(self):
        self.addCleanup(signal.signal, signal.SIGTERM, signal.getsignal(signal.SIGTERM))
        cleaned = []

        def build(args):
            try:
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(5)
            finally:
                cleaned.append(True)

        with mock.patch.object(cb, 'build_base', build), self.assertRaises(SystemExit) as stopped:
            cb.main(['build-base'])
        self.assertEqual((stopped.exception.code, cleaned), (143, [True]))

    def test_a_smoke_vm_that_cannot_be_deleted_fails_the_build_instead_of_swapping(self):
        state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with mock.patch.multiple(cb, STATE=state, build_new_base=lambda: '2.340.0', boot=mock.DEFAULT,
                                 swap_base=mock.DEFAULT, ic=lambda *a, **k: '',
                                 destroy=lambda name: name != 'base-smoke'), \
                mock.patch.object(cb.os, 'geteuid', return_value=0), \
                mock.patch.object(cb.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stderr='')):
            with self.assertRaises(SystemExit) as failed:
                quiet(cb.build_base, argparse.Namespace(rollback=False))
            cb.swap_base.assert_not_called()
        self.assertIn('could not delete the smoke VM base-smoke', str(failed.exception))
        self.assertIn('could not delete the smoke VM base-smoke',
                      json.loads((state / 'base-smoke-failed.json').read_text())['reason'])

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
        self.assertEqual(seen, [('base-new', ('sh', '-c', cb.GUEST_OVERLAY_DIRS, 'sh', '/', cb.OVERLAY_DIRS_RECORD), None),
                                ('base-new', ('tar', '-C', '/', '-x', '--no-overwrite-dir', '--keep-directory-symlink'), None)])
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

    def test_overlay_directory_modes_reach_a_cloned_vm_and_base_directories_keep_theirs(self):
        tmp = Path(tempfile.mkdtemp())
        files = tmp / 'guest'
        for path in ('opt/tools', 'lib/tools', 'etc'):
            (files / path).mkdir(parents=True)
        (files / 'opt/tools/run').write_text('#!/bin/sh\n')
        (files / 'etc/marker').write_text('x\n')
        (files / 'link').symlink_to('opt')
        for path in ('opt', 'opt/tools', 'lib', 'lib/tools', 'etc'):
            (files / path).chmod(0o700)
        base = tmp / 'base'
        for path in ('usr/lib', 'opt', 'etc'):
            (base / path).mkdir(parents=True)
        (base / 'lib').symlink_to('usr/lib')
        (base / 'opt').chmod(0o711)
        (base / 'etc').chmod(0o755)

        def push(vm):
            with mock.patch.multiple(cb, GUEST_FILES=files,
                                     ic_exec=lambda name, *command, user: [str(vm) if c == '/' else c for c in command]):
                cb.push_guest_files('cbx-job')

        def mode(path):
            return stat.S_IMODE(os.stat(path).st_mode)
        push(base)
        self.assertEqual((mode(base / 'opt/tools'), mode(base / 'usr/lib/tools')), (0o700, 0o700))
        for path in ('opt/tools', 'lib/tools'):
            (files / path).chmod(0o755)
        vm = tmp / 'vm'
        shutil.copytree(base, vm, symlinks=True)
        push(vm)
        self.assertEqual((mode(vm / 'opt/tools'), mode(vm / 'usr/lib/tools')), (0o755, 0o755))
        self.assertEqual((mode(vm / 'opt'), mode(vm / 'etc')), (0o711, 0o755))
        self.assertTrue((vm / 'lib').is_symlink())
        self.assertEqual((vm / 'opt/tools/run').read_text(), '#!/bin/sh\n')

    def test_an_overlay_directory_loses_set_group_id_when_the_overlay_drops_it(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / 'guest/opt/shared').mkdir(parents=True)
        (tmp / 'guest/opt/shared').chmod(0o2755)
        vm = tmp / 'vm'
        vm.mkdir()
        with mock.patch.multiple(cb, GUEST_FILES=tmp / 'guest',
                                 ic_exec=lambda name, *command, user: [str(vm) if c == '/' else c for c in command]):
            cb.push_guest_files('cbx-job')
            (vm / 'opt/shared').chmod(0o2755)
            (tmp / 'guest/opt/shared').chmod(0o755)
            cb.push_guest_files('cbx-job')
        self.assertEqual(stat.S_IMODE(os.stat(vm / 'opt/shared').st_mode), 0o755)

    def test_a_non_utf8_overlay_directory_name_keeps_its_bytes_and_mode(self):
        tmp = Path(tempfile.mkdtemp())
        name = os.fsdecode(b'caf\xe9')
        (tmp / 'guest/opt' / name).mkdir(parents=True)
        vm = tmp / 'vm'
        vm.mkdir()
        with mock.patch.multiple(cb, GUEST_FILES=tmp / 'guest',
                                 ic_exec=lambda name, *command, user: [str(vm) if c == '/' else c for c in command]):
            cb.push_guest_files('cbx-job')
            (tmp / 'guest/opt' / name).chmod(0o700)
            cb.push_guest_files('cbx-job')
        self.assertEqual(stat.S_IMODE(os.stat(vm / 'opt' / name).st_mode), 0o700)

    def test_guest_files_that_claim_the_overlay_record_path_are_refused(self):
        for make in (Path.mkdir, lambda path: path.write_text('x\n')):
            files = Path(tempfile.mkdtemp()) / 'guest'
            (files / 'var/lib/coldbox').mkdir(parents=True)
            make(files / 'var/lib/coldbox/overlay-dirs')
            with self.subTest(make=make), mock.patch.multiple(cb, GUEST_FILES=files), \
                    mock.patch.object(cb.subprocess, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'var/lib/coldbox/overlay-dirs'):
                    cb.push_guest_files('cbx-job')
                run.assert_not_called()

    def test_a_guest_directory_name_with_a_newline_is_refused(self):
        files = Path(tempfile.mkdtemp()) / 'guest'
        (files / 'opt/bad\nname').mkdir(parents=True)
        with mock.patch.multiple(cb, GUEST_FILES=files), mock.patch.object(cb.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'newline'):
                cb.push_guest_files('cbx-job')
        run.assert_not_called()

    def push_into_fake_vm(self, overlay):
        tmp = Path(tempfile.mkdtemp())
        for path, text in overlay.items():
            (tmp / 'guest' / path).parent.mkdir(parents=True, exist_ok=True)
            if isinstance(text, Path):
                (tmp / 'guest' / path).symlink_to(text)
            else:
                (tmp / 'guest' / path).write_text(text)
        vm = tmp / 'vm'
        (vm / 'usr/local/bin').mkdir(parents=True)

        def in_vm(name, *command, user):
            return [str(vm) if c == '/' else str(vm) + c if c.startswith('/usr/') else c for c in command]
        with mock.patch.multiple(cb, GUEST_FILES=tmp / 'guest', ic_exec=in_vm):
            cb.push_guest_files('base-new')
        return vm / 'usr/local/bin/coldverify'

    def test_shipped_coldverify_lands_on_the_guest_path(self):
        link = self.push_into_fake_vm({'opt/coldverify/coldverify': '#!/bin/sh\n'})
        self.assertEqual(os.readlink(link), '/opt/coldverify/coldverify')

    def test_coldverify_link_can_be_pushed_again_onto_a_cloned_base(self):
        tmp = Path(tempfile.mkdtemp())
        files = tmp / 'guest'
        (files / 'opt/coldverify').mkdir(parents=True)
        (files / 'opt/coldverify/coldverify').write_text('new')
        vm = tmp / 'vm'
        (vm / 'usr/local/bin').mkdir(parents=True)
        (vm / 'usr/local/bin/coldverify').symlink_to('/opt/coldverify/coldverify')

        def in_vm(name, *command, user):
            return [str(vm) if c == '/' else str(vm) + c if c.startswith('/usr/') else c for c in command]
        with mock.patch.multiple(cb, GUEST_FILES=files, ic_exec=in_vm):
            cb.push_guest_files('cbx-job')
        self.assertEqual(os.readlink(vm / 'usr/local/bin/coldverify'), '/opt/coldverify/coldverify')
        self.assertEqual((vm / 'opt/coldverify/coldverify').read_text(), 'new')

    def test_a_shipped_coldverify_symlink_lands_on_the_guest_path(self):
        link = self.push_into_fake_vm({'opt/coldverify/coldverify': Path('/srv/coldverify/coldverify')})
        self.assertEqual(os.readlink(link), '/opt/coldverify/coldverify')

    def test_a_coldverify_the_guest_files_put_on_path_is_kept(self):
        link = self.push_into_fake_vm({'opt/coldverify/coldverify': '#!/bin/sh\n', 'usr/local/bin/coldverify': 'own\n'})
        self.assertEqual(link.read_text(), 'own\n')

    def test_no_shipped_coldverify_means_no_path_entry(self):
        link = self.push_into_fake_vm({'opt/tool/run': '#!/bin/sh\n'})
        self.assertFalse(os.path.lexists(link))

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

    def test_every_vm_coldbox_creates_is_marked_managed_with_an_expiry(self):
        calls = []
        with mock.patch.multiple(cb, sync_acl=lambda: None, isolated=lambda *a: None, wait_ready=lambda *a, **k: None,
                                 ic=lambda *a, **k: calls.append(a)):
            cb.boot('cbx-1-a', 1794396038, False, '--config', f'{cb.ADHOC}=true')
        self.assertEqual(calls[0], ('copy', cb.BASE, 'cbx-1-a', '--config', 'user.coldbox.managed=true',
                                    '--config', 'user.coldbox.expires=1794396038', '--config', f'{cb.ADHOC}=true'))
        calls.clear()
        with mock.patch.multiple(cb, STATE=Path(tempfile.mkdtemp()), fetch_json=lambda url: None,
                                 runner_release=lambda _: ('2.340.0', 'u', 'd'), wait_ready=lambda *a, **k: None,
                                 destroy=lambda name: True, push_guest_files=lambda name: None,
                                 seed_cloud_init_on_create_only=lambda name: None, ic=lambda *a, **k: calls.append(a)), \
                mock.patch.object(cb.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout='')):
            cb.build_new_base()
        launch = calls[0]
        self.assertEqual(launch[:5], ('launch', cb.IMAGE, 'base-new', '--vm', '--profile'))
        self.assertIn('user.coldbox.managed=true', launch)
        self.assertIn('user.coldbox.expires=never', launch)

    def test_a_build_stops_when_the_live_base_cannot_be_given_its_rollback_window(self):
        for stderr, raises in (('Error: Instance not found', False), ('Error: connection refused', True)):
            def ic(*args, **kwargs):
                raise subprocess.CalledProcessError(1, args, stderr=stderr)
            with self.subTest(stderr=stderr), mock.patch.object(cb, 'ic', ic):
                if raises:
                    with self.assertRaises(subprocess.CalledProcessError):
                        cb.retire_live_base()
                else:
                    cb.retire_live_base()

    def test_no_guest_files_means_no_copy(self):
        with mock.patch.multiple(cb, GUEST_FILES=Path(tempfile.mkdtemp()) / 'missing'), \
                mock.patch.object(cb.subprocess, 'run') as run:
            cb.push_guest_files('base-new')
        run.assert_not_called()

    def test_guest_files_land_before_the_snapshot(self):
        calls = []
        with mock.patch.multiple(cb, STATE=Path(tempfile.mkdtemp()), fetch_json=lambda url: None, runner_release=lambda _: ('2.340.0', 'u', 'd'),
                                 wait_ready=lambda *a, **k: None, destroy=lambda name: True,
                                 push_guest_files=lambda name: calls.append(('guest-files', name)),
                                 seed_cloud_init_on_create_only=lambda name: calls.append(('seed', name)),
                                 ic=lambda *a, **k: calls.append(a)), \
                mock.patch.object(cb.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout='')):
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


class NewId(unittest.TestCase):
    def test_two_hosts_never_pick_the_same_name_and_the_start_time_still_reads(self):
        names = {}
        for host in ('host-p', 'host-a', 'CI.Host_With.A.Very-Long-FQDN.example.com', '__'):
            with mock.patch.object(cb.os, 'uname', return_value=mock.Mock(nodename=host)), \
                    mock.patch.object(cb.secrets, 'token_hex', return_value='abcdef'), \
                    mock.patch.object(cb.time, 'time', return_value=1700000000.5):
                names[host] = cb.new_id()
        self.assertEqual(len(set(names.values())), 4)
        self.assertEqual(names['host-a'], 'cbx-1700000000-abcdef-host-a')
        for name in names.values():
            self.assertRegex(name, r'^cbx-[a-z0-9-]+[a-z0-9]$')
            self.assertLessEqual(len(name), 63)
            self.assertTrue(cb.starting(name, 1700000001))


class Unregister(unittest.TestCase):
    def test_deletes_only_this_hosts_runners_on_every_page_and_leaves_the_other_hosts_online(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (tmp / 'state/receipts').mkdir(parents=True)
        ours = [{'id': i, 'name': f'cbx-1700000000-{i:06x}'} for i in range(0, 101, 2)]
        theirs = [{'id': i, 'name': f'cbx-1700000000-{i:06x}'} for i in range(1, 101, 2)]
        for runner in ours:
            cb.write_json(tmp / f"state/receipts/{runner['name']}.json", dict(id=runner['name'], runner_id=runner['id']))
        killed = {'id': 100, 'name': 'cbx-1700000000-killed'}
        (tmp / f"state/receipts/{killed['name']}.registered").touch()
        ours[-1] = killed
        cb.write_json(tmp / 'state/elsewhere.json', {})
        (tmp / 'state/receipts/cbx-x').mkdir()
        theirs.append({'id': 102, 'name': 'cbx-x/../../elsewhere'})
        listed = sorted(ours + theirs, key=lambda runner: runner['id']) + [{'id': 103, 'name': 'build-box'}]
        (tmp / 'pages').write_text(json.dumps({'runners': listed[:100]}) + json.dumps({'runners': listed[100:]}))
        (tmp / 'gh').write_text('#!/bin/sh\n'
                                f'echo "$@" >> {tmp}/calls\n'
                                'case "$*" in\n'
                                '  *DELETE*) ;;\n'
                                '  *--paginate*) python3 -c "import json,sys; d=json.JSONDecoder(); t=open(sys.argv[1]).read(); i=0\n'
                                'while i < len(t):\n'
                                '    o, i = d.raw_decode(t, i)\n'
                                '    [print(r[\\"id\\"], r[\\"name\\"]) for r in o[\\"runners\\"] if r[\\"name\\"].startswith(\\"cbx-\\")]" '
                                f'{tmp}/pages ;;\n'
                                '  *) exit 1 ;;\n'
                                'esac\n')
        (tmp / 'gh').chmod(0o755)
        (tmp / 'sudo').write_text('#!/bin/sh\nexit 0\n')
        (tmp / 'sudo').chmod(0o755)
        env = dict(os.environ, PATH=f'{tmp}:{os.environ["PATH"]}', COLDBOX_STATE=str(tmp / 'state'))
        result = subprocess.run([sys.executable, str(ROOT / 'coldbox'), 'unregister', 'me/app'], env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        deleted = [int(line.split('/')[-1]) for line in (tmp / 'calls').read_text().splitlines() if 'DELETE' in line]
        self.assertEqual(deleted, [runner['id'] for runner in ours])
        self.assertIn(f'deleted {len(ours)} coldbox runner registration(s) this host started', result.stdout)


class Status(unittest.TestCase):
    def test_a_host_without_gh_or_incus_gets_failures_not_a_crash(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / 'systemctl').write_text('#!/bin/sh\nexit 3\n')
        (tmp / 'systemctl').chmod(0o755)
        with mock.patch.dict(os.environ, PATH=str(tmp)), mock.patch.object(cb, 'STATE', tmp):
            code, output = quiet(cb.status, None)
        self.assertEqual(code, 1)
        build = hashlib.sha256((ROOT / 'coldbox').read_bytes()).hexdigest()[:12]
        self.assertEqual(output.splitlines()[0], f'coldbox 0.2.0 (build {build})')
        self.assertIn('FAIL  GitHub auth: no GitHub App and gh is not installed', output)
        self.assertIn('FAIL  Coldbox is installed', output)

    def test_status_shows_queue_age_state_and_receipt_duration(self):
        state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (state / 'systemctl').write_text('#!/bin/sh\nexit 3\n')
        (state / 'systemctl').chmod(0o755)
        (state / 'queues/me').mkdir(parents=True)
        cb.write_json(state / 'queues/me/app.json', dict(queued=2, oldest_wait_seconds=600,
                      stalled=True, observed_at='2026-01-01T00:00:00Z'))
        (state / 'receipts').mkdir()
        cb.write_json(state / 'receipts/cbx-one.json', dict(id='cbx-one', verdict='pass', repo='me/app',
                      started_at='2026-01-01T00:00:00Z', github=dict(queue_wait_seconds=42)))
        with mock.patch.dict(os.environ, PATH=str(state)), mock.patch.object(cb, 'STATE', state):
            _, output = quiet(cb.status, None)
        self.assertIn('queue me/app: 2 queued; oldest waited 600 s; STALLED; observed 2026-01-01T00:00:00Z', output)
        self.assertIn('queue waited 42 s', output)

    def test_missing_kvm_fix_depends_on_what_systemd_detect_virt_reports(self):
        for virt, fix in (('kvm', 'this host is a kvm VM or container, so Coldbox needs nested virtualization '
                                  'from its provider, see docs/vps.md'),
                          ('lxc', 'this host is a lxc VM or container, so Coldbox needs nested virtualization '
                                  'from its provider, see docs/vps.md'),
                          ('wsl', 'WSL2 gives Coldbox no KVM, so run it on a Linux machine, see README.md#wsl2'),
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

    def healthy_status(self, state, rows, registered=()):
        (state / 'systemctl').write_text('#!/bin/sh\nexit 0\n')
        (state / 'systemctl').chmod(0o755)
        built = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
        incus = {'environment': {'server_version': '6.0.7'}, 'created_at': built, 'config': {},
                 'space': {'used': 0, 'total': GIB}}
        kvm = lambda path: state if str(path) == '/dev/kvm' else Path(path)
        with mock.patch.dict(os.environ, PATH=str(state)), mock.patch.object(cb, 'Path', kvm), \
                mock.patch.multiple(cb, STATE=state, app_token=lambda: 'token',
                                    registrations=lambda: [dict(r) for r in registered],
                                    ic=lambda *a, **k: json.dumps(rows) if 'list' in a else '2',
                                    query=lambda path: incus, vm_memory=lambda: GIB, host_memory=lambda: 8 * GIB):
            return quiet(cb.status, None)

    def test_status_fails_while_a_job_vm_has_no_live_worker_and_passes_once_it_has_one(self):
        state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        rows = [{'name': 'cbx-1794396038-2feb90', 'status': 'Stopped', 'config': {'user.coldbox.expires': '0'}},
                {'name': 'cbx-9-adhoc', 'status': 'Running',
                 'config': {cb.ADHOC: 'true', 'user.coldbox.expires': '0'}}]
        (state / 'workers').mkdir()
        (state / 'workers' / '999999999').write_text(json.dumps(['me/app', '1', 'cbx-1794396038-2feb90', 'base']))
        code, output = self.healthy_status(state, rows)
        self.assertTrue((state / 'workers' / '999999999').exists(), 'status must not prune worker records unlocked')
        self.assertEqual(code, 1, output)
        self.assertIn('FAIL  1 job VM(s) without a live worker: cbx-1794396038-2feb90\n      fix: coldbox reap\n', output)
        self.assertEqual(output.count('FAIL'), 1, output)
        live_worker(self, state, 'cbx-1794396038-2feb90')
        code, output = self.healthy_status(state, rows)
        self.assertEqual(code, 0, output)
        self.assertIn('PASS  0 job VM(s) without a live worker\n', output)

    def test_status_fails_when_a_ci_host_guest_overlay_differs_and_passes_once_it_matches(self):
        state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for tool in ('bash', 'find', 'sort', 'xargs', 'sha256sum', 'cut'):
            (state / tool).symlink_to(shutil.which(tool))
        overlays = state / 'overlays'
        for host in (os.uname().nodename, 'peer-a', 'peer-b'):
            for tool in ('coldstack', 'coldverify'):
                (overlays / host / 'opt' / tool / 'sub').mkdir(parents=True)
                (overlays / host / 'opt' / tool / 'sub' / 'cold').write_text(f'{tool} main\n')
            (overlays / host / 'etc').mkdir()
            (overlays / host / 'etc' / 'host').write_text(f'{host}\n')
        hosts = state / 'ci-hosts.json'
        hosts.write_text(json.dumps(dict(primary='peer-a', overflow=[os.uname().nodename, 'peer-b'])))
        peer = lambda host, command: command[:-1] + [str(overlays / host / 'opt')]
        line = f"guest overlay {overlays / os.uname().nodename / 'opt'} matches peer-a peer-b"
        with mock.patch.multiple(cb, CI_HOSTS=hosts, GUEST_FILES=overlays / os.uname().nodename, ssh=peer):
            code, output = self.healthy_status(state, [])
            self.assertEqual(code, 0, output)
            self.assertIn(f'PASS  {line}\n', output)
            (overlays / 'peer-b' / 'opt' / 'coldstack' / 'sub' / 'cold').write_text('coldstack a7d8347\n')
            code, output = self.healthy_status(state, [])
            self.assertEqual(code, 1, output)
            self.assertIn(f'FAIL  {line}: differs on peer-b: opt/coldstack\n      fix: run sudo systemctl start '
                          'coldbox-refresh.service on every CI host so all copy the same commits\n', output)
            self.assertEqual(output.count('FAIL'), 1, output)
            (overlays / 'peer-a' / 'opt' / 'coldverify' / 'sub' / 'cold').chmod(0o755)
            (overlays / 'peer-a' / 'opt' / '.hidden').mkdir()
            (overlays / 'peer-a' / 'opt' / 'tool').symlink_to('coldstack/sub/cold')
            code, output = self.healthy_status(state, [])
            self.assertIn(f'FAIL  {line}: differs on peer-a: opt/.hidden, peer-a: opt/coldverify, peer-a: opt/tool, '
                          'peer-b: opt/coldstack\n', output)
            if os.geteuid() != 0:
                (overlays / 'peer-b' / 'opt' / 'coldstack' / 'sub').chmod(0)
                self.addCleanup((overlays / 'peer-b' / 'opt' / 'coldstack' / 'sub').chmod, 0o755)
                code, output = self.healthy_status(state, [])
                self.assertIn('differs on peer-a: opt/.hidden, peer-a: opt/coldverify, peer-a: opt/tool, '
                              'peer-b: unreadable\n', output)
            shutil.rmtree(overlays / 'peer-a')
            code, output = self.healthy_status(state, [])
            self.assertIn(f'FAIL  {line}: differs on peer-a: unreadable, peer-b: unreadable\n', output)
            hosts.write_text('{"primary": "peer-a"')
            code, output = self.healthy_status(state, [])
            self.assertEqual(code, 1, output)
            self.assertIn('FAIL  guest overlay drift unchecked: CI host admission denied', output)

    def test_status_fails_on_an_instance_without_lifecycle_metadata(self):
        state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        mark = {'user.coldbox.managed': 'true'}
        rows = [{'name': 'base-dev', 'status': 'Stopped', 'config': {**mark, 'user.coldbox.expires': 'never'}},
                {'name': 'base-prev', 'status': 'Stopped', 'config': {**mark, 'user.coldbox.expires': '1794396038'}},
                {'name': 'base-dev-pre62', 'status': 'Stopped', 'config': {}},
                {'name': 'base-smoke', 'status': 'Stopped', 'config': {**mark, 'user.coldbox.expires': 'soon'}},
                {'name': 'base-new', 'status': 'Stopped', 'config': {'user.coldbox.expires': '1794396038'}}]
        code, output = self.healthy_status(state, rows)
        self.assertEqual(code, 1, output)
        self.assertIn('FAIL  3 Incus instance(s) in project coldbox without user.coldbox.managed=true and '
                      'user.coldbox.expires: base-dev-pre62 base-smoke base-new\n', output)
        self.assertEqual(output.count('FAIL'), 1, output)
        code, output = self.healthy_status(state, rows[:2])
        self.assertEqual(code, 0, output)
        self.assertIn('PASS  0 Incus instance(s) in project coldbox without user.coldbox.managed=true and '
                      'user.coldbox.expires\n', output)

    def test_status_fails_on_a_receipt_left_with_vm_destroyed_false_and_no_live_worker(self):
        state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (state / 'receipts').mkdir()
        cb.write_json(state / 'receipts/cbx-1794263789-ca724b.json',
                      dict(unfinished_receipt('cbx-1794263789-ca724b'), started_at=cb.now_iso()))
        code, output = self.healthy_status(state, [])
        self.assertEqual(code, 1, output)
        self.assertIn('FAIL  1 receipt(s) with vm.destroyed false and no live worker: cbx-1794263789-ca724b\n'
                      '      fix: coldbox reap\n', output)
        self.assertEqual(output.count('FAIL'), 1, output)
        live_worker(self, state, 'cbx-1794263789-ca724b')
        code, output = self.healthy_status(state, [])
        self.assertEqual(code, 0, output)

    def test_status_fails_on_a_receipt_past_its_ttl_with_vm_destroyed_false_even_with_a_live_worker(self):
        state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (state / 'receipts').mkdir()
        late, fresh = 'cbx-1794263789-ca724b', 'cbx-1794263790-0f00d1'
        cb.write_json(state / f'receipts/{late}.json', unfinished_receipt(late))
        cb.write_json(state / f'receipts/{fresh}.json', dict(unfinished_receipt(fresh), started_at=cb.now_iso()))
        live_worker(self, state, late)
        live_worker(self, state, fresh)
        code, output = self.healthy_status(state, [])
        self.assertEqual(code, 1, output)
        self.assertIn(f'FAIL  1 receipt(s) past their VM TTL with vm.destroyed false: {late}\n'
                      f"      fix: coldbox reap; if it stays, stop the worker still holding it ({state / 'workers'}/<pid>)\n",
                      output)
        self.assertEqual(output.count('FAIL'), 1, output)
        cb.write_json(state / f'receipts/{late}.json', dict(unfinished_receipt(late), started_at=cb.now_iso()))
        code, output = self.healthy_status(state, [])
        self.assertEqual(code, 0, output)
        self.assertIn('PASS  0 receipt(s) past their VM TTL with vm.destroyed false\n', output)

    def test_status_fails_slots_past_the_core_bound_or_the_large_vm_room(self):
        state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for threads, slots, code in ((40, 2, 0), (40, 3, 1), (6, 1, 0), (6, 2, 1)):
            with self.subTest(threads=threads, slots=slots), mock.patch.object(cb.os, 'cpu_count', lambda: threads):
                self.assertEqual(self.healthy_status(state, [], [dict(target='myorg', active='active', slots=slots)])[0], code)
        with mock.patch.object(cb.os, 'cpu_count', lambda: 6):
            stopped = dict(target='me/stopped', active='inactive', configured=True, slots=1)
            self.assertIn('FAIL  job VMs: 2 vCPU, 1 GiB each; 2 slot(s) registered',
                          self.healthy_status(state, [], [dict(target='myorg', active='active', slots=1), stopped])[1])
            _, output = self.healthy_status(state, [], [dict(target='myorg', active='active', slots=2)])
        self.assertIn('FAIL  job VMs: 2 vCPU, 1 GiB each; 2 slot(s) registered, at most 1 leave room for one more VM '
                      'within 2 job VMs (CPU threads less the reserve, 3 per VM) and for one large VM in 8 GiB RAM\n'
                      '      fix: unregister a repo or register again with fewer --slots\n', output)

    def test_status_lists_visibility_and_fails_a_public_repo_registered_without_allow_public(self):
        state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        repos = {'me/closed': dict(full_name='me/closed', visibility='private'),
                 'me/open': dict(full_name='me/open', visibility='public', owner=dict(type='User')),
                 'me/opted': dict(full_name='me/opted', visibility='public', owner=dict(type='User')),
                 'org/open': dict(full_name='org/open', visibility='public', owner=dict(type='Organization'))}

        def github(path, *_args):
            if path.endswith('/fork-pr-contributor-approval'):
                return dict(approval_policy=cb.APPROVAL)
            if '/collaborators?' in path:
                return [dict(login='me')]
            if path == 'repos/me/gone':
                raise RuntimeError('gh api repos/me/gone: HTTP 404')
            return repos[path.removeprefix('repos/')]

        registered = [dict(target=target, active='active', slots=0, allow_public=target == 'me/opted')
                      for target in (*repos, 'me/gone', 'myorg')]
        with mock.patch.object(cb, 'gh', github):
            code, output = self.healthy_status(state, [], registered)
        self.assertEqual(code, 1, output)
        self.assertIn('FAIL  me/open is public and registered without --allow-public, so no jobs run\n'
                      "      fix: move me/open's workflows to GitHub-hosted runners, or, with the owner's approval, "
                      f'set its fork-pr-contributor-approval to {cb.APPROVAL} and run coldbox register me/open '
                      '--slots 0 --allow-public\n', output)
        self.assertIn('PASS  me/opted is public with --allow-public: jobs from me (write access) run without approval',
                      output)
        self.assertIn('FAIL  me/gone: visibility unreadable from GitHub: gh api repos/me/gone: HTTP 404', output)
        self.assertIn('FAIL  org/open is public and registered without --allow-public, so no jobs run\n'
                      "      fix: move org/open's workflows to GitHub-hosted runners; --allow-public serves only repos "
                      'owned by a user account\n', output)
        self.assertEqual(output.count('FAIL'), 3, output)
        for target, visibility in (('me/closed', 'private'), ('me/open', 'public'), ('me/opted', 'public'),
                                   ('me/gone', 'unknown'), ('myorg', 'organization')):
            self.assertRegex(output, rf'\n  {target} +active +burst only[^\n]*; {visibility}\n')
        with mock.patch.object(cb, 'gh', github):
            code, output = self.healthy_status(state, [], [r for r in registered
                                                          if r['target'] not in ('me/open', 'org/open', 'me/gone')])
        self.assertEqual(code, 0, output)

    def test_registrations_read_the_allow_public_opt_in_from_the_unit_drop_in(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        units = ''
        for name, dropin in (('me-open', 'COLDBOX_ALLOW_PUBLIC=0\nEnvironment=COLDBOX_SLOTS=0\n'),
                             ('me-opted', 'COLDBOX_ALLOW_PUBLIC=1\nEnvironment=COLDBOX_SLOTS=0\n')):
            unit = f'coldbox-runner@{name}.service'
            units += f'{unit} loaded active running Coldbox\n'
            (tmp / f'{unit}.d').mkdir()
            (tmp / f'{unit}.d/coldbox.conf').write_text(f'[Service]\nEnvironment={dropin}')
        (tmp / 'systemctl').write_text(f"#!/bin/sh\nprintf '{units}'\n")
        (tmp / 'systemd-escape').write_text('#!/bin/sh\necho "$2" | tr - /\n')
        for tool in ('systemctl', 'systemd-escape'):
            (tmp / tool).chmod(0o755)
        etc = lambda path: tmp / str(path).removeprefix('/etc/systemd/system/')
        with mock.patch.dict(os.environ, PATH=f"{tmp}{os.pathsep}{os.environ['PATH']}"), \
                mock.patch.object(cb, 'Path', etc):
            found = {r['target']: r['allow_public'] for r in cb.registrations()}
        self.assertEqual(found, {'me/open': False, 'me/opted': True})

    def test_an_unreadable_receipt_ttl_counts_as_past_it(self):
        now, started = time.time(), cb.now_iso()
        self.assertFalse(cb.past_ttl(dict(started_at=started, vm=dict(ttl_seconds=3600)), now))
        for ttl in (float('nan'), float('inf'), 10**400, -1, '3600', True, 3600.0, None):
            with self.subTest(ttl=ttl):
                self.assertTrue(cb.past_ttl(dict(started_at=started, vm=dict(ttl_seconds=ttl)), now))
        for receipt in (dict(vm=dict(ttl_seconds=3600)), dict(started_at='yesterday', vm=dict(ttl_seconds=3600)),
                        dict(started_at=started, vm=None)):
            with self.subTest(receipt=receipt):
                self.assertTrue(cb.past_ttl(receipt, now))

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

    def test_a_cpu_reserve_is_listed_and_a_malformed_one_fails(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / 'systemctl').write_text('#!/bin/sh\nexit 3\n')
        (tmp / 'systemctl').chmod(0o755)
        for text, expected in (('20\n', 'reserve: bursts leave 20 CPU thread(s) to other work'),
                               ('half\n', "FAIL  " + f"{tmp / 'reserve'} must hold a whole number of CPU threads")):
            (tmp / 'reserve').write_text(text)
            with mock.patch.dict(os.environ, PATH=str(tmp)), mock.patch.object(cb, 'STATE', tmp), \
                    mock.patch.object(cb, 'RESERVE', tmp / 'reserve'), mock.patch.object(cb, 'registrations', lambda: []):
                output = quiet(cb.status, None)[1]
            self.assertIn(expected, output)


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
HOST_MEMORY = cb.host_memory
RUNNER_01 = dict(cpus=40, load=7.0, available=210 * GIB, floor=31 * GIB, vm_memory=16 * GIB)


class ScalingDecision(unittest.TestCase):
    def test_one_burst_per_queued_job_while_available_memory_stays_above_the_floor(self):
        roomy = dict(RUNNER_01, cpus=100)
        self.assertEqual(cb.burst_count(3, cb.rooms(3, **roomy)), 3)
        self.assertEqual(cb.burst_count(30, cb.rooms(3, **roomy)), 11)
        self.assertEqual(cb.burst_count(30, cb.rooms(15, **roomy)), 11)
        self.assertEqual(cb.burst_count(0, cb.rooms(3, **roomy)), 0)

    def test_every_job_vm_idle_or_busy_takes_cores_per_job_threads_after_the_reserve(self):
        free = dict(RUNNER_01, load=0, available=1024 * GIB)
        self.assertEqual(cb.burst_count(30, cb.rooms(3, **RUNNER_01)), 10)
        self.assertEqual(cb.burst_count(30, cb.rooms(9, **free)), 4)
        self.assertEqual(cb.burst_count(30, cb.rooms(13, **free)), 0)
        self.assertEqual(cb.burst_count(30, cb.rooms(9, **free, reserve=4)), 3)

    def test_busy_cpu_threads_stop_scaling(self):
        self.assertEqual(cb.burst_count(5, cb.rooms(3, **dict(RUNNER_01, load=38.5))), 1)
        self.assertEqual(cb.burst_count(5, cb.rooms(3, **dict(RUNNER_01, load=40.2))), 0)

    def test_hyperthreads_count_as_free_cpu(self):
        self.assertEqual(cb.burst_count(5, cb.rooms(3, **dict(RUNNER_01, load=25.0))), 5)

    def test_a_host_with_little_cpu_gets_few_or_no_job_vms(self):
        self.assertEqual(cb.burst_count(30, cb.rooms(0, **dict(RUNNER_01, cpus=4, load=0))), 1)
        self.assertEqual(cb.burst_count(30, cb.rooms(0, **dict(RUNNER_01, cpus=2, load=0))), 0)

    def test_low_available_memory_stops_scaling(self):
        self.assertEqual(cb.burst_count(5, cb.rooms(3, **dict(RUNNER_01, available=46 * GIB))), 0)
        self.assertEqual(cb.burst_count(5, cb.rooms(3, **dict(RUNNER_01, available=47 * GIB))), 1)
        self.assertEqual(cb.burst_count(5, cb.rooms(3, **dict(RUNNER_01, available=63 * GIB))), 2)

    def test_only_jobs_a_coldbox_runner_can_take_count(self):
        self.assertTrue(cb.runs_here(['self-hosted', 'coldbox']))
        self.assertTrue(cb.runs_here(['self-hosted', 'Linux', 'X64', 'coldbox']))
        self.assertFalse(cb.runs_here(['ubuntu-latest']))
        self.assertFalse(cb.runs_here(['self-hosted', 'agentfleet-linux', 'other-host']))
        self.assertFalse(cb.runs_here([]))


class FakeGitHub:
    def __init__(self, runs, jobs, runners=(), repo=None, policy=cb.APPROVAL, writers=('me',)):
        self.runs, self.jobs, self.runners = runs, jobs, list(runners)
        self.repo = repo or {'full_name': 'me/app', 'visibility': 'private'}
        self.policy, self.writers = policy, writers

    def get(self, path):
        page = int(path.rsplit('page=', 1)[1]) if 'page=' in path else 1
        chunk = slice((page - 1) * 100, page * 100)
        if path == 'repos/me/app':
            return self.repo
        if path == 'repos/me/app/actions/permissions/fork-pr-contributor-approval':
            return {'approval_policy': self.policy}
        if path.startswith('repos/me/app/collaborators?permission=push&per_page=100&page='):
            return [{'login': login} for login in self.writers][chunk]
        if '/runners?' in path:
            return {'runners': self.runners[chunk]}
        if '/jobs?' in path:
            return {'jobs': self.jobs[int(path.split('/runs/')[1].split('/')[0])][chunk]}
        status = path.split('status=')[1].split('&')[0]
        return {'workflow_runs': [run if isinstance(run, dict) else {'id': run} for run in self.runs.get(status, [])][chunk]}

    def forget_unused(self):
        pass


def queued(n, labels=('self-hosted', 'coldbox')):
    return [{'status': 'queued', 'labels': list(labels)}] * n


def queued_ago(*seconds):
    return [{'status': 'queued', 'labels': ['coldbox'], 'created_at': time.strftime(
        '%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - ago))} for ago in seconds]


class QueuedJobs(unittest.TestCase):
    def test_queue_observation_records_oldest_and_alerts_once_then_clears(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(cb, 'STATE', Path(directory)):
            github = FakeGitHub({'queued': [1]}, {1: queued_ago(600, 30)})
            first = quiet(cb.observe_queue, github, 'me/app')[1]
            self.assertIn('STALLED', first)
            self.assertNotIn('STALLED', quiet(cb.observe_queue, github, 'me/app')[1])
            observation = json.loads((Path(directory) / 'queues/me/app.json').read_text())
            self.assertEqual(observation['queued'], 2)
            self.assertGreaterEqual(observation['oldest_wait_seconds'], 600)
            self.assertIn('CLEARED', quiet(cb.observe_queue, FakeGitHub({}, {}), 'me/app')[1])

    def test_queue_count_is_read_only_and_returns_oldest_eligible_age(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(cb, 'STATE', Path(directory)), \
                mock.patch.object(cb.time, 'time', return_value=1000):
            github = FakeGitHub({'queued': [1]}, {1: queued_ago(120, 20) + queued(1, ['other'])})
            self.assertEqual(cb.queued_jobs(github, 'me/app'), (2, 120, {'default': 2}))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_threshold_config_and_recovery_to_younger_queue(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(cb, 'STATE', Path(directory)), \
                mock.patch.dict(os.environ, COLDBOX_QUEUE_STALL_SECONDS='10'):
            self.assertIn('STALLED', quiet(cb.observe_queue, FakeGitHub({'queued': [1]}, {1: queued_ago(20)}), 'me/app')[1])
            self.assertIn('CLEARED', quiet(cb.observe_queue, FakeGitHub({'queued': [1]}, {1: queued_ago(1)}), 'me/app')[1])
            for value in ('0', '-1', 'bad'):
                with mock.patch.dict(os.environ, COLDBOX_QUEUE_STALL_SECONDS=value), self.assertRaises(ValueError):
                    cb.observe_queue(FakeGitHub({}, {}), 'me/app')

    def test_failed_github_read_keeps_stalled_observation(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(cb, 'STATE', Path(directory)):
            quiet(cb.observe_queue, FakeGitHub({'queued': [1]}, {1: queued_ago(600)}), 'me/app')
            path = Path(directory) / 'queues/me/app.json'
            previous = path.read_text()
            with mock.patch.object(cb, 'listing', side_effect=RuntimeError('unavailable')), self.assertRaises(RuntimeError):
                cb.observe_queue(None, 'me/app')
            self.assertEqual(path.read_text(), previous)
            self.assertEqual(json.loads(previous)['threshold_seconds'], 300)

    def test_observation_failure_does_not_prevent_scaling(self):
        with mock.patch.object(cb, 'write_json', side_effect=PermissionError('read only')):
            result, log = quiet(cb.observe_queue, FakeGitHub({'queued': [1]}, {1: queued_ago(20)}), 'me/app')
        self.assertEqual(result[0], 1)
        self.assertIn('queue observation unavailable', log)


    def test_queued_jobs_in_started_runs_and_past_the_first_page_count(self):
        github = FakeGitHub({'queued': [1], 'in_progress': [2]},
                            {1: queued(1), 2: [{'status': 'in_progress', 'labels': ['coldbox']}] * 100 + queued(4)
                             + queued(2, ['ubuntu-latest'])})
        self.assertEqual(cb.queued_jobs(github, 'me/app'), (5, 0, {'default': 5}))

    def test_a_queued_job_in_a_run_left_pending_by_a_concurrency_handoff_counts(self):
        github = FakeGitHub({'pending': [1]}, {1: queued(1)})
        self.assertEqual(cb.queued_jobs(github, 'me/app'), (1, 0, {'default': 1}))

    def test_only_jobs_queued_longer_than_the_delay_count(self):
        github = FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued_ago(30, 200, 32)})
        self.assertEqual(cb.queued_jobs(github, 'me/app', 31)[0], 2)
        self.assertEqual(cb.queued_jobs(github, 'me/app')[0], 3)


class QueueDuration(unittest.TestCase):
    def test_matches_runner_in_current_attempt_and_records_duration(self):
        metadata = dict(repo='me/app', run_id='123', run_attempt='2', started_at='2026-01-01T00:02:00Z')
        jobs = [dict(id=1, runner_name='other'), dict(id=2, runner_name='cbx-one',
                created_at='2026-01-01T00:00:00Z', started_at='2026-01-01T00:01:30Z')]
        with mock.patch.object(cb, 'gh', return_value={'jobs': jobs}) as api:
            cb.record_queue_wait(metadata, 'cbx-one')
        self.assertEqual(metadata['queue_wait_seconds'], 90)
        self.assertEqual(metadata['queued_at'], '2026-01-01T00:00:00Z')
        self.assertEqual(metadata['job_id'], 2)
        api.assert_called_once_with('repos/me/app/actions/runs/123/attempts/2/jobs?per_page=100&page=1')

    def test_runner_receipt_and_log_include_queue_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            metadata = dict(repo='me/app', sha='abc', run_id='123', run_attempt='1')
            def pull(name, path):
                return metadata if path.endswith('/job.json') else None
            with mock.patch.multiple(cb, STATE=state, boot=lambda *a: None, ic=lambda *a: None,
                    ic_exec=lambda *a, **kw: ['false' if a[1] == 'coldbox' else 'true'], tee=lambda *a: 0, pull=pull,
                    destroy=lambda name: True, runner_admission=lambda *a: dict(host='ci-host')), \
                    mock.patch.object(cb, 'gh', return_value={'jobs': [dict(id=4, runner_name='cbx-test',
                        created_at='2026-01-01T00:00:00Z', started_at='2026-01-01T00:00:42Z')]}):
                _, log = quiet(cb.job, 'runner', 'cbx-test', 60, ['true'], 'me/app')
            self.assertIn('cbx-test queue waited 42 s', log)
            receipt = json.loads((state / 'receipts/cbx-test.json').read_text())
            self.assertEqual(receipt['github']['queue_wait_seconds'], 42)

    def test_records_job_completion_and_each_step_duration(self):
        metadata = dict(repo='me/app', run_id='123', run_attempt='1')
        steps = [dict(name='Set up job', conclusion='success', started_at='2026-01-01T00:01:00Z',
                      completed_at='2026-01-01T00:01:03Z'),
                 dict(name='test', conclusion='failure', started_at='2026-01-01T00:01:03Z',
                      completed_at='2026-01-01T00:03:03Z'),
                 dict(name='never ran', conclusion='skipped', started_at=None, completed_at=None)]
        jobs = [dict(id=2, runner_name='cbx-one', created_at='2026-01-01T00:00:00Z',
                     started_at='2026-01-01T00:01:00Z', completed_at='2026-01-01T00:03:05Z', steps=steps)]
        with mock.patch.object(cb, 'gh', return_value={'jobs': jobs}):
            quiet(cb.record_queue_wait, metadata, 'cbx-one')
        self.assertEqual(metadata['completed_at'], '2026-01-01T00:03:05Z')
        self.assertEqual(metadata['steps'], [dict(name='Set up job', conclusion='success', seconds=3),
                                             dict(name='test', conclusion='failure', seconds=120),
                                             dict(name='never ran', conclusion='skipped', seconds=None)])

    def test_missing_or_invalid_job_timing_is_unavailable(self):
        for started in (None, 'invalid', '2025-12-31T23:59:59Z'):
            with self.subTest(started=started):
                metadata = dict(repo='me/app', run_id='123', run_attempt='1')
                jobs = [dict(id=2, runner_name='cbx-one', created_at='2026-01-01T00:00:00Z', started_at=started)]
                with mock.patch.object(cb, 'gh', return_value={'jobs': jobs}):
                    _, log = quiet(cb.record_queue_wait, metadata, 'cbx-one')
                self.assertNotIn('queue_wait_seconds', metadata)
                self.assertIn('queue wait unavailable', log)

    def test_api_failure_keeps_metadata_and_reports_unavailable(self):
        metadata = dict(repo='me/app', run_id='123', run_attempt='1')
        with mock.patch.object(cb, 'gh', side_effect=RuntimeError('unavailable')):
            _, log = quiet(cb.record_queue_wait, metadata, 'cbx-one')
        self.assertNotIn('queue_wait_seconds', metadata)
        self.assertIn('queue wait unavailable', log)

class HostCpuLoad(unittest.TestCase):
    def test_samples_idle_without_double_counting_guest(self):
        samples = ['cpu 100 0 20 100 0 0 0 0 80 0\n',
                   'cpu 150 0 30 130 10 0 0 0 140 0\n']
        with mock.patch.object(cb.Path, 'read_text', side_effect=samples), \
                mock.patch.object(cb.time, 'sleep'), mock.patch.object(cb.os, 'cpu_count', return_value=10):
            self.assertEqual(cb.host_cpu_load(), 7.0)

    def test_invalid_samples_fail_closed(self):
        for samples in ((OSError(),), ('',), ('cpu bad\n',), ('cpu -1 0 0 1 0 0 0 0\n',),
                        ('cpu 1 0 0 1 0 0 0 0\n', OSError()),
                        ('cpu 1 0 0 1 0 0 0 0\n', 'cpu 1 0 0 1 0 0 0 0\n'),
                        ('cpu 2 0 0 2 0 0 0 0\n', 'cpu 1 0 0 3 0 0 0 0\n')):
            with self.subTest(samples=samples), mock.patch.object(cb.Path, 'read_text', side_effect=samples), \
                    mock.patch.object(cb.time, 'sleep'), mock.patch.object(cb.os, 'cpu_count', return_value=10), \
                    mock.patch.object(cb.os, 'getloadavg', return_value=(12, 0, 0)):
                self.assertEqual(cb.host_cpu_load(), 12)

    def test_invalid_samples_use_cpu_count_when_raw_load_is_lower(self):
        with mock.patch.object(cb.Path, 'read_text', side_effect=OSError()), \
                mock.patch.object(cb.os, 'cpu_count', return_value=10), \
                mock.patch.object(cb.os, 'getloadavg', return_value=(3, 0, 0)):
            self.assertEqual(cb.host_cpu_load(), 10)


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
                                storage_refusal=lambda: None,
                                host_memory=lambda field='MemTotal': (247 if field == 'MemTotal' else 210) * GIB).items():
            patcher = mock.patch.object(cb, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for patcher in (mock.patch.object(cb.subprocess, 'Popen', Child), mock.patch.object(cb.os, 'cpu_count', lambda: 40),
                        mock.patch.object(cb.os, 'getloadavg', lambda: (7.0, 7.0, 7.0)),
                        mock.patch.object(cb, 'host_cpu_load', lambda: 7.0)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.args = argparse.Namespace(target='me/app', slots=0, ttl=21600, allow_public=False, allow_forks=False)

    def scale(self, github, bursts=None):
        bursts = [] if bursts is None else bursts
        quiet(cb.autoscale, github, self.args, bursts)
        return bursts

    def test_a_mixed_queue_starts_bursts_of_each_requested_size_within_memory(self):
        large = ('self-hosted', 'coldbox-large')
        idle = {'name': 'cbx-1-l', 'busy': False, 'status': 'online',
                'labels': [{'name': label} for label in cb.runner_labels('large')]}
        github = FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(2) + queued(4, large)}, [idle])
        with mock.patch.object(cb, 'vm_shape', lambda size: (50 * GIB, {}) if size == 'large' else (None, {})):
            log = quiet(cb.autoscale, github, self.args, bursts := [])[1]
        self.assertEqual([burst.size for burst in bursts], ['default', 'default', 'large', 'large', 'large'])
        self.assertEqual([argv[-2:] for argv in self.started[2:]], [['--size', 'large']] * 3)
        self.assertTrue(all('--size' not in argv for argv in self.started[:2]))
        self.assertIn('4 queued job(s) and 1 idle runner(s) on me/app for large VMs', log)
        self.assertIn('started 3 burst runner(s)', log)
        self.assertEqual(json.loads((self.state / 'workers' / str(os.getpid())).read_text())[3:],
                         ['burst', None, 50 * GIB])

    def test_autoscale_logs_oldest_wait(self):
        log = quiet(cb.autoscale, FakeGitHub({'queued': [1]}, {1: queued_ago(600)}), self.args, [])[1]
        self.assertIn('oldest waited 600 s', log)

    def test_a_pool_without_room_starts_no_burst_and_reports_no_vm_room(self):
        full = 'coldbox: storage pool coldbox has 30 GiB free, below one 40 GiB root disk plus a 10 GiB reserve'
        github = FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(3)})
        with mock.patch.object(cb, 'storage_refusal', lambda: full):
            log = quiet(cb.autoscale, github, self.args, [])[1]
            self.assertEqual(cb.vm_room(), (0, 0))
        self.assertEqual(self.started, [])
        self.assertIn(full, log)
        self.assertIn('started 0 burst runner(s)', log)
        self.assertGreater(cb.vm_room()[1], 0)
        self.assertEqual(len(self.scale(github)), 3)

    def test_measured_host_cap_restricts_actual_burst_starts(self):
        (self.state / 'ramp-cap.json').write_text(json.dumps(dict(limit=2, expires=time.time()+60)))
        self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(5)}))
        self.assertEqual(len(self.started), 2)

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
        self.assertEqual((self.state / 'workers' / str(os.getpid())).read_text(), json.dumps(['me/app', cb.process_start(os.getpid()), bursts[-1].name, 'burst', None]))

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
        (self.state / 'receipts').mkdir()
        (self.state / 'receipts' / 'cbx-o.log').write_text('Listening for Jobs\n')
        with mock.patch.object(cb, 'rooms', wraps=cb.rooms) as planned:
            self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(30)}))
        self.assertEqual(planned.call_args.args[0], 1)
        self.assertEqual(len(self.started), 11)
        self.assertFalse((folder / '999999999').exists())
        self.assertNotEqual((folder / str(os.getpid())).read_text(), 'reused/pid 1')

    def test_a_host_whose_cpu_threads_are_busy_leaves_queued_jobs_to_other_hosts(self):
        bursts, log = [], io.StringIO()
        with mock.patch.object(cb.os, 'getloadavg', lambda: (44.0, 44.0, 44.0)), \
                mock.patch.object(cb, 'host_cpu_load', return_value=44.0), contextlib.redirect_stderr(log):
            cb.autoscale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(3)}), self.args, bursts)
        self.assertEqual(bursts, [])
        self.assertEqual(self.started, [])
        self.assertEqual(log.getvalue(), 'coldbox: queue on me/app: 3 queued job(s); oldest waited 0 s\n'
                                         'coldbox: 3 queued job(s) and 0 idle runner(s) on me/app; oldest waited 0 s; started 0 burst '
                                         'runner(s); host has 0 busy and 0 warm job VM(s) and load 44.0 on 40 CPU '
                                         "threads; raw load 44.0; booting 0; room {'ceiling': 13, 'memory': 11, 'load': -4}; "
                                         f'VM memory {16 * GIB}; stopping False; declined by load\n')

    def test_every_host_role_counts_raw_load(self):
        for role in (('overflow-host', 'primary-host'), RuntimeError('denied'), (os.uname().nodename, os.uname().nodename)):
            self.started.clear()
            with self.subTest(role=role), mock.patch.object(cb, 'ci_host', side_effect=[role]), \
                    mock.patch.object(cb.os, 'getloadavg', return_value=(59.0, 0, 0)), \
                    mock.patch.object(cb, 'host_cpu_load', return_value=32.0):
                self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(3)}))
            self.assertEqual(len(self.started), 0)

    def test_a_cpu_starved_primary_starts_no_burst_though_its_cpu_sample_shows_idle_ticks(self):
        for raw, started in ((105.6, 0), (59.0, 0), (34.0, 3)):
            self.started.clear()
            with self.subTest(raw=raw), mock.patch.object(cb.os, 'getloadavg', return_value=(raw, 0, 0)), \
                    mock.patch.object(cb, 'host_cpu_load', return_value=31.0):
                output = quiet(cb.autoscale, FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(3)}), self.args, [])[1]
            self.assertEqual(len(self.started), started, output)
            self.assertIn(f'load {raw:.1f} on 40 CPU threads', output)

    def test_busy_cpu_blocks_bursts(self):
        with mock.patch.object(cb, 'host_cpu_load', return_value=40.0):
            self.assertEqual(self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued_ago(3, 120)})), [])

    def test_bursts_still_booting_count_as_load_on_every_repo(self):
        folder = self.state / 'workers'
        folder.mkdir()
        record = folder / str(os.getppid())
        for name, expected in ((f'cbx-{int(time.time())}-b', 1), ('cbx-1-b', 2)):
            for leftover in folder.iterdir():
                leftover.unlink()
            record.write_text(json.dumps(['other/repo', cb.process_start(os.getppid()), name, 'burst']))
            self.started.clear()
            with mock.patch.object(cb.os, 'getloadavg', lambda: (38.0, 38.0, 38.0)), \
                    mock.patch.object(cb, 'host_cpu_load', return_value=38.0):
                self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(5)}))
            self.assertEqual(len(self.started), expected, name)

    def test_bursts_still_starting_are_not_started_again_on_the_next_pass(self):
        github = FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(3)})
        bursts = self.scale(github)
        self.assertEqual(len(bursts), 3)
        self.scale(github, bursts)
        self.assertEqual(len(self.started), 3)

    def test_bursts_past_boot_are_counted_by_real_available_memory_not_their_limit(self):
        workers = [argparse.Namespace(pid=n, target='other/repo', name=f'cbx-1-{n}', kind='burst', writers=None)
                   for n in range(5)]
        workers += [argparse.Namespace(pid=100 + n, target='x/y', name=f'cbx-1-w{n}', kind='base', writers=None)
                    for n in range(2)]
        (self.state / 'receipts').mkdir()
        for worker in workers:
            (self.state / 'receipts' / f'{worker.name}.log').write_text('Listening for Jobs\n')
        for free, started in ((188, 10), (79, 4), (66, 2)):
            self.started.clear()
            with self.subTest(free=free), mock.patch.object(cb, 'live_workers', lambda folder: workers), \
                    mock.patch.object(cb, 'host_vms', lambda: {worker.name for worker in workers}), \
                    mock.patch.object(cb, 'vm_memory', lambda: 12 * GIB), \
                    mock.patch.object(cb, 'host_cpu_load', lambda: 30.0), mock.patch.object(cb.os, 'cpu_count', lambda: 100), \
                    mock.patch.object(cb, 'host_memory',
                                      lambda field='MemTotal': (247 if field == 'MemTotal' else free) * GIB):
                self.assertEqual(cb.host_load(workers), (5, 2))
                self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(12)}))
                self.assertEqual(len(self.started), started)
                self.assertGreaterEqual(free * GIB - started * 12 * GIB, 247 * GIB // 8)

    def test_a_vm_not_yet_ready_reserves_its_memory_however_long_it_boots(self):
        workers = [argparse.Namespace(pid=n, target='other/repo', name=f'cbx-1-{n}', kind='burst', writers=None)
                   for n in range(9)]
        receipts = self.state / 'receipts'
        receipts.mkdir()
        for worker in workers:
            (receipts / f'{worker.name}.log').write_text('')
        with mock.patch.object(cb, 'live_workers', lambda folder: workers), \
                mock.patch.object(cb, 'vm_memory', lambda: 12 * GIB), mock.patch.object(cb.os, 'cpu_count', lambda: 100), \
                mock.patch.object(cb, 'host_memory', lambda field='MemTotal': (247 if field == 'MemTotal' else 196) * GIB):
            self.assertFalse(any(cb.starting(worker.name, time.time()) for worker in workers))
            self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(5)}))
            self.assertEqual(len(self.started), 4)
            self.started.clear()
            for worker in workers:
                (receipts / f'{worker.name}.log').write_text('Listening for Jobs\n')
            self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(5)}))
            self.assertEqual(len(self.started), 5)
            self.started.clear()
            (self.state / 'ready').mkdir()
            for worker in workers:
                (receipts / f'{worker.name}.log').unlink()
                (self.state / 'ready' / worker.name).touch()
            self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(5)}))
            self.assertEqual(len(self.started), 5)

    def test_a_finished_burst_drops_its_pid_file(self):
        folder = self.state / 'workers'
        folder.mkdir()
        (folder / '4242').write_text(json.dumps(['me/app', '1', 'cbx-d', 'burst']))
        done = argparse.Namespace(pid=4242, poll=lambda: 0, returncode=0)
        bursts = self.scale(FakeGitHub({'queued': [], 'in_progress': []}, {}), [done])
        self.assertEqual(bursts, [])
        self.assertFalse((folder / '4242').exists())

    def test_idle_and_busy_job_vms_together_never_exceed_the_ceiling(self):
        workers, receipts = [], self.state / 'receipts'
        receipts.mkdir()
        with mock.patch.object(cb, 'host_vms', lambda: {worker.name for worker in workers}), \
                mock.patch.object(cb, 'host_memory', lambda field='MemTotal': (247 if field == 'MemTotal' else 1000) * GIB):
            while cb.burst_count(1, cb.host_room(workers, 12 * GIB, 12 * GIB, 0, 0)):
                workers.append(argparse.Namespace(pid=len(workers), target='x/y', name=f'cbx-1-{len(workers)}',
                                                  kind='base', writers=None))
                (receipts / f'{workers[-1].name}.log').write_text('Listening for Jobs\n')
            self.assertEqual(cb.host_load(workers), (0, len(workers)))
            self.assertLess(len(workers), 40)
            self.assertLessEqual(len(workers) * 12 * GIB, 247 * GIB - cb.host_share(247 * GIB))
            for worker in workers:
                (receipts / f'{worker.name}.log').write_text('Running job: verify\n')
            self.assertEqual(cb.host_load(workers), (len(workers), 0))
            self.assertEqual(cb.burst_count(1, cb.host_room(workers, 12 * GIB, 12 * GIB, 0, 0)), 0)

    def test_every_job_vm_at_its_full_limit_leaves_the_floor_and_host_share_on_each_ci_host(self):
        for host, threads, total, vm in (('host-a', 40, 259615292 * 1024, 12 * GIB), ('host-b', 40, 259615292 * 1024, 16 * GIB),
                                         ('host-c', 24, 193434316 * 1024, 16 * GIB), ('host-d', 24, 193434292 * 1024, 16 * GIB)):
            with self.subTest(host=host), mock.patch.object(cb.os, 'cpu_count', lambda: threads), \
                    mock.patch.object(cb, 'host_memory', lambda field='MemTotal': total):
                ceiling = cb.host_room([], vm, vm, 0, 0)['ceiling']
                self.assertGreater(ceiling, 0)
                self.assertLessEqual(ceiling * vm + cb.host_share(total), total)

    def test_register_counts_every_slot_but_not_busy_vms(self):
        folder = self.state / 'workers'
        folder.mkdir()
        (folder / str(os.getpid())).write_text(json.dumps(['other/repo', cb.process_start(os.getpid()), 'cbx-o', 'burst']))
        with mock.patch.object(cb, 'host_vms', lambda: {f'cbx-{n}' for n in range(15)}), \
                mock.patch.object(cb, 'guard') as guard, mock.patch.object(cb, 'instance', lambda target: 'me-new'), \
                mock.patch.object(cb.subprocess, 'run'):
            code, _ = quiet(cb.register, argparse.Namespace(target='me/new', slots=8, allow_public=False,
                                                            allow_forks=False))
            self.assertEqual(code, 0)
            guard.reset_mock()
            with self.assertRaises(SystemExit) as refused:
                cb.register(argparse.Namespace(target='me/new', slots=9, allow_public=False, allow_forks=False))
        self.assertIn('12 slot VMs of 16 GiB, counting every registered slot', str(refused.exception))
        self.assertIn('(32.375 GiB stays with the host)', str(refused.exception))
        guard.assert_not_called()

    def test_register_leaves_ceiling_room_for_one_large_vm_beside_every_slot(self):
        with mock.patch.object(cb, 'host_memory', lambda field='MemTotal': 247 * GIB), mock.patch.object(cb.os, 'cpu_count', lambda: 100), \
                mock.patch.object(cb, 'vm_memory', lambda: 12 * GIB), \
                mock.patch.object(cb, 'registrations', lambda: []), \
                mock.patch.object(cb, 'guard') as guard, mock.patch.object(cb, 'instance', lambda target: 'me-new'), \
                mock.patch.object(cb.subprocess, 'run'):
            code, _ = quiet(cb.register, argparse.Namespace(target='me/new', slots=15, allow_public=False,
                                                            allow_forks=False))
            self.assertEqual(code, 0)
            guard.reset_mock()
            with self.assertRaises(SystemExit) as refused:
                cb.register(argparse.Namespace(target='me/new', slots=16, allow_public=False, allow_forks=False))
        self.assertIn('16 slot VMs of 12 GiB, counting every registered slot, plus one 24 GiB large VM exceed',
                      str(refused.exception))
        self.assertIn('Lower --slots to at most 15', str(refused.exception))
        guard.assert_not_called()

    def test_register_counts_a_registered_slot_that_is_running_a_job(self):
        cb.note_worker(os.getpid(), 'x/y', 'cbx-base', 'base')
        receipts = self.state / 'receipts'
        receipts.mkdir()
        (receipts / 'cbx-base.log').write_bytes(b'2026-11-09 14:58:02Z: Running job: verify\n')
        with mock.patch.object(cb, 'registrations', lambda: [dict(active='active', slots=1, target='x/y')]), \
                mock.patch.object(cb, 'guard') as guard, mock.patch.object(cb.subprocess, 'run'), \
                self.assertRaises(SystemExit) as refused:
            cb.register(argparse.Namespace(target='me/new', slots=15, allow_public=False, allow_forks=False))
        self.assertIn('16 slot VMs of 16 GiB, counting every registered slot', str(refused.exception))
        guard.assert_not_called()

    def test_slot_vms_still_booting_reserve_their_memory_from_bursts(self):
        folder = self.state / 'workers'
        folder.mkdir()
        (folder / str(os.getppid())).write_text(json.dumps(['other/repo', cb.process_start(os.getppid()),
                                                            f'cbx-{int(time.time())}-s', 'base']))
        with mock.patch.object(cb, 'host_memory', lambda field='MemTotal': (247 if field == 'MemTotal' else 128) * GIB):
            self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(5)}))
        self.assertEqual(len(self.started), 5)

    def test_register_refuses_more_vms_than_cpu_threads(self):
        args = argparse.Namespace(target='me/new', slots=5, allow_public=False, allow_forks=False)
        with mock.patch.object(cb.os, 'cpu_count', lambda: 4), \
                mock.patch.object(cb, 'host_memory', lambda field='MemTotal': 1024 * GIB), \
                mock.patch.object(cb, 'guard') as guard, self.assertRaises(SystemExit) as refused:
            cb.register(args)
        self.assertIn('4 CPU threads', str(refused.exception))
        self.assertNotIn('COLDBOX_MEMORY', str(refused.exception))
        guard.assert_not_called()

    def test_vms_still_on_the_host_count_against_the_ceiling(self):
        with mock.patch.object(cb, 'vm_memory', lambda: GIB), mock.patch.object(cb, 'host_vms', lambda: {f'cbx-{n}' for n in range(11)}):
            self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(5)}))
        self.assertEqual(len(self.started), 2)

    def test_a_burst_still_booting_counts_on_top_of_vms_already_on_the_host(self):
        folder = self.state / 'workers'
        folder.mkdir()
        (folder / str(os.getppid())).write_text(json.dumps(['other/repo', cb.process_start(os.getppid()), 'cbx-booting', 'burst']))
        with mock.patch.object(cb, 'vm_memory', lambda: GIB), mock.patch.object(cb, 'host_vms', lambda: {f'cbx-{n}' for n in range(11)}):
            self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(5)}))
        self.assertEqual(len(self.started), 1)

    def test_a_base_worker_and_its_vm_count_once(self):
        folder = self.state / 'workers'
        folder.mkdir()
        (folder / str(os.getpid())).write_text(json.dumps(['x/y', cb.process_start(os.getpid()), 'cbx-base', 'base']))
        with mock.patch.object(cb, 'host_vms', lambda: {'cbx-base'}), \
                mock.patch.object(cb, 'registrations', lambda: [dict(active='active', slots=3, target='x/y')]), \
                mock.patch.object(cb, 'rooms', wraps=cb.rooms) as planned:
            self.assertEqual(cb.host_load(cb.live_workers(folder)), (0, 1))
            self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(1)}))
        self.assertEqual(planned.call_args.args[0], 1)

    def test_a_base_worker_counts_its_vm_once_while_it_boots_waits_and_runs_a_job(self):
        folder = self.state / 'workers'
        cb.note_worker(os.getpid(), 'x/y', 'cbx-base', 'base')
        log = self.state / 'receipts' / 'cbx-base.log'
        log.parent.mkdir()
        with mock.patch.object(cb, 'host_vms', lambda: {'cbx-base'}), \
                mock.patch.object(cb, 'registrations', lambda: [dict(active='active', slots=1, target='x/y')]):
            for output, busy in ((None, 0), (b'Listening for Jobs\n', 0), (b'Running job: test\n', 1)):
                if output:
                    log.write_bytes(output)
                with self.subTest(output=output):
                    workers = cb.live_workers(folder)
                    self.assertEqual(cb.capacity_used(workers), busy)
                    self.assertEqual(sum(cb.host_load(workers)), 1)

    def test_pruning_keeps_a_live_workers_record_it_cannot_read(self):
        folder = self.state / 'workers'
        folder.mkdir()
        newer = json.dumps(dict(target='x/y', start=cb.process_start(os.getpid()), name='cbx-base', kind='base'))
        (folder / str(os.getpid())).write_text(newer)
        (folder / '999999999').write_text(newer)
        self.assertEqual(cb.live_workers(folder), [])
        self.assertTrue((folder / str(os.getpid())).exists())
        self.assertFalse((folder / '999999999').exists())

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

    def test_register_records_the_public_opt_in_and_names_who_runs_at_once(self):
        args = argparse.Namespace(target='me/app', slots=0, allow_public=True, allow_forks=False)
        commands = []
        with mock.patch.object(cb, 'guard', lambda target, allow: ['me']), \
                mock.patch.object(cb, 'instance', lambda target: 'me-app'), \
                mock.patch.object(cb.subprocess, 'run', lambda command, **kw: commands.append((command, kw))):
            _, out = quiet(cb.register, args)
        dropin = next(kw['input'] for command, kw in commands if command[:2] == ['sudo', 'tee'])
        self.assertIn(['Environment', 'COLDBOX_ALLOW_PUBLIC=1'], [line.split('=', 1) for line in dropin.splitlines()])
        self.assertIn('Jobs triggered by me (write access) run at once', out)

    def test_register_refuses_public_with_forks_and_never_takes_the_opt_in_from_the_environment(self):
        args = argparse.Namespace(target='me/app', slots=1, allow_public=True, allow_forks=True)
        with mock.patch.object(cb, 'guard') as guard, self.assertRaisesRegex(SystemExit, 'cannot be combined'):
            cb.register(args)
        guard.assert_not_called()
        seen = {}
        with mock.patch.dict(os.environ, COLDBOX_ALLOW_PUBLIC='1'), mock.patch.object(cb, 'incus_group_exec'), \
                mock.patch.object(cb, 'register', lambda a: seen.setdefault('register', a.allow_public)), \
                mock.patch.object(cb, 'serve', lambda a: seen.setdefault('serve', a.allow_public)):
            cb.main(['register', 'me/app'])
            cb.main(['serve', 'me/app'])
        self.assertEqual(seen, {'register': False, 'serve': True})

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

    def test_a_record_keeps_the_writers_its_worker_trusts_and_old_records_still_read(self):
        cb.note_worker(os.getpid(), 'me/app', 'cbx-1', 'base', ['me'])
        self.assertEqual([w.writers for w in cb.live_workers(self.state / 'workers')], [['me']])
        (self.state / 'workers' / str(os.getpid())).write_text(
            json.dumps(['me/app', cb.process_start(os.getpid()), 'cbx-1', 'base']))
        self.assertEqual([(w.name, w.writers) for w in cb.live_workers(self.state / 'workers')], [('cbx-1', None)])

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
        with mock.patch.object(cb, 'host_vms', lambda: {f'cbx-{n}' for n in range(40)}):
            self.assertFalse(cb.admit('me/app', 'cbx-solo', 'once'))
        self.assertFalse((self.state / 'workers' / str(os.getpid())).exists())
        self.assertTrue(cb.admit('me/app', 'cbx-solo', 'once'))
        self.assertEqual([w.kind for w in cb.live_workers(self.state / 'workers')], ['once'])

    def test_a_slot_vm_refills_only_below_the_core_bound(self):
        with mock.patch.object(cb, 'registrations', lambda: [dict(active='active', slots=1, target='x/y')]), \
                mock.patch.object(cb, 'host_memory', lambda field='MemTotal': (1024 if field == 'MemTotal' else 1000) * GIB):
            with mock.patch.object(cb, 'host_vms', lambda: {f'cbx-{n}' for n in range(12)}):
                self.assertTrue(cb.admit('x/y', 'cbx-base', 'base'))
            with mock.patch.object(cb, 'host_vms', lambda: {f'cbx-{n}' for n in range(13)}):
                self.assertFalse(cb.admit('x/y', 'cbx-base', 'base'))

    def test_slot_vms_count_against_the_core_bound_whether_idle_or_busy(self):
        workers = [argparse.Namespace(pid=100 + n, target='x/y', name=f'cbx-1-w{n}', kind='base', writers=None)
                   for n in range(38)]
        receipts = self.state / 'receipts'
        receipts.mkdir()
        for worker in workers:
            (receipts / f'{worker.name}.log').write_text('Listening for Jobs\n')
        with mock.patch.object(cb, 'live_workers', lambda folder: workers), \
                mock.patch.object(cb, 'host_vms', lambda: {worker.name for worker in workers}), \
                mock.patch.object(cb, 'registrations', lambda: [dict(active='active', slots=38, target='x/y')]):
            for log, busy, started in (('Listening for Jobs\n', 38, 0), ('Running job: verify\n', 38, 0)):
                for worker in workers:
                    (receipts / f'{worker.name}.log').write_text(log)
                self.started.clear()
                with self.subTest(busy=busy), mock.patch.object(cb, 'rooms', wraps=cb.rooms) as counted:
                    self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(3)}))
                    self.assertEqual(counted.call_args.args[0], busy)
                    self.assertEqual(len(self.started), started)

    def test_bursts_beside_slots_idle_or_busy_count_real_memory(self):
        workers = [argparse.Namespace(pid=100 + n, target='x/y', name=f'cbx-1-w{n}', kind='base', writers=None)
                   for n in range(4)]
        receipts = self.state / 'receipts'
        receipts.mkdir()
        for worker in workers:
            (receipts / f'{worker.name}.log').write_text('Listening for Jobs\n')
        for log, busy, free, room in (('Listening for Jobs\n', 0, 79, 3), ('Listening for Jobs\n', 0, 78, 2),
                                      ('Running job: verify\n', 4, 79, 3), ('Running job: verify\n', 4, 78, 2)):
            self.started.clear()
            for worker in workers:
                (receipts / f'{worker.name}.log').write_text(log)
            with self.subTest(busy=busy, free=free), \
                    mock.patch.object(cb, 'live_workers', lambda folder, prune=True: workers), \
                    mock.patch.object(cb, 'host_vms', lambda: {worker.name for worker in workers}), \
                    mock.patch.object(cb, 'registrations', lambda: [dict(active='active', slots=4, target='x/y')]), \
                    mock.patch.object(cb, 'host_memory',
                                      lambda field='MemTotal': (247 if field == 'MemTotal' else free) * GIB), \
                    mock.patch.object(cb, 'rooms', wraps=cb.rooms) as counted:
                self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(3)}))
                self.assertEqual(counted.call_args.args[0], len(workers))
                self.assertEqual(len(self.started), room)
                self.assertEqual(cb.vm_room(), (busy, busy + room))
                self.assertEqual(cb.admit('x/y', 'cbx-1-burst', 'once'), bool(room))

    def test_warm_slots_past_the_busy_ceiling_start_while_real_ram_is_free(self):
        with mock.patch.object(cb, 'registrations', lambda: [dict(active='active', slots=20, target='x/y')]):
            self.assertTrue(cb.admit('x/y', 'cbx-base', 'base'))

    def test_on_an_idle_host_admit_starts_any_vm_size_fits_accepts(self):
        tmp = Path(tempfile.mkdtemp())
        files = {'/proc/meminfo': tmp / 'meminfo', '/proc/spl/kstat/zfs/arcstats': tmp / 'arcstats',
                 '/sys/module/zfs/parameters/zfs_arc_pc_percent': tmp / 'pc'}
        (tmp / 'pc').write_text('0\n')
        real_path = cb.Path

        def idle(total, available, arc, arc_min, clean=None):
            clean = arc if clean is None else clean
            files['/proc/meminfo'].write_text(f'MemTotal: {total // 1024} kB\nMemAvailable: {available // 1024} kB\n')
            files['/proc/spl/kstat/zfs/arcstats'].write_text(
                f'13 1 0x01 3 816 1 2\nname type data\nhits 4 9\nsize 4 {arc}\nc_min 4 {arc_min}\n'
                f'mru_evictable_data 4 {clean - 3}\nmru_evictable_metadata 4 1\nmfu_evictable_data 4 1\n'
                f'mfu_evictable_metadata 4 1\n')

        with mock.patch.object(cb, 'Path', lambda path, *rest: real_path(files.get(str(path), path), *rest)), \
                mock.patch.object(cb, 'host_memory', HOST_MEMORY):
            measured = 8104764 * 1024
            idle(measured, 4902292 * 1024, 2522034304, 259352448)
            with mock.patch.object(cb, 'vm_memory', lambda: 3 * GIB):
                self.assertTrue(cb.fits(1, 3 * GIB, measured))
                self.assertTrue(cb.admit('x/y', 'cbx-base', 'base'))
            for total in [measured] + [size * GIB for size in (4, 5, 6, 8, 12, 16, 17, 32, 64, 248)]:
                arc_min = total // 32
                arc = arc_min + cb.memory_floor(total)
                idle(total, total - 1536 * 2**20 - (arc - arc_min), arc, arc_min)
                sizes = range(256 * 2**20, total, 256 * 2**20)
                for vm in [size for size in sizes if cb.fits(1, size, total)]:
                    with self.subTest(total=total, vm=vm), mock.patch.object(cb, 'vm_memory', lambda: vm):
                        self.assertTrue(cb.admit('x/y', 'cbx-base', 'base'))

    def test_dirty_arc_that_zfs_cannot_evict_is_not_counted_as_available(self):
        tmp = Path(tempfile.mkdtemp())
        arcstats = tmp / 'arcstats'
        real_path = cb.Path
        (tmp / 'meminfo').write_text(f'MemTotal: {64 * GIB // 1024} kB\nMemAvailable: {10 * GIB // 1024} kB\n')
        files = {'/proc/meminfo': tmp / 'meminfo', '/proc/spl/kstat/zfs/arcstats': arcstats,
                 '/sys/module/zfs/parameters/zfs_arc_pc_percent': tmp / 'pc'}
        with mock.patch.object(cb, 'Path', lambda path, *rest: real_path(files.get(str(path), path), *rest)):
            for clean, available, pc in ((GIB, 11 * GIB, '0'), (20 * GIB, 28 * GIB, '0'), (None, 10 * GIB, '0'),
                                         (20 * GIB, 10 * GIB, '25'), (20 * GIB, 10 * GIB, None)):
                if pc is None:
                    (tmp / 'pc').unlink(missing_ok=True)
                else:
                    (tmp / 'pc').write_text(f'{pc}\n')
                lines = '' if clean is None else ''.join(f'{name} 4 {value}\n' for name, value in (
                    ('mru_evictable_data', clean), ('mru_evictable_metadata', 0), ('mfu_evictable_data', 0),
                    ('mfu_evictable_metadata', 0)))
                arcstats.write_text(f'13 1 0x01\nname type data\nsize 4 {20 * GIB}\nc_min 4 {2 * GIB}\n{lines}')
                with self.subTest(clean=clean, pc=pc):
                    self.assertEqual(HOST_MEMORY('MemAvailable'), available)

    def test_a_large_arc_counts_at_most_the_floor_so_a_burst_cannot_eat_into_it(self):
        tmp = Path(tempfile.mkdtemp())
        files = {'/proc/meminfo': tmp / 'meminfo', '/proc/spl/kstat/zfs/arcstats': tmp / 'arcstats',
                 '/sys/module/zfs/parameters/zfs_arc_pc_percent': tmp / 'pc'}
        files['/proc/meminfo'].write_text(f'MemTotal: {64 * GIB // 1024} kB\nMemAvailable: {10 * GIB // 1024} kB\n')
        files['/proc/spl/kstat/zfs/arcstats'].write_text(
            f'13 1 0x01\nname type data\nsize 4 {42 * GIB}\nc_min 4 {2 * GIB}\nmru_evictable_data 4 {40 * GIB}\n'
            'mru_evictable_metadata 4 0\nmfu_evictable_data 4 0\nmfu_evictable_metadata 4 0\n')
        files['/sys/module/zfs/parameters/zfs_arc_pc_percent'].write_text('0\n')
        real_path = cb.Path
        with mock.patch.object(cb, 'Path', lambda path, *rest: real_path(files.get(str(path), path), *rest)), \
                mock.patch.object(cb, 'host_memory', HOST_MEMORY), mock.patch.object(cb, 'vm_memory', lambda: 6 * GIB):
            self.assertEqual(HOST_MEMORY('MemAvailable'), 18 * GIB)
            self.assertTrue(cb.admit('x/y', 'cbx-base', 'base'))
            (self.state / 'workers' / str(os.getppid())).write_text(
                json.dumps(['other/repo', cb.process_start(os.getppid()), f'cbx-{int(time.time())}-b', 'burst']))
            self.assertFalse(cb.admit('x/y', 'cbx-base', 'base'))

    def test_every_vm_still_booting_needs_its_full_memory_free_now(self):
        folder = self.state / 'workers'
        folder.mkdir()
        (folder / str(os.getppid())).write_text(
            json.dumps(['other/repo', cb.process_start(os.getppid()), f'cbx-{int(time.time())}-b', 'burst']))
        for free, admitted in ((63, True), (62, False)):
            with mock.patch.object(cb, 'host_memory', lambda field='MemTotal': (247 if field == 'MemTotal' else free) * GIB):
                self.assertEqual(cb.admit('x/y', 'cbx-base', 'base'), admitted, free)

    def test_slot_refills_reserve_full_memory_for_booting_bursts_and_real_memory_for_busy_ones(self):
        folder = self.state / 'workers'
        folder.mkdir()
        (folder / str(os.getppid())).write_text(
            json.dumps(['other/repo', cb.process_start(os.getppid()), 'cbx-1-b', 'burst']))
        (self.state / 'receipts').mkdir()
        for log, slots, free, admitted in (('', 1, 63, True), ('', 1, 62, False), ('', 15, 63, True), ('', 15, 62, False),
                                           *(('Running job: verify\n', slots, free, admitted) for slots in (1, 15)
                                             for free, admitted in ((47, True), (46, False)))):
            (self.state / 'receipts' / 'cbx-1-b.log').write_text(log)
            with self.subTest(booting=not log, slots=slots, free=free), \
                    mock.patch.object(cb, 'registrations', lambda: [dict(active='active', slots=slots, target='x/y')]), \
                    mock.patch.object(cb, 'host_memory', lambda field='MemTotal': (247 if field == 'MemTotal' else free) * GIB):
                self.assertEqual(cb.admit('x/y', 'cbx-base', 'base'), admitted)

    def test_slot_refills_beside_vms_already_started_count_their_real_memory(self):
        folder = self.state / 'workers'
        folder.mkdir()
        (folder / str(os.getppid())).write_text(
            json.dumps(['other/repo', cb.process_start(os.getppid()), 'cbx-1-b', 'burst']))
        (folder / '1').write_text(json.dumps(['me/app', cb.process_start(1), 'cbx-2-s', 'base']))
        (self.state / 'ready').mkdir()
        for name in ('cbx-1-b', 'cbx-2-s'):
            (self.state / 'ready' / name).touch()
        for slots, free, admitted in ((2, 47, True), (2, 46, False), (15, 47, True), (15, 46, False)):
            with self.subTest(slots=slots, free=free), mock.patch.object(cb, 'host_vms', lambda: {'cbx-1-b', 'cbx-2-s'}), \
                    mock.patch.object(cb, 'registrations', lambda: [dict(active='active', slots=slots, target='me/app')]), \
                    mock.patch.object(cb, 'host_memory', lambda field='MemTotal': (247 if field == 'MemTotal' else free) * GIB):
                self.assertEqual(cb.admit('me/app', 'cbx-base', 'base'), admitted)

    def test_slot_refills_reserve_full_memory_for_ad_hoc_vms_whose_launcher_exited(self):
        for slots, free, admitted in ((1, 63, True), (1, 62, False), (15, 63, True), (15, 62, False)):
            with self.subTest(slots=slots, free=free), mock.patch.object(cb, 'host_vms', lambda: {'cbx-1-adhoc'}), \
                    mock.patch.object(cb, 'registrations', lambda: [dict(active='active', slots=slots, target='x/y')]), \
                    mock.patch.object(cb, 'host_memory', lambda field='MemTotal': (247 if field == 'MemTotal' else free) * GIB):
                self.assertEqual(cb.admit('x/y', 'cbx-base', 'base'), admitted)

    def test_a_slot_vm_counts_as_busy_once_its_runner_takes_a_job(self):
        folder = self.state / 'workers'
        cb.note_worker(os.getpid(), 'x/y', 'cbx-base', 'base')
        receipts = self.state / 'receipts'
        receipts.mkdir()
        with mock.patch.object(cb, 'registrations', lambda: []):
            self.assertEqual(cb.host_load(cb.live_workers(folder)), (0, 1))
            (receipts / 'cbx-base.log').write_bytes(b'2026-11-09 14:53:19Z: Listening for Jobs\n')
            self.assertEqual(cb.host_load(cb.live_workers(folder)), (0, 1))
            (receipts / 'cbx-base.log').write_bytes(b'2026-11-09 14:58:02Z: Running job: verify\n')
            self.assertEqual(cb.host_load(cb.live_workers(folder)), (1, 0))

    def test_every_finished_burst_is_reaped_in_one_pass(self):
        done = [argparse.Namespace(pid=n, poll=lambda: 0, returncode=None) for n in (1, 2, 3)]
        self.assertEqual(self.scale(FakeGitHub({'queued': [], 'in_progress': []}, {}), done), [])

    def serve_burst_only(self, github, seconds=0):
        """Run the service on a clock that only its own sleep moves, and stop it after that many seconds."""
        args = argparse.Namespace(**dict(vars(self.args), slots=0, once=False, worker=False, idle=0))
        handlers = cb.signal.getsignal(cb.signal.SIGINT), cb.signal.getsignal(cb.signal.SIGTERM)
        self.addCleanup(cb.signal.signal, cb.signal.SIGINT, handlers[0])
        self.addCleanup(cb.signal.signal, cb.signal.SIGTERM, handlers[1])
        self.addCleanup(cb.STOPPING.clear)
        start = time.time()
        clock = [start]

        def sleep(_):
            clock[0] += 1
            if clock[0] - start > seconds:
                cb.signal.getsignal(cb.signal.SIGTERM)(cb.signal.SIGTERM, None)

        with mock.patch.object(cb, 'GitHub', lambda: github), mock.patch.object(cb.time, 'sleep', sleep), \
                mock.patch.object(cb.time, 'time', lambda: clock[0]), \
                mock.patch.object(cb, 'autoscale', wraps=cb.autoscale) as autoscale:
            code = quiet(cb.serve, args)[0]
        self.passes = autoscale.call_count
        return code

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
                mock.patch.object(cb, 'rooms', wraps=cb.rooms) as counted:
            self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(1)}))
        self.assertEqual(counted.call_args.args[0], 0)
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

    def test_primary_probe_counts_each_job_once_and_every_new_reservation(self):
        arriving = argparse.Namespace(pid=2, target='me/app', name='cbx-2-b', kind='burst', writers=None)
        with mock.patch.object(cb, 'live_workers', return_value=[arriving]), \
                mock.patch.object(cb, 'host_vms', side_effect=[{'cbx-1-a'}, {'cbx-2-b'}, {'cbx-2-b'}, {'cbx-2-b'}]):
            self.assertEqual(cb.vm_room()[0], 1)
        reserved = [argparse.Namespace(pid=n, target='me/app', name=f'cbx-{n}-r', kind='burst', writers=None)
                    for n in range(3)]
        with mock.patch.object(cb, 'live_workers', return_value=reserved), \
                mock.patch.object(cb, 'host_vms', return_value={'cbx-0-r', 'cbx-9-z'}):
            self.assertEqual(cb.vm_room()[0], 4)

    def test_primary_probe_answers_while_the_host_lock_is_held(self):
        held = cb.host_lock()
        result = []
        probe = threading.Thread(target=lambda: result.append(self.primary_probe(80)), daemon=True)
        try:
            probe.start()
            probe.join(10)
            self.assertFalse(probe.is_alive(), 'primary probe waited on the autoscale host lock')
        finally:
            held.close()
            probe.join(10)
        self.assertGreater(result[0]['vm_ceiling'], 0)

    def test_every_listed_host_starts_default_and_sized_bursts(self):
        jobs = queued_ago(1) + [dict(job, labels=['self-hosted', 'coldbox-large']) for job in queued_ago(1, 1)]
        with mock.patch.object(cb, 'ci_host', return_value=('overflow-host', 'primary-host')), \
                mock.patch.object(cb, 'vm_shape', lambda size: (None, {})):
            bursts = self.scale(FakeGitHub({'queued': [1]}, {1: jobs}))
        self.assertEqual(sorted(burst.size for burst in bursts), ['default', 'large', 'large'])

    def primary_probe(self, idle, target='x/y'):
        samples = ['cpu 0 0 0 0 0 0 0 0\n', f'cpu {100-idle} 0 0 {idle} 0 0 0 0\n']
        read_text = cb.Path.read_text
        with mock.patch.object(cb.Path, 'read_text', lambda p: samples.pop(0) if str(p) == '/proc/stat' else read_text(p)), \
                mock.patch.object(cb.time, 'sleep'), mock.patch('runpy.run_path', side_effect=lambda _: vars(cb)):
            return json.loads(quiet(exec, cb.HOST_PROBE, {'requested_target': target})[1])

    def test_primary_probe_measures_cpu_memory_and_booting_admission(self):
        self.assertEqual(self.primary_probe(80)['ramp_room'], self.primary_probe(80)['vm_ceiling'])
        self.assertAlmostEqual(self.primary_probe(80)['busy_cpus'], os.cpu_count() * 0.2)
        cap = self.state / 'ramp-cap.json'
        cap.write_text(json.dumps(dict(limit=0, expires=time.time()+60)))
        self.assertEqual((self.primary_probe(80)['ramp_room'], self.primary_probe(80)['reserve']), (0, 0))
        cap.write_text('{')
        self.assertIsNone(self.primary_probe(1)['ramp_room'])
        cap.unlink()
        with mock.patch.object(cb, 'host_memory', return_value=17 * GIB):
            self.assertEqual(self.primary_probe(80)['available_memory'], 17 * GIB)
        self.assertEqual(self.primary_probe(80)['booting'], 0)
        with mock.patch.object(cb.os, 'getloadavg', return_value=(109.0, 0, 0)):
            self.assertEqual(self.primary_probe(80)['load_avg'], 109.0)
        cb.note_worker(os.getpid(), 'me/app', f'cbx-{int(time.time())}-a', 'burst')
        self.assertEqual(self.primary_probe(80)['booting'], 1)

    def test_the_ranking_log_shows_the_load_each_host_reported(self):
        me = os.uname().nodename
        policy = self.state / 'ci-hosts.json'
        policy.write_text(json.dumps(dict(primary='primary-host', overflow=['host-a', me])))
        def evidence(target, host):
            if host == me:
                return None
            return dict(host=host, target=target, measured_at=time.time(), cpus=24, busy_cpus=20.5,
                        available_memory=144 * GIB, vm_memory=16 * GIB, vms=15, vm_ceiling=20, reserve=0,
                        ramp_room=20, booting=0, load_avg=109.0)
        with mock.patch.object(cb, 'CI_HOSTS', policy), mock.patch.object(cb, 'host_evidence', evidence):
            ranked, output = quiet(cb.least_loaded, ['host-a', me], 'me/app')
        self.assertEqual(ranked, [])
        self.assertIn('least-load ranking: host-a room -85 (busy 20.5, load 109.0, booting 0, 15/20 VMs, 24 CPUs); '
                      f'{me} unmeasured', output)

    def test_primary_with_room_but_stopped_repo_service_is_unavailable(self):
        samples = ['cpu 0 0 0 0 0 0 0 0', 'cpu 20 0 0 80 0 0 0 0']
        read_text = cb.Path.read_text
        with mock.patch.object(cb, 'registrations', return_value=[dict(target='x/y', active='inactive', slots=0)]), \
                mock.patch('runpy.run_path', side_effect=lambda _: vars(cb)), \
                mock.patch.object(cb.Path, 'read_text', lambda p: samples.pop(0) if str(p) == '/proc/stat' else read_text(p)), \
                mock.patch.object(cb.time, 'sleep'), self.assertRaises(RuntimeError):
            quiet(exec, cb.HOST_PROBE, {'requested_target': 'x/y'})

    def test_primary_probe_does_not_report_full_when_cpu_signal_is_invalid(self):
        for samples in (['cpu 1 0 0 1 0 0 0 0'] * 2, ['bad'], [OSError('unreadable')]):
            with mock.patch.object(cb.Path, 'read_text', side_effect=samples), \
                    mock.patch.object(cb.time, 'sleep'), mock.patch('runpy.run_path', return_value=vars(cb)), \
                    self.assertRaises((OSError, ValueError)):
                quiet(exec, cb.HOST_PROBE, {'requested_target': 'x/y'})

    def test_primary_probe_uses_bounded_ssh_and_rejects_bad_signals(self):
        with mock.patch.object(cb.subprocess, 'run') as run:
            for output in ('full', 'room', '', 'full extra'):
                run.return_value = argparse.Namespace(stdout=output)
                self.assertIsNone(cb.host_evidence('me/app', 'primary-host'))
            self.assertEqual(run.call_args.kwargs['timeout'], 8)
            self.assertIn('BatchMode=yes', run.call_args.args[0])
            for error in (subprocess.TimeoutExpired('ssh', 8), subprocess.CalledProcessError(255, 'ssh')):
                run.side_effect = error
                self.assertIsNone(cb.host_evidence('me/app', 'primary-host'))

    def test_a_freshly_queued_job_gets_a_burst_at_once(self):
        self.assertEqual(len(self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued_ago(1)}))), 1)

    def test_every_host_polls_every_30_s_with_nothing_queued(self):
        self.serve_burst_only(FakeGitHub({'queued': [], 'in_progress': []}, {}), seconds=180)
        self.assertEqual(self.passes, 7)

    def reserve(self, threads):
        path = self.state / 'reserve'
        path.write_text(f'{threads}\n')
        patcher = mock.patch.object(cb, 'RESERVE', path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_cpu_reserve_leaves_its_threads_to_other_work(self):
        self.reserve(31)
        bursts, log = [], io.StringIO()
        with contextlib.redirect_stderr(log):
            cb.autoscale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(5)}), self.args, bursts)
        self.assertEqual(len(bursts), 3)
        self.assertIn('load 7.0 on 40 CPU threads (31 reserved)', log.getvalue())

    def test_the_reserved_work_using_its_threads_does_not_count_against_ci_twice(self):
        self.reserve(20)
        with mock.patch.object(cb.os, 'getloadavg', lambda: (25.0, 25.0, 25.0)), \
                mock.patch.object(cb, 'host_cpu_load', lambda: 25.0):
            self.assertEqual(len(self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(5)}))), 5)

    def test_a_reserved_host_with_no_free_thread_starts_nothing(self):
        self.reserve(20)
        with mock.patch.object(cb.os, 'getloadavg', lambda: (39.5, 39.5, 39.5)), \
                mock.patch.object(cb, 'host_cpu_load', lambda: 39.5):
            self.assertEqual(self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(5)})), [])

    def test_a_reserve_of_every_thread_starts_nothing(self):
        self.reserve(40)
        self.assertEqual(self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(5)})), [])

    def test_a_reserve_far_above_the_cpu_count_starts_nothing(self):
        self.reserve('9' * 400)
        self.assertEqual(self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(5)})), [])

    def test_a_malformed_reserve_file_fails_the_pass_instead_of_bursting(self):
        self.reserve('half')
        with self.assertRaises(ValueError):
            self.scale(FakeGitHub({'queued': [1], 'in_progress': []}, {1: queued(5)}))
        self.assertEqual(self.started, [])

    def test_nothing_queued_starts_nothing(self):
        self.scale(FakeGitHub({'queued': [], 'in_progress': [1]}, {1: [{'status': 'in_progress', 'labels': ['coldbox']}]}))
        self.assertEqual(self.started, [])

    PUBLIC = {'full_name': 'me/app', 'visibility': 'public', 'owner': {'type': 'User'}}

    def workflow_run(self, run_id, event, actor, head='me/app', rerun_by=None):
        return {'id': run_id, 'event': event, 'head_repository': {'full_name': head}, 'actor': {'login': actor},
                'triggering_actor': {'login': rerun_by or actor}}

    def scale_public(self, runs, policy=cb.APPROVAL, allow_public=True, awaiting_approval=()):
        self.args.allow_public = allow_public
        github = FakeGitHub({'queued': runs, 'in_progress': [], 'action_required': list(awaiting_approval)},
                            {run['id']: queued(1) for run in [*runs, *awaiting_approval]}, repo=self.PUBLIC,
                            policy=policy)
        return quiet(cb.autoscale, github, self.args, [])[1]

    def test_public_opt_in_scales_the_owners_push_and_pull_request(self):
        self.scale_public([self.workflow_run(1, 'push', 'me'), self.workflow_run(2, 'pull_request', 'Me'),
                           self.workflow_run(3, 'workflow_dispatch', 'me')])
        self.assertEqual(len(self.started), 3)

    def test_public_opt_in_holds_an_outside_fork_pull_request_until_github_approval(self):
        waiting = self.workflow_run(1, 'pull_request', 'stranger', head='stranger/app')
        self.scale_public([], awaiting_approval=[waiting])
        self.assertEqual(self.started, [])
        self.scale_public([waiting])
        self.assertEqual(len(self.started), 1)

    def test_public_opt_in_never_scales_an_outside_users_job_that_github_does_not_gate(self):
        log = self.scale_public([self.workflow_run(1, 'pull_request_target', 'stranger', head='stranger/app'),
                                 self.workflow_run(2, 'issue_comment', 'stranger'), self.workflow_run(3, 'push', 'stranger'),
                                 self.workflow_run(4, 'pull_request', 'stranger')])
        self.assertEqual(self.started, [])
        self.assertIn('not scaling for run 2 on me/app: issue_comment event triggered by stranger, who has no write '
                      'access', log)

    def waiting_runners(self, idle_rules=('me', 'pal'), busy_rules=('me', 'pal')):
        cb.note_worker(os.getpid(), 'me/app', 'cbx-busy', 'base', list(busy_rules))
        cb.note_worker(os.getppid(), 'me/app', 'cbx-idle', 'burst', list(idle_rules))
        return [{'id': 1, 'name': 'cbx-idle', 'busy': False, 'status': 'online'},
                {'id': 2, 'name': 'cbx-busy', 'busy': True, 'status': 'online'},
                {'id': 3, 'name': 'cbx-other-host', 'busy': False, 'status': 'online'}]

    def test_changed_write_access_releases_only_idle_runners_and_keeps_running_jobs(self):
        self.args.allow_public = True
        github = FakeGitHub({'queued': [], 'in_progress': []}, {}, self.waiting_runners(), repo=self.PUBLIC,
                            writers=('me', 'pal'))
        deleted, signaled = [], []
        with mock.patch.object(cb, 'gh', lambda *a: deleted.append(a)), \
                mock.patch.object(cb.os, 'kill', lambda pid, sig: signaled.append((pid, sig))):
            cb.autoscale(github, self.args, [])
            self.assertEqual((deleted, signaled), ([], []))
            github.writers = ('me',)
            log = quiet(cb.autoscale, github, self.args, [])[1]
        self.assertEqual(deleted, [('-X', 'DELETE', 'repos/me/app/actions/runners/1')])
        self.assertEqual(signaled, [(os.getppid(), cb.signal.SIGUSR1)])
        self.assertIn('released 1 waiting runner(s) on me/app that held out-of-date approval rules', log)

    def test_a_failed_release_is_retried_on_the_next_pass(self):
        self.args.allow_public = True
        github = FakeGitHub({'queued': [], 'in_progress': []}, {}, self.waiting_runners(), repo=self.PUBLIC,
                            writers=('me',))
        calls, signaled = [], []

        def flaky(*a):
            calls.append(a)
            if len(calls) == 1:
                raise RuntimeError('gh api: HTTP 502')

        with mock.patch.object(cb, 'gh', flaky), \
                mock.patch.object(cb.os, 'kill', lambda pid, sig: signaled.append(pid)):
            quiet(cb.autoscale, github, self.args, [])
            self.assertEqual(signaled, [])
            quiet(cb.autoscale, github, self.args, [])
        self.assertEqual(len(calls), 2)
        self.assertEqual(signaled, [os.getppid()])

    def test_a_runner_registered_with_old_rules_after_a_change_is_still_released(self):
        self.args.allow_public = True
        github = FakeGitHub({'queued': [], 'in_progress': []}, {}, [], repo=self.PUBLIC, writers=('me',))
        cb.note_worker(os.getppid(), 'me/app', 'cbx-idle', 'burst', ['me', 'pal'])
        deleted = []
        with mock.patch.object(cb, 'gh', lambda *a: deleted.append(a)), mock.patch.object(cb.os, 'kill'):
            quiet(cb.autoscale, github, self.args, [])
            self.assertEqual(deleted, [])
            github.runners = [{'id': 1, 'name': 'cbx-idle', 'busy': False, 'status': 'online'}]
            quiet(cb.autoscale, github, self.args, [])
        self.assertEqual(deleted, [('-X', 'DELETE', 'repos/me/app/actions/runners/1')])

    def test_runners_holding_current_rules_are_left_alone(self):
        self.args.allow_public = True
        github = FakeGitHub({'queued': [], 'in_progress': []}, {}, self.waiting_runners(('me',), ('me',)),
                            repo=self.PUBLIC, writers=('me',))
        with mock.patch.object(cb, 'gh', side_effect=AssertionError('deleted a runner')), \
                mock.patch.object(cb.os, 'kill', side_effect=AssertionError('signaled a worker')):
            log = quiet(cb.autoscale, github, self.args, [])[1]
        self.assertNotIn('released', log)

    def test_a_failed_approval_gate_also_releases_idle_runners(self):
        self.args.allow_public = True
        github = FakeGitHub({'queued': [], 'in_progress': []}, {}, self.waiting_runners(), repo=self.PUBLIC,
                            policy='first_time_contributors')
        deleted = []
        with mock.patch.object(cb, 'gh', lambda *a: deleted.append(a)), mock.patch.object(cb.os, 'kill'), \
                self.assertRaises(cb.Unsafe):
            quiet(cb.autoscale, github, self.args, [])
        self.assertEqual(deleted, [('-X', 'DELETE', 'repos/me/app/actions/runners/1')])

    def test_a_writer_re_running_an_outside_run_counts_as_the_writer(self):
        self.scale_public([self.workflow_run(1, 'pull_request_target', 'stranger', head='stranger/app', rerun_by='me')])
        self.assertEqual(len(self.started), 1)

    def test_public_repo_without_the_opt_in_or_without_github_approval_is_not_scaled(self):
        owner = [self.workflow_run(1, 'push', 'me')]
        with self.assertRaisesRegex(ValueError, 'not scaling it'):
            self.scale_public(owner, allow_public=False)
        with self.assertRaisesRegex(cb.Unsafe, '-f approval_policy=all_external_contributors'), \
                mock.patch.object(cb, 'release_waiting'):
            self.scale_public(owner, policy='first_time_contributors')
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
        self.assertEqual(cb.free_runners(runners, bursts, now), {'default': 3})


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
    def setUp(self):
        patcher = mock.patch.object(cb, 'CLEANUP', cb.threading.Event())
        patcher.start()
        self.addCleanup(patcher.stop)

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


class RunnerUpdates(unittest.TestCase):
    def test_busy_job_survives_replace_and_next_job_uses_new_code(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            installed = root / 'coldbox'
            driver = root / 'driver.py'
            installed.write_bytes((ROOT / 'coldbox').read_bytes())
            driver.write_text('''import argparse, importlib.machinery, importlib.util, sys, time
from pathlib import Path
path = Path(sys.argv[1])
loader = importlib.machinery.SourceFileLoader('worker', str(path))
cb = importlib.util.module_from_spec(importlib.util.spec_from_loader('worker', loader))
loader.exec_module(cb)
root = path.parent
cb.guard = lambda *a: None
cb.admit = lambda *a: True
cb.ci_host = lambda: ('ci-primary', 'ci-primary')
cb.runner_admission = lambda *a: {}
cb.gh = lambda *a, **kw: {'runner': {'id': 7}, 'encoded_jit_config': 'c'}
def job(*a, **kw):
    (root / 'started').write_text('old')
    while not (root / 'finish').exists():
        time.sleep(.01)
    (root / 'completed').write_text('old')
    return {'verdict': 'pass', 'job_seconds': 1}
cb.job = job
cb.serve(argparse.Namespace(target='owner/repo', worker=True, slots=1, ttl=60,
    allow_public=False, allow_forks=False, once=False, idle=0, name=None, size='default'))
''')
            process = subprocess.Popen([sys.executable, str(driver), str(installed)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.addCleanup(lambda: process.poll() is None and process.kill())
            try:
                import time
                deadline = time.monotonic() + 10
                while not (root / 'started').exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue((root / 'started').exists())
                replacement = root / 'next'
                replacement.write_text("from pathlib import Path\nPath(__file__).with_name('next-job').write_text('new')\n")
                replacement.replace(installed)
                self.assertIsNone(process.poll())
                self.assertFalse((root / 'next-job').exists())
                (root / 'finish').touch()
                out, err = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, err.decode())
                self.assertEqual((root / 'completed').read_text(), 'old')
                self.assertEqual((root / 'next-job').read_text(), 'new')
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_replace_after_module_load_is_seen_before_first_registration(self):
        self.assert_reload_before_registration(False)

    def test_replace_during_guard_is_seen_before_first_registration(self):
        self.assert_reload_before_registration(True)

    def assert_reload_before_registration(self, during_guard):
        with tempfile.TemporaryDirectory() as folder:
            installed = Path(folder) / 'coldbox'
            replacement = "print('new worker')\n"
            installed.write_bytes((ROOT / 'coldbox').read_bytes()) if during_guard else installed.write_text(replacement)
            def guard(*_):
                if during_guard:
                    installed.write_text(replacement)
            args = argparse.Namespace(target='owner/repo', worker=True, slots=1, ttl=60,
                                      allow_public=False, allow_forks=False, once=False, idle=0, name=None, size='default')
            handler = cb.signal.getsignal(cb.signal.SIGTERM)
            idle_handler = cb.signal.getsignal(cb.signal.SIGUSR1)
            self.addCleanup(cb.signal.signal, cb.signal.SIGTERM, handler)
            self.addCleanup(cb.signal.signal, cb.signal.SIGUSR1, idle_handler)
            with mock.patch.object(cb, '__file__', str(installed)), \
                    mock.patch.object(cb.os, 'execv', side_effect=SystemExit(0)) as execute, \
                    mock.patch.object(cb, 'guard', side_effect=guard), mock.patch.object(cb, 'admit', return_value=True), \
                    mock.patch.object(cb, 'gh', side_effect=AssertionError('registered stale code')) as gh:
                with self.assertRaises(SystemExit) as stopped:
                    cb.serve(args)
            self.assertEqual(stopped.exception.code, 0)
            self.assertEqual(execute.call_args.args, (sys.executable,
                [sys.executable, str(installed), 'serve', 'owner/repo', '--worker', '--ttl', '60', '--slots', '1']))
            gh.assert_not_called()

    def test_transient_jit_failures_retry_with_backoff(self):
        for detail in ('HTTP 502: Bad Gateway', 'HTTP 503: No server is currently available',
                       'dial tcp: lookup api.github.com: temporary failure', 'connection reset by peer',
                       'Post https://api.github.com: EOF', 'error connecting to api.github.com'):
            with self.subTest(detail=detail), mock.patch.object(cb, 'gh_env', return_value=None), \
                    mock.patch.object(cb.subprocess, 'run', side_effect=[
                        subprocess.CompletedProcess([], 1, '', detail),
                        subprocess.CompletedProcess([], 0, '{"runners":[]}', ''),
                        subprocess.CompletedProcess([], 1, '', detail),
                        subprocess.CompletedProcess([], 0, '{"runners":[]}', ''),
                        subprocess.CompletedProcess([], 0, '{"encoded_jit_config":"new"}', '')]) as run, \
                    mock.patch.object(cb.time, 'sleep') as sleep:
                self.assertEqual(cb.gh('-X', 'POST', 'repos/owner/repo/actions/runners/generate-jitconfig',
                                       body={'name': 'cbx-unique'}), {'encoded_jit_config': 'new'})
                self.assertEqual(run.call_count, 5)
                self.assertEqual(sum('POST' in call.args[0] for call in run.call_args_list), 3)
                self.assertEqual(sleep.call_args_list, [mock.call(1), mock.call(2)])

    def test_persistent_transient_failure_exits_nonzero(self):
        for sig in (cb.signal.SIGTERM, cb.signal.SIGUSR1):
            self.addCleanup(cb.signal.signal, sig, cb.signal.getsignal(sig))
        results = [item for _ in range(5) for item in (
            subprocess.CompletedProcess([], 1, '', 'HTTP 503'),
            subprocess.CompletedProcess([], 0, '{"runners":[]}', ''))]
        with mock.patch.object(cb, 'incus_group_exec'), mock.patch.object(cb, 'gh_env', return_value=None), \
                mock.patch.object(cb, 'guard', return_value=None), mock.patch.object(cb, 'admit', return_value=True), \
                mock.patch.object(cb.subprocess, 'run', side_effect=results) as run, \
                mock.patch.object(cb.time, 'sleep') as sleep:
            self.assertEqual(cb.main(['serve', 'owner/repo', '--worker']), 125)
        self.assertEqual(run.call_count, 10)
        self.assertEqual(sum('POST' in call.args[0] for call in run.call_args_list), 5)
        self.assertTrue(all('generate-jitconfig' in call.args[0][4] for call in run.call_args_list
                            if 'POST' in call.args[0]))
        self.assertEqual(sleep.call_args_list, [mock.call(1), mock.call(2), mock.call(4), mock.call(8)])

    def test_lost_jit_response_removes_unused_registration_before_retry(self):
        results = [subprocess.TimeoutExpired('gh', 60),
                   subprocess.CompletedProcess([], 0, json.dumps({'runners': [
                       {'id': 7, 'name': 'cbx-unique', 'busy': False},
                       {'id': 8, 'name': 'cbx-other', 'busy': True}]}), ''),
                   subprocess.CompletedProcess([], 0, '', ''),
                   subprocess.CompletedProcess([], 0, '{"encoded_jit_config":"new"}', '')]
        with mock.patch.object(cb, 'gh_env', return_value=None), \
                mock.patch.object(cb.subprocess, 'run', side_effect=results) as run, \
                mock.patch.object(cb.time, 'sleep'):
            result = cb.gh('-X', 'POST', 'repos/owner/repo/actions/runners/generate-jitconfig',
                           body={'name': 'cbx-unique'})
        self.assertEqual(result, {'encoded_jit_config': 'new'})
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ['gh', 'api', '-X', 'POST', 'repos/owner/repo/actions/runners/generate-jitconfig', '--input', '-'],
            ['gh', 'api', 'repos/owner/repo/actions/runners?per_page=100&page=1'],
            ['gh', 'api', '-X', 'DELETE', 'repos/owner/repo/actions/runners/7'],
            ['gh', 'api', '-X', 'POST', 'repos/owner/repo/actions/runners/generate-jitconfig', '--input', '-']])

    def test_ambiguous_jit_registration_never_deletes_a_busy_runner(self):
        with mock.patch.object(cb, 'gh_env', return_value=None), \
                mock.patch.object(cb.subprocess, 'run', side_effect=[subprocess.TimeoutExpired('gh', 60),
                    subprocess.CompletedProcess([], 0, json.dumps({'runners': [
                        {'id': 7, 'name': 'cbx-unique', 'busy': True}]}), '')]) as run, \
                mock.patch.object(cb.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'is busy'):
                cb.gh('-X', 'POST', 'repos/owner/repo/actions/runners/generate-jitconfig', body={'name': 'cbx-unique'})
        self.assertEqual(run.call_count, 2)

    def test_final_lost_jit_response_is_reconciled_before_exit(self):
        results = [item for _ in range(5) for item in (
            subprocess.TimeoutExpired('gh', 60),
            subprocess.CompletedProcess([], 0, '{"runners":[]}', ''))]
        with mock.patch.object(cb, 'gh_env', return_value=None), \
                mock.patch.object(cb.subprocess, 'run', side_effect=results) as run, \
                mock.patch.object(cb.time, 'sleep'):
            with self.assertRaises(cb.GitHubTransient):
                cb.gh('-X', 'POST', 'repos/owner/repo/actions/runners/generate-jitconfig', body={'name': 'cbx-unique'})
        self.assertEqual(run.call_count, 10)
        self.assertEqual(run.call_args.args[0], ['gh', 'api', 'repos/owner/repo/actions/runners?per_page=100&page=1'])

    def test_transient_reconciliation_failure_stays_in_retry_loop(self):
        results = [subprocess.TimeoutExpired('gh', 60)] + [
            subprocess.CompletedProcess([], 1, '', 'HTTP 503')] * 2 + [
            subprocess.CompletedProcess([], 0, '{"runners":[]}', ''),
            subprocess.CompletedProcess([], 0, '{"encoded_jit_config":"new"}', '')]
        with mock.patch.object(cb, 'gh_env', return_value=None), \
                mock.patch.object(cb.subprocess, 'run', side_effect=results) as run, \
                mock.patch.object(cb.time, 'sleep'):
            self.assertEqual(cb.gh('-X', 'POST', 'repos/owner/repo/actions/runners/generate-jitconfig',
                                   body={'name': 'cbx-unique'}), {'encoded_jit_config': 'new'})
        self.assertEqual(run.call_count, 5)

    def test_app_failure_before_jit_does_not_reconcile(self):
        with mock.patch.object(cb, 'gh_env', side_effect=[cb.GitHubTransient('network error'), None]), \
                mock.patch.object(cb.subprocess, 'run', return_value=subprocess.CompletedProcess(
                    [], 0, '{"encoded_jit_config":"new"}', '')) as run, mock.patch.object(cb.time, 'sleep'):
            self.assertEqual(cb.gh('-X', 'POST', 'repos/owner/repo/actions/runners/generate-jitconfig',
                                   body={'name': 'cbx-unique'}), {'encoded_jit_config': 'new'})
        self.assertEqual(run.call_count, 1)

    def test_lost_cleanup_response_treats_deleted_runner_as_absent(self):
        results = [subprocess.TimeoutExpired('gh', 60),
                   subprocess.CompletedProcess([], 0, json.dumps({'runners': [
                       {'id': 7, 'name': 'cbx-unique', 'busy': False}]}), ''),
                   subprocess.TimeoutExpired('gh', 60),
                   subprocess.CompletedProcess([], 1, '', 'HTTP 404'),
                   subprocess.CompletedProcess([], 0, '{"encoded_jit_config":"new"}', '')]
        with mock.patch.object(cb, 'gh_env', return_value=None), \
                mock.patch.object(cb.subprocess, 'run', side_effect=results) as run, \
                mock.patch.object(cb.time, 'sleep'):
            self.assertEqual(cb.gh('-X', 'POST', 'repos/owner/repo/actions/runners/generate-jitconfig',
                                   body={'name': 'cbx-unique'}), {'encoded_jit_config': 'new'})
        self.assertEqual(run.call_count, 5)

    def test_jit_and_reconciliation_share_four_retries(self):
        results = [subprocess.TimeoutExpired('gh', 60)] + [
            subprocess.CompletedProcess([], 1, '', 'HTTP 503')] * 4
        with mock.patch.object(cb, 'gh_env', return_value=None), \
                mock.patch.object(cb.subprocess, 'run', side_effect=results) as run, \
                mock.patch.object(cb.time, 'sleep') as sleep:
            with self.assertRaises(cb.GitHubTransient):
                cb.gh('-X', 'POST', 'repos/owner/repo/actions/runners/generate-jitconfig', body={'name': 'cbx-unique'})
        self.assertEqual(run.call_count, 5)
        self.assertEqual(sleep.call_args_list, [mock.call(1), mock.call(2), mock.call(4), mock.call(8)])

    def test_idle_release_wakes_worker_after_lost_delete_response(self):
        with mock.patch.object(cb, 'CLEANUP', cb.threading.Event()), \
                mock.patch.object(cb, 'gh_env', return_value=None), \
                mock.patch.object(cb.subprocess, 'run', side_effect=[subprocess.TimeoutExpired('gh', 60),
                    subprocess.CompletedProcess([], 1, '', 'HTTP 404')]), \
                mock.patch.object(cb.os, 'kill') as kill, mock.patch.object(cb.time, 'sleep'):
            cb.release_idle('owner/repo', 7)
        kill.assert_called_once_with(os.getpid(), cb.signal.SIGUSR1)

    def test_first_delete_404_does_not_wake_worker(self):
        with mock.patch.object(cb, 'CLEANUP', cb.threading.Event()), \
                mock.patch.object(cb, 'gh_env', return_value=None), \
                mock.patch.object(cb.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'HTTP 404')), \
                mock.patch.object(cb.os, 'kill') as kill, mock.patch.object(cb.threading, 'Timer') as timer:
            cb.release_idle('owner/repo', 7)
        kill.assert_not_called()
        timer.assert_called_once_with(60, cb.release_idle, ('owner/repo', 7))

    def test_permission_failure_is_not_retried(self):
        with mock.patch.object(cb, 'gh_env', return_value=None), \
                mock.patch.object(cb.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'HTTP 403')) as run, \
                mock.patch.object(cb.time, 'sleep') as sleep:
            with self.assertRaises(RuntimeError):
                cb.gh('repos/owner/repo')
        self.assertEqual(run.call_count, 1)
        sleep.assert_not_called()

    def test_runner_unit_restarts_unexpected_success(self):
        unit = (ROOT / 'install.sh').read_text().split('coldbox-runner@.service <<EOF')[1].split('EOF')[0]
        self.assertIn('Restart=always', unit)

    def test_app_transient_errors_retry_without_auth_fallback(self):
        with mock.patch.object(cb, 'gh_env', side_effect=[cb.GitHubTransient('HTTP 503'),
                                                        {'GH_TOKEN': 'fixture-secret'}]) as auth, \
                mock.patch.object(cb.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{}', '')) as run, \
                mock.patch.object(cb.time, 'sleep') as sleep:
            self.assertEqual(cb.gh('repos/owner/repo'), {})
        self.assertEqual(auth.call_count, 2)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs['env'], {'GH_TOKEN': 'fixture-secret'})
        sleep.assert_called_once_with(1)

    def test_app_retry_exhaustion_does_not_expose_credentials(self):
        with mock.patch.object(cb, 'gh_env', return_value={'GH_TOKEN': 'fixture-secret'}), \
                mock.patch.object(cb.subprocess, 'run', return_value=subprocess.CompletedProcess(
                    [], 1, '', 'HTTP 503 fixture-secret')) as run, mock.patch.object(cb.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 503') as caught:
                cb.gh('repos/owner/repo')
        self.assertNotIn('fixture-secret', str(caught.exception))
        self.assertEqual(run.call_count, 5)

    def test_network_timeout_retries(self):
        with mock.patch.object(cb, 'gh_env', return_value=None), \
                mock.patch.object(cb.subprocess, 'run', side_effect=[subprocess.TimeoutExpired('gh', 60),
                    subprocess.CompletedProcess([], 0, '{}', '')]) as run, mock.patch.object(cb.time, 'sleep') as sleep:
            self.assertEqual(cb.gh('repos/owner/repo'), {})
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(1)


class GracefulStop(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.log = Path(folder.name) / 'runner.log'
        self.log.write_bytes(b'Running job: verify\n')
        patcher = mock.patch.multiple(cb, ACTIVE_LOG=self.log, DRAINING=cb.threading.Event(), create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        handler = cb.signal.getsignal(cb.signal.SIGTERM)
        self.addCleanup(cb.signal.signal, cb.signal.SIGTERM, handler)

    def test_first_stop_drains_an_assigned_job(self):
        cb.stop_once()
        self.assertTrue(cb.DRAINING.is_set())

    def test_second_stop_forces_a_draining_job(self):
        cb.stop_once()
        with self.assertRaises(SystemExit) as raised:
            cb.stop_once()
        self.assertEqual(raised.exception.code, 143)

    def test_idle_finished_and_missing_logs_stop_immediately(self):
        for log in (b'Listening for Jobs\n', b'Running job: verify\nJob verify completed with result: Succeeded\n'):
            self.log.write_bytes(log)
            with self.assertRaises(SystemExit) as raised:
                cb.stop_once()
            self.assertEqual(raised.exception.code, 143)
        self.log.unlink()
        with self.assertRaises(SystemExit) as raised:
            cb.stop_once()
        self.assertEqual(raised.exception.code, 143)

    def test_worker_exits_after_the_drained_job_without_registering_again(self):
        args = argparse.Namespace(target='me/repo', worker=True, slots=1, ttl=60, allow_public=False,
                                  allow_forks=False, once=False, idle=0, name=None, size='default')
        runner = {'runner': {'id': 7}, 'encoded_jit_config': 'c'}
        def finish(*a, **kw):
            if cb.DRAINING.is_set():
                self.fail('registered another job')
            cb.DRAINING.set()
            return {'verdict': 'pass', 'job_seconds': 1}
        with mock.patch.multiple(cb, guard=lambda *a: None, admit=lambda *a: True,
                                 gh=mock.DEFAULT, job=mock.DEFAULT) as patched:
            patched['gh'].return_value = runner
            patched['job'].side_effect = finish
            self.assertEqual(cb.serve(args), 143)
        self.assertEqual(sum('generate-jitconfig' in str(call) for call in patched['gh'].call_args_list), 1)

    def test_runner_unit_allows_jobs_to_drain_for_the_vm_lifetime(self):
        text = (ROOT / 'install.sh').read_text().split('coldbox-runner@.service <<EOF', 1)[1].split('EOF', 1)[0]
        self.assertIn('KillMode=mixed', text)
        self.assertIn('TimeoutStopSec=21600', text)


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
        self.assertEqual(started[0][2:], ['serve', 'me/repo', '--worker', '--ttl', '60', '--slots', '1', '--allow-forks', '--once'])

    def test_supervisor_forwards_each_stop_only_once_even_while_waiting(self):
        children = []
        class Child:
            def __init__(self, argv):
                self.signals = []
                children.append(self)
            def poll(self):
                return None
            def send_signal(self, sig):
                self.signals.append(sig)
            def wait(self):
                if self is children[0]:
                    self_outer.assertEqual(self.signals, [cb.signal.SIGTERM])
                    cb.signal.getsignal(cb.signal.SIGTERM)(cb.signal.SIGTERM, None)
                return 143
        self_outer = self
        args = argparse.Namespace(target='org', slots=2, ttl=60, allow_public=False, allow_forks=False,
                                  once=False, worker=False, idle=0)
        for sig in (cb.signal.SIGINT, cb.signal.SIGTERM):
            self.addCleanup(cb.signal.signal, sig, cb.signal.getsignal(sig))
        self.addCleanup(cb.STOPPING.clear)
        def stop(_):
            cb.signal.getsignal(cb.signal.SIGTERM)(cb.signal.SIGTERM, None)
        with mock.patch.object(cb.subprocess, 'Popen', Child), mock.patch.object(cb.time, 'sleep', stop):
            self.assertEqual(cb.serve(args), 143)
        self.assertEqual(children[0].signals, [cb.signal.SIGTERM] * 2)

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
    def setUp(self):
        self.enterContext(mock.patch.object(cb, 'STATE', Path(self.enterContext(tempfile.TemporaryDirectory()))))

    def test_a_worker_killed_right_after_registering_leaves_a_record_that_the_runner_is_this_hosts(self):
        args = cb.argparse.Namespace(target='me/repo', slots=1, ttl=60, allow_public=False, allow_forks=False,
                                     once=True, worker=False, idle=0, name='cbx-1700000000-abcdef', size='default')

        def killed_after_registering(*a, **k):
            raise SystemExit(137)

        with mock.patch.multiple(cb, guard=lambda *a: None, admit=mock.DEFAULT, gh=killed_after_registering), \
                self.assertRaises(SystemExit):
            quiet(cb.serve, args)
        self.assertTrue((cb.STATE / 'receipts/cbx-1700000000-abcdef.registered').exists())

    def test_a_registration_github_refused_leaves_no_record_that_the_runner_is_this_hosts(self):
        args = cb.argparse.Namespace(target='me/repo', slots=1, ttl=60, allow_public=False, allow_forks=False,
                                     once=True, worker=False, idle=0, name='cbx-1700000000-abcdef', size='default')

        def refused(*a, **k):
            raise RuntimeError('gh api: HTTP 409: Already exists')

        with mock.patch.multiple(cb, guard=lambda *a: None, admit=mock.DEFAULT, gh=refused), \
                self.assertRaises(RuntimeError):
            quiet(cb.serve, args)
        self.assertFalse((cb.STATE / 'receipts/cbx-1700000000-abcdef.registered').exists())

    def test_a_registration_whose_outcome_is_unknown_keeps_the_record_that_the_runner_is_this_hosts(self):
        args = cb.argparse.Namespace(target='me/repo', slots=1, ttl=60, allow_public=False, allow_forks=False,
                                     once=True, worker=False, idle=0, name='cbx-1700000000-abcdef', size='default')

        def timed_out(*a, **k):
            raise cb.GitHubTransient('GitHub request timed out')

        with mock.patch.multiple(cb, guard=lambda *a: None, admit=mock.DEFAULT, gh=timed_out), \
                self.assertRaises(cb.GitHubTransient):
            quiet(cb.serve, args)
        self.assertTrue((cb.STATE / 'receipts/cbx-1700000000-abcdef.registered').exists())

    def test_a_sized_burst_registers_its_size_label_and_boots_and_admits_that_size(self):
        runner = {'runner': {'id': 7}, 'encoded_jit_config': 'c'}
        args = cb.argparse.Namespace(target='me/repo', slots=1, ttl=60, allow_public=False, allow_forks=False,
                                     once=True, worker=True, idle=0, name=None, size='large')
        handler = cb.signal.getsignal(cb.signal.SIGTERM)
        self.addCleanup(cb.signal.signal, cb.signal.SIGTERM, handler)
        bodies, configs = [], []
        with mock.patch.multiple(cb, guard=lambda *a: None, admit=mock.DEFAULT,
                                 vm_shape=lambda size: (32 * GIB, {'limits.cpu': '16', 'limits.memory': '32768MiB'}),
                                 gh=lambda *a, body=None: bodies.append(body) or runner,
                                 job=lambda *a, **k: configs.append(k['config']) or {'verdict': 'pass'}) as patched:
            self.assertEqual(quiet(cb.serve, args)[0], 0)
        patched['admit'].assert_called_once_with('me/repo', mock.ANY, 'once', None, 32 * GIB)
        self.assertEqual(bodies[0]['labels'], ['self-hosted', 'linux', 'x64', 'coldbox-large'])
        self.assertEqual(configs, [['--config', 'limits.cpu=16', '--config', 'limits.memory=32768MiB']])

    def test_exit_status_follows_the_receipt_verdict(self):
        runner = {'runner': {'id': 7}, 'encoded_jit_config': 'c'}
        args = cb.argparse.Namespace(target='me/repo', slots=1, ttl=60, allow_public=False, allow_forks=False,
                                     once=True, worker=False, idle=0, name=None, size='default')
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
        envs = []
        for writers in (None, ['me', 'pal']):
            with mock.patch.multiple(cb, guard=lambda *a: writers, gh=lambda *a, **k: runner, admit=mock.DEFAULT,
                                     job=lambda *a, **k: envs.append(k['env']) or {'verdict': 'pass'}) as admitted:
                quiet(cb.serve, args)
        self.assertEqual(envs[0], ['ACTIONS_RUNNER_HOOK_JOB_STARTED=/opt/coldbox-job-started.sh',
                                    f'COLDBOX_CI_PRIMARY={os.uname().nodename}'])
        self.assertEqual(envs[1][2:], ['COLDBOX_PUBLIC_WRITERS=me,pal'])
        admitted['admit'].assert_called_once_with('me/repo', mock.ANY, 'once', ['me', 'pal'], None)
        patched['note_worker'].assert_not_called()
        patched['admit'].assert_called_with('me/repo', mock.ANY, 'once', None, None)
        args.worker = True
        with mock.patch.multiple(cb, guard=lambda *a: None, gh=lambda *a, **k: runner, note_worker=mock.DEFAULT,
                                 admit=mock.DEFAULT, job=lambda *a, **k: {'verdict': 'pass'}) as patched:
            quiet(cb.serve, args)
        patched['note_worker'].assert_not_called()
        patched['admit'].assert_called_once_with('me/repo', mock.ANY, 'once', None, None)


class SwapBase(unittest.TestCase):
    def swap(self, instances, failing_move=None):
        def ic(*args, **kwargs):
            _, source, target = args
            if (source, target) == failing_move or source not in instances or target in instances:
                raise cb.subprocess.CalledProcessError(1, args)
            instances[target] = instances.pop(source)

        with mock.patch.multiple(cb, ic=ic, destroy=lambda name: (instances.pop(name, None), True)[1]):
            cb.swap_base()
        return instances

    def test_replaces_the_base(self):
        self.assertEqual(self.swap({'base-dev': 'old', 'base-new': 'new'}), {'base-dev': 'new', 'base-prev': 'old'})

    def test_first_install_has_no_old_base(self):
        self.assertEqual(self.swap({'base-new': 'new'}), {'base-dev': 'new'})

    def test_failed_swap_keeps_the_old_base(self):
        instances = {'base-dev': 'old', 'base-new': 'new'}
        with self.assertRaises(cb.subprocess.CalledProcessError):
            self.swap(instances, failing_move=('base-new', 'base-dev'))
        self.assertEqual(instances['base-dev'], 'old')

    def test_replaces_the_previous_base(self):
        self.assertEqual(self.swap({'base-dev': 'old', 'base-new': 'new', 'base-prev': 'older'}),
                         {'base-dev': 'new', 'base-prev': 'old'})


class BaseRollback(unittest.TestCase):
    def rollback(self, instances, failing_move=None):
        def ic(*args, **kwargs):
            if args[0] == 'info':
                if args[1] not in instances:
                    raise cb.subprocess.CalledProcessError(1, args, stderr='Error: Instance not found')
                return ''
            _, source, target = args
            if (source, target) == failing_move or source not in instances or target in instances:
                raise cb.subprocess.CalledProcessError(1, args)
            instances[target] = instances.pop(source)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.multiple(cb, STATE=Path(tmp), ic=ic, build_new_base=mock.DEFAULT), \
                mock.patch.object(cb.os, 'geteuid', return_value=0):
            (Path(tmp) / 'base-packages.txt').write_text('gh 2\n')
            (Path(tmp) / 'base-packages.prev.txt').write_text('gh 1\n')
            result = quiet(cb.build_base, argparse.Namespace(rollback=True))
            self.assertEqual((Path(tmp) / 'base-packages.txt').read_text(), 'gh 1\n')
            self.assertEqual((Path(tmp) / 'base-packages.prev.txt').read_text(), 'gh 2\n')
            cb.build_new_base.assert_not_called()
            return result

    def test_exchanges_the_bases_without_building(self):
        instances = {'base-dev': 'current', 'base-prev': 'previous'}
        self.assertEqual(self.rollback(instances)[0], 0)
        self.assertEqual(instances, {'base-dev': 'previous', 'base-prev': 'current'})

    def test_retry_after_first_rename_restores_and_exchanges_bases(self):
        instances = {'base-rollback': 'current', 'base-prev': 'previous'}
        self.assertEqual(self.rollback(instances)[0], 0)
        self.assertEqual(instances, {'base-dev': 'previous', 'base-prev': 'current'})

    def test_retry_after_second_rename_finishes_the_exchange(self):
        instances = {'base-dev': 'previous', 'base-rollback': 'current'}
        self.assertEqual(self.rollback(instances)[0], 0)
        self.assertEqual(instances, {'base-dev': 'previous', 'base-prev': 'current'})

    def test_failed_final_move_restores_both_bases(self):
        instances = {'base-dev': 'current', 'base-prev': 'previous'}
        with self.assertRaises(cb.subprocess.CalledProcessError):
            self.rollback(instances, ('base-rollback', 'base-prev'))
        self.assertEqual(instances, {'base-dev': 'current', 'base-prev': 'previous'})

    def test_missing_previous_base_changes_nothing(self):
        instances = {'base-dev': 'current'}
        with self.assertRaisesRegex(SystemExit, 'no base-prev'):
            self.rollback(instances)
        self.assertEqual(instances, {'base-dev': 'current'})

    def test_failed_promotion_restores_current_base(self):
        instances = {'base-dev': 'current', 'base-prev': 'previous'}
        with self.assertRaises(cb.subprocess.CalledProcessError):
            self.rollback(instances, ('base-prev', 'base-dev'))
        self.assertEqual(instances, {'base-dev': 'current', 'base-prev': 'previous'})


class BasePackageChanges(unittest.TestCase):
    def test_prints_exactly_added_removed_changed_packages_and_runner(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(cb, 'STATE', Path(tmp)):
            (Path(tmp) / 'base-packages.prev.txt').write_text('same 1\nremoved 3\nchanged 1\n')
            (Path(tmp) / 'base-packages.txt').write_text('same 1\ngh 2.46.0\nchanged 2\n')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                cb.print_base_changes('2.338.1', '2.338.2')
            self.assertEqual(output.getvalue().splitlines(), [
                'changed: 1 -> 2', 'gh: (none) -> 2.46.0', 'removed: 3 -> (none)',
                'actions-runner: 2.338.1 -> 2.338.2'])

    def test_first_manifest_prints_count_only(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(cb, 'STATE', Path(tmp)):
            (Path(tmp) / 'base-packages.txt').write_text('gh 2.46.0\nsame 1\n')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                cb.print_base_changes('(none)', '2.338.2')
            self.assertEqual(output.getvalue(), 'Packages: 2\n')

    def test_unchanged_manifest_says_so(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(cb, 'STATE', Path(tmp)):
            for name in ('base-packages.txt', 'base-packages.prev.txt'):
                (Path(tmp) / name).write_text('gh 2.46.0\n')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                cb.print_base_changes('2.338.2', '2.338.2')
            self.assertEqual(output.getvalue(), 'Packages: no changes\nactions-runner: 2.338.2 -> 2.338.2\n')


class HostMemoryAdmission(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.rows = []
        self.owned = set()
        self.adhoc = {}
        self.pool = dict(total=100 * GIB, used=10 * GIB)
        patches = dict(STATE=self.state, vm_memory=lambda: 4 * GIB, vm_disk=lambda: 40 * GIB,
                       host_memory=lambda field='MemTotal': (23 if field == 'MemTotal' else 20) * GIB // 2, host_vms=lambda: self.owned,
                       registrations=lambda: [], CI_HOSTS=self.state / 'no-ci-hosts.json',
                       VM_MAX=self.state / 'vm-max', adhoc_vms=lambda: self.adhoc)
        for name, value in patches.items():
            patcher = mock.patch.object(cb, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(cb, 'query', side_effect=self.instances)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in (('cpu_count', 8), ('getloadavg', (0.0, 0.0, 0.0))):
            patcher = mock.patch.object(cb.os, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def instances(self, path):
        if path == f'/1.0/storage-pools/{cb.POOL}/resources':
            return dict(space=self.pool)
        self.assertEqual(path, '/1.0/instances?recursion=1&all-projects=true')
        return self.rows

    def test_a_pool_without_room_for_one_root_disk_plus_a_tenth_refuses_the_vm(self):
        self.pool['used'] = 70 * GIB
        self.assertEqual(quiet(cb.admit, 'repo', 'cbx-new', 'once'), (False, (
            f'coldbox: storage pool {cb.POOL} has 30 GiB free, below one 40 GiB root disk plus a 10 GiB reserve; '
            'run coldbox reap, or reinstall with a larger COLDBOX_POOL_SIZE\n')))
        self.pool['used'] = 50 * GIB + 1
        self.assertFalse(quiet(cb.admit, 'repo', 'cbx-new', 'once')[0])
        self.assertFalse((self.state / 'workers' / str(os.getpid())).exists())
        self.pool['used'] = 50 * GIB
        self.assertEqual(quiet(cb.admit, 'repo', 'cbx-new', 'once'), (True, ''))

    def vm(self, name='bootstrap', project='default', memory='8GiB', status='Running', **extra):
        return dict(name=name, project=project, type='virtual-machine', status=status,
                    config={}, expanded_config={'limits.memory': memory}, **extra)

    def test_foreign_vm_blocks_admission(self):
        self.rows = [self.vm()]
        self.assertFalse(cb.admit('repo', 'cbx-new', 'once'))
        self.assertFalse((self.state / 'workers' / str(os.getpid())).exists())

    def test_a_foreign_vm_reserves_its_limit_from_available_memory_while_the_ceiling_has_room(self):
        self.rows = [self.vm(memory='30GiB')]
        for available, admitted in ((47, True), (46, False)):
            with self.subTest(available=available), \
                    mock.patch.object(cb, 'host_memory', lambda field='MemTotal': (100 if field == 'MemTotal' else available) * GIB):
                self.assertGreater(cb.host_room([], 4 * GIB, 4 * GIB, 0, 0)['ceiling'], 0)
                self.assertEqual(cb.admit('repo', 'cbx-new', 'once'), admitted)

    def test_admit_reserves_each_vm_at_its_own_size(self):
        cb.note_worker(os.getppid(), 'repo', 'cbx-1-large', 'once', None, 6 * GIB)
        self.assertEqual(cb.live_workers(self.state / 'workers')[0].memory, 6 * GIB)
        self.assertFalse(cb.admit('repo', 'cbx-new', 'once'))
        self.assertFalse(cb.admit('repo', 'cbx-new', 'once', memory=3 * GIB))
        self.assertTrue(cb.admit('repo', 'cbx-new', 'once', memory=2 * GIB))
        record = json.loads((self.state / 'workers' / str(os.getpid())).read_text())
        self.assertEqual(record[3:], ['once', None, 2 * GIB])
        cb.note_worker(os.getppid(), 'repo', 'cbx-1-small', 'once', None, 2 * GIB)
        self.assertTrue(cb.admit('repo', 'cbx-new', 'once', memory=6 * GIB))
        self.assertTrue(cb.admit('repo', 'cbx-new', 'once'))
        self.assertFalse(cb.admit('repo', 'cbx-new', 'once', memory=7 * GIB))

    def test_a_worker_record_with_a_bad_size_is_unreadable(self):
        folder = self.state / 'workers'
        folder.mkdir()
        for size in (0, -1, '4GiB', 1.5, True):
            (folder / str(os.getppid())).write_text(json.dumps(['repo', cb.process_start(os.getppid()), 'cbx-1-a',
                                                                'once', None, size]))
            with self.subTest(size=size):
                self.assertEqual(cb.live_workers(folder, prune=False), [])
                self.assertEqual(cb.unreadable_live_workers(folder), [str(os.getppid())])

    def test_tracked_vm_is_not_counted_twice(self):
        cb.note_worker(os.getppid(), 'repo', 'cbx-worker', 'once')
        self.owned = {'cbx-worker'}
        self.rows = [self.vm('cbx-worker', cb.PROJECT, '4GiB')]
        self.assertTrue(cb.admit('repo', 'cbx-new', 'once'))

    def test_untracked_coldbox_vm_is_already_in_host_load(self):
        self.owned = {'cbx-orphan'}
        self.rows = [self.vm('cbx-orphan', cb.PROJECT, '4GiB')]
        self.assertTrue(cb.admit('repo', 'cbx-new', 'once'))

    def test_an_untracked_vm_still_booting_reserves_its_own_larger_limit_from_available_memory(self):
        self.owned = {'cbx-orphan'}
        self.rows = [self.vm('cbx-orphan', cb.PROJECT, '8GiB')]
        for halves, admitted in ((49, True), (48, False)):
            with self.subTest(available=halves / 2), \
                    mock.patch.object(cb, 'host_memory', lambda field='MemTotal': (200 if field == 'MemTotal' else halves) * GIB // 2):
                self.assertEqual(cb.admit('repo', 'cbx-new', 'once'), admitted)
        (self.state / 'ready').mkdir()
        (self.state / 'ready' / 'cbx-orphan').touch()
        with mock.patch.object(cb, 'host_memory', lambda field='MemTotal': (200 if field == 'MemTotal' else 48) * GIB // 2):
            self.assertTrue(cb.admit('repo', 'cbx-new', 'once'))

    def test_owned_vm_with_a_larger_limit_reserves_the_difference(self):
        self.owned = {'cbx-orphan'}
        self.rows = [self.vm('cbx-orphan', cb.PROJECT, '4GiB')]
        self.assertTrue(cb.admit('repo', 'cbx-new', 'once'))
        self.rows = [self.vm('cbx-orphan', cb.PROJECT, '5GiB')]
        self.assertFalse(cb.admit('repo', 'cbx-new', 'once'))

    def test_same_name_in_another_project_is_foreign(self):
        self.owned = {'cbx-worker'}
        self.rows = [self.vm('cbx-worker', 'other', '5GiB')]
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

    def test_vm_up_is_refused_at_the_ad_hoc_limit(self):
        (self.state / 'vm-max').write_text('1\n')
        self.owned, self.adhoc = {'cbx-1-a'}, {'cbx-1-a': {}}
        with mock.patch.object(cb, 'boot') as boot:
            with self.assertRaisesRegex(SystemExit, r'1 ad-hoc VM\(s\) already running, the limit in .*vm-max'):
                cb.main(['vm', 'up'])
        boot.assert_not_called()
        self.assertFalse((self.state / 'workers' / str(os.getpid())).exists())

    def test_ad_hoc_limit_counts_a_vm_still_booting(self):
        (self.state / 'vm-max').write_text('1')
        cb.note_worker(os.getppid(), 'vm', 'cbx-booting', 'vm')
        with self.assertRaisesRegex(SystemExit, 'ad-hoc VM'):
            cb.admit('vm', 'cbx-new', 'vm')

    def test_ad_hoc_limit_ignores_ci_vms_and_admits_below_it(self):
        (self.state / 'vm-max').write_text('1')
        cb.note_worker(os.getppid(), 'repo', 'cbx-ci', 'once')
        self.owned = {'cbx-ci'}
        self.assertTrue(cb.admit('vm', 'cbx-new', 'vm'))

    def test_ad_hoc_limit_of_zero_refuses_every_vm_up(self):
        (self.state / 'vm-max').write_text('0')
        with self.assertRaisesRegex(SystemExit, 'ad-hoc VM'):
            cb.admit('vm', 'cbx-new', 'vm')

    def test_ci_jobs_ignore_the_ad_hoc_limit(self):
        (self.state / 'vm-max').write_text('0')
        self.assertTrue(cb.admit('repo', 'cbx-new', 'once'))

    def test_vm_up_is_refused_on_the_ci_primary(self):
        hosts = self.state / 'ci-hosts.json'
        hosts.write_text(json.dumps(dict(primary=os.uname().nodename, overflow=['other'])))
        with mock.patch.multiple(cb, CI_HOSTS=hosts, admit=mock.DEFAULT, boot=mock.DEFAULT) as mocks:
            with self.assertRaisesRegex(SystemExit, 'this host is the CI primary'):
                cb.main(['vm', 'up'])
        mocks['admit'].assert_not_called()
        mocks['boot'].assert_not_called()

    def test_vm_up_runs_on_the_ci_overflow_host(self):
        hosts = self.state / 'ci-hosts.json'
        hosts.write_text(json.dumps(dict(primary='other', overflow=[os.uname().nodename])))
        with mock.patch.multiple(cb, CI_HOSTS=hosts, admit=lambda *a: True, new_id=lambda: 'cbx-9-z',
                                 boot=mock.DEFAULT):
            self.assertEqual(quiet(cb.main, ['vm', 'up']), (0, 'cbx-9-z\n'))

    def full_host_with_peers(self, rooms):
        (self.state / 'vm-max').write_text('0')
        hosts = self.state / 'ci-hosts.json'
        hosts.write_text(json.dumps(dict(primary='primary-host', overflow=[os.uname().nodename, *rooms])))
        rooms = dict(rooms, **{'primary-host': 30})
        return hosts, lambda target, host: None if rooms[host] is None else dict(
            host=host, target=target, measured_at=time.time(), cpus=40, busy_cpus=4, available_memory=200 * GIB,
            vm_memory=16 * GIB, vms=30 - rooms[host], vm_ceiling=30, reserve=0, ramp_room=30, booting=0, load_avg=4.0)

    def test_a_full_host_starts_vm_up_on_the_other_host_with_the_most_room_never_the_primary(self):
        hosts, evidence = self.full_host_with_peers({'vm-a': 2, 'vm-b': 6, 'vm-c': None})
        ran = mock.Mock(side_effect=[argparse.Namespace(returncode=1, stdout=''),
                                     argparse.Namespace(returncode=0, stdout='cbx-5-x-vm-a\n')])
        with mock.patch.multiple(cb, CI_HOSTS=hosts, host_evidence=evidence, boot=mock.DEFAULT) as mocks, \
                mock.patch.object(cb.subprocess, 'run', ran):
            code, out = quiet(cb.main, ['vm', 'up', '--ttl', '60', '--offline'])
        self.assertEqual(code, 0)
        self.assertTrue(out.endswith('cbx-5-x-vm-a\n'), out)
        self.assertIn('started it on vm-a', out)
        mocks['boot'].assert_not_called()
        self.assertEqual([call.args[0][-2:] for call in ran.call_args_list],
                         [['vm-b', 'coldbox vm up --here --ttl 60 --offline'],
                          ['vm-a', 'coldbox vm up --here --ttl 60 --offline']])
        self.assertIn('BatchMode=yes', ran.call_args.args[0])

    def test_vm_up_here_never_forwards_and_unranked_peers_are_tried_last(self):
        hosts, evidence = self.full_host_with_peers({'vm-a': 6})
        with mock.patch.multiple(cb, CI_HOSTS=hosts, host_evidence=evidence), \
                mock.patch.object(cb.subprocess, 'run') as ran, \
                self.assertRaisesRegex(SystemExit, r'0 ad-hoc VM\(s\) already running'):
            cb.main(['vm', 'up', '--here'])
        ran.assert_not_called()
        hosts, evidence = self.full_host_with_peers({'vm-a': 0, 'vm-b': None, 'vm-c': 3})
        ran = mock.Mock(return_value=argparse.Namespace(returncode=1, stdout=''))
        with mock.patch.multiple(cb, CI_HOSTS=hosts, host_evidence=evidence), \
                mock.patch.object(cb.subprocess, 'run', ran), \
                self.assertRaisesRegex(SystemExit, r'0 ad-hoc VM\(s\) already running'):
            cb.main(['vm', 'up'])
        self.assertEqual([call.args[0][-2] for call in ran.call_args_list], ['vm-c', 'vm-a', 'vm-b'])

    def test_vm_up_never_starts_a_vm_on_a_host_whose_tag_is_ambiguous(self):
        hosts, evidence = self.full_host_with_peers({'vm-a': 6, 'vm.a': 6, 'vm-c': 1})
        ran = mock.Mock(return_value=argparse.Namespace(returncode=0, stdout='cbx-5-x-vm-c\n'))
        with mock.patch.multiple(cb, CI_HOSTS=hosts, host_evidence=evidence), mock.patch.object(cb.subprocess, 'run', ran):
            self.assertEqual(quiet(cb.main, ['vm', 'up'])[0], 0)
        self.assertEqual([call.args[0][-2] for call in ran.call_args_list], ['vm-c'])

    def test_exec_and_down_reach_an_ad_hoc_vm_on_the_host_its_name_ends_with(self):
        hosts = self.state / 'ci-hosts.json'
        hosts.write_text(json.dumps(dict(primary='primary-host', overflow=[os.uname().nodename, 'vm-a', 'vm-b'])))
        ran = mock.Mock(return_value=argparse.Namespace(returncode=7))
        with mock.patch.object(cb, 'CI_HOSTS', hosts), mock.patch.object(cb.subprocess, 'run', ran):
            self.assertEqual(cb.main(['vm', 'exec', 'cbx-5-x-vm-b', '--', 'sh', '-c', 'echo "a b"']), 7)
            self.assertEqual(cb.main(['vm', 'down', 'cbx-5-x-vm-b']), 7)
            with self.assertRaisesRegex(SystemExit, 'no ad-hoc VM named cbx-5-x-primary-host'):
                cb.main(['vm', 'down', 'cbx-5-x-primary-host'])
        self.assertEqual([call.args[0][-2:] for call in ran.call_args_list],
                         [['vm-b', "coldbox vm exec cbx-5-x-vm-b -- sh -c 'echo \"a b\"'"],
                          ['vm-b', 'coldbox vm down cbx-5-x-vm-b']])

    def test_exec_and_down_never_forward_a_name_whose_host_tag_is_ambiguous_or_this_hosts(self):
        me = cb.host_tag(os.uname().nodename)
        hosts = self.state / 'ci-hosts.json'
        hosts.write_text(json.dumps(dict(primary='primary-host', overflow=[os.uname().nodename, 'vm-a', 'vm.a', os.uname().nodename.upper()])))
        with mock.patch.object(cb, 'CI_HOSTS', hosts), mock.patch.object(cb.subprocess, 'run') as ran:
            for name in ('cbx-5-x-vm-a', f'cbx-5-x-{me}'):
                with self.subTest(name=name), self.assertRaisesRegex(SystemExit, f'no ad-hoc VM named {name}'):
                    cb.main(['vm', 'down', name])
        ran.assert_not_called()

    def test_vm_up_is_refused_when_ci_hosts_is_unreadable(self):
        for text in ('{', '{"primary": null}', '{"primary": []}', '{"overflow": []}', None):
            hosts = Path(tempfile.mkdtemp()) / 'ci-hosts.json'
            if text is None:
                hosts.symlink_to(hosts.parent / 'gone')
            else:
                hosts.write_text(text)
            with self.subTest(text=text), mock.patch.multiple(cb, CI_HOSTS=hosts, admit=mock.DEFAULT,
                                                              boot=mock.DEFAULT) as mocks:
                with self.assertRaisesRegex(SystemExit, 'cannot read'):
                    cb.main(['vm', 'up'])
                mocks['admit'].assert_not_called()
                mocks['boot'].assert_not_called()

    def test_vm_up_is_refused_when_ci_hosts_cannot_be_looked_up(self):
        hosts = self.state / 'ci-hosts.json'
        with mock.patch.multiple(cb, CI_HOSTS=hosts, admit=mock.DEFAULT, boot=mock.DEFAULT) as mocks, \
                mock.patch.object(type(hosts), 'lstat', side_effect=PermissionError('denied')):
            with self.assertRaisesRegex(SystemExit, 'cannot read.*denied'):
                cb.main(['vm', 'up'])
        mocks['admit'].assert_not_called()
        mocks['boot'].assert_not_called()


class AdhocVm(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(cb, 'CI_HOSTS', Path(tempfile.mkdtemp()) / 'missing')
        patcher.start()
        self.addCleanup(patcher.stop)

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
                  'cb.GUEST_FILES = cb.STATE / "missing-guest-files"\n'
                  'cb.host_memory = lambda field="MemTotal": 64 * 2**30\n'
                  'sys.exit(cb.main(sys.argv[2:]))\n')

    def argv(self, *args):
        return [sys.executable, '-c', self.ROOMY_HOST, str(ROOT / 'coldbox'), *args]

    def fake_incus(self, fail_start=False, undeletable=False, artifacts=None):
        tmp = Path(tempfile.mkdtemp())
        (tmp / 'vm.json').write_text(self.VM)
        fake = tmp / 'incus'
        fake.write_text('#!/bin/sh\n'
                        f'echo "$@" >> {tmp}/calls\n'
                        'case "$*" in\n'
                        f'  *" exec "*{cb.ARTIFACTS}*) cd {artifacts or tempfile.mkdtemp()} && exec tar -c . ;;\n'
                        '  *" exec "*)\n'
                        '    while [ "$1" != "--" ]; do shift; done; shift\n'
                        '    [ "$1" = runuser ] && shift 4\n'
                        '    [ "$1" = python3 ] && [ -n "$FAKE_GUEST" ] && exec python3 -c "$FAKE_GUEST"\n'
                        '    case "$1" in touch|cloud-init) exit 0 ;; coldbox) exit 1 ;; esac\n'
                        '    exec "$@" ;;\n'
                        '  *all-projects*) echo "[]" ;;\n'
                        '  *storage-pools*) echo \'{"space": {"total": 107374182400, "used": 0}}\' ;;\n'
                        f'  *" query "*) cat {tmp}/vm.json ;;\n'
                        '  *" profile get "*limits.cpu*) echo 8 ;;\n'
                        '  *" profile get "*) echo 1GiB ;;\n'
                        '  *" profile device get "*) echo 40GiB ;;\n'
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

    def test_cpus_and_memory_size_only_this_vm(self):
        tmp, env = self.fake_incus()
        self.assertEqual(self.coldbox(env, 'run', '--cpus', '1', '--memory', '512MiB', '--', 'true').returncode, 0)
        copy = [c for c in (tmp / 'calls').read_text().splitlines() if ' copy ' in c]
        self.assertEqual(len(copy), 1)
        self.assertTrue(copy[0].endswith('--config limits.cpu=1 --config limits.memory=512MiB'), copy[0])
        self.assertFalse([c for c in (tmp / 'calls').read_text().splitlines() if ' profile set ' in c])

    def test_without_sizes_the_vm_keeps_the_profile(self):
        tmp, env = self.fake_incus()
        self.assertEqual(self.coldbox(env, 'run', '--', 'true').returncode, 0)
        copy = [c for c in (tmp / 'calls').read_text().splitlines() if ' copy ' in c]
        self.assertEqual(len(copy), 1)
        self.assertNotIn('limits.', copy[0])

    def test_bad_sizes_are_refused_before_any_vm(self):
        for flag, value in (('--cpus', '0'), ('--cpus', '4.5'), ('--cpus', str(os.cpu_count() + 1)),
                            ('--memory', '12'), ('--memory', '0GiB'), ('--memory', '12GB'), ('--memory', '1.5GiB'),
                            ('--memory', '99999999GiB')):
            tmp, env = self.fake_incus()
            result = self.coldbox(env, 'run', flag, value, '--', 'true')
            self.assertEqual(result.returncode, 2, (flag, value, result.stderr))
            self.assertFalse((tmp / 'calls').exists(), (flag, value))

    def test_memory_above_the_profile_is_refused_before_any_vm(self):
        tmp, env = self.fake_incus()
        result = self.coldbox(env, 'run', '--memory', '2GiB', '--', 'true')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("above the coldbox profile's limits.memory", result.stderr)
        self.assertFalse([c for c in (tmp / 'calls').read_text().splitlines() if ' copy ' in c])
        self.assertFalse((tmp / 'state' / 'workers').exists() and any((tmp / 'state' / 'workers').iterdir()))

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

    def test_the_receipt_times_each_phase_and_records_unreadable_resources_as_null(self):
        tmp, env = self.fake_incus()
        self.assertEqual(self.coldbox(env, 'run', '--', 'true').returncode, 0)
        receipt = self.receipt(tmp)
        vm = receipt['vm']
        for field in ('clone_seconds', 'boot_seconds', 'ready_seconds', 'destroy_seconds'):
            self.assertGreaterEqual(vm[field], 0, field)
        self.assertLessEqual(vm['clone_seconds'] + vm['boot_seconds'], vm['ready_seconds'])
        self.assertEqual(vm['host_ceiling'], os.cpu_count())
        self.assertLessEqual(cb.at(receipt['job_started_at']), cb.at(receipt['job_ended_at']))
        self.assertEqual(receipt['resources'], dict.fromkeys(cb.RESOURCE_FIELDS))
        self.assertEqual(receipt['verdict'], 'pass')

    def test_files_the_job_leaves_in_the_artifacts_folder_are_copied_next_to_the_receipt(self):
        tmp, env = self.fake_incus()
        self.assertEqual(self.coldbox(env, 'run', '--', 'true').returncode, 0)
        self.assertNotIn('artifacts', self.receipt(tmp))
        guest = Path(tempfile.mkdtemp())
        (guest / 'coverage').mkdir()
        (guest / 'coverage' / 'index.html').write_text('<p>91%</p>')
        (guest / 'build.log').write_text('ok\n')
        (guest / 'escape').symlink_to('/etc/passwd')
        tmp, env = self.fake_incus(artifacts=guest)
        self.assertEqual(self.coldbox(env, 'run', '--', 'true').returncode, 0)
        receipt = self.receipt(tmp)
        folder = tmp / 'state' / 'receipts' / f"{receipt['id']}.artifacts"
        self.assertEqual(sorted(receipt['artifacts'], key=lambda item: item['path']),
                         [dict(path=str(folder / 'build.log'), bytes=3),
                          dict(path=str(folder / 'coverage' / 'index.html'), bytes=10)])
        self.assertIs(receipt['artifacts_truncated'], False)
        self.assertEqual((folder / 'coverage' / 'index.html').read_text(), '<p>91%</p>')
        self.assertFalse((folder / 'escape').exists() or (folder / 'escape').is_symlink())
        self.assertEqual(receipt['verdict'], 'pass')

    @staticmethod
    def guest_at(guest):
        return lambda name, *command, **kwargs: [part.replace(cb.ARTIFACTS, str(guest)) for part in command]

    def test_artifacts_past_the_size_or_file_cap_are_not_copied_and_the_receipt_says_so(self):
        guest = Path(tempfile.mkdtemp())
        for index in range(3):
            (guest / f'{index}.bin').write_bytes(b'x' * 40)
        for caps in (dict(ARTIFACT_BYTES=100), dict(ARTIFACT_FILES=2)):
            with self.subTest(caps):
                folder = Path(tempfile.mkdtemp()) / 'cbx-a.artifacts'
                with mock.patch.multiple(cb, ic_exec=self.guest_at(guest), **caps):
                    copied, complete = cb.copy_artifacts('cbx-a', folder)
                self.assertEqual((len(copied), complete), (2, False))
                self.assertEqual(len(list(folder.iterdir())), 2)

    def test_guest_tar_metadata_cannot_exhaust_host_memory(self):
        records = b''.join(b'14 k%07x=1\n' % index for index in range(3_000_000))
        stream = Path(tempfile.mkdtemp()) / 'pax.tar'
        with tarfile.open(stream, 'w', format=tarfile.USTAR_FORMAT) as tar:
            header = tarfile.TarInfo('pax')
            header.type, header.size = tarfile.XHDTYPE, len(records)
            tar.addfile(header, io.BytesIO(records))
            tar.addfile(tarfile.TarInfo('after.txt'), io.BytesIO())
        with mock.patch.multiple(cb, ic_exec=lambda *a, **k: ['cat', str(stream)], ARTIFACT_MEMORY=256 * 2**20):
            self.assertEqual(cb.copy_artifacts('cbx-a', Path(tempfile.mkdtemp()) / 'out'), ([], False))

    def test_a_guest_tar_that_lingers_after_a_complete_stream_is_killed_at_the_deadline(self):
        stream = Path(tempfile.mkdtemp()) / 'ok.tar'
        with tarfile.open(stream, 'w') as tar:
            tar.addfile(tarfile.TarInfo('a.txt'), io.BytesIO())
        command = ['sh', '-c', f'cat {stream}; exec sleep 60']
        with mock.patch.multiple(cb, ic_exec=lambda *a, **k: command, ARTIFACT_SECONDS=5):
            started = time.time()
            copied, complete = cb.copy_artifacts('cbx-a', Path(tempfile.mkdtemp()) / 'out')
        self.assertEqual((len(copied), complete), (1, False))
        self.assertLess(time.time() - started, 30)

    def test_a_parser_stuck_past_the_deadline_is_killed(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / 'prlimit').write_text('#!/bin/sh\nexec sleep 60\n')
        (tmp / 'prlimit').chmod(0o755)
        with mock.patch.multiple(cb, ic_exec=lambda *a, **k: ['true'], ARTIFACT_SECONDS=0.5), \
                mock.patch.dict(os.environ, PATH=f"{tmp}:{os.environ['PATH']}"):
            started = time.time()
            self.assertEqual(cb.copy_artifacts('cbx-a', tmp / 'out'), ([], False))
        self.assertLess(time.time() - started, 30)

    def test_hard_linked_artifacts_are_each_copied(self):
        guest = Path(tempfile.mkdtemp())
        (guest / 'a.txt').write_text('same')
        os.link(guest / 'a.txt', guest / 'b.txt')
        folder = Path(tempfile.mkdtemp()) / 'cbx-a.artifacts'
        with mock.patch.object(cb, 'ic_exec', self.guest_at(guest)):
            copied, complete = cb.copy_artifacts('cbx-a', folder)
        self.assertEqual((sorted(item['path'] for item in copied), complete),
                         ([str(folder / 'a.txt'), str(folder / 'b.txt')], True))
        self.assertEqual((folder / 'b.txt').read_text(), 'same')

    def test_a_job_without_an_artifacts_folder_has_nothing_to_copy(self):
        guest = Path(tempfile.mkdtemp()) / 'artifacts'
        with mock.patch.object(cb, 'ic_exec', self.guest_at(guest)):
            self.assertEqual(cb.copy_artifacts('cbx-a', Path(tempfile.mkdtemp()) / 'out'), ([], True))

    def test_an_artifact_copy_that_fails_stalls_or_cannot_start_is_reported_incomplete(self):
        for command in (['false'], ['sleep', '60'], ['sh', '-c', 'echo not a tar'], ['/nonexistent/incus']):
            with self.subTest(command), mock.patch.multiple(cb, ic_exec=lambda *a, **k: command, ARTIFACT_SECONDS=0.2):
                started = time.time()
                self.assertEqual(cb.copy_artifacts('cbx-a', Path(tempfile.mkdtemp())), ([], False))
                self.assertLess(time.time() - started, 10)

    def test_a_vm_that_never_ran_the_job_records_no_artifacts(self):
        tmp, env = self.fake_incus(fail_start=True)
        self.assertEqual(self.coldbox(env, 'run', '--', 'true').returncode, 125)
        self.assertNotIn('artifacts', self.receipt(tmp))
        self.assertNotIn(cb.ARTIFACTS, (tmp / 'calls').read_text())

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


FAKE_GUEST = '''
import json, os, sys
spec = json.load(sys.stdin)
reach = os.environ.get('REACH', 'control').split()
refusal = dict(ipv6=('OSError: unreachable', 101)).get
probes = [dict(p, error=None, errno=None) if p['kind'] in reach else
          dict(p, error=refusal(p['kind'], ('ConnectionRefusedError: refused', 111))[0],
               errno=refusal(p['kind'], (None, 111))[1]) for p in spec['probes']]
print('coldbox-selftest ' + json.dumps(dict(probes=probes, cpus=int(os.environ.get('CPUS', '8')),
                                            memory_bytes=2**30 - 4096, disk_bytes=40 * 2**30, written_bytes=2**30,
                                            write_errno=28)))
'''


SHA = 'a' * 40


class RunRepo(unittest.TestCase):
    VM, fake_adhoc, receipt = AdhocRun.VM, AdhocRun.fake_incus, AdhocRun.receipt
    HOST = AdhocRun.ROOMY_HOST.replace(
        'sys.exit(', 'cb.app_token = lambda path, scope=None: __import__("json").dumps([path, scope, "tok-SECRET"])\n'
                     'sys.exit(')

    def fake_incus(self, clone_fails=False):
        tmp, env = self.fake_adhoc()
        fake = tmp / 'incus'
        fail = '; echo "fatal: could not read from remote" >&2; exit 128' if clone_fails else ''
        fake.write_text(fake.read_text().replace(
            '  *" exec "*)\n',
            f'  *" exec "*"git init"*) printf "%s\\n" "$@" > {tmp}/clone-args; cat > {tmp}/clone-stdin{fail} ;;\n'
            '  *" exec "*)\n', 1))
        return tmp, env

    def assert_no_token(self, tmp, result):
        receipts = tmp / 'state' / 'receipts'
        for text in (result.stdout, result.stderr, (tmp / 'calls').read_text(),
                     *(path.read_text() for path in receipts.glob('cbx-*') if path.is_file())):
            self.assertNotIn('tok-SECRET', text)

    def coldbox(self, env, *args):
        return subprocess.run([sys.executable, '-c', self.HOST, str(ROOT / 'coldbox'), *args], env=env,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def test_repo_and_sha_clone_that_commit_in_the_vm_with_a_read_only_token_for_that_repo_alone(self):
        tmp, env = self.fake_incus()
        result = self.coldbox(env, 'run', '--ttl', '60', '--repo', 'me/app', '--sha', SHA)
        args = (tmp / 'clone-args').read_text().splitlines()
        self.assertEqual(args[-3:], ['clone', 'https://github.com/me/app', SHA], result.stderr)
        self.assertEqual(json.loads((tmp / 'clone-stdin').read_text()),
                         ['repos/me/app', {'repositories': ['app'], 'permissions': {'contents': 'read'}}, 'tok-SECRET'])
        self.assert_no_token(tmp, result)
        checks = [c for c in (tmp / 'calls').read_text().splitlines() if c.endswith('coldbox checks')]
        self.assertEqual(len(checks), 1)
        self.assertIn('--cwd /home/runner/repo', checks[0])
        receipt = self.receipt(tmp)
        self.assertEqual((receipt['mode'], receipt['repo'], receipt['sha'], receipt['vm']['destroyed']),
                         ('local', 'me/app', SHA, True))
        self.assertTrue([c for c in (tmp / 'calls').read_text().splitlines()
                         if ' coldbox junit ' in c and f'--archived {cb.ARCHIVED}' in c])

    def test_a_failed_clone_records_the_error_without_the_token(self):
        tmp, env = self.fake_incus(clone_fails=True)
        result = self.coldbox(env, 'run', '--ttl', '60', '--repo', 'me/app', '--sha', SHA)
        receipt = self.receipt(tmp)
        self.assertEqual((result.returncode, receipt['verdict'], receipt['vm']['destroyed']), (125, 'incomplete', True))
        self.assertIn('could not read from remote', receipt['error'])
        self.assert_no_token(tmp, result)

    def test_bad_or_partial_repo_and_sha_are_refused_before_any_vm(self):
        for args, code in ((['--repo', 'me/app', '--sha', SHA[:7]], 2), (['--repo', 'me/app', '--sha', SHA.upper()], 2),
                           (['--repo', 'me', '--sha', SHA], 2), (['--repo', 'me/../x', '--sha', SHA], 2),
                           (['--repo', 'me/..', '--sha', SHA], 2), (['--repo', 'me/app'], 1), (['--sha', SHA], 1),
                           (['--repo', 'me/app', '--sha', SHA, '--offline'], 1),
                           (['--repo', 'me/app', '--sha', SHA, '.', '--', 'true'], 1),
                           (['--repo', 'me/app', '--sha', SHA, '/tmp'], 1),
                           (['--repo', 'me/app', '--sha', SHA, '.'], 1)):
            tmp, env = self.fake_incus()
            result = self.coldbox(env, 'run', *args)
            self.assertEqual(result.returncode, code, (args, result.stderr))
            self.assertFalse((tmp / 'calls').exists(), args)

    def test_the_clone_script_checks_out_exactly_that_commit_and_lists_its_files(self):
        source = git_repo({'app.py': '', 'tests/test_a.py': ''})
        subprocess.run(['git', '-C', str(source), 'config', 'uploadpack.allowAnySHA1InWant', 'true'], check=True)
        sha = subprocess.run(['git', '-C', str(source), 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
        home = Path(tempfile.mkdtemp())
        script = cb.CLONE.replace(cb.ARCHIVED, str(home / 'archived'))

        def clone(want, token=''):
            return subprocess.run(['bash', '-o', 'pipefail', '-c', script, 'clone', f'file://{source}', want],
                                  input=token, capture_output=True, text=True, env=dict(os.environ, HOME=str(home)))

        cloned = clone(sha, 'secret')
        self.assertEqual(cloned.returncode, 0)
        self.assertNotIn('secret', cloned.stdout + cloned.stderr)
        self.assertEqual(subprocess.run(['git', '-C', str(home / 'repo'), 'rev-parse', 'HEAD'], capture_output=True,
                                        text=True).stdout.strip(), sha)
        self.assertEqual((home / 'archived').read_text().split(), [f'{home}/repo/app.py', f'{home}/repo/tests/test_a.py'])
        self.assertNotIn('secret', (home / 'repo' / '.git' / 'config').read_text())
        shutil.rmtree(home / 'repo')
        failed = clone('b' * 40, 'secret')
        self.assertNotEqual(failed.returncode, 0)
        self.assertNotIn('secret', failed.stdout + failed.stderr)


class IsolationSelftest(unittest.TestCase):
    VM, ROOMY_HOST = AdhocRun.VM, AdhocRun.ROOMY_HOST
    argv, fake_incus, coldbox, receipt = AdhocRun.argv, AdhocRun.fake_incus, AdhocRun.coldbox, AdhocRun.receipt
    EXPECTED = dict(cpus=8, memory_bytes=12 * 2**30, disk_bytes=40 * 2**30, write_bytes=41 * 2**30)

    def selftest(self, guest=FAKE_GUEST, **env):
        tmp, base = self.fake_incus()
        result = self.coldbox(dict(base, FAKE_GUEST=guest, **env), 'selftest', 'isolation', '--ttl', '120')
        return tmp, result

    def found(self, probes, **changes):
        return dict(dict(probes=[dict(p, error=None, errno=None) if p['kind'] == 'control' else
                                 dict(p, error='refused', errno=101 if p['kind'] == 'ipv6' else 111) for p in probes],
                         cpus=8, memory_bytes=12 * 2**30 - 2**20, disk_bytes=40 * 2**30, written_bytes=39 * 2**30,
                         write_errno=28),
                    **changes)

    def test_every_host_address_is_probed_once_on_the_listener_then_public_ipv6_and_a_public_control(self):
        addresses = [{'addr_info': [{'family': 'inet', 'local': '127.0.0.1'}, {'family': 'inet6', 'local': '::1'}]},
                     {'addr_info': [{'family': 'inet', 'local': '192.168.1.5'}, {'family': 'inet6', 'local': 'fe80::1'},
                                    {'family': 'inet6', 'local': '2001:db8::5'}]},
                     {'addr_info': [{'family': 'inet', 'local': '100.101.1.2'}]},
                     {'addr_info': [{'family': 'inet', 'local': '8.8.4.4'}, {'family': 'inet', 'local': '169.254.3.4'}]},
                     {'addr_info': [{'family': 'inet', 'local': cb.GATEWAY}, {'family': 'inet', 'local': '10.204.0.7'}]}]
        probes = cb.isolation_probes(addresses, 4242)
        self.assertEqual([(p['kind'], p['address'], p['port']) for p in probes], [
            ('host', cb.GATEWAY, 4242), ('lan', '192.168.1.5', 4242), ('ipv6', '2001:db8::5', 4242),
            ('tailnet', '100.101.1.2', 4242), ('host', '8.8.4.4', 4242), ('lan', '169.254.3.4', 4242),
            ('lan', '10.204.0.7', 4242),
            ('ipv6', '2606:4700:4700::1111', 443), ('control', 'api.github.com', 443)])

    def test_problems_name_each_broken_promise(self):
        probes = cb.isolation_probes([{'addr_info': [{'family': 'inet', 'local': '192.168.1.5'}]}], 4242)
        self.assertEqual(cb.isolation_problems(self.found(probes), probes, self.EXPECTED), [])
        reached = self.found(probes)
        reached['probes'][1]['error'] = None
        reached['probes'][2].update(error='TimeoutError: timed out', errno=None)
        reached['probes'][-1]['error'] = 'TimeoutError: timed out'
        self.assertEqual(cb.isolation_problems(reached, probes, self.EXPECTED), [
            'lan 192.168.1.5 port 4242 was reachable',
            'ipv6 2606:4700:4700::1111 port 443 failed with TimeoutError: timed out, not network unreachable, '
            'so the VM may route IPv6',
            'control api.github.com port 443 unreachable (TimeoutError: timed out), so the blocked results prove nothing'])
        self.assertEqual(cb.isolation_problems(self.found(probes, cpus=16, memory_bytes=12 * 2**30 + 1,
                                                          disk_bytes=80 * 2**30, write_errno=None),
                                               probes, self.EXPECTED),
                         ["nproc is 16, not the profile's 8",
                          f"MemTotal is {12 * 2**30 + 1} bytes, above the profile's {12 * 2**30}",
                          f"the root disk is {80 * 2**30} bytes, above the profile's {40 * 2**30}",
                          'a 41 GiB write succeeded'])
        self.assertEqual(cb.isolation_problems(self.found(probes, write_errno=5), probes, self.EXPECTED),
                         ['a 41 GiB write failed with errno 5, not ENOSPC'])
        self.assertEqual(cb.isolation_problems(None, probes, self.EXPECTED), ['the guest reported no results'])
        self.assertEqual(cb.isolation_problems(self.found(probes[:-1]), probes, self.EXPECTED),
                         ['the guest did not report every probe'])

    def test_the_guest_script_reports_what_it_reached_its_cpus_memory_and_the_write(self):
        home = Path(tempfile.mkdtemp())
        with socket_listener() as listener:
            port = listener.getsockname()[1]
            closed = socket_listener()
            closed_port = closed.getsockname()[1]
            closed.close()
            spec = dict(probes=[dict(kind='host', address='127.0.0.1', port=port),
                                dict(kind='lan', address='127.0.0.1', port=closed_port)], write_bytes=2**20)
            result = subprocess.run([sys.executable, '-c', cb.SELFTEST_GUEST], input=json.dumps(spec), text=True,
                                    capture_output=True, env=dict(os.environ, HOME=str(home)), check=True)
        found = json.loads(result.stdout.removeprefix(cb.SELFTEST_MARK))
        self.assertEqual([(p['error'] is None, p['errno']) for p in found['probes']], [(True, None), (False, 111)])
        self.assertEqual((found['cpus'], found['written_bytes'], found['write_errno']),
                         (len(os.sched_getaffinity(0)), 64 * 2**20, None))
        self.assertGreater(found['memory_bytes'], 0)
        self.assertGreater(found['disk_bytes'], 0)
        self.assertEqual(list(home.iterdir()), [])

    def test_a_guest_that_reached_nothing_blocked_passes_and_the_receipt_holds_the_results(self):
        tmp, result = self.selftest()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = self.receipt(tmp)
        self.assertEqual((receipt['mode'], receipt['repo'], receipt['verdict'], receipt['vm']['destroyed']),
                         ('adhoc', 'selftest', 'pass', True))
        self.assertEqual(receipt['isolation']['problems'], [])
        self.assertEqual(receipt['isolation']['expected'],
                         dict(cpus=8, memory_bytes=2**30, disk_bytes=40 * 2**30, write_bytes=41 * 2**30))
        self.assertEqual({p['kind'] for p in receipt['isolation']['found']['probes']}, {'host', 'ipv6', 'control'})
        self.assertIn('coldbox: PASS receipt', result.stderr)

    def test_a_reachable_host_address_fails_the_selftest(self):
        tmp, result = self.selftest(REACH='control host')
        self.assertEqual(result.returncode, 1, result.stderr)
        receipt = self.receipt(tmp)
        self.assertEqual(receipt['verdict'], 'fail')
        port = receipt['isolation']['found']['probes'][0]['port']
        self.assertEqual(receipt['isolation']['problems'], [f'host {cb.GATEWAY} port {port} was reachable'])
        self.assertIn('coldbox: FAIL receipt', result.stderr)
        self.assertIn(f'coldbox selftest: FAIL host {cb.GATEWAY} port {port} was reachable', result.stderr)

    def test_a_vm_with_more_cpus_than_the_profile_fails(self):
        tmp, result = self.selftest(CPUS='16')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.receipt(tmp)['isolation']['problems'], ["nproc is 16, not the profile's 8"])

    def test_a_guest_that_reports_nothing_fails(self):
        tmp, result = self.selftest(guest='import sys; sys.stdin.read()')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.receipt(tmp)['isolation']['problems'], ['the guest reported no results'])


def socket_listener():
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen(4)
    return sock


class InstallUnits(unittest.TestCase):
    def test_uninstall_removes_and_install_enables_every_unit_install_writes(self):
        install, uninstall = (ROOT / 'install.sh').read_text(), (ROOT / 'uninstall.sh').read_text()
        units = re.findall(r'^cat >/etc/systemd/system/(\S+) <<EOF$', install, re.M)
        self.assertIn('coldbox-selftest.timer', units)
        start = install.index('cat >/etc/systemd/system/coldbox-selftest.service <<EOF')
        selftest = install[start:install.index('\nEOF\n', start)].splitlines()
        for line in ('User=$user', 'SupplementaryGroups=incus-admin', 'Restart=on-failure', 'StartLimitBurst=3'):
            self.assertIn(line, selftest)
        enabled = re.search(r'^systemctl enable --now (coldbox-.+)$', install, re.M)[1].split()
        disabled = re.search(r'^systemctl disable --now (.+?) 2>', uninstall, re.M)[1].split()
        stop = uninstall.index('\nsystemctl stop coldbox-selftest.service ')
        self.assertLess(uninstall.index('\nsystemctl disable --now coldbox-'), stop)
        self.assertLess(stop, uninstall.index('\nrm -f /etc/systemd/system/coldbox-'))
        for unit in units:
            self.assertIn(f'/etc/systemd/system/{unit}', uninstall)
            if unit.endswith('.timer'):
                self.assertIn(unit, enabled)
                self.assertIn(unit, disabled)


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


class CIAdmission(unittest.TestCase):
    def evidence(self, **changes):
        return dict(dict(host='primary-host', target='me/app', measured_at=time.time(), cpus=8,
                         busy_cpus=8, load_avg=1.0, available_memory=32 * GIB, vm_memory=4 * GIB, vms=3, vm_ceiling=7),
                    **changes)

    def test_without_policy_a_single_host_is_its_own_primary(self):
        with mock.patch.object(cb, 'CI_HOSTS', Path(tempfile.mkdtemp()) / 'missing'), \
                mock.patch.object(cb.os, 'uname', return_value=argparse.Namespace(nodename='solo')), \
                mock.patch.object(cb, 'host_evidence') as probe:
            self.assertEqual(cb.ci_host(), ('solo', 'solo'))
            with mock.patch.object(cb.time, 'time', return_value=1000.5):
                record = cb.runner_admission('me/app', 'cbx-1', 4600)
            probe.assert_not_called()
        self.assertEqual((record['host'], record['primary'], record['admitted_at'], record['ttl'], record['primary_capacity']),
                         ('solo', 'solo', 1000.5, 3599, None))

    def test_without_policy_no_multi_host_path_runs_ssh(self):
        ran = []
        with mock.patch.object(cb, 'CI_HOSTS', Path(tempfile.mkdtemp()) / 'missing'), \
                mock.patch.object(cb.os, 'uname', return_value=argparse.Namespace(nodename='solo')), \
                mock.patch.object(cb, 'ssh', side_effect=AssertionError('ssh without ci-hosts.json')), \
                mock.patch.object(cb.subprocess, 'run', side_effect=lambda argv, **kw: ran.append(argv) or
                                  subprocess.CompletedProcess(argv, 1, '', '')), \
                mock.patch.multiple(cb, admit=lambda *a: False, adhoc_vms=lambda: {}):
            self.assertIsNone(cb.fleet())
            self.assertEqual(cb.adhoc_peers(), [])
            self.assertEqual(cb.runner_admission('me/app', 'cbx-1', time.time() + 3600)['host'], 'solo')
            with self.assertRaisesRegex(SystemExit, 'job VM ceiling'):
                cb.main(['vm', 'up'])
            for action in ('down', 'exec'):
                with self.assertRaisesRegex(SystemExit, 'no ad-hoc VM named'):
                    cb.main(['vm', action, 'cbx-5-x-vm-a'] + ['--', 'true'] * (action == 'exec'))
        self.assertEqual(ran, [])

    def test_policy_parses_once_for_every_multi_host_caller(self):
        path = Path(tempfile.mkdtemp()) / 'ci-hosts.json'
        path.write_text(json.dumps(dict(primary='host-p', overflow=['host-a', 'host-b'])))
        with mock.patch.object(cb, 'CI_HOSTS', path), \
                mock.patch.object(cb.os, 'uname', return_value=argparse.Namespace(nodename='host-a')):
            self.assertEqual(cb.fleet(), ('host-p', ['host-a', 'host-b']))
            self.assertEqual(cb.adhoc_peers(), ['host-b'])
            with mock.patch.object(cb.os, 'uname', return_value=argparse.Namespace(nodename='scripts-host')):
                self.assertEqual(cb.adhoc_peers(), [])
                self.assertEqual(cb.fleet(), ('host-p', ['host-a', 'host-b']))

    def test_policy_naming_another_primary_refuses_register_and_serve(self):
        path = Path(tempfile.mkdtemp()) / 'ci-hosts.json'
        path.write_text(json.dumps(dict(primary='host-p', overflow=[])))
        with mock.patch.object(cb, 'CI_HOSTS', path), \
                mock.patch.object(cb.os, 'uname', return_value=argparse.Namespace(nodename='scripts-host')), \
                mock.patch.object(cb, 'gh') as gh, mock.patch.object(cb, 'serve_slots') as slots:
            for command in (cb.register, cb.serve):
                with self.assertRaisesRegex(RuntimeError, 'not admitted'):
                    command(argparse.Namespace(slots=0))
            gh.assert_not_called()
            slots.assert_not_called()

    def test_policy_admits_each_listed_overflow_host_only(self):
        path = Path(tempfile.mkdtemp()) / 'ci-hosts.json'
        path.write_text(json.dumps(dict(primary='host-p', overflow=['host-a', 'host-b', 'host-c'])))
        with mock.patch.object(cb, 'CI_HOSTS', path):
            for host in ('host-p', 'host-a', 'host-b', 'host-c'):
                with self.subTest(host=host), mock.patch.object(
                        cb.os, 'uname', return_value=argparse.Namespace(nodename=host)):
                    self.assertEqual(cb.ci_host(), (host, 'host-p'))
            with mock.patch.object(cb.os, 'uname', return_value=argparse.Namespace(nodename='scripts-host')), \
                    self.assertRaisesRegex(RuntimeError, 'not admitted'):
                cb.ci_host()
        for overflow in (['host-a', 'host-a'], ['host-a', 'host-p'], ['host-a', '-x']):
            path.write_text(json.dumps(dict(primary='host-p', overflow=overflow)))
            with self.subTest(overflow=overflow), mock.patch.object(cb, 'CI_HOSTS', path), \
                    mock.patch.object(cb.os, 'uname', return_value=argparse.Namespace(nodename='host-a')), \
                    self.assertRaisesRegex(RuntimeError, 'invalid CI hosts'):
                cb.ci_host()

    def test_unreadable_or_invalid_policy_denies_registration(self):
        directory = Path(tempfile.mkdtemp())
        (directory / 'not-json').write_text('{')
        (directory / 'a-directory').mkdir()
        (directory / 'dangling').symlink_to(directory / 'missing')
        for name in ('not-json', 'a-directory', 'dangling'):
            with self.subTest(name=name), mock.patch.object(cb, 'CI_HOSTS', directory / name), \
                    self.assertRaisesRegex(RuntimeError, 'admission denied'):
                cb.register(argparse.Namespace(slots=0))

    def test_every_policy_refusal_names_the_file_its_shape_and_the_docs(self):
        directory = Path(tempfile.mkdtemp())
        (directory / 'not-json').write_text('{')
        (directory / 'no-primary').write_text('{"overflow": []}')
        (directory / 'bad-overflow').write_text('{"primary": "host-p", "overflow": ["host-p"]}')
        (directory / 'unlisted').write_text('{"primary": "host-p", "overflow": []}')
        (directory / 'a-directory').mkdir()
        (directory / 'dangling').symlink_to(directory / 'missing')
        for name in ('not-json', 'no-primary', 'bad-overflow', 'unlisted', 'a-directory', 'dangling'):
            path = directory / name
            with self.subTest(name=name), mock.patch.object(cb, 'CI_HOSTS', path), \
                    mock.patch.object(cb.os, 'uname', return_value=argparse.Namespace(nodename='scripts-host')):
                code, err = quiet(cb.main, ['register', 'me/app', '--slots', '0'])
                self.assertEqual(code, 125)
                self.assertIn(str(path), err)
                self.assertIn('{"primary": "<host>", "overflow": ["<host>", ...]}', err)
                self.assertIn('https://github.com/TayHCode/coldbox#autoscaling', err)
                self.assertNotIn('delete', err)
        with mock.patch.object(cb, 'CI_HOSTS', directory / 'unlisted'), \
                mock.patch.object(cb.os, 'uname', return_value=argparse.Namespace(nodename='scripts-host')):
            self.assertIn('scripts-host is not admitted for CI: it is not listed in', quiet(cb.main, ['register', 'me/app'])[1])
        with mock.patch.object(cb, 'CI_HOSTS', directory / 'no-primary'):
            self.assertIn("missing key 'primary'", quiet(cb.main, ['register', 'me/app'])[1])

    def test_every_listed_host_admits_its_own_jobs_without_probing_the_primary(self):
        with mock.patch.object(cb, 'ci_host', return_value=('overflow-host', 'primary-host')), \
                mock.patch.object(cb, 'host_evidence') as probe:
            record = cb.runner_admission('me/app', 'cbx-test', time.time() + 3600)
        probe.assert_not_called()
        self.assertEqual((record['host'], record['primary'], record['target'], record['guest']),
                         ('overflow-host', 'primary-host', 'me/app', 'cbx-test'))

    def test_workflow_guard_checks_identity_and_overflow_evidence(self):
        evidence = self.evidence()
        record = dict(schema='coldbox.admission/v1', host='overflow-host', primary='primary-host',
                      target='me/app', guest='cbx-test', admitted_at=time.time(), ttl=3600, primary_capacity=evidence)
        args = argparse.Namespace(expected_primary='primary-host', overflow_host=['overflow-host'])
        def read(path):
            return json.dumps(record) if str(path).endswith('admission.json') else 'overflow-host'
        with mock.patch.object(cb.Path, 'read_text', read), mock.patch.dict(os.environ, RUNNER_NAME='cbx-test', GITHUB_REPOSITORY='me/app'):
            self.assertEqual(quiet(cb.admission_check, args)[0], 0)
            for key, value in (('guest', 'cbx-other'), ('host', 'scripts-host')):
                original = record[key]
                record[key] = value
                with self.assertRaisesRegex(RuntimeError, 'guard failed'):
                    cb.admission_check(args)
                record[key] = original

    def test_workflows_that_still_pass_overflow_host_keep_passing_the_guard(self):
        record = dict(schema='coldbox.admission/v1', host='overflow-host', primary='primary-host', target='me/app',
                      guest='cbx-test', admitted_at=time.time(), ttl=3600, primary_capacity=None)
        def read(path):
            return json.dumps(record) if str(path).endswith('admission.json') else 'overflow-host'
        with mock.patch.object(cb.Path, 'read_text', read), \
                mock.patch.dict(os.environ, RUNNER_NAME='cbx-test', GITHUB_REPOSITORY='me/app'):
            for extra in ([], ['--overflow-host', ''], ['--overflow-host', 'overflow-host'], ['--overflow-host']):
                with self.subTest(extra=extra):
                    self.assertEqual(quiet(cb.main, ['admission-check', '--expected-primary', 'primary-host', *extra])[0], 0)
            with mock.patch.dict(record, guest='cbx-other'):
                code, output = quiet(cb.main, ['admission-check', '--expected-primary', 'primary-host', '--overflow-host', ''])
            self.assertNotEqual(code, 0)
            self.assertIn('guard failed', output)

    def test_a_warm_vm_on_any_listed_host_starts_a_job_until_its_ttl_and_never_after(self):
        args = argparse.Namespace(expected_primary='primary-host')
        env = dict(RUNNER_NAME='cbx-test', GITHUB_REPOSITORY='me/app')
        for host in ('primary-host', 'overflow-host'):
            record = dict(schema='coldbox.admission/v1', host=host, primary='primary-host', target='me/app',
                          guest='cbx-test', admitted_at=100000 - 6 * 3600, ttl=6 * 3600, primary_capacity=None)
            def read(path):
                return json.dumps(record) if str(path).endswith('admission.json') else host
            with self.subTest(host=host), mock.patch.object(cb.Path, 'read_text', read):
                for now in (99999, 100000):
                    with mock.patch.object(cb.time, 'time', return_value=now):
                        self.assertEqual(quiet(cb.admission_check, args, env)[0], 0)
                with mock.patch.object(cb.time, 'time', return_value=100000):
                    with mock.patch.dict(record, primary='other-primary'), self.assertRaisesRegex(RuntimeError, 'guard failed'):
                        cb.admission_check(args, env)
                    for ttl in (6 * 3600 - 1, 0, -1, True, False, '21600', 21600.0, None):
                        with mock.patch.dict(record, ttl=ttl), self.assertRaisesRegex(RuntimeError, 'outlived its VM TTL|guard failed'):
                            cb.admission_check(args, env)
                with mock.patch.object(cb.time, 'time', return_value=100001), \
                        self.assertRaisesRegex(RuntimeError, 'outlived its VM TTL'):
                    cb.admission_check(args, env)
                with mock.patch.dict(record, admitted_at=100000, ttl=0), mock.patch.object(cb.time, 'time', return_value=100000), \
                        self.assertRaisesRegex(RuntimeError, 'outlived its VM TTL'):
                    cb.admission_check(args, env)
                with mock.patch.dict(record), mock.patch.object(cb.time, 'time', return_value=100000):
                    del record['ttl']
                    with self.assertRaisesRegex(RuntimeError, 'guard failed'):
                        cb.admission_check(args, env)

    def test_organization_record_accepts_only_its_own_repositories(self):
        record = dict(schema='coldbox.admission/v1', host='ci-primary', primary='ci-primary',
                      target='owner', guest='cbx-org', admitted_at=time.time(), ttl=3600, primary_capacity=None)
        args = argparse.Namespace(expected_primary='ci-primary', overflow_host=[])
        def read(path):
            return json.dumps(record) if str(path).endswith('admission.json') else 'ci-primary'
        with mock.patch.object(cb.Path, 'read_text', read):
            self.assertEqual(quiet(cb.admission_check, args, dict(RUNNER_NAME='cbx-org', GITHUB_REPOSITORY='owner/repo'))[0], 0)
            with self.assertRaisesRegex(RuntimeError, 'guard failed'):
                cb.admission_check(args, dict(RUNNER_NAME='cbx-org', GITHUB_REPOSITORY='other/repo'))

    def test_primary_admission_has_no_separate_six_hour_limit(self):
        record = dict(schema='coldbox.admission/v1', host='ci-primary', primary='ci-primary',
                      target='me/app', guest='cbx-long', admitted_at=1000, ttl=86400, primary_capacity=None)
        args = argparse.Namespace(expected_primary='ci-primary', overflow_host=[])
        env = dict(RUNNER_NAME='cbx-long', GITHUB_REPOSITORY='me/app')
        def read(path):
            return json.dumps(record) if str(path).endswith('admission.json') else 'ci-primary'
        with mock.patch.object(cb.Path, 'read_text', read), mock.patch.object(cb.time, 'time', return_value=30000):
            code, output = quiet(cb.admission_check, args, env)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output)['guest'], 'cbx-long')
            record['admitted_at'] = 30001
            with self.assertRaisesRegex(RuntimeError, 'guard failed'):
                cb.admission_check(args, env)

    def test_runner_admission_sidecar_does_not_break_status(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            receipts = state / 'receipts'
            clock = [1000.0]
            def slow_boot(*args, **kwargs):
                clock[0] += 120
            with mock.patch.multiple(cb, boot=mock.Mock(side_effect=slow_boot), ic=mock.DEFAULT, destroy=lambda name: True,
                                     tee=mock.Mock(side_effect=SystemExit(0))), \
                    mock.patch.object(cb.time, 'time', lambda: clock[0]), \
                    mock.patch.object(cb.subprocess, 'run'), self.assertRaises(SystemExit):
                cb.job('runner', 'cbx-status', 43200, ['run.sh'], repo='me/app', receipts=receipts)
            self.addCleanup(cb.CLEANUP.clear)
            (state / 'systemctl').write_text('#!/bin/sh\nexit 3\n')
            (state / 'systemctl').chmod(0o755)
            with mock.patch.dict(os.environ, PATH=str(state)), mock.patch.object(cb, 'STATE', state):
                code, output = quiet(cb.status, None)
            self.assertEqual(code, 1)
            self.assertIn('cbx-status', output.split('Recent receipts')[1])
            self.assertIn('me/app', output.split('Recent receipts')[1])
            self.assertEqual([p.name for p in receipts.glob('cbx-*.json')], ['cbx-status.json'])
            admission = json.loads((receipts / 'cbx-status.json').read_text())['admission']
            self.assertEqual(admission['guest'], 'cbx-status')
            self.assertEqual((admission['admitted_at'], admission['admitted_at'] + admission['ttl']), (1120.0, 1000 + 43200))

    def test_direct_burst_worker_preserves_zero_slots(self):
        args = argparse.Namespace(target='me/app', ttl=60, slots=0, allow_public=False, allow_forks=False)
        argv = cb.worker_argv(args, '--once', '--idle', '600')
        self.assertEqual(argv[argv.index('--slots')+1], '0')

    def test_job_started_rejects_another_guests_admission_before_untrusted_code(self):
        record = dict(schema='coldbox.admission/v1', host='overflow-host', primary='primary-host',
                      target='me/app', guest='cbx-other', admitted_at=time.time()-60, ttl=3600)
        def read(path):
            return json.dumps(record) if str(path).endswith('admission.json') else 'overflow-host'
        with mock.patch.object(cb.Path, 'read_text', read), self.assertRaisesRegex(RuntimeError, 'guard failed'):
            cb.job_started(dict(COLDBOX_CI_PRIMARY='primary-host', RUNNER_NAME='cbx-test', GITHUB_REPOSITORY='me/app', GITHUB_EVENT_NAME='push'))

    def test_cli_denies_unlisted_host_before_jit_or_service_effects(self):
        with mock.patch.object(cb.os, 'uname', return_value=argparse.Namespace(nodename='scripts-host')), \
                mock.patch.object(cb, 'incus_group_exec'), mock.patch.object(cb, 'gh') as github:
            self.assertEqual(quiet(cb.main, ['serve', 'me/app', '--slots', '0'])[0], 125)
            self.assertEqual(quiet(cb.main, ['register', 'me/app', '--slots', '0'])[0], 125)
            github.assert_not_called()

    def test_the_idle_timer_starts_after_guest_preparation(self):
        events = []
        timer = mock.Mock()
        timer.start.side_effect = lambda: events.append('timer')
        def prepared_job(*args, **kwargs):
            events.append('prepared')
            kwargs['on_ready']()
            events.append('command')
            return {'verdict': 'pass'}
        args = argparse.Namespace(target='me/app', slots=0, worker=True, once=True, idle=600,
                                  ttl=600, name='cbx-overflow', allow_public=False, allow_forks=False, size='default')
        for signum in (cb.signal.SIGTERM, cb.signal.SIGUSR1):
            self.addCleanup(cb.signal.signal, signum, cb.signal.getsignal(signum))
        with mock.patch.multiple(cb, ci_host=lambda: ('overflow-host', 'primary-host'),
                                 runner_admission=lambda *a: {}, guard=lambda *a: None,
                                 note_worker=lambda *a: None, job=prepared_job,
                                 gh=lambda *a, **kw: {'runner': {'id': 7}, 'encoded_jit_config': 'c'}), \
                mock.patch.object(cb.threading, 'Timer', return_value=timer) as factory:
            self.assertEqual(quiet(cb.serve, args)[0], 0)
        factory.assert_called_once_with(600, cb.release_idle, ('me/app', 7))
        self.assertEqual(events, ['prepared', 'timer', 'command'])
        timer.cancel.assert_called_once()


class VmResources(unittest.TestCase):
    METRICS = ('incus_disk_read_bytes_total{device="sda",name="cbx-a",project="coldbox",type="virtual-machine"} 8.0e+08\n'
               'incus_disk_read_bytes_total{device="sda1",name="cbx-a",project="coldbox",type="virtual-machine"} 3e+06\n'
               'incus_disk_read_bytes_total{device="loop0",name="cbx-a",project="coldbox",type="virtual-machine"} 9\n'
               'incus_disk_read_bytes_total{device="sda",name="cbx-ab",project="coldbox",type="virtual-machine"} 5\n'
               'incus_disk_written_bytes_total{device="sda",name="cbx-a",project="coldbox",type="virtual-machine"} 7e+08\n')

    def incus(self, metrics=METRICS, state=None):
        state = state or {'pid': os.getpid(), 'cpu': {'usage': 404145108000}}
        calls = []

        def run(argv, **kwargs):
            calls.append(kwargs.get('timeout'))
            path = argv[-1]
            if '/metrics' in path:
                out = metrics
            elif '/state' in path:
                out = json.dumps(state)
            else:
                out = json.dumps({'expanded_config': {'limits.cpu': '8', 'limits.memory': '12GiB'}})
            return subprocess.CompletedProcess(argv, 0, stdout=out)
        return run, calls

    def test_reads_what_the_vm_was_given_and_used_with_a_timeout_on_every_query(self):
        run, calls = self.incus()
        with mock.patch.object(cb.subprocess, 'run', run):
            found = cb.vm_resources('cbx-a')
        peak = int(next(line.split()[1] for line in Path('/proc/self/status').read_text().splitlines()
                        if line.startswith('VmHWM:'))) * 1024
        self.assertEqual(found, dict(cpus=8, memory_bytes=12 * GIB, peak_rss_bytes=peak, cpu_seconds=404.145,
                                     disk_read_bytes=800_000_000, disk_write_bytes=700_000_000))
        self.assertEqual(calls, [10, 10, 10])

    def test_a_pinned_cpu_set_counts_its_cpus_and_a_bad_cpu_limit_keeps_the_memory(self):
        self.assertEqual([cb.cpu_count(limit) for limit in ('8', '0-3', '0-3,8', '2')], [8, 4, 5, 2])
        run, _ = self.incus()

        def bad_cpu(argv, **kwargs):
            result = run(argv, **kwargs)
            return result if '/state' in argv[-1] or '/metrics' in argv[-1] else subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps({'expanded_config': {'limits.cpu': 'all', 'limits.memory': '12GiB'}}))
        with mock.patch.object(cb.subprocess, 'run', bad_cpu):
            found = cb.vm_resources('cbx-a')
        self.assertEqual((found['cpus'], found['memory_bytes']), (None, 12 * GIB))

    def test_a_value_that_cannot_be_read_is_null_and_never_raises(self):
        cases = [(subprocess.TimeoutExpired('incus', 10), dict.fromkeys(cb.RESOURCE_FIELDS))]
        sdb = 'incus_disk_read_bytes_total{device="sdb",name="cbx-a",project="coldbox",type="virtual-machine"} 1\n'
        for metrics in (self.METRICS.replace('7e+08', '+Inf'),
                        self.METRICS.replace('incus_disk_written', 'incus_other'), self.METRICS + sdb):
            run, _ = self.incus(metrics=metrics, state={'pid': 2**31, 'cpu': {'usage': 1e9}})
            cases.append((run, dict(dict.fromkeys(cb.RESOURCE_FIELDS), cpus=8, memory_bytes=12 * GIB, cpu_seconds=1.0)))
        for run, expected in cases:
            with self.subTest(run=run), mock.patch.object(cb.subprocess, 'run', side_effect=run):
                self.assertEqual(cb.vm_resources('cbx-a'), expected)


class Stats(unittest.TestCase):
    NOW = cb.at('2026-11-11T12:00:00+00:00')

    def runner(self, name, start, queue, ready, wall, host='host-p', repo='me/app', primary='host-p', **extra):
        started = self.NOW - 3000 + start
        stamp = lambda offset: datetime.fromtimestamp(started + offset, timezone.utc).isoformat()
        github = dict(repo=repo, refused=None, runner_started_at=stamp(ready + 5), completed_at=stamp(ready + 5 + wall),
                      steps=[dict(name='Set up job', seconds=2), dict(name='test', seconds=wall - 3)])
        if queue is not None:
            github['queue_wait_seconds'] = queue
        receipt = dict(schema='coldbox.receipt/v1', id=name, mode='runner', repo=repo, host=host, started_at=stamp(0),
                       seconds=ready + wall + 20, vm=dict(ready_seconds=ready, host_ceiling=40, destroyed=True), github=github,
                       admission=dict(host=host, primary=primary), checks=[])
        return dict(receipt, **extra)

    def receipts(self):
        return [self.runner('cbx-1', 0, 10, 40, 100),
                self.runner('cbx-2', 50, 30, 60, 300),
                self.runner('cbx-3', 70, None, 50, 200, host='host-a', repo='me/other'),
                dict(self.runner('cbx-4', 80, 5, 45, 1), github=None)]

    def test_summarizes_queue_boot_and_job_time_per_repo_and_host(self):
        report = cb.stats_report(self.receipts(), 3600, self.NOW)
        app = report['repos']['me/app']
        self.assertEqual(app['jobs'], 2)
        self.assertEqual(app['queue_wait'], dict(n=2, p50=10, p90=30, max=30, missing=0))
        self.assertEqual(app['boot_to_ready'], dict(n=2, p50=40, p90=60, max=60, missing=0))
        self.assertEqual(app['job_wall'], dict(n=2, p50=100, p90=300, max=300, missing=0))
        self.assertEqual((report['jobs'], report['vms']), (3, 4))
        self.assertEqual(report['slowest_steps'][0], dict(repo='me/app', step='test', n=2, p50=97, p90=297, max=297))

    def test_a_job_missing_a_field_is_counted_as_missing_not_as_zero(self):
        other = cb.stats_report(self.receipts(), 3600, self.NOW)['repos']['me/other']
        self.assertEqual(other['queue_wait'], dict(n=0, missing=1))
        self.assertEqual(other['job_wall']['n'], 1)
        old = dict(self.runner('cbx-5', 0, 9, 40, 100))
        del old['github']['completed_at'], old['github']['steps']
        report = cb.stats_report([old], 3600, self.NOW)
        self.assertEqual(report['job_wall'], dict(n=0, missing=1))
        self.assertEqual(report['jobs_without_steps'], 1)

    def test_host_saturation_shows_peak_vms_busy_jobs_ceiling_and_overflow(self):
        hosts = cb.stats_report(self.receipts(), 3600, self.NOW)['hosts']
        self.assertEqual(hosts['host-p'], dict(hosts['host-p'], vms=3, vms_peak=3, busy_peak=2, ceiling=40,
                                                  overflow_jobs=0))
        self.assertEqual((hosts['host-a']['overflow_jobs'], hosts['host-a']['busy_peak']), (1, 1))

    def test_a_vm_started_before_the_window_and_a_job_still_running_count_toward_saturation(self):
        log = Path(tempfile.mkdtemp()) / 'cbx-7.log'
        log.write_text('2026-11-11 11:59:00Z: Listening for Jobs\n2026-11-11 11:59:30Z: Running job: verify\n')
        before = self.runner('cbx-6', 2000, 5, 40, 980)
        live = dict(self.runner('cbx-7', 2900, None, 40, 1), log=str(log), github=None, vm=dict(destroyed=False))
        del live['seconds']
        waiting = dict(live, id='cbx-8', log=os.devnull)
        report = cb.stats_report([before, live, waiting, self.runner('cbx-1', 2950, 10, 20, 30)], 600, self.NOW)
        self.assertEqual(report['jobs'], 1)
        self.assertEqual(report['hosts']['host-p'], dict(report['hosts']['host-p'], vms=3, vms_peak=4,
                                                            busy_peak=3))

    def test_a_job_that_started_in_the_window_on_a_warm_vm_from_before_it_counts(self):
        warm = self.runner('cbx-9', 0, 4, 2700, 100)
        reaped = dict(self.runner('cbx-vm10', 2400, None, 10, 1), github=None,
                      vm=dict(destroyed=True, destroyed_at='2026-11-11T11:51:00+00:00'))
        del reaped['seconds']
        report = cb.stats_report([warm, reaped, self.runner('cbx-vm11', 2500, 2, 10, 1)], 600, self.NOW)
        self.assertEqual((report['jobs'], report['repos']['me/app']['queue_wait']['n']), (2, 2))
        self.assertEqual(report['hosts']['host-p']['vms_peak'], 2)

    def test_an_older_receipt_without_a_job_end_still_counts_its_job_and_busy_time(self):
        old = self.runner('cbx-vm14', 0, 4, 2700, 100)
        del old['github']['completed_at']
        report = cb.stats_report([old, self.runner('cbx-vm15', 2750, 2, 10, 100)], 600, self.NOW)
        self.assertEqual(report['jobs'], 2)
        self.assertEqual(report['job_wall'], dict(n=1, p50=100, p90=100, max=100, missing=1))
        self.assertEqual(report['hosts']['host-p']['busy_peak'], 2)

    def test_a_running_overflow_job_counts_as_overflow(self):
        log = Path(tempfile.mkdtemp()) / 'cbx-vm16.log'
        log.write_text('2026-11-11 11:59:30Z: Running job: verify\n')
        live = dict(self.runner('cbx-vm16', 2900, None, 40, 1, host='host-a'), log=str(log), github=None,
                    vm=dict(destroyed=False))
        del live['seconds']
        report = cb.stats_report([live], 600, self.NOW)
        self.assertEqual((report['jobs'], report['hosts']['host-a']['overflow_jobs'],
                          report['hosts']['host-a']['busy_peak']), (0, 1, 1))

    def test_a_vm_that_failed_to_delete_counts_until_it_is_deleted(self):
        stuck = dict(self.runner('cbx-vm12', 2400, 1, 10, 1), vm=dict(destroyed=False))
        later = self.runner('cbx-vm13', 2500, 1, 10, 1)
        self.assertEqual(cb.stats_report([stuck, later], 600, self.NOW)['hosts']['host-p']['vms_peak'], 2)
        stuck['vm'] = dict(destroyed=True, destroyed_at='2026-11-11T11:59:00+00:00')
        self.assertEqual(cb.stats_report([stuck, later], 600, self.NOW)['hosts']['host-p']['vms_peak'], 2)
        stuck['vm']['destroyed_at'] = '2026-11-11T11:51:00+00:00'
        self.assertEqual(cb.stats_report([stuck, later], 600, self.NOW)['hosts']['host-p']['vms_peak'], 1)

    def test_command_reads_the_window_and_prints_a_table_or_json(self):
        folder = Path(tempfile.mkdtemp())
        for receipt in self.receipts():
            receipt['id'] = f"cbx-{int(cb.at(receipt['started_at']))}-x{receipt['id'][-1]}"
            (folder / f"{receipt['id']}.json").write_text(json.dumps(receipt))
        stale = dict(self.receipts()[0], started_at='2026-11-01T00:00:00+00:00', id='cbx-1761955200-old')
        (folder / 'cbx-1761955200-old.json').write_text(json.dumps(stale))
        (folder / f'cbx-{int(self.NOW)}-bad.json').write_text('{')
        with mock.patch.object(cb.time, 'time', return_value=self.NOW):
            code, text = quiet(cb.main, ['stats', '--since', '1h', '--receipts', str(folder)])
            _, raw = quiet(cb.main, ['stats', '--since', '1h', '--receipts', str(folder), '--json'])
        self.assertEqual(code, 0)
        self.assertIn('coldbox stats: 3 finished jobs on 4 VMs since 2026-11-11T11:00:00+00:00', text)
        self.assertRegex(text, r'me/other +1 +- +50/50/50 +200/200/200 +queue_wait 1')
        self.assertRegex(text, r'host-a +1 +1 +40 +1')
        self.assertIn('skipped 1 unreadable receipt', text)
        report = json.loads(raw)
        self.assertEqual((report['jobs'], report['unreadable_receipts']), (3, 1))

    def outcome_receipts(self):
        vm = lambda **extra: dict(ready_seconds=40, destroy_seconds=10, host_ceiling=40, destroyed=True, **extra)
        passed = dict(self.runner('cbx-vm21', 0, 10, 40, 100), verdict='pass', vm=vm())
        failed = dict(self.runner('cbx-vm22', 100, 30, 60, 300), verdict='fail', cause='test',
                      vm=dict(vm(), ready_seconds=60))
        ttl = dict(self.runner('cbx-vm23', 200, 20, 40, 50), verdict='incomplete', cause='ttl', vm=vm())
        not_ready = dict(self.runner('cbx-vm24', 300, None, 40, 1), github=None, verdict='incomplete',
                         cause='vm_not_ready', vm=dict(destroyed=True))
        left = dict(self.runner('cbx-vm25', 400, 5, 40, 20), verdict='incomplete', cause='vm_not_destroyed',
                    vm=dict(vm(), destroyed=False))
        idle = dict(self.runner('cbx-vm26', 500, None, 40, 1), github=None, verdict='idle', vm=vm())
        canceled = dict(self.runner('cbx-vm27', 600, 5, 40, 30), verdict='canceled', vm=vm())
        refused = dict(self.runner('cbx-vm28', 700, None, 40, 1), github=dict(repo='me/app', refused='fork'),
                       verdict='refused', vm=vm())
        stuck = dict(self.runner('cbx-vm29', -9000, None, 40, 1), github=None, verdict='incomplete',
                     vm=dict(ttl_seconds=3600, destroyed=False))
        del stuck['seconds']
        old = dict(self.runner('cbx-vm30', -9000, 5, 40, 100), verdict='pass', vm=vm())
        across = dict(self.runner('cbx-vm31', -700, 5, 40, 200), verdict='pass', vm=vm())
        return [passed, failed, ttl, not_ready, left, idle, canceled, refused, stuck, old, across]

    def test_reports_definitive_share_incomplete_causes_jobs_per_hour_split_and_orphans(self):
        report = cb.stats_report(self.outcome_receipts(), 3600, self.NOW)
        self.assertEqual(report['outcomes'], {
            'settled': 7, 'definitive': 3, 'definitive_pct': 42.9,
            'verdicts': {'canceled': 1, 'fail': 1, 'incomplete': 3, 'pass': 2},
            'incomplete_causes': {'ttl': 1, 'vm_not_ready': 1, 'vm_not_destroyed': 1}})
        self.assertEqual(report['boot_to_ready'], dict(n=5, p50=40, p90=60, max=60, missing=0))
        self.assertEqual(report['queue_wait'], dict(n=5, p50=10, p90=30, max=30, missing=0))
        self.assertEqual((report['jobs'], report['jobs_per_hour']), (5, 6))
        self.assertEqual(report['split'], dict(jobs=5, missing=0, orchestration_seconds=270, test_seconds=500,
                                               orchestration_pct=35.1))
        self.assertEqual(report['orphans'], 2)
        self.assertEqual(cb.stats_report(self.outcome_receipts(), 7 * 86400, self.NOW)['outcomes']['settled'], 8)

    def test_jobs_per_hour_counts_jobs_by_when_they_ended_and_not_an_interrupted_job(self):
        torn_down_late = dict(self.runner('cbx-vm32', -705, 5, 40, 50), verdict='pass')
        stopped = dict(self.runner('cbx-vm33', 100, None, 40, 30), github=None, job_name='verify',
                       verdict='incomplete', cause='stopped')
        report = cb.stats_report([torn_down_late, stopped, self.runner('cbx-vm34', 200, 5, 40, 30)], 3600, self.NOW)
        self.assertEqual(report['outcomes']['settled'], 3)
        self.assertEqual(report['jobs_per_hour'], 1)

    def test_prints_the_outcome_lines(self):
        text = cb.stats_text(cb.stats_report(self.outcome_receipts(), 3600, self.NOW))
        self.assertIn('definitive (pass or fail): 3 of 7 settled, 42.9%', text)
        self.assertIn('incomplete causes: ttl 1, vm_not_ready 1, vm_not_destroyed 1', text)
        self.assertIn('ready p50/p90: 40/60 s; queue p50/p90: 10/30 s; jobs/hour: 6', text)
        self.assertIn('orchestration (VM ready + destroy) vs test (job wall): 270 s vs 500 s, 35.1% orchestration '
                      'over 5 jobs', text)
        self.assertIn('orphans (vm.destroyed false after the job or its TTL): 2', text)
        empty = cb.stats_text(cb.stats_report([], 3600, self.NOW))
        self.assertIn('definitive (pass or fail): 0 of 0 settled, -', empty)
        self.assertIn('ready p50/p90: -; queue p50/p90: -; jobs/hour: 0', empty)

    def test_window_must_be_a_positive_duration(self):
        self.assertEqual(cb.window('90m'), 5400)
        for text in ('0h', '1', 'h', '-1h', '1w'):
            with self.subTest(text=text), self.assertRaises(argparse.ArgumentTypeError):
                cb.window(text)
