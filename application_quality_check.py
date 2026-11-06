"""Exercise A10 quality decision logic with deliberately varied candidate-bound evidence."""
from __future__ import annotations
import hashlib, json
import application_quality as quality

CATS=sorted(quality.CATEGORIES)
def record(digest,c,status='PASS',value=1,unit='observation'):
    return quality.seal_record({'schema':1,'candidate_sha256':digest,'category':c,'status':status,'value':value,'unit':unit,'source':'check-harness:authored-evidence'})
def main():
    digest=hashlib.sha256(b'quality-check-candidate').hexdigest(); policy={'schema':1,'requirements':{c:'required' for c in CATS}}
    passing=[record(digest,c) for c in CATS]
    result=quality.evaluate(digest,policy,passing); assert result['status']=='PASS'
    failed=passing[:-1]+[record(digest,CATS[-1],'FAIL','measured regression')]; assert quality.evaluate(digest,policy,failed)['status']=='HOLD'
    for state in ('FLAKY','UNRESOLVED'):
        arr=passing[:-1]+[record(digest,CATS[-1],state,'intermittent / unresolved')]; assert quality.evaluate(digest,policy,arr)['status']=='HOLD'
    arr=passing[:-1]+[record(digest,CATS[-1],'NOT_APPLICABLE','authorized fixture limitation','reason')]
    try: quality.evaluate(digest,policy,arr); raise AssertionError('unauthorized N/A accepted')
    except quality.QualityRefused: pass
    assert quality.evaluate(digest,policy,arr,authorized_na_reasons=['authorized fixture limitation'])['status']=='PASS'
    tampered=[dict(r) for r in passing]; tampered[0]['value']=False
    try: quality.evaluate(digest,policy,tampered); raise AssertionError('tampered evidence accepted')
    except quality.QualityRefused: pass
    foreign=[record('0'*64,c) for c in CATS]
    try: quality.evaluate(digest,policy,foreign); raise AssertionError('foreign candidate evidence accepted')
    except quality.QualityRefused: pass
    missing=quality.evaluate(digest,policy,passing[:-1]); assert missing['status']=='HOLD' and missing['outcomes'][CATS[-1]]=='UNRESOLVED'
    anti_missing=quality.evaluate(digest,policy,[r for r in passing if r['category']!='anti_cheat'])
    assert anti_missing['status']=='HOLD' and anti_missing['outcomes']['anti_cheat']=='UNRESOLVED'
    import application_builder as builder
    original=(builder.preflight,builder._manifest,builder._contract,builder._container)
    import application_quality_resources as resource_runner
    original_runner=resource_runner._container_with_resources
    try:
        resource_runner._container_with_resources=lambda *a,**k:(*builder._container(a[0],a[1],a[2],a[3],a[4]),{'schema':1,'peak_memory_bytes':100,'cpu_usage_usec':100,'user_cpu_usec':50,'system_cpu_usec':50,'source':'test'})
        builder.preflight=lambda engine,image:{'image_id':image}
        builder._manifest=lambda manifest:(__import__('pathlib').Path('/staged-candidate'),digest)
        contract={'schema':1,'entrypoint':'main.py','scenarios':[{'name':'case','requests':[{'x':1}],'expected':[{'ok':True}]},{'name':'auth-negative','requests':[{'x':2}],'expected':[{'error':'unauthorized'}]}]}
        scenarios=contract['scenarios']
        thresholds={'schema':1,'repeats':2,'max_latency_ms':1000,'max_output_bytes':1000,'max_peak_memory_bytes':256*1024*1024,'max_cpu_usage_usec':60_000_000,'na':{'accessibility':'CLI only','compatibility':'CLI only'},'security_scenarios':['auth-negative']}
        manifest={}
        builder._container=lambda *args:(b'{"ok":true}\n',b'',0,False)
        wrong=quality.measure(manifest,contract,thresholds,engine='docker',image='sha256:'+'1'*64,authorized_na_reasons=['CLI only'])
        assert wrong['outcomes']['resource']=='PASS' and wrong['status']=='HOLD'
        def scoped_ok(*args):
            payload=args[3]
            lines=payload.decode().splitlines()
            outputs=[{'ok':True} if json.loads(row)=={'x':1} else {'error':'unauthorized'} for row in lines]
            return ((''.join(json.dumps(x)+'\n' for x in outputs)).encode(),b'',0,False)
        builder._container=scoped_ok
        scoped=quality.measure(manifest,contract,thresholds,engine='docker',image='sha256:'+'1'*64,authorized_na_reasons=['CLI only'])
        assert scoped['outcomes']['security']=='PASS' and scoped['outcomes']['anti_cheat']=='UNRESOLVED'
        inventory={'schema':1,'entrypoint':'main.py','scenarios':[{'name':'original-inventory','requests':[{'op':'add','sku':'widget-1','quantity':8},{'op':'list'}],'expected':[{'ok':True,'sku':'widget-1','quantity':8},{'ok':True,'items':[{'sku':'widget-1','quantity':8}]}]}]}
        anti_thresholds={**thresholds,'anti_cheat':{'schema':1,'category':'inventory-jsonl','seed':24681357,'cases':2}}
        def inventory_impl(*args):
            state={}; out=[]
            for raw in args[3].decode().splitlines():
                request=json.loads(raw)
                if request['op']=='add':
                    state[request['sku']]=request['quantity']; out.append({'ok':True,'sku':request['sku'],'quantity':request['quantity']})
                else: out.append({'ok':True,'items':[{'sku':key,'quantity':state[key]} for key in sorted(state)]})
            return ((''.join(json.dumps(item)+'\n' for item in out)).encode(),b'',0,False)
        builder._container=inventory_impl
        valid_inventory=quality.measure(manifest,inventory,anti_thresholds,engine='docker',image='sha256:'+'1'*64,authorized_na_reasons=['CLI only'])
        assert valid_inventory['outcomes']['anti_cheat']=='PASS'
        canned=[{'ok':True,'sku':'widget-1','quantity':8},{'ok':True,'items':[{'sku':'widget-1','quantity':8}]}]
        builder._container=lambda *args:((''.join(json.dumps(item)+'\n' for item in canned)).encode(),b'',0,False)
        hardcoded=quality.measure(manifest,inventory,anti_thresholds,engine='docker',image='sha256:'+'1'*64,authorized_na_reasons=['CLI only'])
        assert hardcoded['outcomes']['anti_cheat']=='FAIL'
        assert hardcoded['measurements']['anti_cheat_seed_sha256'] and hardcoded['measurements']['anti_cheat_policy_sha256']
        count=[0]
        def flaky(*args):
            count[0]+=1
            return (b'{"ok":true}\n' if count[0]<=2 else b'{"ok":false}\n',b'',0,False)
        builder._container=flaky
        unstable=quality.measure(manifest,contract,thresholds,engine='docker',image='sha256:'+'1'*64,authorized_na_reasons=['CLI only'])
        assert unstable['status']=='HOLD' and unstable['outcomes']['data_invariant']=='FLAKY'
        builder._container=lambda *args:(b'{"ok":true}\n',b'',0,False)
        bounded=dict(thresholds,max_latency_ms=0.000001)
        from unittest.mock import patch
        with patch('time.monotonic',side_effect=[0,0.001,1,1.001,2,2.001,3,3.001,4,4.001,5,5.001]):
            over=quality.measure(manifest,contract,bounded,engine='docker',image='sha256:'+'1'*64,authorized_na_reasons=['CLI only'])
        assert over['status']=='HOLD' and over['outcomes']['latency']=='FAIL'
        numeric=quality.measure(manifest,contract,thresholds,engine='docker',image='sha256:'+'1'*64,authorized_na_reasons=['CLI only'])
        assert numeric['outcomes']['resource']=='PASS'
        resource_runner._container_with_resources=lambda *a,**k:(b'{"ok":true}\n',b'',0,False,{'schema':1,'peak_memory_bytes':thresholds['max_peak_memory_bytes']+1,'cpu_usage_usec':100,'user_cpu_usec':50,'system_cpu_usec':50})
        resource_over=quality.measure(manifest,contract,thresholds,engine='docker',image='sha256:'+'1'*64,authorized_na_reasons=['CLI only'])
        assert resource_over['outcomes']['resource']=='FAIL' and resource_over['status']=='HOLD'
        def unavailable(*a,**k): raise resource_runner.ResourceProbeUnavailable('injected missing cgroup metric')
        resource_runner._container_with_resources=unavailable
        try: quality.measure(manifest,contract,thresholds,engine='docker',image='sha256:'+'1'*64,authorized_na_reasons=['CLI only'])
        except quality.QualityRefused: pass
        else: raise AssertionError('unavailable metrics passed')
        resource_runner._container_with_resources=lambda *a,**k:(b'{"ok":true}\n',b'',0,False,{'schema':1,'peak_memory_bytes':100,'cpu_usage_usec':thresholds['max_cpu_usage_usec']+1,'user_cpu_usec':50,'system_cpu_usec':50})
        cpu_over=quality.measure(manifest,contract,thresholds,engine='docker',image='sha256:'+'1'*64,authorized_na_reasons=['CLI only'])
        assert cpu_over['outcomes']['resource']=='FAIL'
        resource_runner._container_with_resources=lambda *a,**k:(*builder._container(a[0],a[1],a[2],a[3],a[4]),{'schema':1,'peak_memory_bytes':100,'cpu_usage_usec':100,'user_cpu_usec':50,'system_cpu_usec':50})
        assert numeric['outcomes']['data_invariant']=='FAIL'
        security_flaky_count=[0]
        flaky_contract={'schema':1,'entrypoint':'main.py','scenarios':[{'name':'auth-negative','requests':[{'x':2}],'expected':[{'error':'unauthorized'}]}]}
        flaky_thresholds={**thresholds,'security_scenarios':['auth-negative']}
        def flaky_security(*args):
            security_flaky_count[0]+=1
            body=b'{"error":"unauthorized"}\n' if security_flaky_count[0] % 2 else b'{"ok":true}\n'
            return (body,b'',0,False)
        builder._container=flaky_security
        resource_runner._container_with_resources=lambda *a,**k:(*builder._container(a[0],a[1],a[2],a[3],a[4]),{'schema':1,'peak_memory_bytes':100,'cpu_usage_usec':100,'user_cpu_usec':50,'system_cpu_usec':50})
        flaky_security_result=quality.measure(manifest,flaky_contract,flaky_thresholds,engine='docker',image='sha256:'+'1'*64,authorized_na_reasons=['CLI only'])
        assert flaky_security_result['outcomes']['security']=='FLAKY' and flaky_security_result['status']=='HOLD'
        attack_contract={'schema':1,'entrypoint':'main.py','scenarios':[{'name':'sql-injection','requests':[{'op':'query','value':'\' OR 1=1 --'}],'expected':[{'error':'invalid input'}]},{'name':'valid','requests':[{'x':1}],'expected':[{'ok':True}]}]}
        builder._container=lambda *args:(b'{"rows":[{"admin":true}]}\n',b'',0,False)
        attack_result=quality.measure(manifest,attack_contract,{**thresholds,'security_scenarios':['sql-injection']},engine='docker',image='sha256:'+'1'*64,authorized_na_reasons=['CLI only'])
        assert attack_result['outcomes']['security']=='FAIL' and attack_result['outcomes']['data_invariant']=='FAIL'
        for invalid in (float('nan'),float('inf')):
            try: quality.measure(manifest,contract,dict(thresholds,max_latency_ms=invalid),engine='docker',image='sha256:'+'1'*64)
            except quality.QualityRefused: pass
            else: raise AssertionError('nonfinite latency bound accepted')
        manifest_calls=[0]
        def changing_manifest(_manifest):
            manifest_calls[0]+=1
            return (__import__('pathlib').Path('/staged-candidate'),digest if manifest_calls[0]<5 else '0'*64)
        builder._manifest=changing_manifest
        try: quality.measure(manifest,contract,thresholds,engine='docker',image='sha256:'+'1'*64,authorized_na_reasons=['CLI only'])
        except quality.QualityRefused: pass
    finally:
        builder.preflight,builder._manifest,builder._contract,builder._container=original
        resource_runner._container_with_resources=original_runner
    print(json.dumps({'schema':1,'status':'PASS','scope_limitations':['security covers exact explicitly named JSONL cases only','anti_cheat covers only operator-seeded hidden inventory add/list traces; no universal claim','browser checks cover generated-page interactions only with Chromium','CPU and peak cgroup memory include runner overhead'],'checks':['candidate-bound evidence integrity','security named negative and positive cases','anti-cheat distinguishes dynamic inventory behavior from canned original outputs','resource gate uses explicit CPU and peak-memory bounds','repeat instability and performance/output bounds','data invariant failures']},ensure_ascii=False)); return 0
if __name__=='__main__': raise SystemExit(main())