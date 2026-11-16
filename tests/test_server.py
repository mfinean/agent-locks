import json
import os
import threading
import types
import urllib.request
from http.server import ThreadingHTTPServer

from lockview import procs, server


def test_the_timeline_is_the_part_of_the_state_the_timeline_draws():
    state = {"now": 5.0, "gate": {"installed": True, "slots": [{"name": "gpu.1"}], "gates": [{"pid": 7}], "config": {}},
             "locks": [{"path": "/x"}], "history": [{"unit": "u"}], "outside": [{"pid": 9}], "remotes": {"hosts": []}}
    assert server.timeline(state) == {"now": 5.0, "gate": {"slots": [{"name": "gpu.1"}], "gates": [{"pid": 7}]},
                                      "locks": [{"path": "/x"}], "history": [{"unit": "u"}]}


def test_a_failed_samples_timeline_is_its_error():
    state = {"error": "Traceback …\nKeyError: 'x'\n", "now": 5.0}
    assert server.timeline(state) == state


def test_the_server_serves_the_latest_timeline():
    sampler = types.SimpleNamespace(lock=threading.Lock(), body=b'{"now": 1, "outside": []}', timeline_body=b'{"now": 1}')
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(sampler))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/api/timeline", timeout=5) as r:
            assert json.load(r) == {"now": 1}
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_server_says_where_it_listens_and_clears_what_exited_servers_said(tmp_path):
    d = tmp_path / "serving"
    d.mkdir()
    (d / "999999999.json").write_text(json.dumps({"pid": 999999999, "start": 1, "port": 1}))  # no such process
    (d / f"{os.getppid()}.json").write_text(json.dumps({"pid": os.getppid(), "start": 1, "port": 2}))  # its pid since reused
    server.announce(8766, d)
    me = {"pid": os.getpid(), "start": procs.read_stat(os.getpid())[2], "port": 8766}
    assert {f.name: json.loads(f.read_text()) for f in d.iterdir()} == {f"{os.getpid()}.json": me}
