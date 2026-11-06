#!/usr/bin/env python3
"""Compile and exercise a manifest-bound WinForms source only inside Windows Sandbox.

Scenario assertions are produced by the independent host screenshot/OCR witness;
guest UIAutomation acknowledgements and guest reports never authorize acceptance.
"""
from __future__ import annotations
import argparse, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time
import xml.etree.ElementTree as ET
from pathlib import Path
import windows_desktop_host_witness as witness
MAX_REQUEST=4_194_304

def sha(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda:f.read(1<<20),b''): h.update(chunk)
    return h.hexdigest()
def fail(message): raise ValueError(message)
def strict_json(data):
    def pairs(rows):
        out={}
        for k,v in rows:
            if k in out: fail('duplicate JSON key')
            out[k]=v
        return out
    return json.loads(data,object_pairs_hook=pairs,parse_constant=lambda _:fail('invalid JSON constant'))
def main():
    cli=argparse.ArgumentParser(description=__doc__); cli.add_argument('--contract',required=True); cli.add_argument('--timeout',type=int,default=120); args=cli.parse_args()
    if os.name!='nt': fail('Windows Sandbox source build is available only on Windows')
    raw=sys.stdin.buffer.read(MAX_REQUEST+1)
    if len(raw)>MAX_REQUEST: fail('request exceeds byte limit')
    req=strict_json(raw.decode('utf-8')); contract=strict_json(Path(args.contract).read_text(encoding='utf-8'))
    required={'schema','source_root','source_sha256','source_path','target','scenarios','timeout_seconds','run_id'}
    if not isinstance(req,dict) or set(req)!=required or type(req['schema']) is not int or req['schema']!=1: fail('invalid request schema')
    if type(args.timeout)is not int or not 1<=args.timeout<=300 or req['timeout_seconds']!=args.timeout or type(req['timeout_seconds']) is not int: fail('timeout mismatch/out of bounds')
    if contract.get('schema')!=1 or req['target']!=contract.get('target') or req['scenarios']!=contract.get('scenarios'): fail('request differs from independent contract')
    if not isinstance(req['run_id'],str) or not re.fullmatch('[0-9a-f]{32}',req['run_id']): fail('invalid run id')
    root=Path(req['source_root']).resolve(strict=True); rel=req['source_path']
    if not root.is_dir() or not isinstance(rel,str) or not rel.endswith('.cs') or '\\' in rel or rel.startswith('/') or any(x in ('','.','..') for x in rel.split('/')): fail('one safe manifest-relative .cs file required')
    source=root.joinpath(*rel.split('/'))
    if source.is_symlink() or not source.is_file() or root not in source.resolve(strict=True).parents or not re.fullmatch('[0-9a-f]{64}',req['source_sha256']) or sha(source)!=req['source_sha256']: fail('source hash/path mismatch')
    sandbox=Path(os.environ.get('WINDIR',r'C:\Windows'))/'System32'/'WindowsSandbox.exe'
    if not sandbox.is_file(): fail('Windows Sandbox client missing')
    temp=Path(tempfile.mkdtemp(prefix='autoforge-source-build-')); hwnd=None; before=set(); evidence=None
    try:
        staged=temp/'candidate'; staged.mkdir(); shutil.copyfile(source,staged/'main.cs')
        if sha(staged/'main.cs')!=req['source_sha256']: fail('staged source hash mismatch')
        (temp/'actuator.ps1').write_text(r'''$ErrorActionPreference='Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName Accessibility
Add-Type -ReferencedAssemblies Accessibility -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using Accessibility;
public static class NativeAccessible {
 public delegate bool EnumProc(IntPtr hwnd, IntPtr arg);
 [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr parent, EnumProc callback, IntPtr arg);
 [DllImport("oleacc.dll")] static extern int AccessibleObjectFromWindow(IntPtr hwnd, uint id, ref Guid iid, [MarshalAs(UnmanagedType.Interface)] out IAccessible result);
 public static IAccessible Find(IntPtr parent, string name) {
  var matches = new List<IAccessible>();
  EnumChildWindows(parent, delegate(IntPtr hwnd, IntPtr arg) {
   Guid iid = new Guid("618736e0-3c3d-11cf-810c-00aa00389b71");
   IAccessible item;
   if (AccessibleObjectFromWindow(hwnd, 0xfffffffc, ref iid, out item) == 0 && item != null) {
    try { if (item.get_accName(0) == name) matches.Add(item); } catch (COMException) {}
   }
   return true;
  }, IntPtr.Zero);
  if (matches.Count != 1) throw new InvalidOperationException("MSAA accessible name not unique: " + name + "; matches=" + matches.Count);
  return matches[0];
 }
 public static void Act(IntPtr parent, string name, string action, string value) {
  IAccessible item = Find(parent, name);
  if (action == "type") item.set_accValue(0, value);
  else if (action == "click") item.accDoDefaultAction(0);
  else if (action == "key") item.accSelect(1, 0);
  else throw new InvalidOperationException("Unsupported accessibility action");
 }
}
'@
$request=Get-Content -LiteralPath 'C:\host\request.json' -Raw | ConvertFrom-Json
$config=Get-Content -LiteralPath 'C:\host\launch.json' -Raw | ConvertFrom-Json
$target=[string]$request.target
$process=Start-Process -FilePath ([string]$config.path) -PassThru
$root=[System.Windows.Automation.AutomationElement]::RootElement
$window=$null
$deadline=[DateTime]::UtcNow.AddSeconds([int]$request.timeout_seconds)
while([DateTime]::UtcNow -lt $deadline -and $null -eq $window){
 if($process.HasExited){throw 'candidate exited before UI appeared'}
 $condition=New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ProcessIdProperty,$process.Id)
 $candidate=$root.FindFirst([System.Windows.Automation.TreeScope]::Children,$condition)
 if($null -ne $candidate -and [string]$candidate.Current.Name -ceq $target){$window=$candidate}
 if($null -eq $window){Start-Sleep -Milliseconds 150}
}
if($null -eq $window){throw 'target window did not appear'}
$walker=[System.Windows.Automation.TreeWalker]::RawViewWalker
function Dump-Raw([System.Windows.Automation.AutomationElement]$node,[int]$depth){
 if($depth -gt 12){return}
 $line=('{0}{1}|name={2}|id={3}|type={4}' -f (' '*$depth),$depth,$node.Current.Name,$node.Current.AutomationId,$node.Current.ControlType.ProgrammaticName)
 Add-Content -LiteralPath 'C:\host\uia-raw-tree.txt' -Value $line -Encoding UTF8
 $child=$walker.GetFirstChild($node)
 while($null -ne $child){Dump-Raw $child ($depth+1);$child=$walker.GetNextSibling($child)}
}
Dump-Raw $window 0
while(-not (Test-Path -LiteralPath 'C:\host\stop')){
 $path='C:\host\action.json'
 if(Test-Path -LiteralPath $path){
  $msg=Get-Content -LiteralPath $path -Raw | ConvertFrom-Json; $a=$msg.action; $result=[ordered]@{schema=1;id=[string]$msg.id;ok=$false;error=$null}
  try{
   if($a.type -eq 'wait'){Start-Sleep -Milliseconds ([Math]::Min(5000,[Math]::Max(0,[int]$a.value)))}
   elseif($a.type -in @('type','click','key')){
    [NativeAccessible]::Act([IntPtr]$window.Current.NativeWindowHandle,[string]$a.target,[string]$a.type,[string]$a.value)
    if($a.type -eq 'key'){$shell=New-Object -ComObject WScript.Shell;$shell.SendKeys([string]$a.value)}
   }
   else{throw 'unsupported action'}
   $result.ok=$true
  }catch{$result.error=$_.Exception.Message}
  [IO.File]::WriteAllText('C:\host\acted.json',($result|ConvertTo-Json -Compress),(New-Object System.Text.UTF8Encoding($false)));Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
 }
 Start-Sleep -Milliseconds 100
}
if(-not $process.HasExited){Stop-Process -Id $process.Id -Force; $process.WaitForExit(5000)|Out-Null}
''',encoding='utf-8',newline='\r\n')
        (temp/'request.json').write_text(json.dumps({'schema':1,'run_id':req['run_id'],'target':req['target'],'timeout_seconds':req['timeout_seconds']}),encoding='utf-8')
        (temp/'guest.ps1').write_text(r'''$ErrorActionPreference='Stop'
Start-Transcript -Path 'C:\host\guest-startup.txt' -Force | Out-Null
$source='C:\candidate\main.cs'
$dir=Join-Path $env:TEMP ('autoforge-build-'+[guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $dir | Out-Null
$exe=Join-Path $dir 'main.exe'
$csc='C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if(-not (Test-Path -LiteralPath $csc)){throw 'Framework64 csc.exe missing in Sandbox'}
$args=@('/nologo','/target:winexe',('/out:'+ $exe),'/reference:System.Windows.Forms.dll','/reference:System.Drawing.dll','/reference:System.dll',$source)
$proc=Start-Process -FilePath $csc -ArgumentList $args -Wait -PassThru -NoNewWindow
if($proc.ExitCode -ne 0){throw ('Sandbox compilation failed: '+$proc.ExitCode)}
if(-not (Test-Path -LiteralPath $exe)){throw 'compiler output missing'}
# csc.exe emits a .NET Framework 4.0-targeted app. Newer Framework WinForms
# accessibility enhancements are opt-in for apps targeting pre-4.7.1; explicitly
# enable the successive improvements so standard controls expose usable UIA peers.
$configPath=$exe+'.config'
$configXml='<?xml version="1.0" encoding="utf-8"?><configuration><runtime><AppContextSwitchOverrides value="Switch.UseLegacyAccessibilityFeatures=false;Switch.UseLegacyAccessibilityFeatures.2=false;Switch.UseLegacyAccessibilityFeatures.3=false;Switch.UseLegacyAccessibilityFeatures.4=false;Switch.UseLegacyAccessibilityFeatures.5=false" /></runtime></configuration>'
[IO.File]::WriteAllText($configPath,$configXml,(New-Object System.Text.UTF8Encoding($false)))
if(-not (Test-Path -LiteralPath $configPath)){throw 'WinForms accessibility runtime config missing'}
[IO.File]::WriteAllText('C:\host\launch.json',('{"path":"'+$exe.Replace('\','\\')+'","args":[]}'),(New-Object System.Text.UTF8Encoding($false)))
& 'C:\host\actuator.ps1'
Stop-Transcript | Out-Null
& "$env:SystemRoot\System32\shutdown.exe" /s /t 0 /f
''',encoding='utf-8',newline='\r\n')
        config=ET.Element('Configuration'); ET.SubElement(config,'VGpu').text='Disable'; ET.SubElement(config,'Networking').text='Disable'; ET.SubElement(config,'ClipboardRedirection').text='Disable'
        mappings=ET.SubElement(config,'MappedFolders')
        for host,guestpath,readonly in ((str(staged),r'C:\candidate',True),(str(temp),r'C:\host',False)):
            mapping=ET.SubElement(mappings,'MappedFolder'); ET.SubElement(mapping,'HostFolder').text=host; ET.SubElement(mapping,'SandboxFolder').text=guestpath; ET.SubElement(mapping,'ReadOnly').text='true' if readonly else 'false'
        logon=ET.SubElement(config,'LogonCommand'); ET.SubElement(logon,'Command').text=r'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "C:\host\guest.ps1"'
        wsb=temp/'run.wsb'; wsb.write_bytes(b'<?xml version="1.0" encoding="UTF-8"?>\n'+ET.tostring(config,encoding='utf-8'))
        evidence=Path(os.environ['AUTOFORGE_HOST_WITNESS_DIR']).resolve() if os.environ.get('AUTOFORGE_HOST_WITNESS_DIR') else None
        if evidence: evidence.mkdir(parents=True,exist_ok=True)
        if before: fail('an existing Windows Sandbox is active')
        sandbox_process=subprocess.Popen([str(sandbox),str(wsb)],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True)
        hwnd=witness._wait_window(before,min(90,req['timeout_seconds']+45))
        witness._focus_window(hwnd)
        evidence=Path(os.environ['AUTOFORGE_HOST_WITNESS_DIR']).resolve() if os.environ.get('AUTOFORGE_HOST_WITNESS_DIR') else None
        if evidence: evidence.mkdir(parents=True,exist_ok=True)
        witness._wait_ui(hwnd,req['target'],min(90,req['timeout_seconds']+45),temp,evidence)
        results=[witness._run_scenario(hwnd,s,temp,evidence,i) for i,s in enumerate(req['scenarios'])]
        (temp/'stop').write_text('stop\n',encoding='utf-8'); deadline=time.monotonic()+30
        while time.monotonic()<deadline and witness.win32gui.IsWindow(hwnd): time.sleep(.3)
        cleanup=not witness.win32gui.IsWindow(hwnd)
        if not cleanup:
            try: witness.win32gui.PostMessage(hwnd,witness.win32con.WM_CLOSE,0,0)
            except Exception: pass
            time.sleep(2); cleanup=not witness.win32gui.IsWindow(hwnd)
        if not cleanup: fail('Sandbox cleanup not confirmed')
        report={'schema':1,'run_id':req['run_id'],'target':req['target'],'source_sha256':req['source_sha256'],'artifact_sha256':None,'build':'SANDBOX_CSC','install':True,'launch':True,'sandbox':True,'cleanup':True,'scenarios':results,'limitations':['source_sha256 is the exact source file byte digest, not a workspace manifest digest.','Source was read-only mapped; compilation and executable ran inside Windows Sandbox with networking disabled.','Guest report and automation results were ignored; host screenshots, OCR and input produced assertions.','OCR proves visible UI only and cannot rule out deliberate visual spoofing.'],'evidence_trust':'host_screenshot_ocr_witness'}
        sys.stdout.buffer.write(witness.canonical(report)+b'\n')
    finally:
        if evidence and (temp/'uia-raw-tree.txt').is_file():
            try: shutil.copy2(temp/'uia-raw-tree.txt',evidence/'uia-raw-tree.txt')
            except OSError: pass
        if evidence and (temp/'guest-startup.txt').is_file():
            try: shutil.copy2(temp/'guest-startup.txt',evidence/'guest-startup.txt')
            except OSError: pass
        if 'sandbox_process' in locals() and sandbox_process.poll() is None:
            sandbox_process.terminate()
            try: sandbox_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                sandbox_process.kill()
                sandbox_process.wait(timeout=5)
        if hwnd is not None:
            try: (temp/'stop').write_text('stop\n',encoding='utf-8')
            except OSError: pass
            for _ in range(20):
                if not witness.win32gui.IsWindow(hwnd): break
                time.sleep(.2)
            if witness.win32gui.IsWindow(hwnd):
                try: witness.win32gui.PostMessage(hwnd,witness.win32con.WM_CLOSE,0,0)
                except Exception: pass
                time.sleep(1)
            if witness.win32gui.IsWindow(hwnd):
                try:
                    _thread_id,pid=witness.win32process.GetWindowThreadProcessId(hwnd)
                    process=witness.win32api.OpenProcess(witness.win32con.PROCESS_TERMINATE,False,pid)
                    witness.win32api.TerminateProcess(process,0)
                except Exception: pass
                time.sleep(1)
            if witness.win32gui.IsWindow(hwnd): fail('Sandbox host window/process could not be terminated; retaining mapped temp directory')
        shutil.rmtree(temp,ignore_errors=True)
if __name__=='__main__':
    try: main()
    except Exception as exc: sys.stderr.write(str(exc)[:2048]+'\n'); raise SystemExit(2)
