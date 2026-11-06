#!/usr/bin/env python3
"""Focused guard checks for the Android build/provision adapter; no SDK or device use."""
from __future__ import annotations
import argparse, hashlib, io, json, subprocess, tempfile, zipfile
from pathlib import Path
import application_android_build as android

def check(condition, message):
    if not condition: raise AssertionError(message)

def isolated_staging_check(image):
    """Use an inert SDK tool to check the real container boundary, not APK output."""
    with tempfile.TemporaryDirectory(prefix='autoforge-android-isolation-') as td:
        base=Path(td); source=base/'source'; source.mkdir()
        (source/'AndroidManifest.xml').write_text('<manifest package="org.example.probe"/>',encoding='utf8')
        (source/'Main.java').write_text('class Main {}',encoding='utf8')
        rows=[{'path':p.name,'size':p.stat().st_size,'sha256':android.sha_file(p)} for p in sorted(source.iterdir())]
        body={'schema':1,'goal':'isolated staging regression','references':[],'files':rows}
        manifest={**body,'workspace':str(source),'candidate_sha256':hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()}
        manifest_path=base/'manifest.json'; manifest_path.write_text(json.dumps(manifest),encoding='utf8')
        sdk=base/'sdk'; platform=sdk/'platforms/android-35'; platform.mkdir(parents=True)
        (platform/'android.jar').write_bytes(b'inert SDK layout fixture')
        tools=sdk/'build-tools/36.0.0'; (tools/'lib').mkdir(parents=True)
        (tools/'lib/d8.jar').write_bytes(b'inert SDK layout fixture')
        (tools/'aapt2').write_text('''#!/bin/sh
set -eu
test "$(id -u)" = 65534
test "$(cat /candidate/Main.java)" = 'class Main {}'
test ! -e /candidate/unlisted.txt
javac -proc:none -d /tmp/work/classes /candidate/Main.java
test -s /tmp/work/classes/Main.class
printf 'stage-boundary' > /output/probe.txt
echo AUTOFORGE_ANDROID_STAGE_BOUNDARY_PASS >&2
exit 73
''',encoding='utf8',newline='\n')
        (tools/'aapt2').chmod(0o755)
        original_run=android._run; dispatch=[]
        def run(argv,timeout=120):
            if len(argv)>1 and argv[1]=='run':
                dispatch.append(argv)
                (source/'Main.java').write_text('changed after staging',encoding='utf8')
                (source/'unlisted.txt').write_text('must not be mounted',encoding='utf8')
            return original_run(argv,timeout)
        from unittest.mock import patch
        with patch.object(android,'_run',side_effect=run):
            try: android.build(manifest_path,sdk,base/'app.apk','docker',image)
            except android.AndroidBuildError as exc:
                check('AUTOFORGE_ANDROID_STAGE_BOUNDARY_PASS' in str(exc),'actual isolated staging/JDK assertion did not pass: '+str(exc))
            else: raise AssertionError('deliberately failing SDK fixture published an APK')
        check(not (base/'app.apk').exists(),'failed build published an APK')
        check(not list(base.glob('.android-build-*')),'completed owned build left staging behind')
        name=dispatch[0][dispatch[0].index('--name')+1]
        remaining=subprocess.run(['docker','ps','-aq','--filter',f'name=^{name}$'],capture_output=True,text=True,timeout=20,check=True)
        check(not remaining.stdout.strip(),'owned build container remains')
    return {'status':'PASS','scope':'actual network-none nonroot JDK compile and manifest-only staging; inert SDK deliberately fails, not APK/native acceptance'}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--isolated-image',help='opt in to actual Docker staging regression with exact local Linux JDK sha256 ID')
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='autoforge-android-check-') as td:
        root=Path(td)
        # Official Google repository namespace and checksum parsing, using a small synthetic excerpt.
        xml=b'''<sdk:sdk-repository xmlns:sdk="http://schemas.android.com/sdk/android/repo/repository2/01"><sdk:remotePackage path="build-tools;36.0.0"><sdk:revision><sdk:major>36</sdk:major></sdk:revision><sdk:archives><sdk:archive><sdk:complete><sdk:size>17</sdk:size><sdk:checksum>0123456789012345678901234567890123456789</sdk:checksum><sdk:url>tools.zip</sdk:url></sdk:complete></sdk:archive></sdk:archives></sdk:remotePackage></sdk:sdk-repository>'''
        rows=android.packages(xml)
        check(len(rows)==1 and rows[0]['path']=='build-tools;36.0.0' and rows[0]['revision']=='36' and rows[0]['url']=='https://dl.google.com/android/repository/tools.zip' and rows[0]['checksum_type']=='sha1' and rows[0]['size']==17, 'official metadata package selection failed')
        # Safe extraction handles package roots and rejects traversal.
        archive=io.BytesIO()
        with zipfile.ZipFile(archive,'w') as z: z.writestr('sdk/file.txt','ok')
        dest=root/'safe'; android._safe_extract(archive.getvalue(),dest)
        check((dest/'sdk'/'file.txt').read_text()=='ok','safe archive extraction failed')
        bad=io.BytesIO()
        with zipfile.ZipFile(bad,'w') as z: z.writestr('../escape','bad')
        try: android._safe_extract(bad.getvalue(),root/'bad')
        except android.AndroidBuildError: pass
        else: raise AssertionError('archive traversal was accepted')
        # Exercise actual installation/receipt handling with tiny local zips and synthetic metadata only.
        host=root/'host'; host.mkdir()
        fixture=[]
        specs=[('platforms;android-35',{'platforms/android-35/android.jar':b'jar'}),('build-tools;36.0.0',{'build-tools/36.0.0/aapt2':b'tool'}),('platform-tools;37',{'platform-tools/adb':b'adb'}),('cmdline-tools;latest',{'cmdline-tools/bin/sdkmanager':b'cmd'}),('emulator',{'emulator/emulator':b'emulator'})]
        for name,entries in specs:
            blob=io.BytesIO()
            with zipfile.ZipFile(blob,'w') as z:
                for member,data in entries.items(): z.writestr(member,data)
            raw=blob.getvalue(); checksum=hashlib.sha1(raw).hexdigest()
            path=root/checksum; path.write_bytes(raw)
            fixture.append({'path':name,'revision':'1','checksum':checksum,'checksum_type':'sha1','size':len(raw),'url':'https://dl.google.com/android/repository/test.zip','test_archive':path})
        import unittest.mock as mock
        fake={'schema':1,'repository':android.REPO,'repository_xml_sha256':'0'*64,'packages':fixture}
        def cached(pkg,cache): return pkg['test_archive']
        with mock.patch.object(android,'official_inventory',return_value=fake), mock.patch.object(android,'packages',return_value=fixture), mock.patch.object(android,'_download_package',side_effect=cached):
            result=android.provision(host/'sdk','35','36.0.0',None,True)
        check(result['status']=='PROVISIONED' and len(result['packages'])==5,'SDK provisioning receipt failed')
        check((host/'sdk'/'platforms'/'android-35'/'android.jar').read_bytes()==b'jar','platform SDK package was not installed correctly')
        check((host/'sdk'/'build-tools'/'36.0.0'/'aapt2').read_bytes()==b'tool','build-tools archive layout failed')
        check((host/'sdk'/'platform-tools'/'adb').read_bytes()==b'adb','platform-tools archive layout failed')
        check((host/'sdk'/'emulator'/'emulator').read_bytes()==b'emulator','emulator archive layout failed')
        check((host/'sdk'/'cmdline-tools'/'latest'/'bin'/'sdkmanager').read_bytes()==b'cmd','command-line tools layout failed')
    result={'status':'PASS','scope':'offline parser, archive traversal defense and provisioning layout guards','sdk_downloaded':False,'candidate_executed':False,'device_used':False}
    if args.isolated_image: result['isolated_staging']=isolated_staging_check(args.isolated_image)
    print(json.dumps(result,sort_keys=True))

if __name__=='__main__': main()
