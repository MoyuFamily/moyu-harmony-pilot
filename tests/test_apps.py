"""Offline HAP lifecycle, acquisition, idempotency and recovery regression tests."""

import io
import json
import shlex
import zipfile
from unittest.mock import Mock

import pytest

from moyu_harmony_pilot.actions import ActionRegistry, ActionRunner
from moyu_harmony_pilot.device import (
    DeviceConfigurationError,
    DeviceError,
    DeviceProfile,
    HarmonyDevice,
)
from moyu_harmony_pilot.device.apps import parse_app_info, parse_app_list
from moyu_harmony_pilot.device.packages import _PackageRedirects, download_hap, hap_metadata
from moyu_harmony_pilot.hdc import CommandResult, HdcClient, HdcTimeoutError
from tests.fakes import FakeDeviceState, FakeHdcTransport
from tests.test_cli import _run_cli


def make_hap(path, *, version=7, bundle="com.example.package"):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "module.json",
            json.dumps(
                {
                    "app": {"bundleName": bundle, "versionCode": version, "versionName": "1.0"},
                    "module": {"name": "entry", "mainElement": "EntryAbility"},
                }
            ),
        )
    return path


class PackageTransport(FakeHdcTransport):
    def __init__(self, devices):
        super().__init__(devices)
        self.apps = {}
        self.submissions = 0
        self.timeout_after_install = False
        self.no_acknowledgement = False

    def execute(self, args, *, target=None, timeout=15):
        if target not in self.devices or self.devices[target].status != "Connected":
            return super().execute(args, target=target, timeout=timeout)
        if args[0] == "install":
            self.calls.append((target, tuple(args), timeout))
            self.submissions += 1
            metadata = hap_metadata(args[-1])
            self.apps[metadata["bundle_name"]] = metadata["version_code"]
            if self.timeout_after_install:
                raise HdcTimeoutError("Synthetic disconnect after installation.")
            message = "processing" if self.no_acknowledgement else "install bundle successfully."
            return CommandResult(tuple(args), 0, message, "", 1)
        if args[0] == "uninstall":
            self.calls.append((target, tuple(args), timeout))
            self.submissions += 1
            self.apps.pop(args[1], None)
            return CommandResult(tuple(args), 0, "uninstall bundle successfully.", "", 1)
        if args[0] == "shell" and args[1].startswith("bm dump"):
            self.calls.append((target, tuple(args), timeout))
            tokens = shlex.split(args[1])
            if "-a" in tokens:
                output = "All bundle names:\n" + "\n".join(self.apps)
            else:
                bundle = tokens[-1]
                output = json.dumps(
                    {
                        "name": bundle,
                        "versionCode": self.apps[bundle],
                        "hapModuleInfos": [
                            {
                                "moduleName": "entry",
                                "isEntry": True,
                                "mainElementName": "EntryAbility",
                            }
                        ],
                    }
                )
            return CommandResult(tuple(args), 0, output, "", 1)
        return super().execute(args, target=target, timeout=timeout)


@pytest.fixture
def packages(tmp_path, monkeypatch):
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **kwargs: [])
    state = FakeDeviceState("SYNTHETIC_PACKAGE")
    transport = PackageTransport([state])
    device = HarmonyDevice(
        HdcClient(transport=transport), DeviceProfile("phone", state.serial), state_dir=tmp_path
    )
    path = make_hap(tmp_path / "example.hap")
    metadata = hap_metadata(path)
    params = {
        "bundle_name": metadata["bundle_name"],
        "version_code": metadata["version_code"],
        "packages": [{"path": metadata["path"], "sha256": metadata["sha256"]}],
    }
    runner = ActionRunner(ActionRegistry.discover(env={}), state_dir=tmp_path)
    return runner, device, transport, params, path


