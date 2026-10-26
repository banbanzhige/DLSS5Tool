"""HDR acceptance must use an immutable independent oracle after submission."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import probe_hdr_analysis as probe, verify_vsr_hdr as verify


@pytest.fixture
def registered_tmp(tmp_path,monkeypatch):
    """Keep CLI scope checks while allowing pytest's configured fixture root."""
    from scripts import build_vsr_cuda
    original_safe = build_vsr_cuda.safe
    def scoped_path(path,parent=None):
        scope = tmp_path if parent is not None and Path(parent) == probe.ROOT/'tmp' else parent
        return original_safe(path,scope)
    monkeypatch.setattr(build_vsr_cuda,'safe',scoped_path)
    return tmp_path


def evidence(candidate_path=None):
    candidate_path = candidate_path or probe.ROOT/probe.ORACLE_SOURCE
    return dict(accepted=True,oracle_commit=probe.ORACLE_COMMIT,oracle_source=probe.ORACLE_SOURCE,
        oracle_sha256=probe.ORACLE_SHA256,oracle_eotf_sha256=dict(probe.ORACLE_EOTF_SHA256),
        candidate_sha256=hashlib.sha256(candidate_path.read_bytes()).hexdigest())


def test_oracle_uses_real_pre_upgrade_source_independent_of_current_head():
    source = probe.load_oracle_source(probe.ROOT/probe.ORACLE_SOURCE)
    assert probe.ORACLE_COMMIT == '22ee79c2368b6beb8d9eee01e9c7b4ee1e7ef711'
    assert hashlib.sha256(source).hexdigest() == probe.ORACLE_SHA256
    assert source.decode().splitlines() != (probe.ROOT/probe.ORACLE_SOURCE).read_text().splitlines()


@pytest.mark.parametrize('line_end', [b'\n', b'\r\n'])
def test_candidate_equal_to_oracle_is_rejected_even_with_different_line_endings(tmp_path,line_end):
    original = probe.load_oracle_source(probe.ROOT/probe.ORACLE_SOURCE)
    candidate = tmp_path/'candidate.py'
    candidate.write_bytes(line_end.join(original.splitlines())+line_end)
    with pytest.raises(ValueError,match='oracle matches candidate'):
        probe.load_oracle_source(candidate)
    with pytest.raises(ValueError,match='oracle matches candidate'):
        probe.validate_analysis_evidence(evidence(candidate),candidate_path=candidate)


def test_wrong_git_blob_is_rejected_before_analysis(monkeypatch):
    monkeypatch.setattr(probe.subprocess,'run',lambda *_,**__:SimpleNamespace(stdout=b'changed oracle'))
    with pytest.raises(ValueError,match='oracle hash mismatch'):
        probe.load_oracle_source(probe.ROOT/probe.ORACLE_SOURCE)


@pytest.mark.parametrize('change', [dict(accepted=False),dict(oracle_commit='HEAD'),
    dict(oracle_commit='f6a1808'),dict(oracle_source='other.py'),dict(oracle_sha256='0'*64),
    dict(candidate_sha256='0'*64),dict(oracle_eotf_sha256={})])
def test_unbound_failed_or_stale_evidence_cannot_pass(change):
    with pytest.raises(ValueError):
        probe.validate_analysis_evidence(evidence()|change)


def test_pre_fix_report_without_reference_commit_is_rejected():
    report = evidence()
    del report['oracle_commit']
    with pytest.raises(ValueError,match='oracle identity'):
        probe.validate_analysis_evidence(report)


def test_valid_evidence_is_checked_against_current_candidate():
    probe.validate_analysis_evidence(evidence())


@pytest.mark.parametrize('name', ['_pq_eotf','_hlg_eotf'])
def test_changed_shared_eotf_dependency_cannot_self_compare(monkeypatch,name):
    from dlss5tool import video_export
    monkeypatch.setattr(video_export,name,lambda values:values)
    with pytest.raises(ValueError,match='oracle EOTF dependency changed'):
        probe.load_oracle_source(probe.ROOT/probe.ORACLE_SOURCE)


def run_small_probe(tmp_path,monkeypatch):
    task = tmp_path/'probe'
    task.mkdir()
    (task/'TASK.md').write_text('Small oracle fixture within registered test task.')
    monkeypatch.setattr(probe,'PROBE_SIZES',((17,16),))
    monkeypatch.setattr(probe.sys,'argv',['probe','--task',str(task),'--label','analysis'])
    return task


def test_probe_saves_pinned_identity_and_compares_real_math(registered_tmp,monkeypatch):
    task = run_small_probe(registered_tmp,monkeypatch)
    assert probe.main() == 0
    report = json.loads((task/'analysis.json').read_text())
    probe.validate_analysis_evidence(report)
    assert report['oracle_commit'] == probe.ORACLE_COMMIT
    assert report['oracle_sha256'] != report['candidate_sha256']
    assert len(report['cases']) == 5 and all(case['equal'] for case in report['cases'])


def test_numeric_regression_fails_independent_probe_and_saves_rejection(registered_tmp,monkeypatch):
    from dlss5tool import guidance_color
    task = run_small_probe(registered_tmp,monkeypatch)
    original = guidance_color.analysis_rgba8
    def changed_math(*args,**kwargs):
        result = original(*args,**kwargs).copy()
        result[0,0,0] ^= 1
        return result
    monkeypatch.setattr(guidance_color,'analysis_rgba8',changed_math)
    with pytest.raises(AssertionError,match='Proxy mismatch'):
        probe.main()
    report = json.loads((task/'analysis.json').read_text())
    assert report['accepted'] is False
    assert report['oracle_commit'] == probe.ORACLE_COMMIT


def test_upgrade_verifier_rejects_legacy_self_comparison_before_binary_checks(registered_tmp,monkeypatch):
    task = registered_tmp/'verify'
    task.mkdir()
    (task/'TASK.md').write_text('Verifier identity fixture within registered test task.')
    legacy = evidence()
    del legacy['oracle_commit']
    for name, value in [('build-final',{}),('operators-final',{'accepted':True}),('analysis-lut',legacy)]:
        (task/(name+'.json')).write_text(json.dumps(value))
    monkeypatch.setattr(verify.sys,'argv',['verify','--task',str(task)])
    with pytest.raises(ValueError,match='oracle identity'):
        verify.main()
    assert not (task/'verification-summary.json').exists()
