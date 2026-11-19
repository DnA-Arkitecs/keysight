"""KeySight server: relays phone camera frames to the HUD and scores Rep #1.

Two ways to reach it from a phone (a phone camera needs a secure HTTPS page):
  --tunnel  Cloudflare quick tunnel: a real https://<name>.trycloudflare.com
            address with a trusted certificate, so no browser warning.
  --lan     self-signed HTTPS on your Wi-Fi (default). One certificate warning.

Pages and sockets:
  /cam      phone page (sends camera frames)
  /hud      PC page (draws the hand skeleton)
  /ws/cam   phone -> server frames (JPEG bytes)
  /ws/hud   server -> HUD frames; HUD -> server hand landmarks -> Rep #1 verdict
"""
import argparse
import asyncio
import collections
import datetime
import ipaddress
import json
import os
import platform
import queue
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import qrcode
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from rep1_finger_zone import classify_finger_steal

ROOT = Path(__file__).parent
STATIC = ROOT / "static"
CERT_DIR = ROOT / "certs"
VENDOR_DIR = ROOT / "vendor"
LAN_PORT = 8443      # --lan: HTTPS on every network card
TUNNEL_PORT = 8080   # --tunnel: plain HTTP on this PC only; Cloudflare adds the HTTPS
CLOUDFLARED_RELEASES = "https://github.com/cloudflare/cloudflared/releases/latest/download/"

app = FastAPI(title="KeySight")
tunnel_mode = False  # set by --tunnel: the HUD page and socket then refuse requests relayed by Cloudflare


class TunnelError(RuntimeError):
    """The tunnel could not be set up. The message says what to do about it."""


# ---------- network + certificate helpers ----------

def lan_ips():
    """Primary LAN IPv4 first, then any other local IPv4 addresses."""
    primary = None
    try:  # connecting a UDP socket sends nothing; it just asks the OS which NIC it would use
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            primary = s.getsockname()[0]
    except OSError:
        pass
    found = [ip for ip in socket.gethostbyname_ex(socket.gethostname())[2] if not ip.startswith("127.")]
    ordered = ([primary] if primary and not primary.startswith("127.") else []) + found
    return list(dict.fromkeys(ordered)) or ["127.0.0.1"]