def test_install_update_uninstall_and_duplicate_keys(packages):
    runner, device, transport, params, _ = packages
    assert (
        runner.run(device, "app.install", params, idempotency_key="install-1").status == "succeeded"
    )
    assert device.app_info(params["bundle_name"]).version_code == 7
    device.start_app(params["bundle_name"], None)
    assert any("-m entry" in " ".join(args) for _, args, _ in transport.calls)
    assert (
        runner.run(device, "app.install", params, idempotency_key="install-1").status
        == "already_done"
    )
    assert transport.submissions == 1
    removal = {"bundle_name": params["bundle_name"]}
    assert (
        runner.run(device, "app.uninstall", removal, idempotency_key="remove-1").status
        == "succeeded"
    )
    assert device.app_info(params["bundle_name"]) is None
    assert (
        runner.run(device, "app.uninstall", removal, idempotency_key="remove-2").status
        == "already_done"
    )
    assert transport.submissions == 2


def test_version_update_and_offline_restore_do_not_replay(packages):
    runner, device, transport, params, path = packages
    transport.apps[params["bundle_name"]] = 6
    transport.devices[device.profile.serial].status = "Offline"
    outcome = runner.run(device, "app.install", params, idempotency_key="upgrade")
    assert outcome.status == "device_offline"
    assert transport.submissions == 0
    transport.devices[device.profile.serial].status = "Connected"
    outcome = runner.run(device, "app.install", params, idempotency_key="upgrade")
    assert outcome.status == "succeeded"
    assert transport.apps[params["bundle_name"]] == 7
    make_hap(path, version=8)
    metadata = hap_metadata(path)
    changed = {
        **params,
        "version_code": 8,
        "packages": [
            {"path": metadata["path"], "sha256": metadata["sha256"]},
        ],
    }
    assert (
        runner.run(device, "app.install", changed, idempotency_key="upgrade").status
        == "configuration_error"
    )
    assert transport.submissions == 1
    assert (
        runner.run(device, "app.install", changed, idempotency_key="upgrade-8").status
        == "succeeded"
    )
    assert transport.apps[params["bundle_name"]] == 8


def test_manual_resolution_is_offline_without_hdc(packages, tmp_path):
    runner, device, transport, params, _ = packages
    transport.timeout_after_install = True
    assert runner.run(device, "app.install", params, idempotency_key="manual").status == "unknown"
    config = tmp_path / "devices.local.json"
    config.write_text(json.dumps({"devices": {"phone": device.profile.serial}}))
    config.chmod(0o600)
    code, output, _ = _run_cli(
        [
            "ledger",
            "resolve",
            "--json",
            "--config",
            str(config),
            "--state-dir",
            str(tmp_path),
            "--hdc-path",
            "/unavailable/hdc",
            "--device",
            "phone",
            "--action",
            "app.install",
            "--idempotency-key",
            "manual",
            "--params-json",
            json.dumps(params),
            "--note",
            "Verified synthetic build",
        ],
        env={},
    )
    assert code == 0, output
    assert json.loads(output)["data"]["status"] == "succeeded"


@pytest.mark.parametrize("existing_version", [None, 6, 7])
def test_uncertain_install_readonly_reconciliation(packages, existing_version):
    runner, device, transport, params, _ = packages
    if existing_version is not None:
        transport.apps[params["bundle_name"]] = existing_version
    transport.timeout_after_install = True
    assert (
        runner.run(device, "app.install", params, idempotency_key="uncertain").status == "unknown"
    )
    assert (
        runner.run(device, "app.install", params, idempotency_key="uncertain").status == "unknown"
    )
    outcome = runner.reconcile(device, "app.install", params, idempotency_key="uncertain")
    assert outcome.status == ("unknown" if existing_version == 7 else "succeeded")
    assert transport.submissions == 1
    if existing_version == 7:
        assert (
            runner.reconcile(
                device,
                "app.install",
                params,
                idempotency_key="uncertain",
                note="Synthetic operator verification",
            ).status
            == "succeeded"
        )


def test_mutated_hap_and_missing_acknowledgement(packages):
    runner, device, transport, params, path = packages
    make_hap(path, version=8)
    assert (
        runner.run(device, "app.install", params, idempotency_key="modified").status
        == "configuration_error"
    )
    assert transport.submissions == 0
    make_hap(path)
    params["packages"][0]["sha256"] = hap_metadata(path)["sha256"]
    transport.no_acknowledgement = True
    assert runner.run(device, "app.install", params, idempotency_key="ack").status == "unknown"
    assert transport.submissions == 1


