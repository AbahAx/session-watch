"""Build public Linux/runtime and source archives from an explicit allowlist."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parent.parent
TOP = ('README.md', 'LICENSE', 'SECURITY.md', 'requirements.txt', 'requirements-dev.txt',
       'requirements.lock.txt', 'requirements-linux.lock.txt', 'pyproject.toml',
       'install.sh', 'online-install.sh', 'Dockerfile', 'compose.yaml', '.dockerignore', '.gitignore', '.gitattributes')
EXCLUDED = {'__pycache__', '.pytest_cache', '.venv', 'node_modules', 'data', '.git'}


def files(root=ROOT, source=False):
    result = {}
    for name in TOP:
        path = root / name
        if path.is_file():
            result[name] = path.read_bytes()
    directories = ['app', 'deploy', 'docs', 'frontend/dist']
    if source:
        directories += ['tests', '.github', 'frontend/src']
        for name in ('frontend/package.json', 'frontend/pnpm-lock.yaml', 'frontend/pnpm-workspace.yaml', 'frontend/index.html',
                     'frontend/vite.config.ts', 'frontend/tsconfig.json'):
            if (root / name).is_file():
                result[name] = (root / name).read_bytes()
    for directory in directories:
        for path in sorted((root / directory).rglob('*')):
            relative = path.relative_to(root)
            if path.is_symlink():
                raise ValueError('Symlink in public source: ' + str(relative))
            if path.is_file() and not any(part in EXCLUDED for part in relative.parts) and path.suffix not in ('.pyc', '.pyo'):
                result[relative.as_posix()] = path.read_bytes()
    for needed in ('frontend/dist/index.html', 'app/main.py', 'requirements-linux.lock.txt', 'install.sh'):
        if needed not in result:
            raise ValueError('Build frontend / prepare dependencies first: missing ' + needed)
    for name, content in result.items():
        # Simple leak tripwires supplement the positive allowlist. Not a secret scanner.
        if any((b'BEGIN ' + marker) in content for marker in (b'OPENSSH PRIVATE KEY', b'RSA PRIVATE KEY', b'EC PRIVATE KEY')):
            raise ValueError('Private key material in public source: ' + name)
        if re.search(rb'\b[0-9]{7,12}:[A-Za-z0-9_-]{30,}\b', content):
            raise ValueError('Possible real Telegram token in public source: ' + name)
    version = re.search(r'__version__\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"', result['app/__init__.py'].decode())[1]
    manifest = {'format': 1, 'version': version, 'kind': 'source' if source else 'linux', 'files': {
        name: {'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
        for name, content in sorted(result.items())}}
    result['release-manifest.json'] = (json.dumps(manifest, indent=2, ensure_ascii=False) + '\n').encode()
    return version, result


def build(output, root=ROOT):
    output.mkdir(parents=True, exist_ok=True)
    version, runtime = files(root)
    archive = output / f'session-watch-v{version}-linux.tar.gz'
    with tarfile.open(archive, 'w:gz', format=tarfile.PAX_FORMAT) as tar:
        for name, content in sorted(runtime.items()):
            info = tarfile.TarInfo('session-watch/' + name)
            info.size = len(content)
            info.mtime = 0
            info.mode = 0o755 if name == 'install.sh' else 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = 'root'
            tar.addfile(info, io.BytesIO(content))
    _, source = files(root, source=True)
    source_archive = output / f'session-watch-v{version}-source.zip'
    with zipfile.ZipFile(source_archive, 'w', compression=zipfile.ZIP_DEFLATED) as zipped:
        for name, content in sorted(source.items()):
            info = zipfile.ZipInfo('session-watch/' + name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100755 if name == 'install.sh' else 0o100644) << 16
            zipped.writestr(info, content)
    assets = [archive, source_archive]
    if (root / 'online-install.sh').is_file():
        launcher = output / 'online-install.sh'
        launcher.write_bytes((root / 'online-install.sh').read_bytes())
        assets.append(launcher)
    sums = ''.join(hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name + '\n' for p in assets)
    (output / 'SHA256SUMS').write_text(sums, encoding='ascii', newline='\n')
    print(sums, end='')
    return archive, source_archive


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    build(parser.parse_args().output)
