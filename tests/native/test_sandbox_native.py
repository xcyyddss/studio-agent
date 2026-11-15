"""Mandatory real MXC acceptance. Missing native capability FAILS, never skips.

Run from an authorized host: python -B -m unittest discover -s tests/native -v
No model, deployment data, external network, or credential access is used.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from studio_runtime.tool_adapters import LocalToolAdapter, sandbox_diagnostics
from studio_runtime.sandbox_runtime import _installation, SandboxSetupError


class NativeSandboxTests(unittest.TestCase):
    def setUp(self):
        scratch=Path(__file__).resolve().parents[2]/'.validation';scratch.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.run=Path(self.tmp.name)/'run';(self.run/'artifacts').mkdir(parents=True)
        self.adapter=LocalToolAdapter(self.run,enable_sandbox_tests=True)
        self.serial=0

    def execute(self, code, extra=None, timeout=15):
        self.serial+=1
        files={'test_native.py':code,**(extra or {})};selected=[]
        for name,content in files.items():
            source=f'v{self.serial}/'+name
            path=self.run/'artifacts'/source;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(content,encoding='utf-8')
            selected.append({'path':'artifacts/'+source,'destination':name})
        self.request={'id':f'NATIVE.{self.serial}','kind':'sandbox.python_tests','arguments':{'files':selected,'timeout_seconds':timeout}}
        return self.adapter.execute(self.request)

    def test_real_probe_resolves_installer_and_preserves_network_and_filesystem_policy(self):
        d=sandbox_diagnostics(probe=True,cwd=self.run)
        self.assertTrue(d['available'],json.dumps(d,ensure_ascii=False))
        self.assertEqual(d['policy']['file_system_root'],'deny');self.assertIs(d['policy']['network'],False)
        item=d['policy']['installations']['codex']
        self.assertEqual(str(Path(item['requested']).resolve()),item['resolved'])
        self.assertEqual(item['resolved'],d['executable'])

    def test_import_data_read_success_and_exact_id_replay(self):
        r=self.execute('import unittest\nfrom calc import add\nfrom pathlib import Path\nclass T(unittest.TestCase):\n def test_result(self): self.assertEqual(add(7,11),18)\n def test_data(self): self.assertEqual(Path("fixture.txt").read_text(),"fixture")\n',
            {'calc.py':'def add(a,b): return a+b\n','fixture.txt':'fixture'})
        self.assertEqual(r['outcome'],'succeeded',json.dumps(r));self.assertEqual(r['payload']['reported_test_count'],2)
        with patch('studio_runtime.tool_adapters._run_bounded',side_effect=AssertionError('must not repeat')):
            self.assertEqual(self.adapter.execute(self.request),r)

    def test_assertion_failure_has_real_output(self):
        r=self.execute('import unittest\nclass T(unittest.TestCase):\n def test_wrong(self): self.assertEqual(1,2)\n')
        self.assertEqual(r['outcome'],'failed',r);self.assertEqual(r['diagnostic']['code'],'test_failure')
        self.assertTrue(r['diagnostic']['test_code_started']);self.assertIn('AssertionError',r['payload']['stderr'])
        self.assertEqual(r['payload']['reported_summary']['failures'],1)

    def test_missing_dependency_is_test_import_error_not_unavailable_mxc(self):
        r=self.execute('import studio_nonexistent_dependency\n')
        self.assertEqual(r['outcome'],'failed',r);self.assertEqual(r['diagnostic']['code'],'test_failure')
        self.assertTrue(r['payload']['preflight']['available']);self.assertIn('ModuleNotFoundError',r['payload']['stderr'])

    def test_zero_and_all_skipped_are_not_validation_success(self):
        r=self.execute('pass\n');self.assertEqual(r['diagnostic']['code'],'no_tests',r)
        r=self.execute('import unittest\n@unittest.skip("fixture")\nclass T(unittest.TestCase):\n def test_skip(self): pass\n')
        self.assertEqual(r['outcome'],'failed',r);self.assertEqual(r['diagnostic']['code'],'all_skipped')

    def test_timeout_uncertain_and_no_automatic_reexecution(self):
        r=self.execute('import unittest,time\nclass T(unittest.TestCase):\n def test_sleep(self): time.sleep(60)\n',timeout=1)
        self.assertEqual(r['outcome'],'uncertain',r);self.assertEqual(r['diagnostic']['code'],'interrupted')
        self.assertTrue(r['payload']['timeout']);self.assertTrue(r['payload']['termination'])
        with patch('studio_runtime.tool_adapters._run_bounded',side_effect=AssertionError('must not repeat')):
            self.assertEqual(self.adapter.execute(self.request),r)

    def test_early_exit_without_runner_summary_is_uncertain(self):
        r=self.execute('import os\nos._exit(0)\n')
        self.assertEqual(r['outcome'],'uncertain',r);self.assertEqual(r['diagnostic']['code'],'runner_incomplete')

    def test_output_overflow_is_bounded_and_uncertain(self):
        r=self.execute('print("x"*200000,flush=True)\n')
        self.assertEqual(r['outcome'],'uncertain',r);self.assertTrue(r['payload']['output_truncated'])
        self.assertLessEqual(len(r['payload']['stdout'].encode()),65536)

    def test_real_os_isolation_blocks_outside_reads_writes_and_host_network(self):
        outside=Path(self.tmp.name)/'outside.txt';outside.write_text('SYNTHETIC_PRIVATE_SENTINEL',encoding='utf-8')
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(5);self.addCleanup(listener.close)
        port=listener.getsockname()[1]
        host=socket.create_connection(('127.0.0.1',port),timeout=1);host.close();peer,_=listener.accept();peer.close()
        code='''import unittest,os,socket
from pathlib import Path
class Isolation(unittest.TestCase):
 def test_read_outside(self):
  with self.assertRaises(OSError): Path(OUTSIDE).read_text()
 def test_write_outside(self):
  with self.assertRaises(OSError): Path(OUTSIDE).write_text("altered")
 def test_write_workspace(self):
  Path("allowed.txt").write_text("ok")
  self.assertEqual(Path("allowed.txt").read_text(),"ok")
 def test_environment(self): self.assertNotIn("STUDIO_PRIVATE_TEST_SENTINEL",os.environ)
 def test_network(self):
  with self.assertRaises(OSError): socket.create_connection(("127.0.0.1",PORT),timeout=1)
'''.replace('OUTSIDE',repr(str(outside))).replace('PORT',str(port))
        with patch.dict(os.environ,{'STUDIO_PRIVATE_TEST_SENTINEL':'not-for-test-code'}):r=self.execute(code)
        self.assertEqual(r['outcome'],'succeeded',json.dumps(r,ensure_ascii=False));self.assertEqual(r['payload']['reported_test_count'],5)
        self.assertEqual(outside.read_text(),'SYNTHETIC_PRIVATE_SENTINEL')

    def test_model_controlled_installation_and_task_links_are_not_accepted(self):
        fake=self.run/'artifacts/codex.exe';fake.write_bytes(b'fixture not executable')
        with self.assertRaises(SandboxSetupError):_installation(fake,name='Codex',untrusted_root=self.run)
        source=self.run/'artifacts/test_link.py';source.write_text('pass')
        alias=self.run/'artifacts/link'
        quote=lambda p:"'"+str(p).replace("'","''")+"'"
        subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',
            'New-Item -ItemType Junction -Path '+quote(alias)+' -Target '+quote(self.run/'artifacts')+' | Out-Null'],
            check=True,capture_output=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        request={'id':'LINK.1','kind':'sandbox.python_tests','arguments':{'files':[{'path':'artifacts/link/test_link.py','destination':'test_link.py'}]}}
        try:
            r=self.adapter.execute(request);self.assertEqual(r['outcome'],'failed');self.assertIn('reparse points are not permitted',r['error'])
        finally:
            alias.rmdir()  # Removes this directory junction, never traverses its target.
