import copy
import json
from pathlib import Path
import unittest
from trace_api_chain.replay import rebuild, canonical, EvidenceError

import trace_api_chain
CONTRACT = json.loads(Path(trace_api_chain.__file__).with_name('contract.json').read_text())

def fixture():
    events = [
        {'event':'start','pid':7,'scope_policy':CONTRACT['version'],'api_catalog':['fixture:api']},
        {'event':'call','uid':1,'parent':None,'kind':'native_boundary','tid':9,'time_ns':10,'api_id':'fixture:api','name':'anything'},
        {'event':'object','id':'o1','kind':'instance'},
        {'event':'references','uid':1,'scope':'native','added':[[None,'root:callable','o1']],'removed':[]},
        {'event':'return','uid':1,'time_ns':100},
        {'event':'end','gaps':[]}]
    native = [dict(py_uid=1,pid=7,tid=9,time=t,event=e,absolute_function=f,
                   absolute_caller=300,path='/fixture.so',offset=f)
              for t,e,f in [(20,'enter',100),(30,'enter',200),(40,'exit',200),(50,'exit',100)]]
    return events, native

class ReplayTests(unittest.TestCase):
    def reject(self, events, native, code):
        with self.assertRaisesRegex(EvidenceError, '^'+code+'$'):
            rebuild(events,native,CONTRACT)

    def test_replay_identical_and_objects_preserved(self):
        e,n=fixture(); a=rebuild(e,n,CONTRACT)
        self.assertEqual(canonical(a),canonical(rebuild(copy.deepcopy(e),copy.deepcopy(n),CONTRACT)))
        self.assertEqual(len(a['nodes']),3)
        self.assertEqual(a['reference_observations'][0]['observation'],e[3])
        self.assertEqual(a['object_observations'][0]['object'],e[2])
        self.assertEqual(a['edges'],[{'from':'p:1','to':'n:0','kind':'window_contains'},
                                     {'from':'n:0','to':'n:1','kind':'instrumented_nesting'}])

    def test_labels_and_payload_do_not_change_topology(self):
        e,n=fixture(); a=rebuild(e,n,CONTRACT)
        e[1]['name']='torch.compile';e[1]['source']='arbitrary text';e[2]['value']='different'
        b=rebuild(e,n,CONTRACT)
        self.assertEqual(a['edges'],b['edges']);self.assertEqual(set(a['nodes']),set(b['nodes']))

    def test_missing_native_enter(self):
        e,n=fixture();self.reject(e,n[1:],'native_exit_without_enter')

    def test_missing_native_exit(self):
        e,n=fixture();self.reject(e,n[:-1],'native_unclosed_stack')

    def test_wrong_native_thread(self):
        e,n=fixture();n[0]['tid']=10;self.reject(e,n,'native_thread_mismatch')

    def test_wrong_native_pid(self):
        e,n=fixture();n[0]['pid']=8;self.reject(e,n,'native_pid_mismatch')

    def test_wrong_native_window(self):
        e,n=fixture();n[0]['py_uid']=2;self.reject(e,n,'native_window_missing')

    def test_outside_native_window(self):
        e,n=fixture();n[0]['time']=101;self.reject(e,n,'native_event_outside_window')

    def test_wrong_python_parent(self):
        e,n=fixture();e[1]['parent']=55;self.reject(e,n,'python_parent_stack_mismatch')

    def test_unknown_object(self):
        e,n=fixture();e[3]['added'][0][2]='missing';self.reject(e,n,'unknown_object_reference')

    def test_missing_python_return(self):
        e,n=fixture();del e[4];self.reject(e,n,'python_unclosed_stack')

    def test_compile_identity_rejected_even_under_alias(self):
        e,n=fixture();e[1]['api_id']='python:torch.compile';e[1]['name']='alias'
        self.reject(e,n,'excluded_api_was_captured')

    def test_missing_api_identity_is_explicit_gap(self):
        e,n=fixture();del e[1]['api_id'];a=rebuild(e,n,CONTRACT)
        self.assertEqual(a['status'],'evidence_scope_incomplete')
        self.assertIn('api_root_identity_missing:p:1',a['gaps'])

    def test_uncatalogued_root_rejected(self):
        e,n=fixture();e[1]['api_id']='unknown';self.reject(e,n,'root_api_not_in_catalog')

    def test_events_after_end_rejected(self):
        e,n=fixture();e.append({'event':'object','id':'o2'});self.reject(e,n,'event_after_capture_end')

    def test_receipt_detects_balanced_call_loss(self):
        e,n=fixture();e[-1]['calls']=2;self.reject(e,n,'python_call_count_mismatch')

    def test_python_time_order(self):
        e,n=fixture();e[4]['time_ns']=9;self.reject(e,n,'python_time_order')

if __name__=='__main__':unittest.main()
