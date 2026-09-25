"""Container membership is not the same as hosting container mounts."""
import io
import os
from pathlib import Path

import pytest

import hermes_constants as hc


@pytest.fixture
def evidence(monkeypatch):
    monkeypatch.setattr(hc, "_container_detected", None)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    markers = set()
    proc = {"/proc/1/cgroup": "0::/\n", "/proc/self/mountinfo": ""}
    monkeypatch.setattr(os.path, "exists", lambda path: path in markers)

    def read(path, *args, **kwargs):
        value = proc[path]
        if isinstance(value, Exception):
            raise value
        return io.StringIO(value)

    monkeypatch.setattr(hc, "open", read, raising=False)
    return markers, proc


def mount(root="/", target="/", fs="ext4", options="rw"):
    return f"42 21 8:1 {root} {target} rw,relatime shared:1 - {fs} source {options}\n"


@pytest.mark.parametrize("marker", ["/.dockerenv", "/run/.containerenv"])
def test_runtime_marker(evidence, marker):
    evidence[0].add(marker)
    assert hc.is_container()


@pytest.mark.parametrize("path", [
    "/docker/abc", "/system.slice/docker-abc.scope", "/libpod/abc",
    "/machine.slice/libpod-abc.scope", "/lxc/demo", "/lxc.payload.demo",
    "/kubepods/burstable/pod123/abc",
    "/kubepods.slice/kubepods-burstable.slice/cri-containerd-abc.scope",
    "/kubepods.slice/crio-abc.scope",
])
def test_membership_cgroup(evidence, path):
    evidence[1]["/proc/1/cgroup"] = f"0::{path}\n"
    assert hc.is_container()


@pytest.mark.parametrize("data", [
    mount(target="/var/lib/docker/overlay2/abc/merged", fs="overlay", options="rw,lowerdir=/var/lib/docker/overlay2/abc/diff"),
    mount(target="/run/containerd/io.containerd.runtime.v2.task/k8s.io/abc/rootfs", fs="overlay", options="rw,lowerdir=/var/lib/containerd/snapshots/1/fs"),
    mount(target="/var/lib/kubelet/pods/abc/volumes/data"),
    mount(target="/srv/crio/data"),
    mount(fs="overlay", options="rw,lowerdir=/srv/ordinary-overlay"),
    mount(), "", "malformed containerd kubepods line\n",
])
def test_host_mounts_do_not_imply_membership(evidence, data):
    evidence[1]["/proc/self/mountinfo"] = data
    assert hc.is_container() is False


@pytest.mark.parametrize("data", [
    mount(fs="overlay", options="rw,lowerdir=/var/lib/docker/overlay2/abc/diff"),
    mount(fs="overlay", options="rw,lowerdir=/var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots/12/fs"),
    mount(fs="overlay", options="rw,upperdir=/var/lib/containers/storage/overlay/abc/diff"),
    mount(root="/var/lib/docker/containers/abc/hostname", target="/etc/hostname"),
    mount(root="/var/lib/kubelet/pods/abc/etc-hosts", target="/etc/hosts"),
    mount(root="/lxc/demo/rootfs"),
    mount(root="/kubepods.slice/pod123/cri-containerd-abc.scope", target="/sys/fs/cgroup", fs="cgroup2"),
])
def test_container_mounts_with_namespaced_cgroups(evidence, data):
    evidence[1]["/proc/self/mountinfo"] = data
    assert hc.is_container() is True


@pytest.mark.parametrize("value", ["", OSError("unreadable proc")])
def test_missing_proc_is_not_container(evidence, value):
    evidence[1].update({key: value for key in evidence[1]})
    assert hc.is_container() is False


@pytest.mark.parametrize("result", [True, False])
def test_detection_is_cached(evidence, result):
    if result:
        evidence[0].add("/.dockerenv")
    assert hc.is_container() is result
    evidence[0].clear()
    evidence[1]["/proc/1/cgroup"] = "/docker/changed"
    assert hc.is_container() is result


@pytest.mark.parametrize('path', ['/system.slice/containerd.service', '/system.slice/docker.service', '/user.slice/my-containerd-job'])
def test_host_runtime_service_is_not_membership(evidence, path):
    evidence[1]['/proc/1/cgroup'] = f'0::{path}\n'
    assert hc.is_container() is False


def test_candidate_module_origin():
    assert Path(hc.__file__).resolve().parent == Path(__file__).resolve().parents[1]
