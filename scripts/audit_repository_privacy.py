"""Offline, redacted privacy checks for Git candidates and reachable history.

This is a rule-based review aid, not proof that a repository contains no secrets.
It never verifies credentials online, modifies Git, or prints matched values.
"""
import argparse
from collections import Counter
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess


RULES = {
    'private-key': r'-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----',
    'github-token': r'\b(?:gh[pousr]_[A-Za-z0-9_]{20,255}|github_pat_[A-Za-z0-9_]{30,255})\b',
    'aws-access-id': r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b',
    'google-api-key': r'\bAIza[0-9A-Za-z_-]{35}\b',
    'service-token': r'\b(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,255}|hf_[A-Za-z0-9]{30,255}|glpat-[A-Za-z0-9_-]{20,255}|xox[baprs]-[A-Za-z0-9-]{20,255}|sk_live_[A-Za-z0-9]{16,255})\b',
    'webhook-secret': r'https://(?:discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]{20,255}|hooks\.slack\.com/services/[A-Za-z0-9/_-]{20,255})',
    'jwt': r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b',
    'credential-assignment': r'''(?i)(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password|passwd|secret[_-]?key|aws[_-]?secret[_-]?access[_-]?key)["']?\s*[:=]\s*["']([^"'\r\n]{4,200})["']''',
    'environment-secret': r'''(?mi)^[ \t]*(?:export[ \t]+)?(?:[A-Z][A-Z0-9_]*_(?:TOKEN|SECRET|PASSWORD|API_KEY)|GITHUB_TOKEN|GH_TOKEN|HF_TOKEN|AWS_SECRET_ACCESS_KEY)[ \t]*=[ \t]*([^\s#"'$%{<][^\s#]*)''',
    'authorization-value': r'''(?i)\bauthorization["']?\s*[:=]\s*["']?(?:Bearer|Basic)\s+([A-Za-z0-9+/=_.-]{16,})''',
    'url-credentials': r'(?i)\b(?:https?|ftp|postgres(?:ql)?|mysql|mongodb|redis)://[^\s/:@]+:[^\s/@]{4,200}@',
    'email': r'\b[A-Za-z0-9.!#$%&*+/=?^_`{|}~\[\]-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b',
    'windows-profile': r'(?i)\b[A-Z]:[\\/]+Users[\\/]+([^\\/\s"\'<>:]+)',
    'unix-profile': r'/(?:home|Users)/([^/\s"\'<>:]+)',
    'private-ip': r'\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b',
    'phone-context': r'(?i)(?:phone|mobile|电话|手机|联系方式)\s*["\']?\s*[:=：]\s*["\']?\+?(?:86[- ]?)?1[3-9]\d{9}\b',
}
PATTERNS = {name: re.compile(pattern) for name, pattern in RULES.items()}
SECRET_RULES = set(RULES) - {'email', 'windows-profile', 'unix-profile', 'private-ip', 'phone-context'}
MAX_EXPANDED = 32 * 1024**2


def git(root, *args, data=None):
    return subprocess.check_output(['git', '--no-optional-locks', '-c', 'core.quotepath=false',
                                   '-c', 'safe.directory=' + Path(root).resolve().as_posix(), *args],
                                   cwd=root, input=data, stderr=subprocess.PIPE)


def git_paths(root, *args):
    return [value.decode('utf-8', 'replace') for value in git(root, *args, '-z').split(b'\0') if value]


def inspect_data(data, path, location, *, privacy=True):
    truncated = False
    if data.startswith(b'\x1f\x8b'):
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            data = stream.read(MAX_EXPANDED + 1)
        truncated = len(data) > MAX_EXPANDED
        data = data[:MAX_EXPANDED]
    utf16 = data.startswith((b'\xff\xfe', b'\xfe\xff'))
    binary = not utf16 and b'\0' in data[:8192]
    if utf16:
        text = data.decode('utf-16', 'replace')
    else:
        text = data.decode('utf-8', 'replace')
    findings = []
    for rule, pattern in PATTERNS.items():
        if not privacy and rule not in SECRET_RULES:
            continue
        # Random compressed image bytes can coincidentally resemble an email.
        # Image metadata is parsed separately; strong secret markers still scan.
        if binary and rule == 'email':
            continue
        seen = set()
        for match in pattern.finditer(text):
            value = match.group(1) if rule == 'credential-assignment' else match.group(0)
            kind = rule
            if rule == 'credential-assignment':
                if (value.lower() in ('none', 'null', 'true', 'false', 'password', 'test', 'example', 'changeme')
                        or value.startswith(('$', '%', '{', '<'))):
                    continue
            elif rule == 'email':
                domain = value.rsplit('@', 1)[1].lower()
                if (domain.endswith('noreply.github.com') or domain in
                        ('example.com', 'example.org', 'example.net', 'localhost')
                        or value.lower() in ('noreply@github.com', 'support@github.com')):
                    kind = 'public-or-example-email'
                else:
                    kind = 'contact-email'
            elif rule in ('windows-profile', 'unix-profile'):
                name = match.group(1).lower()
                kind = ('generic-profile' if name in ('pc', 'user', 'username', 'runner', 'public', 'default')
                        else 'personal-profile')
            fingerprint = hashlib.sha256(value.encode('utf-8')).hexdigest()[:12]
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            findings.append(dict(path=path, line=text.count('\n', 0, match.start()) + 1,
                rule=rule, kind=kind, fingerprint=fingerprint, location=location))
    return findings, truncated


