#!/usr/bin/env python3
"""Run bounded Playwright acceptance against a service attested by the trusted service adapter."""
from __future__ import annotations
import argparse, hashlib, importlib.util, json, os, re, shutil, sys, time
from pathlib import Path
from urllib.parse import urlsplit

def discover_browser_executable():
    candidates=[]
    for name in ('chrome','msedge'):
        found=shutil.which(name) or shutil.which(name+'.exe')
        if found: candidates.append(Path(found))
    roots=(os.environ.get('PROGRAMFILES'),os.environ.get('PROGRAMFILES(X86)'),os.environ.get('LOCALAPPDATA'))
    for root in roots:
        if not root: continue
        base=Path(root)
        candidates.extend((base/'Google/Chrome/Application/chrome.exe',base/'Microsoft/Edge/Application/msedge.exe'))
    for candidate in candidates:
        try:
            if candidate.is_file(): return str(candidate.resolve())
        except OSError: continue
    return None

MAX_STEPS=80
MAX_SHOTS=8
MAX_BYTES=4*1024*1024
class BrowserRefused(ValueError): pass

def validate_attestation(value, origin, digest, *, authorized):
    if authorized is not True: raise BrowserRefused('browser acceptance requires explicit operator authorization')
    if not isinstance(value,dict) or set(value)!={'schema','status','origin','candidate_sha256','service_id','adapter_verified'} or type(value['schema']) is not int or value['schema']!=1 or value['status']!='READY' or value['adapter_verified'] is not True: raise BrowserRefused('service adapter must provide verified READY schema 1 attestation')
    if value['origin']!=origin or value['candidate_sha256']!=digest: raise BrowserRefused('service attestation origin or candidate digest mismatch')
    if not isinstance(value['service_id'],str) or not value['service_id'] or len(value['service_id'])>128: raise BrowserRefused('service_id invalid')
    return value

def _origin(value):
    p=urlsplit(value)
    if p.scheme!='http' or p.hostname!='127.0.0.1' or p.port is None or p.username or p.password or p.path not in ('','/') or p.query or p.fragment: raise BrowserRefused('origin must be explicit http://127.0.0.1:<port> only')
    return f'http://127.0.0.1:{p.port}'

def _contract(raw):
    if not isinstance(raw,dict) or set(raw)!={'schema','start_path','steps'} or type(raw['schema']) is not int or raw['schema']!=1: raise BrowserRefused('browser contract must contain schema 1, start_path, steps')
    path=raw['start_path']
    if not isinstance(path,str) or not path.startswith('/') or path.startswith('//') or any(c in path for c in '\r\n') or len(path)>2048: raise BrowserRefused('invalid start_path')
    steps=raw['steps']
    if not isinstance(steps,list) or not steps or len(steps)>MAX_STEPS: raise BrowserRefused(f'steps must contain 1..{MAX_STEPS} actions')
    shots=0
    for s in steps:
        if not isinstance(s,dict) or s.get('action') not in {'click','fill','select','check','press','focus','wait','assert_text','assert_form','assert_error','assert_error_text','assert_label','assert_focused','assert_enabled','assert_status','assert_empty','assert_list','assert_count','assert_absent','assert_role','reload','screenshot'}: raise BrowserRefused('unknown or malformed browser action')
        action=s['action']; allowed={'click':{'action','selector'},'fill':{'action','selector','value'},'select':{'action','selector','value'},'check':{'action','selector','value'},'press':{'action','selector','value'},'focus':{'action','selector'},'wait':{'action','ms'},'assert_text':{'action','selector','text'},'assert_form':{'action','selector','value'},'assert_error':{'action','selector','text'},'assert_error_text':{'action','selector','text'},'assert_label':{'action','name'},'assert_focused':{'action','selector'},'assert_enabled':{'action','selector'},'assert_status':{'action','role','text'},'assert_empty':{'action','selector'},'assert_list':{'action','selector','count'},'assert_count':{'action','selector','count'},'assert_absent':{'action','selector'},'assert_role':{'action','role','name'},'reload':{'action'},'screenshot':{'action','name'}}[action]
        if set(s)!=allowed: raise BrowserRefused(f'invalid fields for {action}')
        if 'selector' in s and (not isinstance(s['selector'],str) or not s['selector'] or len(s['selector'])>512): raise BrowserRefused('selector required and limited to 512 chars')
        for k in ('value','text','name'):
            if k not in s: continue
            if k=='value' and action=='check':
                if type(s[k]) is not bool: raise BrowserRefused('check value must be boolean')
            elif not isinstance(s[k],str) or len(s[k])>2048: raise BrowserRefused(f'{k} must be bounded text')
        if action in {'assert_role','assert_status'} and (not isinstance(s['role'],str) or s['role'] not in {'alert','button','checkbox','combobox','form','heading','link','list','listitem','main','navigation','option','radio','status','textbox'}): raise BrowserRefused('unsupported accessible role')
        if action=='assert_status' and s['role'] not in {'alert','status'}: raise BrowserRefused('status announcement role must be alert or status')
        if action=='assert_role' and not isinstance(s['name'],str): raise BrowserRefused('role name must be text')
        if action=='press' and s['value'] not in {'Enter','Tab','Shift+Tab','Escape','Space','ArrowUp','ArrowDown','ArrowLeft','ArrowRight'}: raise BrowserRefused('unsupported keyboard key')
        if action=='wait' and (type(s['ms']) is not int or not 0<=s['ms']<=3000): raise BrowserRefused('wait ms must be 0..3000')
        if action=='assert_list' and (type(s['count']) is not int or not 0<=s['count']<=10000): raise BrowserRefused('list count invalid')
        if action in {'assert_count','assert_list'} and (type(s['count']) is not int or not 0<=s['count']<=10000): raise BrowserRefused('list count invalid')
        if action=='check' and type(s['value']) is not bool: raise BrowserRefused('check value must be boolean')
        if action=='screenshot':
            shots+=1
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',s['name']): raise BrowserRefused('screenshot name invalid')
    if shots>MAX_SHOTS: raise BrowserRefused(f'at most {MAX_SHOTS} screenshots allowed')
    return steps

