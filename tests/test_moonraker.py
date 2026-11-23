"""Moonraker client against response shapes captured from the real K1C (Moonraker on Helper Script)."""
import json

import pytest
import requests

from printloop.moonraker import Moonraker, MoonrakerError

UPLOAD_OK = {"action": "create_file", "item": {"modified": 1795389533.07, "size": 43, "permissions": "rw",
             "path": "printloop_api_test.gcode", "root": "gcodes"}, "print_started": False, "print_queued": False}
NOT_FOUND = {"error": {"code": 404, "message": "Not Found", "traceback": "..."}}
KLIPPY_SHUTDOWN = {"error": {"code": 503, "message": "Klippy Disconnected"}}


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.ok = 200 <= status < 300
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        if isinstance(self._body, str):
            raise requests.JSONDecodeError("Expecting value", self._body, 0)
        return self._body


def client(routes, calls=None, base="http://192.168.1.228:7125"):
    m = Moonraker(base)

    def request(method, url, **kw):
        path = url.replace(m.base, "")
        if calls is not None:
            calls.append((method, path, kw))
        r = routes.get((method, path))
        if r is None:
            return Resp(404, NOT_FOUND)
        return r(kw) if callable(r) else r

    m.session.request = request
    return m


def test_upload_unwrapped_response(tmp_path):
    g = tmp_path / "a.gcode"
    g.write_text("; x\n")
    assert client({("POST", "/server/files/upload"): Resp(201, UPLOAD_OK)}).upload(g) == "printloop_api_test.gcode"


def test_wrapped_response_and_status():
    body = {"result": {"eventtime": 1.0, "status": {"print_stats": {"state": "printing", "filename": "x.gcode",
            "print_duration": 60.0, "info": {"current_layer": 3, "total_layer": 40}}, "virtual_sdcard": {"progress": 0.25},
            "extruder": {"temperature": 210.1}, "heater_bed": {"temperature": 60.0}}}}
    st = client({("GET", "/printer/objects/query"): Resp(200, body)}).status()
    assert (st.state, st.progress, st.current_layer, st.extruder_temp) == ("printing", 0.25, 3, 210.1)


def test_status_survives_missing_objects():
    st = client({("GET", "/printer/objects/query"): Resp(200, {"result": {}})}).status()
    assert st.state == "unknown" and st.progress == 0.0


def test_html_response_is_moonraker_error():
    m = client({("GET", "/printer/info"): Resp(200, "<html>fluidd</html>")})
    with pytest.raises(MoonrakerError, match="not a Moonraker JSON"):
        m.printer_info()


def test_error_body_is_moonraker_error():
    with pytest.raises(MoonrakerError, match="Not Found"):
        client({}).printer_info()


def test_webcam_discovery_html_does_not_crash():
    assert client({("GET", "/server/webcams/list"): Resp(200, "<html/>")}).webcam_snapshot_url() is None


def test_webcam_relative_url_uses_web_ui_port_and_skips_disabled():
    cams = {"result": {"webcams": [
        {"name": "old", "enabled": False, "snapshot_url": "http://dead/snap"},
        {"name": "cam", "enabled": True, "snapshot_url": "/webcam/?action=snapshot"},
    ]}}
    m = client({("GET", "/server/webcams/list"): Resp(200, cams)})
    assert m.webcam_snapshot_url() == "http://192.168.1.228:4408/webcam/?action=snapshot"


def test_webcam_relative_url_without_port_in_base():
    cams = {"result": {"webcams": [{"snapshot_url": "/webcam/?action=snapshot"}]}}
    m = client({("GET", "/server/webcams/list"): Resp(200, cams)}, base="http://k1c.local")
    assert m.webcam_snapshot_url() == "http://k1c.local:4408/webcam/?action=snapshot"


def test_gcode_sent_as_json_body():
    calls = []
    client({("POST", "/printer/gcode/script"): Resp(200, {"result": "ok"})}, calls).heaters_off()
    method, path, kw = calls[0]
    assert kw["json"] == {"script": "M104 S0\nM140 S0\nM107"} and "params" not in kw


def test_cancel_and_cool_ok():
    calls = []
    ok = Resp(200, {"result": "ok"})
    client({("POST", "/printer/print/cancel"): ok, ("POST", "/printer/gcode/script"): ok}, calls).cancel_and_cool()
    assert [c[1] for c in calls] == ["/printer/print/cancel", "/printer/gcode/script"]


def test_cancel_and_cool_when_klipper_shut_down():
    calls = []
    down = Resp(503, KLIPPY_SHUTDOWN)
    m = client({("POST", "/printer/print/cancel"): down, ("POST", "/printer/gcode/script"): down,
                ("GET", "/printer/info"): Resp(200, {"result": {"state": "shutdown"}})}, calls)
    m.cancel_and_cool()  # heaters are already off in shutdown: no exception, no estop
    assert "/printer/emergency_stop" not in [c[1] for c in calls]


def test_cancel_and_cool_falls_back_to_estop():
    calls = []
    m = client({("POST", "/printer/print/cancel"): Resp(400, {"error": {"code": 400, "message": "busy"}}),
                ("POST", "/printer/gcode/script"): Resp(400, {"error": {"code": 400, "message": "busy"}}),
                ("GET", "/printer/info"): Resp(200, {"result": {"state": "ready"}}),
                ("POST", "/printer/emergency_stop"): Resp(200, {"result": "ok"})}, calls)
    m.cancel_and_cool()
    assert calls[-1][1] == "/printer/emergency_stop"


def test_cancel_and_cool_raises_when_nothing_works():
    m = client({("GET", "/printer/info"): Resp(200, {"result": {"state": "ready"}})})
    with pytest.raises(MoonrakerError, match="Could not confirm heaters are off"):
        m.cancel_and_cool()


def _busy(status):
    return client({("GET", "/printer/objects/query"): Resp(200, {"result": {"status": status}})}).busy_reason()


def test_busy_reason():
    idle = {"print_stats": {"state": "standby"}, "extruder": {"target": 0.0}, "heater_bed": {"target": 0.0},
            "idle_timeout": {"state": "Idle"}, "virtual_sdcard": {"is_active": False}}
    assert _busy(idle) is None
    assert "printing F7" in _busy({**idle, "print_stats": {"state": "printing", "filename": "F7"}})
    # Started from the touchscreen: still heating, print_stats not yet "printing"
    assert "heaters are on" in _busy({**idle, "extruder": {"target": 130.0}, "heater_bed": {"target": 50.0}})
    assert "busy" in _busy({**idle, "idle_timeout": {"state": "Printing"}})


def test_upload_uses_remote_name(tmp_path):
    g = tmp_path / "model_v1_v1.gcode"
    g.write_text("; x\n")
    calls = []
    m = client({("POST", "/server/files/upload"): Resp(201, UPLOAD_OK)}, calls)
    m.upload(g, "printloop_20261122-195214_model_v1_v1.gcode")
    assert calls[0][2]["files"]["file"][0] == "printloop_20261122-195214_model_v1_v1.gcode"