def test_cli_install_inspect_reconcile_and_report(packages, tmp_path):
    _, device, transport, _, path = packages
    config = tmp_path / "devices.local.json"
    config.write_text(json.dumps({"devices": {"phone": device.profile.serial}}))
    config.chmod(0o600)
    common = ["--json", "--state-dir", str(tmp_path), "--config", str(config)]
    code, output, error = _run_cli(
        [*common, "apps", "install", "--hap", str(path), "--idempotency-key", "cli"],
        transport=transport,
        env={},
    )
    assert code == 0, output + error
    payload = json.loads(output)
    assert payload["data"]["status"] == "succeeded"
    assert device.profile.serial not in output
    code, output, _ = _run_cli(
        [
            *common,
            "ledger",
            "inspect",
            "--device",
            "phone",
            "--action",
            "app.install",
            "--idempotency-key",
            "cli",
        ],
        transport=transport,
        env={},
    )
    assert code == 0
    assert json.loads(output)["data"]["record"]["status"] == "succeeded"
    report = tmp_path / "report.html"
    code, output, _ = _run_cli([*common, "ledger", "report", "--out", str(report)], env={})
    assert code == 0, output
    assert "app.install" in report.read_text()
    assert report.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "payload",
    [
        {
            "name": "com.example.package",
            "versionCode": 7,
            "hapModuleInfos": [{"name": "entry", "mainAbility": "EntryAbility"}],
        },
        {
            "bundleName": "com.example.package",
            "applicationInfo": {"versionCode": 7},
            "hapModuleInfos": [
                {"moduleName": "entry", "isEntry": True, "mainElementName": "EntryAbility"}
            ],
        },
    ],
)
def test_bundle_metadata_variants(payload):
    info = parse_app_info("com.example.package:\n" + json.dumps(payload), "com.example.package")
    assert info.version_code == 7
    assert info.entry_ability == "EntryAbility"
    assert parse_app_list("All bundle names:\ncom.example.package") == ["com.example.package"]
    with pytest.raises(DeviceError):
        parse_app_list("error: service unavailable")
    with pytest.raises(DeviceError):
        parse_app_info("bad dump", "com.example.package")


def test_download_is_bounded_validated_atomic_and_offline(tmp_path, monkeypatch):
    source = make_hap(tmp_path / "source.hap")
    payload = source.read_bytes()
    opener = Mock()

    def response(*args, **kwargs):
        stream = io.BytesIO(payload)
        stream.headers = {"Content-Length": str(len(payload))}
        return stream

    opener.open.side_effect = response
    monkeypatch.setattr("moyu_harmony_pilot.device.packages.build_opener", lambda *args: opener)
    target = tmp_path / "download.hap"
    metadata = download_hap(
        "https://packages.example.org/test.hap", target, sha256=hap_metadata(source)["sha256"]
    )
    assert metadata["path"] == str(target)
    assert target.read_bytes() == payload
    assert target.stat().st_mode & 0o777 == 0o600
    for kwargs in ({"sha256": "0" * 64}, {"max_bytes": 1}):
        with pytest.raises(DeviceConfigurationError):
            download_hap("https://packages.example.org/test.hap", target, **kwargs)
        assert target.read_bytes() == payload
    assert not list(tmp_path.glob(".moyu-download-*"))
    with pytest.raises(DeviceConfigurationError):
        download_hap("file:///etc/passwd", target)
    with pytest.raises(DeviceConfigurationError):
        _PackageRedirects().redirect_request(
            Mock(full_url="https://packages.example.org"),
            None,
            302,
            "",
            {},
            "http://packages.example.org",
        )


def test_local_package_actions_are_not_remotely_exposed():
    registry = ActionRegistry.discover(env={}, allowed_actions=["app.install", "app.uninstall"])
    for action in registry.allowed_actions:
        with pytest.raises(DeviceConfigurationError):
            registry.get(action, remote=True)