def ensure_cert(ips):
    """Make (or reuse) a self-signed cert that is valid for localhost and every LAN IP."""
    CERT_DIR.mkdir(exist_ok=True)
    cert_path, key_path, ips_path = CERT_DIR / "cert.pem", CERT_DIR / "key.pem", CERT_DIR / "ips.txt"
    wanted = ",".join(sorted(ips))
    if cert_path.exists() and key_path.exists() and ips_path.exists() and ips_path.read_text() == wanted:
        return cert_path, key_path

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "KeySight local")])
    san = x509.SubjectAlternativeName(
        [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
        + [x509.IPAddress(ipaddress.ip_address(ip)) for ip in ips]
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(san, critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    ips_path.write_text(wanted)
    return cert_path, key_path


def print_qr(url):
    """Print a QR code made of block characters (light modules drawn solid so it scans on a dark terminal)."""
    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(url)
    qr.make(fit=True)
    solid, blank = "██", "  "
    print()
    for row in qr.get_matrix():
        print("  " + "".join(blank if dark else solid for dark in row))
    print()


# ---------- Cloudflare quick tunnel ----------

# Quick-tunnel names are lowercase words joined by dashes. "api" is Cloudflare's own
# endpoint, which shows up in cloudflared's error lines, so it is never a tunnel.
TUNNEL_URL_RE = re.compile(r"https://([a-z0-9][a-z0-9-]*)\.trycloudflare\.com\b", re.IGNORECASE)


def parse_tunnel_url(line):
    """Return 'https://<name>.trycloudflare.com' found in one cloudflared log line, else None."""
    for match in TUNNEL_URL_RE.finditer(line):
        name = match.group(1).lower()
        if name != "api":
            return f"https://{name}.trycloudflare.com"
    return None


def cloudflared_asset(system=None, machine=None):
    """File name of the official cloudflared download for this computer."""
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    if system == "windows":
        return "cloudflared-windows-amd64.exe"  # also runs on Windows on ARM
    if system == "linux":
        return "cloudflared-linux-" + ("arm64" if machine in ("arm64", "aarch64") else "amd64")
    raise TunnelError("No automatic cloudflared download for this system. "
                      "Install it (macOS: brew install cloudflared) and run again.")


def ensure_cloudflared(vendor_dir=None, which=shutil.which):
    """Path to cloudflared: an installed copy, else vendor/, else download the official one into vendor/."""
    installed = which("cloudflared")
    if installed:
        return Path(installed)
    vendor_dir = Path(vendor_dir or VENDOR_DIR)
    asset = cloudflared_asset()
    dest = vendor_dir / asset
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    vendor_dir.mkdir(parents=True, exist_ok=True)
    print(f"Downloading cloudflared (once) into {vendor_dir.name}/ ...")
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        urllib.request.urlretrieve(CLOUDFLARED_RELEASES + asset, tmp)
    except OSError as err:
        tmp.unlink(missing_ok=True)
        raise TunnelError(f"Could not download cloudflared ({err}). Check the internet connection, "
                          "or install cloudflared yourself and run again.") from err
    tmp.replace(dest)
    dest.chmod(dest.stat().st_mode | stat.S_IXUSR)
    return dest


def build_tunnel_command(cloudflared, port=TUNNEL_PORT):
    return [str(cloudflared), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"]


def start_tunnel(cloudflared, port=TUNNEL_PORT, timeout=45.0):
    """Start cloudflared and wait for its public address. Returns (process, 'https://<name>.trycloudflare.com')."""
    proc = subprocess.Popen(
        build_tunnel_command(cloudflared, port),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    lines, found = queue.Queue(), threading.Event()

    def read_output():  # always drain the pipe so cloudflared never blocks on a full buffer
        for line in proc.stdout:
            if not found.is_set():
                lines.put(line)
        lines.put(None)

    threading.Thread(target=read_output, daemon=True).start()
    tail = collections.deque(maxlen=6)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                line = lines.get(timeout=max(0.05, deadline - time.monotonic()))
            except queue.Empty:
                line = ""
            if line is None:
                raise TunnelError("cloudflared stopped before it gave an address:\n  " + "\n  ".join(tail))
            url = parse_tunnel_url(line)
            if url:
                found.set()
                return proc, url
            if line.strip():
                tail.append(line.strip())
            if time.monotonic() >= deadline:
                raise TunnelError(f"cloudflared gave no address within {timeout:g}s:\n  " + "\n  ".join(tail))
    except BaseException:
        proc.kill()
        raise


def came_through_tunnel(headers):
    """True when Cloudflare relayed this request. Only cloudflared adds this header."""
    return tunnel_mode and "cf-connecting-ip" in headers


# ---------- live relay ----------

class HudClient:
    """One open /hud page. Keeps only the newest frame so a slow PC never builds a backlog."""

    def __init__(self, ws):
        self.ws = ws
        self.latest = None
        self.wake = asyncio.Event()
        self.send_lock = asyncio.Lock()
        self.purity = 100.0  # 10-Finger Purity %, one running score per HUD page

    async def send_json(self, payload):
        async with self.send_lock:
            await self.ws.send_text(json.dumps(payload))

    async def pump_frames(self):
        while True:
            await self.wake.wait()
            self.wake.clear()
            frame, self.latest = self.latest, None
            if frame is not None:
                async with self.send_lock:
                    await self.ws.send_bytes(frame)


huds = set()
cams = set()


async def tell_huds_status():
    for h in list(huds):
        try:
            await h.send_json({"type": "status", "cams": len(cams)})
        except Exception:
            pass


def score_hands(hud, hands):
    """Run Rep #1 on each hand and update the HUD's purity score."""
    results = []
    for hand in hands:
        verdict = classify_finger_steal(hand["label"], hand["landmarks"])
        hud.purity = max(0.0, min(100.0, hud.purity + verdict["purity_delta"]))
        results.append({"label": hand["label"], **verdict})
    return {"type": "rep1", "results": results, "purity": round(hud.purity, 1)}


@app.websocket("/ws/cam")
async def ws_cam(ws: WebSocket):
    await ws.accept()
    cams.add(ws)
    await tell_huds_status()
    try:
        while True:
            frame = await ws.receive_bytes()
            for h in huds:
                h.latest = frame
                h.wake.set()
    except WebSocketDisconnect:
        pass
    finally:
        cams.discard(ws)
        await tell_huds_status()


@app.websocket("/ws/hud")
async def ws_hud(ws: WebSocket):
    if came_through_tunnel(ws.headers):  # the tunnel address is for the phone; the HUD stays on this PC
        await ws.close(code=1008)
        return
    await ws.accept()
    hud = HudClient(ws)
    huds.add(hud)
    pump = asyncio.create_task(hud.pump_frames())
    await hud.send_json({"type": "status", "cams": len(cams)})
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            if msg.get("type") == "hands":
                try:
                    await hud.send_json(score_hands(hud, msg["hands"]))
                except (ValueError, KeyError, TypeError) as err:
                    await hud.send_json({"type": "error", "message": str(err)})
            elif msg.get("type") == "reset":
                hud.purity = 100.0
    except WebSocketDisconnect:
        pass
    finally:
        pump.cancel()
        huds.discard(hud)


@app.get("/cam")
def cam_page():
    return FileResponse(STATIC / "cam.html")


@app.get("/hud")
def hud_page(request: Request):
    if came_through_tunnel(request.headers):
        raise HTTPException(status_code=403, detail="The HUD opens on the PC running KeySight only.")
    return FileResponse(STATIC / "hud.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---------- start-up ----------

def run_lan(no_browser):
    ips = lan_ips()
    cert, key = ensure_cert(ips)
    cam_url = f"https://{ips[0]}:{LAN_PORT}/cam"
    print("\nKeySight is starting (LAN mode). Everything stays on your Wi-Fi.")
    print("\n1) On your phone (same Wi-Fi), scan this:")
    print_qr(cam_url)
    print(f"   or type: {cam_url}")
    print("   The phone shows a certificate warning once: tap Advanced / Show Details, then continue.")
    print("   Want no warning? Run again with --tunnel.")
    print(f"\n2) On this PC: https://localhost:{LAN_PORT}/hud  (opening now)\n")
    if not no_browser:
        threading.Timer(2.0, webbrowser.open, args=[f"https://localhost:{LAN_PORT}/hud"]).start()
    uvicorn.run(app, host="0.0.0.0", port=LAN_PORT, ssl_certfile=str(cert), ssl_keyfile=str(key),
                log_level="warning")
    return 0


def run_tunnel(no_browser):
    try:
        cloudflared = ensure_cloudflared()
    except TunnelError as err:
        print(f"\n{err}", file=sys.stderr)
        return 1

    global tunnel_mode
    tunnel_mode = True
    server =uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=TUNNEL_PORT, log_level="warning"))
    state = {"proc": None, "error": None}

    def open_tunnel():
        try:
            state["proc"], public = start_tunnel(cloudflared, TUNNEL_PORT)
        except TunnelError as err:
            state["error"] = err
            server.should_exit = True
            return
        cam_url = f"{public}/cam"
        print("\nKeySight is starting (tunnel mode). Your phone gets a trusted HTTPS address, no warning.")
        print("\n1) On your phone, scan this:")
        print_qr(cam_url)
        print(f"   or type: {cam_url}")
        print("   Phone frames travel through Cloudflare. Press Ctrl+C here to close the address.")
        print(f"\n2) On this PC: http://localhost:{TUNNEL_PORT}/hud  (opening now)\n")
        if not no_browser:
            webbrowser.open(f"http://localhost:{TUNNEL_PORT}/hud")

    threading.Thread(target=open_tunnel, daemon=True).start()
    try:
        server.run()
    finally:
        if state["proc"]:
            state["proc"].terminate()
    if state["error"]:
        print(f"\n{state['error']}", file=sys.stderr)
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="KeySight: phone camera in, hand skeleton HUD out.")
    parser.add_argument("--no-browser", action="store_true", help="do not open the HUD on this PC")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--lan", action="store_true",
                      help="self-signed HTTPS on your Wi-Fi, one certificate warning on the phone (default)")
    mode.add_argument("--tunnel", action="store_true",
                      help="Cloudflare quick tunnel: trusted HTTPS address, no warning on the phone")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    return (run_tunnel if args.tunnel else run_lan)(args.no_browser)


if __name__ == "__main__":
    sys.exit(main())
