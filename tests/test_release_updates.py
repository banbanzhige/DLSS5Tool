"""Release workflow contracts; real tiny ZIPs, no GPU or release-sized copies."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from dlss5tool import delta_update as delta
from scripts import build_file_update as builder, release_updates as workflow, package_editions


class ReleaseUpdateWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.old = self.root / 'old'
        self.new = self.root / 'new'
        self.make_release(self.old, 'v2.2.0', b'old')
        self.make_release(self.new, 'v2.2.1', b'new')
        self.policy = self.root / 'policy.json'
        delta.write_json(self.policy, {'schema': 1, 'first_updater_version': 'v2.2.0',
            'baselines': [{'version': 'v2.2.0', 'packages': str(self.old)}]})

    def make_release(self, root, version, content):
        root.mkdir()
        (root / 'verification').mkdir()
        for edition in ('lite', 'full'):
            folder = root / f'DLSS5Tool-{version}-win64-{edition}'
            folder.mkdir()
            (folder / 'DLSS5Tool.exe').write_bytes(content)
            (folder / delta.HELPER).write_bytes(b'helper')
            (folder / '_internal').mkdir()
            (folder / '_internal/unchanged.dll').write_bytes(b'large unchanged dependency')
            if edition == 'full':
                (folder / 'mods/enhancement').mkdir(parents=True)
                (folder / 'mods/enhancement/guidance_worker.exe').write_bytes(content)
                (folder / 'mods/models').mkdir()
                (folder / 'mods/models/raft.pth').write_bytes(b'unchanged model')
            delta.write_json(root / f'verification/{edition}-files.json', builder.inventory(folder, edition))
        delta.write_json(root / 'package-report.json', {'version': version, 'full_equals_lite_plus_addon': True})

    def plan(self, **kwargs):
        return workflow.plan_updates('v2.2.1', policy_path=self.policy, **kwargs)

    def build(self, plan=None):
        plan = plan or self.plan()
        folders = {e: self.new / f'DLSS5Tool-v2.2.1-win64-{e}' for e in ('lite', 'full')}
        inventories = {e: builder.inventory(p, e) for e, p in folders.items()}
        assets = self.new / 'github-assets'
        assets.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            updates = workflow.build_updates(plan, folders, inventories, self.new, assets)
        report = {'version': 'v2.2.1', 'incremental_updates': updates}
        delta.write_json(assets / 'package-report.json', report)
        (assets / 'SHA256SUMS.txt').write_text(''.join(f"{r['sha256']}  {r['name']}\n" for r in updates['assets']))
        return assets, report

    def test_two_payloads_generated_and_gate_passes(self):
        assets, report = self.build()
        self.assertEqual(len(list(assets.glob('*.dlssupdate'))), 2)
        self.assertEqual(workflow.verify_upload_updates(assets), report['incremental_updates'])
        for entry in report['incremental_updates']['assets']:
            self.assertEqual(entry['payload_bytes'], 3 if entry['edition'] == 'lite' else 6)

    def test_later_releases_cannot_claim_initial_exemption(self):
        with self.assertRaisesRegex(delta.DeltaError, 'only allowed'):
            self.plan(initial=True)
        with self.assertRaises(delta.DeltaError):
            workflow.plan_updates('v2.2.0', policy_path=self.policy)

    def test_initial_baseline_requires_no_old_files(self):
        plan = workflow.plan_updates('v2.2.0', initial=True, policy_path=self.policy)
        folders = {e: self.old / f'DLSS5Tool-v2.2.0-win64-{e}' for e in ('lite', 'full')}
        inventories = {e: builder.inventory(p, e) for e, p in folders.items()}
        updates = workflow.build_updates(plan, folders, inventories, self.old, self.old)
        report = {'version': 'v2.2.0', 'incremental_updates': updates}
        (self.old / 'SHA256SUMS.txt').touch()
        self.assertEqual(workflow.verify_upload_updates(self.old, report)['assets'], [])

    def test_initial_release_cannot_omit_helper(self):
        plan = workflow.plan_updates('v2.2.0', initial=True, policy_path=self.policy)
        with self.assertRaisesRegex(delta.DeltaError, 'missing'):
            workflow.build_updates(plan, {}, {}, self.old, self.old)

    def test_missing_policy_or_empty_baselines_fail(self):
        with self.assertRaises(FileNotFoundError):
            workflow.plan_updates('v2.2.1', policy_path=self.root / 'absent.json')
        delta.write_json(self.policy, {'schema': 1, 'first_updater_version': 'v2.2.0', 'baselines': []})
        with self.assertRaises(delta.DeltaError):
            self.plan()

    def test_missing_or_modified_baseline_fails_before_output(self):
        (self.old / 'DLSS5Tool-v2.2.0-win64-full/DLSS5Tool.exe').write_bytes(b'modified')
        with self.assertRaisesRegex(delta.DeltaError, 'missing or modified'):
            self.plan()
        self.assertFalse((self.new / 'github-assets').exists())

    def test_override_relocates_only_declared_version(self):
        relocated = self.root / 'relocated'
        self.old.rename(relocated)
        plan = self.plan(overrides=[relocated])
        self.assertEqual(plan['baselines'][0]['version'], 'v2.2.0')
        with self.assertRaisesRegex(delta.DeltaError, 'not declared'):
            self.plan(overrides=[self.new])

    def test_duplicate_future_and_pre_updater_baselines_rejected(self):
        for versions in (['v2.2.0', 'v2.2.0'], ['v2.2.1'], ['v2.1.3']):
            delta.write_json(self.policy, {'schema': 1, 'first_updater_version': 'v2.2.0',
                'baselines': [{'version': v, 'packages': str(self.old)} for v in versions]})
            with self.assertRaises(delta.DeltaError):
                self.plan()

    def test_multiple_required_baselines_produce_all_pairs(self):
        previous = self.root / 'previous'
        self.make_release(previous, 'v2.2.2', b'prior')
        delta.write_json(self.policy, {'schema': 1, 'first_updater_version': 'v2.2.0',
            'baselines': [{'version': 'v2.2.0', 'packages': str(self.old)},
                          {'version': 'v2.2.2', 'packages': str(previous)}]})
        plan = workflow.plan_updates('v2.2.3', policy_path=self.policy)
        assets, report = self.build(plan)
        report['version'] = 'v2.2.3'
        self.assertEqual(len(workflow.verify_upload_updates(assets, report, policy_path=self.policy)['assets']), 4)

    def test_report_cannot_silently_drop_a_policy_baseline(self):
        assets, report = self.build()
        policy = delta.read_json(self.policy)
        policy['baselines'].append({'version': 'v2.2.2', 'packages': str(self.root / 'another')})
        delta.write_json(self.policy, policy)
        with self.assertRaisesRegex(delta.DeltaError, 'coverage'):
            workflow.verify_upload_updates(assets, report, policy_path=self.policy)

    def test_gate_rejects_missing_full_payload(self):
        assets, report = self.build()
        (assets / delta.asset_name('v2.2.0', 'v2.2.1', 'full')).unlink()
        with self.assertRaisesRegex(delta.DeltaError, 'Missing or unexpected'):
            workflow.verify_upload_updates(assets, report)

    def test_gate_rejects_corrupt_payload_or_checksum(self):
        assets, report = self.build()
        path = assets / report['incremental_updates']['assets'][0]['name']
        original = path.read_bytes()
        path.write_bytes(b'corrupt')
        with self.assertRaises(delta.DeltaError):
            workflow.verify_upload_updates(assets, report)
        path.write_bytes(original)
        (assets / 'SHA256SUMS.txt').write_text('')
        with self.assertRaises(delta.DeltaError):
            workflow.verify_upload_updates(assets, report)

    def test_failure_does_not_silently_skip_full(self):
        real = builder.build
        def fail_full(*args, **kwargs):
            if args[5] == 'full':
                raise delta.DeltaError('full failed')
            return real(*args, **kwargs)
        with mock.patch.object(builder, 'build', side_effect=fail_full):
            with self.assertRaisesRegex(delta.DeltaError, 'full failed'):
                self.build()
        self.assertFalse((self.new / 'github-assets/package-report.json').exists())

    def test_target_drift_after_packaging_is_rejected(self):
        folder = self.new / 'DLSS5Tool-v2.2.1-win64-lite'
        inventory = builder.inventory(folder, 'lite')
        (folder / 'DLSS5Tool.exe').write_bytes(b'drift')
        with self.assertRaisesRegex(delta.DeltaError, 'packaged release inventory'):
            builder.build(self.old / 'DLSS5Tool-v2.2.0-win64-lite', folder,
                          self.new / 'bad-output', 'v2.2.0', 'v2.2.1', 'lite', expected_after=inventory)

    def test_entrypoint_preflights_before_large_copy_and_requires_gate(self):
        import inspect
        source = inspect.getsource(package_editions.main)
        self.assertLess(source.index('plan_updates('), source.index('output.mkdir('))
        self.assertLess(source.index('build_updates('), source.index("'package-report.json'"))
        self.assertLess(source.index('verify_upload_updates('), source.index("print('DONE'"))

    def test_real_cli_missing_baseline_stops_before_output(self):
        import sys
        out = self.root / 'cli-output'
        arguments = ['package_editions.py', '--base', str(self.root), '--component', str(self.root),
                     '--models', str(self.root), '--licenses', str(self.root), '--output', str(out),
                     '--allow-external-output']
        with (mock.patch.object(sys, 'argv', arguments),
              mock.patch.object(package_editions, 'plan_updates', side_effect=delta.DeltaError('Missing baseline'))):
            with self.assertRaises(delta.DeltaError):
                package_editions.main()
        self.assertFalse(out.exists())

    def test_complete_edition_entrypoint_collects_both_incremental_assets(self):
        import hashlib
        import sys
        repo = self.root / 'repo'
        inputs = self.root / 'inputs'
        for folder in (repo / 'dist', repo / 'scripts', repo / 'third_party/NVIDIA-DLSS', repo / 'licenses',
                       repo / 'mods', repo / 'docs/release', inputs / 'component',
                       inputs / 'models', inputs / 'licenses'):
            folder.mkdir(parents=True, exist_ok=True)
        for path in (repo / 'LICENSE', repo / 'scripts/Join-ReleaseArchive.ps1', repo / 'third_party/NVIDIA-DLSS/LICENSE.txt',
                     repo / 'licenses/torchvision-LICENSE.txt', repo / 'mods/README.md',
                     repo / 'docs/release/RELEASE_NOTES_v2.2.1.md'):
            path.write_text('test fixture')
        (inputs / 'component/guidance_worker.exe').write_bytes(b'worker new')
        (inputs / 'models' / package_editions.MODELS[0]).write_bytes(b'model')
        (inputs / 'licenses/DepthAnythingV2-CODE-LICENSE.txt').write_bytes(b'x' * 1200)
        output = repo / 'dist/complete'
        arguments = ['package_editions.py', '--base', str(self.new / 'DLSS5Tool-v2.2.1-win64-lite'),
                     '--component', str(inputs / 'component'), '--models', str(inputs / 'models'),
                     '--licenses', str(inputs / 'licenses'), '--output', str(output)]
        with (mock.patch.object(sys, 'argv', arguments),
              mock.patch.object(package_editions, 'ROOT', repo),
              mock.patch.object(package_editions, 'APP_VERSION', 'v2.2.1'),
              mock.patch.object(package_editions, 'WORKER_SHA', hashlib.sha256(b'worker new').hexdigest()),
              mock.patch.object(package_editions, 'plan_updates', return_value=self.plan()),
              contextlib.redirect_stdout(io.StringIO()) as log):
            package_editions.main()
        self.assertIn('DONE', log.getvalue())
        assets = output / 'github-assets'
        self.assertEqual(len(workflow.verify_upload_updates(assets)['assets']), 2)
        notice = (assets / 'README-UPLOAD.txt').read_text()
        for edition in ('lite', 'full'):
            self.assertIn(delta.asset_name('v2.2.0', 'v2.2.1', edition), notice)
        report = delta.read_json(assets / 'package-report.json')
        self.assertEqual(report['version'], 'v2.2.1')
        self.assertFalse(report['published'])


if __name__ == '__main__':
    unittest.main()