def run(origin, digest, attestation, contract, out_dir, *, timeout=60, authorized=False, browser_executable=None, browser_engine='chromium'):
    origin=_origin(origin)
    if not isinstance(digest,str) or not re.fullmatch(r'[0-9a-f]{64}',digest): raise BrowserRefused('candidate digest must be lowercase SHA-256')
    validate_attestation(attestation,origin,digest,authorized=authorized)
    steps=_contract(contract)
    if importlib.util.find_spec('playwright') is None: raise BrowserRefused('BLOCKED: Playwright is not installed')
    if type(timeout) is not int or not 5<=timeout<=120: raise BrowserRefused('timeout must be 5..120 seconds')
    if browser_engine not in {'chromium','firefox','webkit'}: raise BrowserRefused('browser_engine must be chromium, firefox, or webkit')
    if browser_executable is not None:
        executable_path=Path(browser_executable).resolve()
        expected={'chromium':{'chrome.exe','msedge.exe'},'firefox':{'firefox.exe'},'webkit':{'playwright.exe'}}[browser_engine]
        if executable_path.name.lower() not in expected or not executable_path.is_file(): raise BrowserRefused('browser executable does not match the selected Playwright engine')
        browser_executable=str(executable_path)
    try:
        from playwright.sync_api import Error as PlaywrightError
    except ImportError: PlaywrightError=RuntimeError
    from playwright.sync_api import sync_playwright, expect
    dest=Path(out_dir).resolve(); dest.mkdir(parents=True,exist_ok=True)
    artifacts=[]; start=time.monotonic(); deadline=start+timeout
    browser=None
    try:
        with sync_playwright() as p:
            engine=p.chromium if browser_engine=='chromium' else p.firefox if browser_engine=='firefox' else p.webkit
            executable=browser_executable or (discover_browser_executable() if browser_engine=='chromium' else None)
            engine_name='msedge' if executable and Path(executable).name.lower()=='msedge.exe' else browser_engine
            try:
                launch={'headless':True}
                if executable: launch['executable_path']=executable
                browser=engine.launch(**launch)
            except PlaywrightError as e: raise BrowserRefused(f'BLOCKED: no usable {browser_engine} browser; no browsers are downloaded automatically: {e}') from e
            context=browser.new_context(accept_downloads=False,service_workers='block'); blocked=[]
            page=context.new_page(); page.set_default_timeout(min(5000,max(1,int((deadline-time.monotonic())*1000))))
            failures=[]
            response_trace=[]
            request_trace=[]
            def record_request(request):
                if len(request_trace)<32:
                    request_trace.append({'method':request.method,'path':urlsplit(request.url).path[:256]})
            def record_response(response):
                if len(response_trace)<24:
                    response_trace.append({'method':response.request.method,'status':response.status,'path':urlsplit(response.url).path[:256]})
                if response.status>=400 and len(failures)<16:
                    failures.append({'kind':'http','status':response.status,'path':urlsplit(response.url).path[:512]})
            def record_page_error(error):
                if len(failures)<16:
                    failures.append({'kind':'javascript','error':str(error)[:512]})
            page.on('request',record_request)
            page.on('response',record_response)
            page.on('pageerror',record_page_error)
            def guard(route, request):
                u=urlsplit(request.url)
                if u.scheme not in ('http','https','data','blob','about') or (u.scheme in ('http','https') and (u.hostname!='127.0.0.1' or u.port!=urlsplit(origin).port)):
                    blocked.append(request.url[:512]); route.abort()
                else: route.continue_()
            context.route('**/*',guard)
            context.on('page',lambda popup: popup.close())
            page.route('**/*',guard)
            def goto(path):
                u=urlsplit(origin+path)
                if u.scheme!='http' or u.hostname!=urlsplit(origin).hostname or u.port!=urlsplit(origin).port: raise BrowserRefused('navigation escaped attested service origin')
                page.goto(origin+path,wait_until='domcontentloaded',timeout=max(1,int((deadline-time.monotonic())*1000)))
            goto(contract['start_path'])
            observations=[]
            for n,s in enumerate(steps):
                if time.monotonic()>=deadline: raise BrowserRefused('browser acceptance timed out')
                a=s['action']; loc=page.locator(s['selector']) if 'selector' in s else None
                if a=='click': loc.click()
                elif a=='focus': loc.focus()
                elif a=='fill': loc.fill(s['value'])
                elif a=='select': loc.select_option(s['value'])
                elif a=='check': loc.set_checked(s['value'])
                elif a=='press': loc.press(s['value'])
                elif a=='wait': page.wait_for_timeout(s['ms'])
                elif a=='assert_label': expect(page.get_by_label(s['name'],exact=True)).to_be_visible()
                elif a=='assert_disabled': expect(loc).to_be_disabled()
                elif a=='assert_enabled': expect(loc).to_be_enabled()
                elif a=='assert_status': expect(page.get_by_role(s['role'])).to_contain_text(s['text'])
                elif a in ('assert_text','assert_error','assert_error_text'):
                    expect(loc).to_be_visible()
                    expect(loc).to_contain_text(s['text'])
                elif a=='assert_form': expect(loc).to_have_value(s['value'])
                elif a=='assert_empty':
                    expect(loc).to_be_visible()
                    expect(loc).to_have_text(re.compile(r'^\s*$'))
                    expect(loc.locator('*')).to_have_count(0)
                elif a=='assert_list':
                    expect(loc).to_be_visible()
                    expect(loc.locator(':scope > *')).to_have_count(s['count'])
                elif a=='assert_count': expect(loc).to_have_count(s['count'])
                elif a=='assert_absent': expect(loc).to_have_count(0)
                elif a=='assert_role': expect(page.get_by_role(s['role'],name=s['name'],exact=True)).to_be_visible()
                elif a=='reload': page.reload(wait_until='domcontentloaded')
                elif a=='screenshot':
                    path=dest/(s['name']+'.png')
                    if path.exists(): raise BrowserRefused('refusing to overwrite screenshot artifact')
                    page.screenshot(path=str(path),full_page=True,timeout=max(1,min(5000,int((deadline-time.monotonic())*1000))))
                    size=path.stat().st_size
                    if size<=0 or size>MAX_BYTES: path.unlink(missing_ok=True); raise BrowserRefused('screenshot exceeds artifact size bound')
                    artifacts.append({'name':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'size':size})
                observations.append({'step':n,'action':a,'pass':True})
            if blocked: raise BrowserRefused('external or non-loopback resource request attempted')
            browser_version=browser.version
            context.close(); browser.close(); browser=None
        return {'schema':1,'status':'PASS','candidate_sha256':digest,'service_id':attestation['service_id'],'origin':origin,'browser_engine':engine_name,'browser_brand':('Microsoft Edge' if engine_name=='msedge' else 'Google Chrome' if browser_engine=='chromium' and executable else None),'browser_version':browser_version,'observations':observations,'artifacts':artifacts,'duration_ms':round((time.monotonic()-start)*1000),'limitations':['Declarative browser interactions/assertions only; no arbitrary JavaScript evaluation.','HTTP(S) resources are restricted to loopback at the attested port.','Only the selected browser engine is measured; no cross-engine claim unless separate runs are supplied.','Adapter attestation identity is provided in-process by the trusted service controller; it is not independently cryptographically authenticated.']}
    except Exception as e:
        if isinstance(e,BrowserRefused): raise
        diagnostics=json.dumps({'failures':failures,'requests':request_trace,'responses':response_trace},ensure_ascii=False,separators=(',',':')) if 'failures' in locals() else '{}'
        raise BrowserRefused(f'browser acceptance failed: {type(e).__name__}: {e}; browser diagnostics: {diagnostics}') from e
    finally:
        if browser is not None:
            try: browser.close()
            except Exception: pass

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--origin',required=True); p.add_argument('--candidate-sha256',required=True); p.add_argument('--attestation',type=Path,required=True); p.add_argument('--contract',type=Path,required=True); p.add_argument('--artifacts',type=Path,required=True); p.add_argument('--timeout',type=int,default=60); p.add_argument('--authorized',action='store_true',help='explicit operator authorization for this live candidate'); p.add_argument('--browser-engine',choices=('chromium','firefox','webkit'),default='chromium'); p.add_argument('--browser-executable',help='optional installed engine executable; no browser profile is reused'); a=p.parse_args(argv)
    try:
        result=run(a.origin,a.candidate_sha256,json.loads(a.attestation.read_text(encoding='utf-8')),json.loads(a.contract.read_text(encoding='utf-8')),a.artifacts,timeout=a.timeout,authorized=a.authorized,browser_executable=a.browser_executable,browser_engine=a.browser_engine); print(json.dumps(result,ensure_ascii=False)); return 0
    except (OSError,ValueError,json.JSONDecodeError) as e:
        print(json.dumps({'schema':1,'status':'BLOCKED' if 'BLOCKED' in str(e) else 'FAIL','reason':str(e)},ensure_ascii=False),file=sys.stderr); return 2
if __name__=='__main__': raise SystemExit(main())