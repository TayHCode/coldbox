import base64
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from test_coldbox import cb, Body


class AppAuth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        cls.key = Path(cls.scratch.name) / 'test.pem'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048',
                        '-out', str(cls.key)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.scratch.cleanup()

    def setUp(self):
        self.env = Path(self.scratch.name) / 'github-app.env'
        self.env.write_text('COLDBOX_GITHUB_APP_ID=12345\n')
        patches = [mock.patch.object(cb, 'APP_KEY', self.key, create=True),
                   mock.patch.object(cb, 'APP_ENV', self.env, create=True),
                   mock.patch.object(cb, 'APP_TOKENS', {}, create=True),
                   mock.patch.dict(os.environ, {'COLDBOX_GITHUB_APP_ID': ''}),
                   mock.patch.object(cb.time, 'time', return_value=1000000)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.requests = []

    def response(self, request, timeout):
        self.requests.append(request)
        if '/app/installations?' in request.full_url:
            value = [{'id': 7, 'account': {'login': 'owner'}}]
        elif request.full_url.endswith('/access_tokens'):
            value = {'token': 'test-token', 'expires_at': cb.datetime.fromtimestamp(cb.time.time() + 3600, cb.timezone.utc).isoformat()}
        else:
            value = {'ok': True}
        body = Body(json.dumps(value).encode())
        body.headers = {}
        return body

    def test_polling_uses_app_and_reuses_token(self):
        with mock.patch.object(cb.urllib.request, 'urlopen', self.response), \
                mock.patch.object(cb.subprocess, 'run', wraps=subprocess.run) as run:
            self.assertEqual(cb.GitHub().get('repos/owner/repo'), {'ok': True})
            self.assertEqual(cb.GitHub().get('repos/owner/other'), {'ok': True})
        self.assertFalse(any(call.args[0][:2] == ['gh', 'auth'] for call in run.call_args_list))
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(self.requests[-1].get_header('Authorization'), 'Bearer test-token')
        self.assertEqual(self.requests[1].get_method(), 'POST')
        jwt = self.requests[0].get_header('Authorization').split()[1]
        header, payload, signature = jwt.split('.')
        decode = lambda part: base64.urlsafe_b64decode(part + '=' * (-len(part) % 4))
        self.assertEqual(json.loads(decode(header)), {'alg': 'RS256', 'typ': 'JWT'})
        self.assertEqual(json.loads(decode(payload)), {'iat': 999940, 'exp': 1000540, 'iss': '12345'})
        public = Path(self.scratch.name) / 'public.pem'
        sig = Path(self.scratch.name) / 'signature'
        public.write_bytes(subprocess.run(['openssl', 'pkey', '-in', str(self.key), '-pubout'],
                                          check=True, capture_output=True).stdout)
        sig.write_bytes(decode(signature))
        verified = subprocess.run(['openssl', 'dgst', '-sha256', '-verify', str(public), '-signature', str(sig)],
                                  input=f'{header}.{payload}'.encode(), capture_output=True)
        self.assertEqual(verified.returncode, 0)

    def test_repo_token_asks_for_one_repository_with_contents_read_only(self):
        with mock.patch.object(cb.urllib.request, 'urlopen', self.response):
            self.assertEqual(cb.repo_token('owner/app'), 'test-token')
        mint = [r for r in self.requests if r.full_url.endswith('/access_tokens')]
        self.assertEqual([(r.full_url, r.get_method()) for r in mint],
                         [('https://api.github.com/app/installations/7/access_tokens', 'POST')])
        self.assertEqual(json.loads(mint[0].data), {'repositories': ['app'], 'permissions': {'contents': 'read'}})

    def test_without_an_app_an_installation_or_a_contents_grant_repo_token_warns_and_clones_anonymously(self):
        def refuse(request, timeout):
            if request.full_url.endswith('/access_tokens'):
                raise cb.urllib.error.HTTPError(request.full_url, 422, 'Unprocessable', {}, io.BytesIO(b''))
            return self.response(request, timeout)

        for urlopen, repo, key in ((refuse, 'owner/app', self.key), (self.response, 'pypa/sampleproject', self.key),
                                   (self.response, 'owner/app', Path(self.scratch.name) / 'missing.pem')):
            err = io.StringIO()
            with mock.patch.object(cb.urllib.request, 'urlopen', urlopen), mock.patch.object(cb.sys, 'stderr', err), \
                    mock.patch.object(cb, 'APP_KEY', key):
                self.assertIsNone(cb.repo_token(repo))
            self.assertIn(f'no GitHub App token can read {repo} (the App needs Contents: read on that repo); '
                          'cloning anonymously', err.getvalue())

    def test_a_transient_mint_failure_still_stops_the_run(self):
        def down(request, timeout):
            if request.full_url.endswith('/access_tokens'):
                raise cb.urllib.error.HTTPError(request.full_url, 502, 'Bad Gateway', {}, io.BytesIO(b''))
            return self.response(request, timeout)

        with mock.patch.object(cb.urllib.request, 'urlopen', down), self.assertRaises(cb.GitHubTransient):
            cb.repo_token('owner/app')

    def test_refresh_before_expiration(self):
        with mock.patch.object(cb.urllib.request, 'urlopen', self.response):
            cb.GitHub().get('repos/owner/repo')
            with mock.patch.object(cb.time, 'time', return_value=1003301):
                cb.GitHub().get('repos/owner/repo')
        self.assertEqual(sum(r.full_url.endswith('/access_tokens') for r in self.requests), 2)

    def test_gh_receives_app_token_even_with_personal_token_in_environment(self):
        with mock.patch.object(cb.urllib.request, 'urlopen', self.response), \
                mock.patch.object(cb, 'app_jwt', return_value='fixture-jwt', create=True), \
                mock.patch.dict(os.environ, {'GH_TOKEN': 'fixture-personal'}), \
                mock.patch.object(cb.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{}')) as run:
            cb.gh('-X', 'POST', 'repos/owner/repo/actions/runners/generate-jitconfig', body={})
        self.assertEqual(run.call_args.kwargs['env']['GH_TOKEN'], 'test-token')

    def test_configured_failure_never_uses_gh_auth_or_echoes_response(self):
        failure = cb.urllib.error.HTTPError('https://api.github.com/app/installations', 401, 'fixture-secret', {},
                                           io.BytesIO(b'fixture-secret'))
        with mock.patch.object(cb.urllib.request, 'urlopen', side_effect=failure), \
                mock.patch.object(cb, 'app_jwt', return_value='fixture-jwt', create=True), \
                mock.patch.object(cb.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'GitHub App') as caught:
                cb.GitHub().get('repos/owner/repo')
        self.assertNotIn('fixture-secret', str(caught.exception))
        run.assert_not_called()

    def test_missing_key_or_id_preserves_gh_auth(self):
        for missing in ('key', 'id'):
            with self.subTest(missing=missing):
                self.env.write_text('' if missing == 'id' else 'COLDBOX_GITHUB_APP_ID=12345\n')
                key = self.key if missing == 'id' else Path(self.scratch.name) / 'absent.pem'
                with mock.patch.object(cb, 'APP_KEY', key), \
                        mock.patch.object(cb.urllib.request, 'urlopen', self.response), \
                        mock.patch.object(cb.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'legacy')) as run:
                    cb.GitHub().get('repos/owner/repo')
                self.assertEqual(run.call_args.args[0], ['gh', 'auth', 'token'])
                self.assertEqual(self.requests[-1].get_header('Authorization'), 'Bearer legacy')

    def test_other_owner_cannot_reuse_cached_token(self):
        with mock.patch.object(cb.urllib.request, 'urlopen', self.response):
            cb.GitHub().get('repos/owner/repo')
            with self.assertRaisesRegex(RuntimeError, 'installation'):
                cb.GitHub().get('orgs/stranger/actions/runners')

    def test_signing_failure_does_not_echo_stderr_or_fall_back(self):
        with mock.patch.object(cb.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, b'', b'fixture-secret')) as run:
            with self.assertRaisesRegex(RuntimeError, 'signing failed') as caught:
                cb.GitHub().get('repos/owner/repo')
        self.assertNotIn('fixture-secret', str(caught.exception))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][0], 'openssl')

    def test_release_lookup_uses_installation_token(self):
        with mock.patch.object(cb.urllib.request, 'urlopen', self.response):
            self.assertEqual(cb.fetch_json(cb.RUNNER_RELEASES), {'ok': True})
        self.assertEqual(self.requests[-1].get_header('Authorization'), 'Bearer test-token')

    def test_non_github_fetch_does_not_receive_token(self):
        with mock.patch.object(cb.urllib.request, 'urlopen', self.response):
            cb.fetch_json('https://example.com/data')
        self.assertEqual(len(self.requests), 1)
        self.assertIsNone(self.requests[0].get_header('Authorization'))

    def test_gh_error_does_not_echo_tokens(self):
        with mock.patch.object(cb.urllib.request, 'urlopen', self.response), \
                mock.patch.object(cb, 'app_jwt', return_value='fixture-jwt'), \
                mock.patch.object(cb.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'fixture-secret')):
            with self.assertRaisesRegex(RuntimeError, 'GitHub App') as caught:
                cb.gh('repos/owner/repo')
        self.assertNotIn('fixture-secret', str(caught.exception))

    def test_unregistration_listing_uses_app_token(self):
        commands = []
        def run(command, **kwargs):
            commands.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, '', '')
        with mock.patch.object(cb.urllib.request, 'urlopen', self.response), \
                mock.patch.object(cb, 'app_jwt', return_value='fixture-jwt'), \
                mock.patch.object(cb.subprocess, 'run', run):
            cb.unregister(type('Args', (), {'target': 'owner/repo'})())
        listing = next(kwargs for command, kwargs in commands if command[:2] == ['gh', 'api'])
        self.assertEqual(listing['env']['GH_TOKEN'], 'test-token')

    def test_host_wide_calls_allow_multiple_installations(self):
        original = self.response
        def response(request, timeout):
            if '/app/installations?' in request.full_url:
                body = Body(json.dumps([{'id': 7, 'account': {'login': 'owner'}},
                                        {'id': 8, 'account': {'login': 'another'}}]).encode())
                body.headers = {}
                return body
            return original(request, timeout)
        with mock.patch.object(cb.urllib.request, 'urlopen', response):
            self.assertEqual(cb.fetch_json(cb.RUNNER_RELEASES), {'ok': True})
        self.assertTrue(self.requests[0].full_url.endswith('/7/access_tokens'))

    def test_busy_runner_error_retains_http_status(self):
        with mock.patch.object(cb.urllib.request, 'urlopen', self.response), \
                mock.patch.object(cb, 'app_jwt', return_value='fixture-jwt'), \
                mock.patch.object(cb.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'busy (HTTP 422) fixture-secret')):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 422') as caught:
                cb.gh('repos/owner/repo/actions/runners/1')
        self.assertNotIn('fixture-secret', str(caught.exception))

    def test_status_accepts_app_auth_without_personal_login(self):
        import contextlib
        output = io.StringIO()
        with mock.patch.object(cb, 'app_token', return_value='test-token'), \
                mock.patch.object(cb, 'kvm_fix', return_value=''), \
                mock.patch.object(cb, 'registrations', side_effect=RuntimeError('stop-after-auth')), \
                mock.patch.object(cb.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '')) as run, \
                contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, 'stop-after-auth'):
                cb.status(None)
        self.assertIn('PASS  GitHub auth: GitHub App installation token', output.getvalue())
        self.assertNotIn('WARN', output.getvalue())
        self.assertFalse(any(call.args[0][:2] == ['gh', 'auth'] for call in run.call_args_list))


