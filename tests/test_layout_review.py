"""The code/home boundary, after the 2026-11-09 review: release identity belongs to the checkout that holds the
package (an installed package without one fingerprints its own files); the gate's test processes carry neither this
home's settings nor its path; a child can be bound to another home; agent.toml values are validated, not coerced;
the launch command names its home and adoption matches on it; the browser profile lives in the agent folder."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class ReleaseIdentity(unittest.TestCase):
    def test_identity_is_the_checkouts_not_the_homes(self):
        from tabularium.ops import release
        self.assertEqual(release.identity(), release.identity(release.REPO), "the default is the checkout that holds the package")
        self.assertTrue(release.identity()['digest'])
        with tempfile.TemporaryDirectory() as home:                   # a home that is not a checkout has no identity of its own
            with self.assertRaises(RuntimeError):
                release.identity(Path(home))

    def test_installed_package_without_a_checkout_still_has_a_digest(self):
        from tabularium.ops import release
        with patch.object(release, 'REPO', None):
            first, second = release.identity(), release.identity()
            self.assertEqual(release.uncommitted(), [])
            self.assertEqual(release.commit(), '')
        self.assertTrue(first['digest'])
        self.assertEqual(first, second)
        self.assertEqual(first['commit'], '')
        self.assertTrue(release.same_source(first, second), "startup compares two of these and must agree")

    def test_gate_runs_without_this_homes_settings_and_with_the_checkout_as_home(self):
        from tabularium.ops import release
        calls = []

        def run(argv, **kwargs):
            calls.append(kwargs)
            return type('Result', (), {'returncode': 0, 'stdout': '', 'stderr': ''})()
        target = release.identity()
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / '.env').write_text('TELEGRAM_BOT_TOKEN=1:secret\nSOME_KEY=value\n', encoding='utf-8')
            with patch.object(release, 'uncommitted', return_value=[]), patch.object(release, 'identity', return_value=target), \
                    patch('subprocess.run', side_effect=run), \
                    patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': '1:secret', 'SOME_KEY': 'value', 'UNRELATED': 'kept'}):
                release.test_version(Path(home), target)
        self.assertTrue(calls)
        env = calls[0]['env']
        self.assertNotIn('TELEGRAM_BOT_TOKEN', env)
        self.assertNotIn('SOME_KEY', env)
        self.assertEqual(env['UNRELATED'], 'kept')
        self.assertEqual(Path(env['TABULARIUM_HOME']), release.REPO)
        self.assertEqual(Path(calls[0]['cwd']), release.REPO)


class Settings(unittest.TestCase):
    def test_settings_keys_names_without_values(self):
        from tabularium import config
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / 'config.bat').write_text('@echo off\nrem settings\nset A_KEY=one\nset "B_KEY=two"\n', encoding='utf-8')
            self.assertEqual(config.settings_keys(home), {'A_KEY', 'B_KEY'})
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop('A_KEY', None)
                os.environ.pop('B_KEY', None)
                found = config.load_env(home)
                self.assertEqual(found, {'A_KEY': 'one', 'B_KEY': 'two'})
                self.assertEqual(os.environ['B_KEY'], 'two')
                os.environ.pop('A_KEY'); os.environ.pop('B_KEY')


class ChildEnvironment(unittest.TestCase):
    def test_child_can_be_bound_to_another_home(self):
        from tabularium import paths
        with tempfile.TemporaryDirectory() as other:
            env = paths.child_env(other, PYTHONIOENCODING='utf-8')
            self.assertEqual(Path(env['TABULARIUM_HOME']), Path(other).resolve())
            self.assertEqual(env['PYTHONIOENCODING'], 'utf-8')
        self.assertEqual(Path(paths.child_env()['TABULARIUM_HOME']), paths.home())
        self.assertIn(str(paths.PACKAGE.parent), paths.child_env()['PYTHONPATH'].split(os.pathsep)[0])


class AgentToml(unittest.TestCase):
    def setUp(self):
        from tabularium.agents import configure, coordinator
        self.configure, self.default = configure, coordinator.CONFIG

    def test_a_string_is_not_a_list(self):
        with self.assertRaises(ValueError):
            self.configure(self.default, {'window': 'bad'})
        with self.assertRaises(ValueError):
            self.configure(self.default, {'tools_remove': 'send_email'})

    def test_a_bool_is_not_an_int_and_ports_have_a_range(self):
        with self.assertRaises(ValueError):
            self.configure(self.default, {'instance_port': True})
        with self.assertRaises(ValueError):
            self.configure(self.default, {'instance_port': 70000})
        self.assertEqual(self.configure(self.default, {'instance_port': 8300}).instance_port, 8300)

    def test_well_formed_values_apply(self):
        cfg = self.configure(self.default, {'tools_remove': ['send_email'], 'window': list(self.default.window)})
        self.assertIn('send_email', cfg.tools_remove)
        self.assertEqual(cfg.window, self.default.window)


class Launch(unittest.TestCase):
    def test_launch_command_names_the_home_and_adoption_matches_on_it(self):
        from tabularium.ops import supervisor
        argv = supervisor.command('cato')
        self.assertEqual(argv[1:4], ['-m', 'tabularium', '--home'])
        self.assertEqual(Path(argv[4]), supervisor.ROOT)
        self.assertEqual(argv[5:], ['run', 'cato'])
        self.assertEqual(supervisor.command('dashboard')[5:], ['dashboard'])
        src = (ROOT / 'src' / 'tabularium' / 'ops' / 'supervisor.py').read_text(encoding='utf-8')
        self.assertNotIn("'D:\\\\\\\\Agents'", src, "no fixed installation path in the adoption query")
        self.assertIn('str(ROOT).replace', src)

    def test_browser_profile_follows_the_agent_folder(self):
        src = (ROOT / 'src' / 'tabularium' / 'core' / 'agent_app.py').read_text(encoding='utf-8')
        self.assertNotIn('D:\\agents', src)
        self.assertIn('self.AGENT_DIR / "browser_profile"', src)

    def test_base_import_does_not_need_pywin32(self):
        import importlib
        import tabularium.models.openai_chatgpt as module
        with patch.dict(sys.modules, {'win32crypt': None}):
            reloaded = importlib.reload(module)
            self.assertIsNone(reloaded.win32crypt)
            with self.assertRaises(RuntimeError):
                reloaded._dpapi()
        importlib.reload(module)


if __name__ == '__main__':
    unittest.main()
