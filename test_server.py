"""Tests for the KeySight server: frame relay, Rep #1 over WebSocket, certificate.

The relay tests run twice, over http/ws and over https/wss, because the phone page
uses wss on a secure address (LAN certificate or tunnel) and ws on http://localhost.
test_live_* start a real server and talk to it over real sockets.
"""
import re
import ssl
import threading
import time
import urllib.request

import pytest
from cryptography import x509
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.sync.client import connect

import server
from rep1_finger_zone import PINKY_PENALTY

SCHEMES = [pytest.param("http://testserver", id="http+ws"), pytest.param("https://testserver", id="https+wss")]


def hand(index_x=0.40):
    pts = [[0.5, 0.5, 0.0] for _ in range(21)]
    pts[5][0], pts[17][0] = 0.40, 0.55
    pts[8][0], pts[12][0], pts[16][0], pts[20][0] = index_x, 0.45, 0.50, 0.55
    return pts


@pytest.fixture(params=SCHEMES)
def client(request):
    return TestClient(server.app, base_url=request.param)


def test_pages_are_served(client):
    assert "KeySight Cam" in client.get("/cam").text
    assert "KeySight HUD" in client.get("/hud").text


def test_phone_frame_reaches_hud(client):
    with client.websocket_connect("/ws/hud") as hud:
        assert hud.receive_json() == {"type": "status", "cams": 0}
        with client.websocket_connect("/ws/cam") as cam:
            assert hud.receive_json() == {"type": "status", "cams": 1}
            cam.send_bytes(b"fake-jpeg")
            assert hud.receive_bytes() == b"fake-jpeg"


def test_hud_gets_rep1_verdict_and_purity(client):
    with client.websocket_connect("/ws/hud") as hud:
        hud.receive_json()  # status
        hud.send_json({"type": "hands", "hands": [{"label": "Left", "landmarks": hand(index_x=0.56)}]})
        reply = hud.receive_json()
        assert reply["type"] == "rep1"
        assert reply["results"][0]["thief_finger"] == "Index"
        assert reply["results"][0]["zone"] == "pinky"
        assert reply["purity"] == round(100 + PINKY_PENALTY, 1)


def test_bad_hand_data_returns_error_not_crash(client):
    with client.websocket_connect("/ws/hud") as hud:
        hud.receive_json()
        hud.send_json({"type": "hands", "hands": [{"label": "Left", "landmarks": [[0, 0, 0]]}]})
        assert hud.receive_json()["type"] == "error"


# ---------- the pages pick ws or wss from the address they were opened on ----------

@pytest.mark.parametrize("page", ["cam.html", "hud.html"])
def test_pages_choose_ws_or_wss_from_location(page):
    html = (server.STATIC / page).read_text(encoding="utf-8")
    assert 'location.protocol === "https:" ? "wss:" : "ws:"' in html
    assert re.search(r"new WebSocket\(`\$\{WS_SCHEME\}//\$\{location\.host\}/ws/", html)
    assert "wss://" not in html and "ws://" not in html  # nothing hard-wired


# ---------- tunnel mode keeps the HUD on the PC ----------

TUNNEL_HEADERS = {"cf-connecting-ip": "203.0.113.9"}  # what Cloudflare adds to every relayed request


def test_tunnel_phone_can_send_frames_but_not_open_hud(monkeypatch):
    monkeypatch.setattr(server, "tunnel_mode", True)
    client = TestClient(server.app, base_url="https://quiet-name.trycloudflare.com")
    assert client.get("/cam", headers=TUNNEL_HEADERS).status_code == 200
    assert client.get("/hud", headers=TUNNEL_HEADERS).status_code == 403
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/hud", headers=TUNNEL_HEADERS):
            pass
    with client.websocket_connect("/ws/cam", headers=TUNNEL_HEADERS):
        pass


def test_tunnel_pc_still_opens_hud_on_localhost(monkeypatch):
    monkeypatch.setattr(server, "tunnel_mode", True)
    client = TestClient(server.app, base_url="http://localhost:8080")  # no Cloudflare header
    assert client.get("/hud").status_code == 200
    with client.websocket_connect("/ws/hud") as hud:
        assert hud.receive_json()["type"] == "status"


def test_lan_mode_does_not_look_at_the_tunnel_header():
    client = TestClient(server.app)
    assert client.get("/hud", headers=TUNNEL_HEADERS).status_code == 200


# ---------- real sockets: plain http/ws and self-signed https/wss ----------

class LiveServer:
    def __init__(self, **tls):
        import uvicorn
        self.tls = bool(tls)
        self.server = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=0,
                                                    log_level="error", **tls))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline, "server did not start"
            time.sleep(0.02)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)

    @property
    def http_base(self):
        return f"{'https' if self.tls else 'http'}://127.0.0.1:{self.port}"

    @property
    def ws_base(self):
        return f"{'wss' if self.tls else 'ws'}://127.0.0.1:{self.port}"


def trust_anything():
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    return ctx


def relay_one_frame(live, ctx):
    with connect(f"{live.ws_base}/ws/hud", ssl=ctx) as hud:
        assert '"cams": 0' in hud.recv(timeout=5)
        with connect(f"{live.ws_base}/ws/cam", ssl=ctx) as cam:
            assert '"cams": 1' in hud.recv(timeout=5)
            cam.send(b"\xff\xd8fake-jpeg")
            assert hud.recv(timeout=5) == b"\xff\xd8fake-jpeg"


def test_live_http_and_ws():
    with LiveServer() as live:
        assert live.http_base.startswith("http://") and live.ws_base.startswith("ws://")
        with urllib.request.urlopen(f"{live.http_base}/cam", timeout=5) as page:
            assert b"KeySight Cam" in page.read()
        relay_one_frame(live, None)


def test_live_https_and_wss_with_self_signed_cert(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CERT_DIR", tmp_path)
    cert, key = server.ensure_cert(["127.0.0.1"])
    with LiveServer(ssl_certfile=str(cert), ssl_keyfile=str(key)) as live:
        assert live.http_base.startswith("https://") and live.ws_base.startswith("wss://")
        with urllib.request.urlopen(f"{live.http_base}/hud", timeout=5, context=trust_anything()) as page:
            assert b"KeySight HUD" in page.read()
        relay_one_frame(live, trust_anything())


def test_certificate_covers_lan_ip(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CERT_DIR", tmp_path)
    cert_path, key_path = server.ensure_cert(["192.168.1.50"])
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    names = [str(ip) for ip in san.get_values_for_type(x509.IPAddress)]
    assert "192.168.1.50" in names and "127.0.0.1" in names
    assert key_path.exists()
    # same IPs -> certificate is reused, not remade
    assert server.ensure_cert(["192.168.1.50"])[0].read_bytes() == cert_path.read_bytes()


def test_qr_prints_blocks(capsys):
    server.print_qr("https://192.168.1.50:8443/cam")
    out = capsys.readouterr().out
    assert "██" in out and len(out.splitlines()) > 20


def test_cli_modes_are_exclusive():
    with pytest.raises(SystemExit):
        server.main(["--lan", "--tunnel"])