def summarize(findings):
    return dict(by_rule=dict(Counter(item['rule'] for item in findings)),
                by_kind=dict(Counter(item['kind'] for item in findings)),
                files=len({item['path'] for item in findings}))


def inspect_image(data, path, location):
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        exif = image.getexif()
        info = dict(path=path, location=location, size=image.size,
                    metadata_keys=sorted(image.info), exif_tags=sorted(exif.keys()), gps=34853 in exif)
        findings, _ = inspect_data(str(dict(image.info, exif=dict(exif))).encode(), path, location)
        if info['gps']:
            findings.append(dict(path=path, location=location, rule='image-gps', kind='image-gps', line=0))
        return info, findings


def scan(root, history=False, max_file_bytes=0):
    tracked = git_paths(root, 'ls-files')
    ignored = git_paths(root, 'ls-files', '-ci', '--exclude-standard')
    untracked = git_paths(root, 'ls-files', '--others', '--exclude-standard')
    report = dict(tracked=len(tracked), tracked_ignored=len(ignored),
                  current_findings=[], history_findings=[], skipped=[], images=[], history_images=[])
    for path in dict.fromkeys(tracked + untracked):
        source = root / path
        if not source.is_file():
            report['skipped'].append(dict(path=path, reason='not a working-tree file'))
            continue
        if max_file_bytes and source.stat().st_size > max_file_bytes:
            report['skipped'].append(dict(path=path, reason='working-tree file exceeds selected size limit'))
            continue
        data = source.read_bytes()
        findings, truncated = inspect_data(data, path, 'working-tree')
        report['current_findings'].extend(findings)
        if truncated:
            report['skipped'].append(dict(path=path, reason='gzip exceeds expanded-size limit'))
        if source.suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp', '.ico'):
            try:
                info, extra = inspect_image(data, path, 'image-metadata')
                report['images'].append(info)
                report['current_findings'].extend(extra)
            except (OSError, ValueError) as error:
                report['skipped'].append(dict(path=path, reason='image metadata: ' + type(error).__name__))
    metadata = git(root, 'log', '--all', '--format=%H%n%an <%ae>%n%cn <%ce>%n%B').decode('utf-8', 'replace')
    report['commit_metadata'], _ = inspect_data(metadata.encode(), '<git commit metadata>', 'git-log')
    report['commit_count'] = len(git(root, 'log', '--all', '--format=%H').splitlines())
    try:
        config = git(root, 'config', '--get-regexp', r'remote\..*\.url|http\..*extraheader|credential\..*')
    except subprocess.CalledProcessError as error:
        if error.returncode != 1:
            raise
        config = b''
    report['local_git_config'], _ = inspect_data(config, '<local Git config>', 'local-only')
    if history:
        object_paths = {}
        for line in git(root, 'rev-list', '--objects', '--all').splitlines():
            oid, _, path = line.partition(b' ')
            object_paths[oid.decode()] = path.decode('utf-8', 'replace')
        sizes = git(root, 'cat-file', '--batch-check', data=('\n'.join(object_paths) + '\n').encode())
        blobs = [(oid, int(size)) for oid, kind, size in
                 (line.decode().split() for line in sizes.splitlines()) if kind == 'blob']
        report['history_blobs'] = len(blobs)
        report['history_blob_bytes'] = sum(size for _, size in blobs)
        print(json.dumps(dict(stage='history', blobs=len(blobs), bytes=report['history_blob_bytes'])), flush=True)
        process = subprocess.Popen(['git', 'cat-file', '--batch'], cwd=root,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            for index, (oid, size) in enumerate(blobs):
                process.stdin.write((oid + '\n').encode())
                process.stdin.flush()
                process.stdout.readline()
                data = process.stdout.read(size)
                process.stdout.read(1)
                findings, truncated = inspect_data(data, object_paths[oid], oid)
                report['history_findings'].extend(findings)
                if Path(object_paths[oid]).suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp', '.ico'):
                    info, extra = inspect_image(data, object_paths[oid], oid)
                    report['history_images'].append(info)
                    report['history_findings'].extend(extra)
                if truncated:
                    report['skipped'].append(dict(path=object_paths[oid], location=oid,
                                                 reason='gzip exceeds expanded-size limit'))
                if (index + 1) % 1000 == 0:
                    print(json.dumps(dict(stage='history', done=index + 1, total=len(blobs))), flush=True)
        finally:
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()
            process.wait(timeout=10)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd(), help='Git project to inspect')
    parser.add_argument('--history', action='store_true')
    parser.add_argument('--max-file-bytes', type=int, default=0, help='Optional current-file size limit; skipped files are reported')
    parser.add_argument('--report', type=Path, help='Save redacted findings to this file')
    args = parser.parse_args()
    root = Path(git(args.root, 'rev-parse', '--show-toplevel').decode().strip())
    report = scan(root, args.history, max_file_bytes=args.max_file_bytes)
    summary = {name: summarize(report[name]) for name in
               ('current_findings', 'history_findings', 'commit_metadata', 'local_git_config')}
    summary.update(tracked=report['tracked'], tracked_ignored=report['tracked_ignored'],
                   commits=report['commit_count'], skipped=report['skipped'], images=report['images'])
    if args.report:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
