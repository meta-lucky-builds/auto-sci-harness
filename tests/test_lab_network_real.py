"""The Lab network rule on real Docker (GOAL 18.6): a box on the session's internal network reaches neither the LAN nor
the host, directly or through the gateway; every attempt through the gateway is recorded. Uses the existing sandbox
image for both the box and the gateway; nothing is pulled or built, and every container and network is removed.

Destinations: 192.168.65.254 and host.docker.internal are the Docker Desktop host; 192.168.1.1 is this machine's LAN
router in the stage-p0 probe (files/s2_docker_probe.txt: CONNECTED from a box on the default bridge). On another
network the LAN address may not answer at all, which cannot make these tests pass wrongly: the direct probes assert a
failure with "unreachable", and the bridge control shows the same probe does get out."""

from __future__ import annotations

import json
import subprocess
import uuid

import pytest

from custom_harness.lab import egress as G
from custom_harness.lab.box import DockerCLI
from custom_harness.sandbox.docker import docker_available

IMAGE = G.GATEWAY_IMAGE


def _has_image() -> bool:
    return docker_available() and subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True,
                                                 check=False).returncode == 0


pytestmark = pytest.mark.skipif(not _has_image(), reason=f"needs Docker and the image {IMAGE}")

DIRECT = """
import json, socket
out = {}
for host, port in (("192.168.1.1", 80), ("192.168.65.254", 53), ("1.1.1.1", 443), ("172.17.0.1", 80)):
    s = socket.socket(); s.settimeout(3)
    try:
        s.connect((host, port)); out[host] = "CONNECTED"
    except OSError as e:
        out[host] = str(e)
    s.close()
for name in ("host.docker.internal", "example.com"):
    try:
        out[name] = socket.gethostbyname(name)
    except OSError as e:
        out[name] = "NO-DNS"
print(json.dumps(out))
"""

THROUGH = """
import json, os, socket
gw = os.environ["HTTP_PROXY"].split("//")[1].split(":")[0]
out = {}
for target in ("192.168.1.1:80", "192.168.65.254:53", "host.docker.internal:80", "169.254.169.254:80", "127.0.0.1:3128"):
    s = socket.create_connection((gw, 3128), timeout=20)
    s.sendall(f"CONNECT {target} HTTP/1.1\\r\\nHost: x\\r\\n\\r\\n".encode())
    out[target] = s.recv(200).decode("latin-1").split("\\r\\n")[0]
    s.close()
print(json.dumps(out))
"""


@pytest.fixture
def lab():
    sid = "p0t-" + uuid.uuid4().hex[:8]
    docker = DockerCLI()
    net = G.Network(sid, docker)
    box = f"chh-lab-{sid}"
    try:
        net.up(IMAGE, ())
        env = [x for k, v in G.proxy_env(sid).items() for x in ("-e", f"{k}={v}")]
        r = docker.call(["run", "-d", "--init", "--name", box, "--network", net.name, "--cap-drop", "ALL",
                         "--security-opt", "no-new-privileges", "--user", "1000:1000", *env, IMAGE, "sleep", "300"])
        assert r.returncode == 0, r.stderr
        yield sid, net, box, docker
    finally:
        docker.call(["rm", "-f", box])
        net.down()


def _in_box(docker: DockerCLI, box: str, code: str) -> dict:
    r = docker.call(["exec", box, "python", "-c", code], timeout=120)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_a_program_that_ignores_the_proxy_reaches_nothing(lab):
    _, _, box, docker = lab
    got = _in_box(docker, box, DIRECT)
    assert "CONNECTED" not in got.values(), got
    assert got["example.com"] == "NO-DNS" and got["host.docker.internal"] == "NO-DNS", got
    assert all("unreachable" in got[h].lower() for h in ("192.168.1.1", "192.168.65.254", "1.1.1.1", "172.17.0.1")), got


def test_the_gateway_refuses_the_lan_and_the_host_and_records_it(lab):
    _, net, box, docker = lab
    got = _in_box(docker, box, THROUGH)
    assert all(v.startswith("HTTP/1.1 403") for v in got.values()), got
    rows = [json.loads(x) for x in net.log_lines() or []]
    assert len(rows) == len(got) and all(r["status"] == "denied" for r in rows), rows


def test_the_network_is_internal_and_the_gateway_is_locked_down(lab):
    _, net, _, docker = lab
    assert net.is_internal()
    doc = json.loads(docker.call(["inspect", net.gateway]).stdout)[0]
    host = doc["HostConfig"]
    assert doc["Config"]["User"] == "1000:1000" and host["ReadonlyRootfs"] and host["CapDrop"] == ["ALL"]
    assert not doc["Mounts"]


def test_control_the_same_probe_gets_out_on_the_default_bridge():
    """The direct probe can see a route when one exists, so its failures above mean something."""
    r = subprocess.run(["docker", "run", "--rm", "--network", "bridge", IMAGE, "python", "-c", DIRECT],
                       capture_output=True, text=True, timeout=120, check=False)
    got = json.loads(r.stdout)
    assert got["host.docker.internal"] != "NO-DNS", got