class StatusAuth(unittest.TestCase):
    def status_output(self, **patches):
        import contextlib
        output = io.StringIO()
        with mock.patch.multiple(cb, kvm_fix=lambda: '', registrations=mock.Mock(side_effect=RuntimeError('stop-after-auth')),
                                 **patches), contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, 'stop-after-auth'):
                cb.status(None)
        return output.getvalue()

    def test_personal_login_is_named_and_warned_without_failing(self):
        with mock.patch.object(cb.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '')):
            output = self.status_output(app_token=lambda: None)
        self.assertIn('WARN  GitHub auth: personal gh login', output)
        self.assertIn('fix: coldbox setup-app', output)
        self.assertNotIn('FAIL  GitHub auth', output)

    def test_neither_app_nor_login_fails(self):
        with mock.patch.object(cb.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '')):
            output = self.status_output(app_token=lambda: None)
        self.assertIn('FAIL  GitHub auth: no GitHub App and no gh login', output)

    def test_a_failing_app_fails_without_falling_back_to_gh(self):
        with mock.patch.object(cb.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '')) as run:
            output = self.status_output(app_token=mock.Mock(side_effect=RuntimeError('GitHub App authentication failed: HTTP 401')))
        self.assertIn('FAIL  GitHub auth: GitHub App configured but failing: GitHub App authentication failed: HTTP 401', output)
        self.assertFalse(any(call.args[0][:2] == ['gh', 'auth'] for call in run.call_args_list))


