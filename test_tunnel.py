"""Tests for tunnel mode: reading the trycloudflare address, finding or downloading cloudflared."""
import sys
import textwrap

import pytest

import server

# A real cloudflared start-up prints a banner like this on stderr.
BANNER = [
    "2026-11-19T15:00:01Z INF Thank you for trying Cloudflare Tunnel. Doing so, without a Cloudflare account, is a quick way to experiment and try it out.",
    "2026-11-19T15:00:01Z INF Requesting new quick Tunnel on trycloudflare.com...",
    "2026-11-19T15:00:03Z INF +--------------------------------------------------------------------------------------------+",
    "2026-11-19T15:00:03Z INF |  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |",
    "2026-11-19T15:00:03Z INF |  https://quiet-river-demo-42.trycloudflare.com                                             |",
    "2026-11-19T15:00:03Z INF +--------------------------------------------------------------------------------------------+",
]


# ---------- URL parser ----------

def test_parser_reads_the_boxed_banner_line():
    urls = [server.parse_tunnel_url(line) for line in BANNER]
    assert urls[4] == "https://quiet-river-demo-42.trycloudflare.com"
    assert [u for u in urls if u] == ["https://quiet-river-demo-42.trycloudflare.com"]


@pytest.mark.parametrize("line, expected", [
    ("https://a-b-c.trycloudflare.com", "https://a-b-c.trycloudflare.com"),
    ("visit https://Loud-Name.TryCloudflare.com/cam now", "https://loud-name.trycloudflare.com"),
    ("|  https://x9.trycloudflare.com  |", "https://x9.trycloudflare.com"),
    ("", None),
    ("INF Requesting new quick Tunnel on trycloudflare.com...", None),
    ("http://insecure.trycloudflare.com", None),
    ("https://example.com/trycloudflare.com", None),
    ("https://-bad.trycloudflare.com", None),
    # Cloudflare's own endpoint appears in error lines and is not a tunnel
    ('ERR failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel": EOF', None),
    ("ERR https://api.trycloudflare.com/tunnel then https://real-one.trycloudflare.com",
     "https://real-one.trycloudflare.com"),
])
def test_parser_cases(line, expected):
    assert server.parse_tunnel_url(line) == expected


# ---------- finding or downloading cloudflared ----------

def test_asset_names():
    assert server.cloudflared_asset("Windows", "AMD64") == "cloudflared-windows-amd64.exe"
    assert server.cloudflared_asset("Windows", "ARM64") == "cloudflared-windows-amd64.exe"
    assert server.cloudflared_asset("Linux", "x86_64") == "cloudflared-linux-amd64"
    assert server.cloudflared_asset("Linux", "aarch64") == "cloudflared-linux-arm64"
    with pytest.raises(server.TunnelError, match="brew install cloudflared"):
        server.cloudflared_asset("Darwin", "arm64")


def test_installed_cloudflared_is_used_and_nothing_downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(server.urllib.request, "urlretrieve", lambda *a: pytest.fail("must not download"))
    found = server.ensure_cloudflared(tmp_path, which=lambda name: r"C:\tools\cloudflared.exe")
    assert str(found) == r"C:\tools\cloudflared.exe"
    assert list(tmp_path.iterdir()) == []


def test_vendor_copy_is_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "cloudflared_asset", lambda *a: "cloudflared-test")
    (tmp_path / "cloudflared-test").write_bytes(b"exe")
    monkeypatch.setattr(server.urllib.request, "urlretrieve", lambda *a: pytest.fail("must not download"))
    assert server.ensure_cloudflared(tmp_path, which=lambda name: None) == tmp_path / "cloudflared-test"


def test_missing_cloudflared_downloads_official_release_into_vendor(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "cloudflared_asset", lambda *a: "cloudflared-test")
    asked = []

    def fake_download(url, dest):
        asked.append(url)
        open(dest, "wb").write(b"binary")

    monkeypatch.setattr(server.urllib.request, "urlretrieve", fake_download)
    vendor = tmp_path / "vendor"
    path = server.ensure_cloudflared(vendor, which=lambda name: None)
    assert path == vendor / "cloudflared-test" and path.read_bytes() == b"binary"
    assert asked == ["https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-test"]
    assert not list(vendor.glob("*.part"))


def test_failed_download_leaves_no_partial_file(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "cloudflared_asset", lambda *a: "cloudflared-test")

    def broken(url, dest):
        open(dest, "wb").write(b"half")
        raise OSError("network down")

    monkeypatch.setattr(server.urllib.request, "urlretrieve", broken)
    with pytest.raises(server.TunnelError, match="network down"):
        server.ensure_cloudflared(tmp_path, which=lambda name: None)
    assert list(tmp_path.iterdir()) == []


def test_default_vendor_folder_is_git_ignored():
    ignored = (server.ROOT / ".gitignore").read_text().split()
    assert server.VENDOR_DIR.name + "/" in ignored


# ---------- starting the tunnel process ----------

def fake_cloudflared(monkeypatch, body):
    code = textwrap.dedent(body)
    monkeypatch.setattr(server, "build_tunnel_command", lambda exe, port: [sys.executable, "-u", "-c", code])


def test_command_points_cloudflared_at_local_http():
    cmd = server.build_tunnel_command("cloudflared", 8080)
    assert cmd[:2] == ["cloudflared", "tunnel"]
    assert cmd[cmd.index("--url") + 1] == "http://127.0.0.1:8080"


def test_start_tunnel_returns_the_public_address(monkeypatch):
    fake_cloudflared(monkeypatch, f"""
        import time
        for line in {BANNER!r}:
            print(line)
        time.sleep(30)
    """)
    proc, url = server.start_tunnel("ignored", 8080, timeout=10)
    try:
        assert url == "https://quiet-river-demo-42.trycloudflare.com"
        assert proc.poll() is None  # still running, holding the tunnel open
    finally:
        proc.kill()


def test_start_tunnel_reports_cloudflared_exiting_early(monkeypatch):
    fake_cloudflared(monkeypatch, """
        print('ERR failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel": EOF')
    """)
    with pytest.raises(server.TunnelError, match="stopped before it gave an address") as err:
        server.start_tunnel("ignored", 8080, timeout=10)
    assert "failed to request quick Tunnel" in str(err.value)


def test_start_tunnel_times_out_and_stops_the_process(monkeypatch):
    fake_cloudflared(monkeypatch, """
        import time
        print('INF still thinking')
        time.sleep(30)
    """)
    with pytest.raises(server.TunnelError, match="no address within"):
        server.start_tunnel("ignored", 8080, timeout=1)
