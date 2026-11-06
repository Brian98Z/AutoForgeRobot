#!/usr/bin/env python3
"""Fast deterministic regressions for independent benchmark semantics and generators."""
import json
import benchmark_independent_check as check

def main():
    items=[{'id':'a','quantity':4},{'id':'b','quantity':4},{'id':'c','quantity':-2}]
    assert [x['id'] for x in check.expected('inventory-order-001',{'op':'sort','items':items})['items']]==['a','b','c']
    assert check.expected('inventory-order-001',{'op':'sort','items':[]})=={'items':[]}
    assert check.expected('inventory-order-001',{'op':'sort','items':[{'id':'a','quantity':True}]})=={'error':'invalid_input'}
    tied=[{'id':'z','quantity':4},{'id':'a','quantity':4}]
    assert [x['id'] for x in check.expected('inventory-order-001',{'op':'sort','items':tied})['items']]==['z','a']
    assert check.expected('order-validation-001',{'op':'validate_order','quantity':True})=={'accepted':False}
    assert check.expected('order-validation-001',{'op':'validate_order','quantity':1})=={'accepted':True}
    assert check.expected('order-validation-001',{'op':'validate_order','quantity':0})=={'accepted':False}
    assert check.expected('ticket-status-001',{'op':'transition','from':'open','to':'resolved'})=={'accepted':True,'status':'resolved'}
    assert check.expected('ticket-status-001',{'op':'transition','from':'resolved','to':'open'})=={'accepted':False,'status':'resolved'}
    assert check.expected('content-render-001',{'op':'render','title':'a','body':'b'})=={'text':'a\nb'}
    assert check.expected('content-render-001',None)=={'error':'invalid_content'}
    for value in (None,[],"x",False,7):
        assert check.expected('content-render-001',value)=={'error':'invalid_content'}
        assert check.expected('inventory-order-001',value)=={'error':'invalid_input'}
    assert check.expected('ticket-status-001',{'op':'transition','from':'open','to':7})=={'accepted':False}
    assert check.expected('ticket-status-001',{'op':'transition','from':'open'})=={'accepted':False}
    for tid in check.DEFAULT_CANDIDATES:
        first=check.scenarios(tid,9876,100);assert first==check.scenarios(tid,9876,100) and first!=check.scenarios(tid,9877,100)
        assert len([check.expected(tid,x) for x in first])==len(first)
    wrong={'inventory-order-001':({'op':'sort','items':items},{'items':list(reversed(items))}),'order-validation-001':({'op':'validate_order','quantity':0},{'accepted':True}),'ticket-status-001':({'op':'transition','from':'resolved','to':'open'},{'accepted':True,'status':'open'}),'content-render-001':({'op':'render','title':'a','body':'b'},{'text':'ab'})}
    for tid,(request,output) in wrong.items():assert output!=check.expected(tid,request),tid
    print(json.dumps({'status':'PASS','checks':['public-spec evaluators','stable-sort tie/permutation behavior','integer/boolean boundary','ticket transitions','render newline and invalid content','seed repeatability','deliberate wrong output detection'],'scope':'reference semantics and seeded scenario generator; no candidate execution'}))
if __name__=='__main__':main()
