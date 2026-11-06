"""Trusted source-to-APK and owned-emulator acceptance for generated inventory apps."""
from __future__ import annotations
import argparse,hashlib,json,os,re,subprocess,sys,time,uuid,xml.etree.ElementTree as ET
from pathlib import Path
import application_android_build as build
import application_builder
import application_platform as platform

def run(argv,timeout=30):
    p=subprocess.run([str(x) for x in argv],stdin=subprocess.DEVNULL,capture_output=True,timeout=timeout)
    if p.returncode:raise ValueError(p.stderr.decode('utf8','replace')[-2000:])
    return p.stdout

def verify(manifest_path,sdk_linux,sdk_win,image,output,package):
    output.mkdir(parents=True,exist_ok=False)
    manifest=platform._strict(manifest_path);platform._manifest(manifest)
    receipt=build.build(manifest_path,sdk_linux,output/'app.apk','docker',image)
    (output/'build.json').write_text(json.dumps(receipt,indent=2),encoding='utf8')
    adb=sdk_win/'platform-tools/adb.exe';emu=sdk_win/'emulator/emulator.exe';serial='emulator-5580';proc=None;installed=False
    def call(*args,timeout=30):return run([adb,'-s',serial,*args],timeout)
    def dump():
        call('shell','uiautomator','dump','/sdcard/autoforge-generated.xml')
        return call('shell','cat','/sdcard/autoforge-generated.xml').decode('utf8')
    def nodes(xml):return list(ET.fromstring(xml).iter('node'))
    def wait_text(value,label):
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            xml=dump()
            if any(n.get('text')==value for n in nodes(xml)):
                (output/(label+'.xml')).write_text(xml,encoding='utf8');return xml
            time.sleep(.3)
        raise ValueError('expected visible text missing: '+value)
    def tap(desc):
        matches=[n for n in nodes(dump()) if n.get('content-desc')==desc]
        if len(matches)!=1:raise ValueError('unique UI content description missing: '+desc)
        m=re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]',matches[0].get('bounds',''))
        if not m:raise ValueError('invalid UI bounds')
        x1,y1,x2,y2=map(int,m.groups());call('shell','input','tap',str((x1+x2)//2),str((y1+y2)//2))
    checks=[]
    try:
        if serial in run([adb,'devices']).decode():raise ValueError('emulator serial already occupied; no adoption')
        proc=subprocess.Popen([str(emu),'-avd','autoforge_api35','-no-snapshot','-no-audio','-no-boot-anim','-no-window','-gpu','swiftshader_indirect','-port','5580','-accel','on'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env={**os.environ,'ANDROID_SDK_ROOT':str(sdk_win),'JAVA_HOME':'D:/Android Studio/jbr'},creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            if proc.poll() is not None:raise ValueError('owned emulator exited')
            try:
                if call('shell','getprop','sys.boot_completed').strip()==b'1':break
            except (ValueError,subprocess.TimeoutExpired):pass
            time.sleep(1)
        else:raise ValueError('emulator boot timeout')
        if ('package:'+package) in call('shell','pm','list','packages',package).decode():raise ValueError('preexisting package; refusing overwrite')
        call('install','-t',str(output/'app.apk'),timeout=60);installed=True
        call('shell','am','start','-W','-n',package+'/.MainActivity');wait_text('0 items','initial');checks.append('initial')
        for i,text in enumerate(('Alpha','Beta'),1):
            tap('item_name');call('shell','input','text',text);call('shell','input','keyevent','BACK');tap('add_item');wait_text(str(i)+' items','added-'+text);wait_text(text,'row-'+text);checks.append('add-'+text)
        tap('add_item');wait_text('invalid_input','invalid');checks.append('invalid')
        call('shell','am','force-stop',package);call('shell','am','start','-W','-n',package+'/.MainActivity');wait_text('2 items','restart');wait_text('Alpha','persist-alpha');wait_text('Beta','persist-beta');checks.append('persistence')
        screenshot=call('exec-out','screencap','-p');(output/'screen.png').write_bytes(screenshot)
        return {'status':'PASS','candidate_sha256':manifest['candidate_sha256'],'build':receipt,'checks':checks,'screenshot_sha256':hashlib.sha256(screenshot).hexdigest(),'scope':'Generated source on owned API35 emulator; not physical device or release signing'}
    finally:
        cleanup_errors=[]
        if installed:
            try:
                call('shell','am','force-stop',package);call('uninstall',package,timeout=60)
                if ('package:'+package) in call('shell','pm','list','packages',package).decode():cleanup_errors.append('owned package remains installed')
            except Exception as e:cleanup_errors.append('owned package cleanup unconfirmed: '+str(e))
        if proc is not None:
            try:call('emu','kill')
            except Exception:pass
            try:proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],capture_output=True,timeout=20)
                proc.wait(timeout=10)
            deadline=time.monotonic()+15
            while True:
                devices=run([adb,'devices']).decode().splitlines()[1:]
                if not any(line.split() and line.split()[0]==serial for line in devices):break
                if time.monotonic()>=deadline:
                    cleanup_errors.append('owned emulator still visible');break
                time.sleep(.2)
        if cleanup_errors:raise ValueError('; '.join(cleanup_errors))

def main():
    p=argparse.ArgumentParser();p.add_argument('--image',required=True);p.add_argument('--package',required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--sdk-linux',type=Path,default=Path('F:/OMP/tools/android-sdk-linux'));p.add_argument('--sdk-win',type=Path,default=Path('F:/OMP/tools/android-sdk-win'));a=p.parse_args()
    env=application_builder._strict_loads(sys.stdin.buffer.read(application_builder.MAX_INPUT+1))
    if not isinstance(env,dict) or set(env)!={'schema','workspace_manifest','candidate_sha256'} or env['schema']!=1:raise ValueError('invalid envelope')
    manifest=env['workspace_manifest'];_,digest=platform._manifest(manifest)
    if digest!=env['candidate_sha256']:raise ValueError('candidate digest mismatch')
    work=a.output/uuid.uuid4().hex;work.parent.mkdir(parents=True,exist_ok=True)
    mf=work.with_suffix('.manifest.json');mf.write_text(json.dumps(manifest),encoding='utf8')
    try:report=verify(mf,a.sdk_linux,a.sdk_win,a.image,work,a.package)
    except (ValueError,OSError,subprocess.TimeoutExpired) as e:report={'status':'FAIL','candidate_sha256':digest,'reason':str(e)[:2500]}
    with work.with_suffix('.report.json').open('x',encoding='utf8') as f:json.dump(report,f,indent=2)
    passed=report['status']=='PASS'
    print(json.dumps({'schema':1,'candidate_sha256':digest,'build_pass':passed,'test_pass':passed,'observations':report.get('checks',[report.get('reason','unknown')]),'limitations':['Real source build and owned emulator; '+str(work.with_suffix('.report.json'))]}));return 0
if __name__=='__main__':raise SystemExit(main())
