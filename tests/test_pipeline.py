import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vector_corpus import fingerprint


def load_script(name):
    path = ROOT / name
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch_discord = load_script('fetch-discord.py')
enrich_links = load_script('enrich-links.py')
build_page = load_script('build-page.py')


class VectorFingerprintTests(unittest.TestCase):
    def test_same_size_content_and_order_changes_are_stale(self):
        entries = [
            {'title': 'Alpha', 'description': 'First'},
            {'title': 'Beta', 'description': 'Second'},
        ]
        edited = [dict(entry) for entry in entries]
        edited[0]['title'] = 'Omega'

        self.assertNotEqual(fingerprint(entries), fingerprint(edited))
        self.assertNotEqual(fingerprint(entries), fingerprint(list(reversed(entries))))


class ArchivedThreadTests(unittest.TestCase):
    def test_newly_archived_threads_are_found_after_an_earlier_cursor(self):
        responses = {
            '/guilds/guild/channels': [{
                'id': 'parent', 'name': 'forum', 'type': 15, 'parent_id': None,
                'permission_overwrites': [],
            }],
            '/guilds/guild/threads/active': {'threads': []},
            '/channels/parent/threads/archived/public?limit=100': {
                'threads': [
                    {'id': 'new', 'name': 'new post', 'type': 11, 'parent_id': 'parent',
                     'thread_metadata': {'archive_timestamp': '2026-09-01T00:00:00+00:00'}},
                    {'id': 'old', 'name': 'old post', 'type': 11, 'parent_id': 'parent',
                     'thread_metadata': {'archive_timestamp': '2026-08-01T00:00:00+00:00'}},
                ],
                'has_more': True,
            },
        }
        store = {'channels': {}, 'messages': [],
                 'archiveCursors': {'parent': '2026-08-15T00:00:00+00:00'}}
        with mock.patch.object(fetch_discord, 'api', side_effect=lambda path, soft=False: responses[path]):
            targets, updates = fetch_discord.discover('guild', store, False)

        self.assertEqual([target['id'] for target in targets], ['new'])
        self.assertEqual(updates, {'parent': '2026-09-01T00:00:00+00:00'})


class EnrichmentSafetyTests(unittest.TestCase):
    @staticmethod
    def answer(address):
        family = socket.AF_INET6 if ':' in address else socket.AF_INET
        endpoint = (address, 443, 0, 0) if family == socket.AF_INET6 else (address, 443)
        return [(family, socket.SOCK_STREAM, 6, '', endpoint)]

    def test_private_or_mixed_dns_answers_are_rejected(self):
        mixed = self.answer('93.184.216.34') + self.answer('127.0.0.1')
        with mock.patch.object(enrich_links.socket, 'getaddrinfo', return_value=mixed):
            with self.assertRaises(enrich_links.UnsafeDestination):
                enrich_links.public_address('example.test', 443)

    def test_url_credentials_are_rejected_before_connecting(self):
        with mock.patch.object(enrich_links, 'public_address') as resolve:
            with self.assertRaises(enrich_links.UnsafeDestination):
                enrich_links.open_public('https://user:pass@example.test/', {})
        resolve.assert_not_called()

    def test_gzip_output_is_bounded_after_decompression(self):
        compressed = gzip.compress(b'x' * (enrich_links.MAX_BYTES * 2))

        class Response:
            def __init__(self):
                self.body = io.BytesIO(compressed)

            def getheader(self, name, default=''):
                return 'gzip' if name == 'Content-Encoding' else default

            def read(self, size):
                return self.body.read(size)

        self.assertEqual(len(enrich_links.read_limited(Response())), enrich_links.MAX_BYTES)

    def test_redirect_destination_is_revalidated(self):
        class Redirect:
            status = 302

            def getheader(self, name, default=None):
                return 'http://127.0.0.1/private' if name == 'Location' else default

            def close(self):
                pass

        class Connection:
            def close(self):
                pass

        real_open = enrich_links.open_public

        def first_then_real(url, headers):
            if url == 'https://public.example/start':
                return Connection(), Redirect()
            return real_open(url, headers)

        with mock.patch.object(enrich_links, 'open_public', side_effect=first_then_real):
            with self.assertRaises(enrich_links.UnsafeDestination):
                enrich_links.get('https://public.example/start')

    def test_completed_fetch_is_checkpointed_before_an_interruption(self):
        harvest = {
            'messages': [{
                'urls': [
                    {'url': 'https://example.com/one'},
                    {'url': 'https://example.com/two'},
                ],
            }],
        }
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / 'link-meta.json'
            harvest_path = Path(tmp) / 'discord.json'
            harvest_path.write_text(json.dumps(harvest))
            with (
                mock.patch.object(enrich_links, 'CACHE', str(cache_path)),
                mock.patch.object(enrich_links, 'HARVEST', str(harvest_path)),
                mock.patch.object(enrich_links, 'fetch_meta', side_effect=[
                    ('First page', 'First description'), KeyboardInterrupt(),
                ]),
                mock.patch.object(enrich_links.time, 'sleep'),
                mock.patch.object(sys, 'argv', ['enrich-links.py']),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    enrich_links.main()

            cache = json.loads(cache_path.read_text())
            first_key = build_page.canonical('https://example.com/one')
            self.assertEqual(cache[first_key]['title'], 'First page')


class MergeTests(unittest.TestCase):
    def test_mirror_urls_in_one_message_are_one_sighting(self):
        harvest = {
            'guildId': '1',
            'channels': {'2': {'name': 'links'}},
            'messages': [{
                'id': '3', 'ch': '2', 'ts': '2026-09-01T00:00:00+00:00',
                'author': 'tester', 'said': '',
                'urls': [
                    {'url': 'https://x.com/user/status/123', 'text': None},
                    {'url': 'https://fxtwitter.com/user/status/123', 'text': None},
                ],
            }],
        }
        entries = build_page.discord_entries(harvest, {})
        self.assertEqual(len(entries), 1)


class FixtureTests(unittest.TestCase):
    def test_fixture_build_does_not_touch_tracked_harvest_or_page(self):
        harvest = ROOT / 'data' / 'discord.json'
        page = ROOT / 'index.html'
        before = {
            harvest: hashlib.sha256(harvest.read_bytes()).digest(),
            page: hashlib.sha256(page.read_bytes()).digest(),
        }
        unsafe = subprocess.run(
            [sys.executable, str(ROOT / 'make-fixture.py')],
            cwd=ROOT, capture_output=True, text=True)
        self.assertNotEqual(unsafe.returncode, 0)
        self.assertIn('--out is required', unsafe.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / 'discord.json'
            fixture_page = Path(tmp) / 'index.html'
            shutil.copyfile(page, fixture_page)
            subprocess.run(
                [sys.executable, str(ROOT / 'make-fixture.py'), '--out', str(fixture)],
                check=True, cwd=ROOT, capture_output=True, text=True)
            command = [sys.executable, str(ROOT / 'build-page.py'),
                       '--discord', str(fixture), '--page', str(fixture_page)]
            subprocess.run(command, check=True, cwd=ROOT, capture_output=True, text=True)
            first = hashlib.sha256(fixture_page.read_bytes()).digest()
            subprocess.run(command, check=True, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(hashlib.sha256(fixture_page.read_bytes()).digest(), first)
            self.assertTrue(json.loads(fixture.read_text())['fixture'])

        for path, digest in before.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), digest)


if __name__ == '__main__':
    unittest.main()
