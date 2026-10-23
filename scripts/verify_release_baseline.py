"""Match official GitHub asset digests, ZIP bytes and extracted update baselines.

Reads existing artifacts only. Historical delta assets are reported separately:
new updates are built from official full/lite archive bytes, never old deltas.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.package_editions import inventory,sha
from dlss5tool import delta_update as delta


def official_version(release):
    if release['draft'] or release['prerelease']:
        raise ValueError('An official non-draft, non-prerelease baseline is required')
    tag = release['tag_name']
    # GitHub can retain a generated draft tag after publication. Keep the raw
    # metadata and require an exact version title; asset digests remain required.
    version = release.get('name') if re.fullmatch(r'untagged-[0-9a-f]+', tag) else tag
    try:
        return delta.version(version)
    except delta.DeltaError as error:
        raise ValueError(str(error)) from error


def verify(packages,release):
    version=official_version(release)
    report=json.loads((packages/'package-report.json').read_text(encoding='utf-8'))
    if report['version']!=version:raise ValueError('Baseline version mismatch')
    assets={a['name']:a for a in release['assets']}
    results=[]
    for name,a in assets.items():
        if Path(name).name!=name or not a['digest'].startswith('sha256:'):
            raise ValueError('Invalid GitHub asset identity')
        path=packages/'github-assets'/name
        actual=sha(path) if path.is_file() else None
        size=path.stat().st_size if path.is_file() else None
        matched=(actual==a['digest'][7:] and size==a['size'])
        historical=name.endswith('.dlssupdate')
        results.append(dict(name=name,local_sha256=actual,github_digest=a['digest'],
                            local_bytes=size,github_bytes=a['size'],matched=matched,historical_delta=historical))
        print(json.dumps(results[-1]),flush=True)
        if not historical and not matched:raise ValueError('Official archive mismatch: '+name)
    directories=[]
    for edition in ('lite','full'):
        record=report['editions'][edition]
        archive=packages/record['name']
        archive_sha=sha(archive)
        if edition=='lite':
            canonical=assets[f'DLSS5Tool-{version}-win64.zip']
            if archive_sha!=canonical['digest'][7:]:raise ValueError('Lite archive not the official bytes')
        else:
            combined=hashlib.sha256()
            for part in record['volumes']:
                if part['name'] not in assets:raise ValueError('Missing official full volume')
                with (packages/'github-assets'/part['name']).open('rb') as handle:
                    for chunk in iter(lambda:handle.read(8*1024**2),b''):combined.update(chunk)
            if combined.hexdigest()!=archive_sha:raise ValueError('Full ZIP differs from official joined volumes')
        expected=json.loads((packages/f'verification/{edition}-files.json').read_text(encoding='utf-8'))
        with zipfile.ZipFile(archive) as bundle:
            if len(bundle.namelist())!=len(expected) or set(bundle.namelist())!=set(expected):
                raise ValueError('Archive file set mismatch: '+edition)
            for name,item in expected.items():
                with bundle.open(name) as handle:
                    digest=hashlib.file_digest(handle,'sha256').hexdigest()
                if digest!=item['sha256'] or bundle.getinfo(name).file_size!=item['size']:
                    raise ValueError('Official archive manifest mismatch: '+name)
        actual=inventory(packages/f'DLSS5Tool-{version}-win64-{edition}')
        if actual!=expected:raise ValueError('Extracted baseline differs from official archive: '+edition)
        directories.append(dict(edition=edition,archive_sha256=archive_sha,files=len(expected),
                                archive_manifest_exact=True,extracted_baseline_exact=True))
        print('PASS official archive and extracted baseline:',edition,flush=True)
    return dict(version=version,release_url=release['url'],release_tag=release['tag_name'],
                release_name=release.get('name'),assets=results,directories=directories,
                historical_deltas_used=False,baseline_verified=True,
                historical_delta_warnings=[r for r in results if r['historical_delta'] and not r['matched']])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--packages',type=Path,required=True)
    parser.add_argument('--release-json',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    result=verify(args.packages.resolve(),json.loads(args.release_json.read_text(encoding='utf-8')))
    with args.report.open('x',encoding='utf-8') as handle:json.dump(result,handle,indent=2)
