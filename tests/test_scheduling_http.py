"""Real loopback HTTP; temporary runs, no model invocation."""
import http.client
import base64
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from studio_runtime.service import StudioService
from studio_runtime.web import make_server


class HTTPTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".validation"
        base.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=base)
        self.service = StudioService(self.tmp.name, max_workers=7)
        self.server = make_server(self.service, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.token = self.request("GET", "/api/session")[1]["token"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.service.close()
        self.tmp.cleanup()

    def request(self, method, path, data=None, *, token=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        headers = {"Content-Type":"application/json"}
        if token and hasattr(self, "token"):
            headers["X-Studio-Token"] = self.token
        connection.request(method, path, body=None if data is None else json.dumps(data), headers=headers)
        response = connection.getresponse()
        code, body = response.status, json.loads(response.read())
        connection.close()
        return code, body

    def test_create_adjust_status_restart_and_csrf(self):
        code, session = self.request("GET", "/api/session")
        self.assertEqual(session["default_workers"], 7)
        code, created = self.request("POST", "/api/runs",
            {"goal":"HTTP scheduling fixture", "start":False, "max_workers":6})
        self.assertEqual(code, 201)
        run_id = created["id"]
        path = "/api/runs/" + run_id
        self.assertEqual(self.request("GET", path)[1]["scheduling"]["max_workers"], 6)
        self.assertEqual(self.request("POST", path+"/scheduling", {"max_workers":11})[0], 200)
        self.assertEqual(self.request("GET", path)[1]["scheduling"]["max_workers"], 11)
        reopened = StudioService(self.tmp.name, max_workers=2)
        try:
            self.assertEqual(reopened.status(run_id)["scheduling"]["max_workers"], 11)
        finally:
            reopened.close()
        for invalid in (0, 33, True, "6"):
            self.assertEqual(self.request("POST", path+"/scheduling", {"max_workers":invalid})[0], 400)
        self.assertEqual(self.request("POST", path+"/scheduling", {"max_workers":3}, token=False)[0], 403)
        self.assertEqual(self.request("GET", path)[1]["scheduling"]["max_workers"], 11)
        self.assertFalse(self.service.threads)

    def test_sandbox_check_and_retry_endpoints_are_protected_and_do_not_confuse_model_retry(self):
        self.service.create('Synthetic HTTP sandbox',run_id='sandbox',sandbox_tests=True)
        base='/api/runs/sandbox/'
        with patch.object(self.service,'check_sandbox',return_value={'available':False,'reason':'fixture'}) as check:
            self.assertEqual(self.request('POST',base+'sandbox-check',{},token=False)[0],403)
            check.assert_not_called()
            code,value=self.request('POST',base+'sandbox-check',{})
            self.assertEqual(code,200);self.assertFalse(value['available']);check.assert_called_once_with('sandbox')
        self.assertFalse(self.service.threads)
        with patch.object(self.service,'retry_tool',return_value={'new_request_id':'TOOL.NEW'}) as retry,patch.object(self.service,'start') as start:
            code,value=self.request('POST',base+'retry-tool',{'request_id':'TOOL.1','reason':'repaired','allow_uncertain':True})
            self.assertEqual(code,200);self.assertEqual(value['new_request_id'],'TOOL.NEW')
            retry.assert_called_once_with('sandbox','TOOL.1',reason='repaired',allow_uncertain=True)
            start.assert_called_once_with('sandbox')
        with patch.object(self.service,'dismiss_tool_recovery',return_value={'id':'INPUT.DECISION'}) as dismiss,patch.object(self.service,'start') as start:
            self.assertEqual(self.request('POST',base+'dismiss-tool-recovery',{'request_id':'TOOL.1','reason':'unneeded'},token=False)[0],403)
            dismiss.assert_not_called()
            self.assertEqual(self.request('POST',base+'dismiss-tool-recovery',{'request_id':'TOOL.1','reason':'unneeded'})[0],200)
            dismiss.assert_called_once_with('sandbox','TOOL.1',reason='unneeded')
            start.assert_called_once_with('sandbox')

    def test_serve_receives_cli_workers_and_validates_before_server_start(self):
        from studio_runtime.__main__ import main
        with patch("studio_runtime.web.serve") as serve:
            self.assertEqual(main(["--data-root",self.tmp.name,"--workers","9","serve","--port","8766"]), 0)
            self.assertEqual(serve.call_args.kwargs["max_workers"], 9)
        with patch("studio_runtime.web.serve") as serve:
            self.assertNotEqual(main(["--workers","0","serve"]), 0)
            serve.assert_not_called()

    def test_multiple_file_input_is_atomic_versioned_and_idempotent(self):
        from studio_runtime.workflow import Workflow
        start = patch.object(self.service, "start", return_value={"started":False})
        start.start()
        self.addCleanup(start.stop)
        created=self.request("POST","/api/runs",{"goal":"Multiple files fixture","start":False})[1]
        rid=created["id"];endpoint="/api/runs/"+rid+"/input"
        body={"text":"Two files","topic_id":"ROOT","profile_scope":"none",
            "submission_id":"multi-regression-001","attachments":[
                {"name":name,"data_base64":base64.b64encode(content).decode()}
                for name,content in [("a.txt",b"first file"),("b.txt",b"second file")]]}
        code,_=self.request("POST",endpoint,body);self.assertEqual(code,200)
        self.assertEqual(self.request("POST",endpoint,body)[0],200)
        wf=Workflow(self.service.path(rid))
        with wf._tx() as (con,state):
            self.assertEqual(len(state["file_exchange"]),2)
            self.assertEqual(con.execute("SELECT count(*) FROM attachment_blobs").fetchone()[0],2)
            inputs=[i for i in state["user_inputs"] if i.get("text")=="Two files"]
            self.assertEqual(len(inputs),1);self.assertEqual(len(inputs[0]["attachments"]),2)
            ids=[f["id"] for f in inputs[0]["attachments"]]
        bad={**body,"submission_id":"multi-regression-002",
             "attachments":[{**f,"replaces":ids[0]} for f in body["attachments"]]}
        self.assertEqual(self.request("POST",endpoint,bad)[0],400)
        good={**body,"submission_id":"multi-regression-003",
              "attachments":[{**f,"replaces":fid} for f,fid in zip(body["attachments"],ids)]}
        self.assertEqual(self.request("POST",endpoint,good)[0],200)
        with wf._tx() as (con,state):
            self.assertEqual(len(state["file_exchange"]),4)
            for original in ids:
                revised=next(f for f in state["file_exchange"].values() if f["replaces"]==original)
                self.assertEqual(revised["version"],2)
                self.assertEqual(con.execute("SELECT raw FROM attachment_blobs WHERE id=?",(original,)).fetchone()[0],
                    b"first file" if original==ids[0] else b"second file")
        self.assertFalse(self.service.threads)


if __name__ == "__main__":
    unittest.main()
