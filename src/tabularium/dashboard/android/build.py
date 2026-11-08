"""Build with the official JDK/Android SDK tools; no Gradle or app dependencies."""
import argparse
import hashlib
import json
import os
import subprocess
import shutil
import zipfile
from pathlib import Path


def build(args):
    root = Path(__file__).resolve().parent
    output = root/'build'
    for name in ['generated', 'classes', 'dex', 'policy-tests']:
        directory = (output/name).resolve()
        if directory.parent != output.resolve():
            raise ValueError('Build directory escaped output folder')
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)
    java = Path(args.java)/'bin'
    sdk = Path(args.sdk)
    tools = sdk/'build-tools/35.0.0'
    android = sdk/'platforms/android-35/android.jar'
    env = {**os.environ, 'JAVA_HOME': str(Path(args.java))}
    def run(*command):
        result = subprocess.run([str(c) for c in command], env=env, cwd=root, capture_output=True, text=True,
                                timeout=180, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        print(result.stdout + result.stderr, end='')
        result.check_returncode()
    policy = root/'src/net/tabularium/agents/OriginPolicy.java'
    run(java/'javac.exe', '--release', '8', '-d', output/'policy-tests', policy, root/'tests/OriginPolicyTest.java')
    run(java/'java.exe', '-cp', output/'policy-tests', 'net.tabularium.agents.OriginPolicyTest')
    config = output/'generated/BuildConfig.java'
    config.write_text('package net.tabularium.agents; final class BuildConfig { static final String HOME_URL = '
                      + json.dumps(args.url) + '; }', encoding='utf-8')
    run(tools/'aapt2.exe', 'compile', '--dir', root/'res', '-o', output/'resources.zip')
    run(tools/'aapt2.exe', 'link', '-I', android, '--manifest', root/'AndroidManifest.xml',
        '--java', output/'generated', '-o', output/'unsigned.apk', output/'resources.zip')
    sources = list((root/'src').rglob('*.java')) + list((output/'generated').rglob('*.java'))
    run(java/'javac.exe', '--release', '8', '-encoding', 'UTF-8', '-classpath', android,
        '-d', output/'classes', *sources)
    run(tools/'d8.bat', '--min-api', '26', '--lib', android, '--output', output/'dex',
        *list((output/'classes').rglob('*.class')))
    archive = zipfile.ZipFile(output/'unsigned.apk', 'a', compression=zipfile.ZIP_DEFLATED)
    try:
        for dex in (output/'dex').glob('*.dex'):
            archive.write(dex, dex.name)
    finally:
        archive.close()
    run(tools/'zipalign.exe', '-f', '4', output/'unsigned.apk', output/'aligned.apk')
    run(tools/'apksigner.bat', 'sign', '--ks', args.keystore, '--ks-pass', 'file:'+args.password_file,
        '--out', output/'agents.apk', output/'aligned.apk')
    run(tools/'apksigner.bat', 'verify', '--verbose', output/'agents.apk')
    digest = hashlib.sha256((output/'agents.apk').read_bytes()).hexdigest()
    (output/'agents.apk.sha256').write_text(digest+'  agents.apk\n', encoding='ascii')
    print('APK:', output/'agents.apk')
    print('SHA256:', digest)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for name in ['java', 'sdk', 'keystore', 'password-file']:
        parser.add_argument('--'+name, required=True)
    parser.add_argument('--url', default='')
    build(parser.parse_args())
