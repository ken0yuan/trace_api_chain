import importlib.util,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('guard',Path(__file__).with_name('capacity_guard.py'));g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)

class CapacityGuardTests(unittest.TestCase):
 def test_below_threshold_requests_pause(self):self.assertEqual(g.decision(1023.9,[10],False,False,None,[]),'pause')
 def test_at_threshold_does_not_pause(self):self.assertEqual(g.decision(1024,[10],False,False,None,[]),'observing')
 def test_owned_pause_waits_controller(self):self.assertEqual(g.decision(1400,[10],True,True,None,[]),'draining')
 def test_owned_pause_waits_child(self):self.assertEqual(g.decision(1400,[],True,True,None,[11]),'draining')
 def test_hysteresis_waits(self):self.assertEqual(g.decision(1279.9,[],True,True,None,[]),'waiting_capacity')
 def test_resume_only_owned_drained(self):self.assertEqual(g.decision(1280,[],True,True,None,[]),'resume')
 def test_unowned_stopped_task_not_resumed(self):self.assertEqual(g.decision(1500,[],False,True,None,[]),'external_stop_respected')
 def test_fault_prevents_restart(self):self.assertEqual(g.decision(1500,[],True,True,'pipeline_fault',[]),'blocked_pipeline_fault')
 def test_two_controllers_block(self):self.assertEqual(g.decision(900,[1,2],False,False,None,[]),'blocked_duplicate_controllers')
 def test_no_controller_no_new_start(self):self.assertEqual(g.decision(1500,[],False,False,None,[]),'observing')
 def test_exclusive_marker_and_archive(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'STOP';a=Path(d)/'archive';g.create_marker(p,'one')
   with self.assertRaises(FileExistsError):g.create_marker(p,'two')
   self.assertFalse(g.marker_matches(p,'two'));g.release_marker(p,'one',a);self.assertFalse(p.exists());self.assertTrue(g.marker_matches(a,'one'))
 def test_external_marker_never_removed(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'STOP';p.write_text('manual stop')
   with self.assertRaises(ValueError):g.release_marker(p,'one',Path(d)/'archive')
   self.assertEqual(p.read_text(),'manual stop')
 def test_symlink_marker_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'real';g.create_marker(p,'one');s=Path(d)/'STOP';s.symlink_to(p);self.assertFalse(g.marker_matches(s,'one'))
 def test_source_and_environment_tamper_blocks(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);env=root/'env';env.write_text('{}');source=root/'driver';source.write_text('original');info={'trace_commit':'a','api_commit':'b','runtime':'c'};g.atomic(root/'process.json',info)
   recipe={'process':info,'environment_file':str(env),'environment_sha256':g.digest(env),'sha256':{str(source):g.digest(source)}};g.verify_recipe(recipe,root);source.write_text('changed')
   with self.assertRaises(ValueError):g.verify_recipe(recipe,root)
 def test_changed_version_blocks(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);g.atomic(root/'process.json',{'trace_commit':'new'})
   with self.assertRaises(ValueError):g.verify_recipe({'process':{'trace_commit':'old'}},root)
 def test_real_tick_fault_leaves_stop_and_process_intact(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'results').mkdir();g.atomic(root/'results/a.json',{'id':'a','pipeline_error':'SIGKILL'});g.atomic(root/'process.json',{'pid':10});g.atomic(root/'recipe.json',{'root':d,'driver':d+'/driver'});guard=g.Guard(root/'recipe.json')
   with patch.object(g,'verify_recipe'),patch.object(g,'inventory',return_value=([],[])):guard.tick()
   self.assertEqual(guard.state['phase'],'blocked_pipeline_fault');self.assertFalse((root/'STOP').exists());self.assertEqual(g.read(root/'process.json'),{'pid':10})
 def test_pause_does_not_signal_or_start_process(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'results').mkdir();g.atomic(root/'process.json',{'pid':10});g.atomic(root/'recipe.json',{'root':d,'driver':d+'/driver'});guard=g.Guard(root/'recipe.json')
   with patch.object(g,'verify_recipe'),patch.object(g,'inventory',return_value=([10],[11])),patch.object(g.shutil,'disk_usage',return_value=type('Usage',(),{'free':900*1024**3})()),patch.object(g.subprocess,'Popen') as start,patch.object(g.os,'kill') as kill:guard.tick()
   self.assertEqual(guard.state['phase'],'draining');start.assert_not_called();kill.assert_not_called();self.assertTrue(g.marker_matches(root/'STOP',guard.state['pause']['token']))
 def test_closed_child_gate_cannot_execute_driver(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);g.atomic(root/'recipe.json',{'root':d})
   import io
   with patch.object(g,'verify_recipe'),patch.object(g.sys,'stdin',type('Input',(),{'buffer':io.BytesIO(b'')})()),patch.object(g.os,'execve') as execute:
    with self.assertRaises(SystemExit):g.child(root/'recipe.json','token',0)
   execute.assert_not_called()

if __name__=='__main__':unittest.main()