class SetupApp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        cls.download = Path(cls.scratch.name) / 'downloaded.pem'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048',
                        '-out', str(cls.download)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.scratch.cleanup()

    def setUp(self):
        etc = Path(tempfile.mkdtemp(dir=self.scratch.name))
        self.key, self.env = etc / 'coldbox' / 'github-app.pem', etc / 'coldbox' / 'github-app.env'
        self.permissions = dict(administration='write', actions='read', metadata='read')
        self.requests = []
        patches = [mock.patch.object(cb, 'APP_KEY', self.key), mock.patch.object(cb, 'APP_ENV', self.env),
                   mock.patch.object(cb.os, 'geteuid', return_value=0),
                   mock.patch.dict(os.environ, SUDO_USER=cb.pwd.getpwuid(os.getuid()).pw_name),
                   mock.patch.object(cb.os, 'chown'),
                   mock.patch.object(cb.urllib.request, 'urlopen', self.response)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def response(self, request, timeout):
        self.requests.append(request)
        return Body(json.dumps({'slug': 'coldbox-test', 'permissions': self.permissions}).encode())

    def setup(self, *argv):
        args = cb.argparse.Namespace(org=None, app_id=None, key=None)
        for flag, value in zip(argv[::2], argv[1::2]):
            setattr(args, flag.lstrip('-').replace('-', '_'), value)
        output = io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(output):
            code = cb.setup_app(args)
        return code, output.getvalue()

    def test_without_arguments_it_prints_a_prefilled_least_privilege_form(self):
        code, output = self.setup()
        self.assertEqual(code, 0)
        url = cb.urllib.parse.urlsplit(output.split('Open ', 1)[1].split()[0])
        query = dict(cb.urllib.parse.parse_qsl(url.query))
        self.assertEqual((url.netloc, url.path), ('github.com', '/settings/apps/new'))
        self.assertEqual({k: v for k, v in query.items() if v in ('read', 'write')}, self.permissions)
        self.assertEqual((query['public'], query['webhook_active']), ('false', 'false'))
        self.assertEqual(self.requests, [])

    def test_org_form_adds_only_the_org_runner_permission(self):
        _, output = self.setup('--org', 'acme')
        url = cb.urllib.parse.urlsplit(output.split('Open ', 1)[1].split()[0])
        self.assertEqual(url.path, '/organizations/acme/settings/apps/new')
        self.assertEqual(dict(cb.urllib.parse.parse_qsl(url.query))['organization_self_hosted_runners'], 'write')

    def test_installs_the_key_mode_600_and_the_app_id_after_github_accepts_them(self):
        self.addCleanup(os.umask, os.umask(0o077))
        code, output = self.setup('--app-id', '4242', '--key', str(self.download))
        self.assertEqual(code, 0)
        self.assertEqual(self.key.read_bytes(), self.download.read_bytes())
        self.assertEqual(self.key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.env.read_text(), 'COLDBOX_GITHUB_APP_ID=4242\n')
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.requests[0].full_url, 'https://api.github.com/app')
        self.assertTrue(self.requests[0].get_header('Authorization').startswith('Bearer '))
        self.assertIn('https://github.com/apps/coldbox-test/installations/new', output)
        self.assertNotIn('PRIVATE KEY', output)

    def test_refuses_an_app_with_more_permissions_and_writes_nothing(self):
        self.permissions.update(contents='write', actions='write')
        with self.assertRaisesRegex(cb.ConfigError, 'remove actions:write, contents:write; add actions:read$'):
            self.setup('--app-id', '4242', '--key', str(self.download))
        self.assertFalse(self.key.exists() or self.env.exists())

    def test_refuses_a_non_numeric_id_a_half_given_pair_and_an_existing_setup(self):
        with self.assertRaisesRegex(cb.ConfigError, 'numeric'):
            self.setup('--app-id', '42x', '--key', str(self.download))
        with self.assertRaisesRegex(cb.ConfigError, 'both'):
            self.setup('--app-id', '4242')
        self.env.parent.mkdir(parents=True)
        self.env.write_text('COLDBOX_GITHUB_APP_ID=1\n')
        with self.assertRaisesRegex(cb.ConfigError, 'already configured'):
            self.setup('--app-id', '4242', '--key', str(self.download))
        self.assertEqual(self.env.read_text(), 'COLDBOX_GITHUB_APP_ID=1\n')
        self.assertEqual(self.requests, [])

    def test_github_rejecting_the_key_writes_nothing(self):
        def reject(request, timeout):
            raise cb.urllib.error.HTTPError(request.full_url, 401, 'Unauthorized', {}, None)
        with mock.patch.object(cb.urllib.request, 'urlopen', reject):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 401'):
                self.setup('--app-id', '4242', '--key', str(self.download))
        self.assertFalse(self.key.exists() or self.env.exists())

    def test_refuses_an_app_missing_a_permission_coldbox_needs(self):
        self.permissions['administration'] = 'read'
        with self.assertRaisesRegex(cb.ConfigError, 'add administration:write$'):
            self.setup('--app-id', '4242', '--key', str(self.download))
        self.permissions['administration'] = 'write'
        del self.permissions['metadata']
        with self.assertRaisesRegex(cb.ConfigError, 'add metadata:read$'):
            self.setup('--app-id', '4242', '--key', str(self.download))
        self.permissions['metadata'] = 'read'
        with self.assertRaisesRegex(cb.ConfigError, 'add organization_self_hosted_runners:write$'):
            self.setup('--org', 'acme', '--app-id', '4242', '--key', str(self.download))
        self.assertFalse(self.key.exists() or self.env.exists())

    def test_repo_setup_refuses_the_org_runner_permission(self):
        self.permissions['organization_self_hosted_runners'] = 'write'
        with self.assertRaisesRegex(cb.ConfigError, 'remove organization_self_hosted_runners:write$'):
            self.setup('--app-id', '4242', '--key', str(self.download))
        self.assertFalse(self.key.exists() or self.env.exists())

    def test_org_app_with_the_org_runner_permission_is_accepted(self):
        self.permissions['organization_self_hosted_runners'] = 'write'
        code, _ = self.setup('--org', 'acme', '--app-id', '4242', '--key', str(self.download))
        self.assertEqual(code, 0)
        self.assertEqual(self.env.read_text(), 'COLDBOX_GITHUB_APP_ID=4242\n')

    def test_a_failure_while_installing_leaves_no_partial_setup(self):
        with mock.patch.object(cb.os, 'chown', side_effect=PermissionError('chown')):
            with self.assertRaises(PermissionError):
                self.setup('--app-id', '4242', '--key', str(self.download))
        self.assertFalse(self.key.exists() or self.env.exists())
        code, _ = self.setup('--app-id', '4242', '--key', str(self.download))
        self.assertEqual(code, 0)
