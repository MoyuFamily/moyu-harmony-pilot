"""Offline package lifecycle, acquisition, idempotency and recovery regression tests."""

import io
import json
import shlex
import shutil
import zipfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from moyu_harmony_pilot.actions import ActionRegistry, ActionRunner
from moyu_harmony_pilot.device import (
    DeviceConfigurationError,
    DeviceProfile,
    HarmonyDevice,
)
from moyu_harmony_pilot.device.packages import (
    _PackageRedirects,
    download_hap,
    hap_metadata,
    package_metadata,
)
from moyu_harmony_pilot.hdc import CommandResult, HdcClient, HdcTimeoutError
from tests.fakes import FakeDeviceState, FakeHdcTransport
from tests.test_cli import _run_cli


def make_hap(path, *, version=7, bundle="com.example.package", module="entry"):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "module.json",
            json.dumps(
                {
                    "app": {"bundleName": bundle, "versionCode": version, "versionName": "1.0"},
                    "module": {"name": module, "mainElement": "EntryAbility"},
                }
            ),
        )
    return path


def make_app(path, modules, *, version=7, bundle="com.example.package"):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "pack.info",
            json.dumps({"summary": {"app": {"bundleName": bundle, "version": {"code": version}}}}),
        )
        for name, contents in modules:
            archive.writestr(name, contents)
    return path


class PackageTransport(FakeHdcTransport):
    def __init__(self, devices):
        super().__init__(devices)
        self.apps = {}
        self.submissions = 0
        self.timeout_after_install = False
        self.no_acknowledgement = False
        self.install_paths = []
        self.install_payloads = []
        self.install_modes = []

    def execute(self, args, *, target=None, timeout=15):
        if target not in self.devices or self.devices[target].status != "Connected":
            return super().execute(args, target=target, timeout=timeout)
        if args[0] == "install":
            self.calls.append((target, tuple(args), timeout))
            self.submissions += 1
            self.install_paths = [Path(path) for path in args[1:]]
            self.install_payloads = [path.read_bytes() for path in self.install_paths]
            self.install_modes = [path.stat().st_mode & 0o777 for path in self.install_paths]
            metadata = package_metadata(args[-1])
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
                output = "ID: 100:\n" + "".join(f"\t{bundle}\n" for bundle in self.apps)
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


def test_manual_release_is_offline_without_hdc_and_allows_retry(packages, tmp_path):
    runner, device, transport, params, _ = packages
    transport.timeout_after_install = True
    assert runner.run(device, "app.install", params, idempotency_key="stuck").status == "unknown"
    assert runner.run(device, "app.install", params, idempotency_key="stuck").status == "unknown"
    assert transport.submissions == 1
    # Simulate operator verifying the package was never installed on the device.
    transport.apps.clear()

    config = tmp_path / "devices.local.json"
    config.write_text(json.dumps({"devices": {"phone": device.profile.serial}}))
    config.chmod(0o600)
    base = [
        "ledger",
        "release",
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
        "stuck",
        "--params-json",
        json.dumps(params),
        "--note",
        "Verified device never installed package",
    ]
    code, output, _ = _run_cli(base, env={})
    assert code == 0, output
    data = json.loads(output)["data"]
    assert data["released"] is True
    assert data["action"] == "app.install"

    # Retrying with the same idempotency key now executes and succeeds.
    transport.timeout_after_install = False
    retried = runner.run(device, "app.install", params, idempotency_key="stuck")
    assert retried.status == "succeeded"
    assert transport.submissions == 2

    # Confirmed completion cannot be released via CLI or SDK.
    code, output, _ = _run_cli(base, env={})
    assert code == 2, output
    with pytest.raises(DeviceConfigurationError, match="Only uncertain reservations"):
        runner.release(
            device,
            "app.install",
            params,
            idempotency_key="stuck",
            note="Should be rejected",
        )

    # SDK release also works directly for uncertain reservations.
    transport.apps.clear()
    transport.timeout_after_install = True
    assert (
        runner.run(device, "app.install", params, idempotency_key="sdk-stuck").status == "unknown"
    )
    transport.apps.clear()
    runner.release(
        device,
        "app.install",
        params,
        idempotency_key="sdk-stuck",
        note="Verified unapplied in SDK",
    )
    transport.timeout_after_install = False
    assert (
        runner.run(device, "app.install", params, idempotency_key="sdk-stuck").status
        == "succeeded"
    )


@pytest.mark.parametrize(
    "existing_version",
    [
        None,
        7,
    ],
    ids=['new-install', 'already-current-needs-reconciliation'],
)
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


@pytest.mark.parametrize(
    "times_out",
    [
        True,
    ],
    ids=['uncertain-install-cleanup'],
)
def test_sdk_expands_multi_module_app_once_and_cleans_up(packages, tmp_path, times_out):
    _, device, transport, _, hap = packages
    transport.devices[device.profile.serial].api_version = "21"
    feature = make_hap(tmp_path / "feature.hap", module="feature")
    shared = make_hap(tmp_path / "shared.hsp", module="shared")
    modules = [
        ("entry.hap", hap.read_bytes()),
        ("feature.hap", feature.read_bytes()),
        ("shared.hsp", shared.read_bytes()),
    ]
    app = make_app(tmp_path / "release.app", modules)
    transport.timeout_after_install = times_out
    if times_out:
        with pytest.raises(HdcTimeoutError):
            device.install_app([app])
    else:
        device.install_app([app])
    assert transport.submissions == 1
    assert transport.install_payloads == [contents for _, contents in modules]
    assert [path.suffix for path in transport.install_paths] == [".hap", ".hap", ".hsp"]
    assert transport.install_modes == [0o600, 0o600, 0o600]
    assert all(not path.parent.exists() for path in transport.install_paths)


@pytest.mark.parametrize(
    "api",
    [
        "22",
    ],
    ids=['native-app-api'],
)
def test_sdk_preserves_intact_app_on_supported_device(packages, tmp_path, api):
    _, device, transport, _, hap = packages
    transport.devices[device.profile.serial].api_version = api
    app = make_app(tmp_path / "release.app", [("entry.hap", hap.read_bytes())])
    device.install_app([app])
    assert transport.install_paths == [app]
    assert transport.install_payloads == [app.read_bytes()]
    assert transport.submissions == 1


def test_native_app_install_failure_never_falls_back_or_replays(packages, tmp_path):
    runner, device, transport, params, hap = packages
    app = make_app(tmp_path / "release.app", [("entry.hap", hap.read_bytes())])
    params["packages"] = [{"path": str(app), "sha256": package_metadata(app)["sha256"]}]
    transport.no_acknowledgement = True
    for _ in range(2):
        outcome = runner.run(device, "app.install", params, idempotency_key="native-failure")
        assert outcome.status == "unknown"
    assert transport.submissions == 1
    assert [path.suffix for path in transport.install_paths] == [".app"]
    assert transport.install_payloads == [app.read_bytes()]
    assert all(not path.exists() for path in transport.install_paths)


@pytest.mark.parametrize(
    "api",
    [
        "",
    ],
    ids=['unknown-api'],
)
def test_unknown_api_rejects_app_before_submission(packages, tmp_path, api):
    _, device, transport, _, hap = packages
    transport.devices[device.profile.serial].api_version = api
    app = make_app(tmp_path / "release.app", [("entry.hap", hap.read_bytes())])
    with pytest.raises(DeviceConfigurationError, match="device API"):
        device.install_app([app])
    assert transport.submissions == 0


@pytest.mark.parametrize(
    "name",
    [
        "../entry.hap",
    ],
    ids=['parent-traversal'],
)
def test_app_rejects_unsafe_members(tmp_path, name):
    hap = make_hap(tmp_path / "source.hap")
    app = make_app(tmp_path / "release.app", [(name, hap.read_bytes())])
    with pytest.raises(DeviceConfigurationError, match="Unsafe"):
        package_metadata(app)


def test_app_contract_describes_formats_and_rejects_conflicting_sources(packages):
    _, _, transport, _, hap = packages
    code, output, _ = _run_cli(["schema", "--json"], env={})
    assert code == 0
    options = json.loads(output)["data"]["commands"]["apps"]["subcommands"]["install"]["options"]
    assert options["--package"]["repeatable"] is True
    assert ".app" in options["--package"]["description"]
    code, output, _ = _run_cli(["plugins", "describe", "app.install", "--json"], env={})
    assert code == 0
    assert ".app" in output
    code, output, _ = _run_cli(
        ["apps", "install", "--package", str(hap), "--url", "https://packages.example.org/app",
         "--idempotency-key", "conflict", "--json"], transport=transport, env={},
    )
    assert code == 2
    assert json.loads(output)["error"]["code"] == "configuration_error"
    assert transport.calls == []


def test_app_modified_during_staging_stays_unknown_without_replay(packages, tmp_path, monkeypatch):
    runner, device, transport, params, hap = packages
    app = make_app(tmp_path / "release.app", [("entry.hap", hap.read_bytes())])
    params["packages"] = [{"path": str(app), "sha256": package_metadata(app)["sha256"]}]
    copyfile = shutil.copyfile

    def changed_copy(source, destination):
        copyfile(source, destination)
        with Path(destination).open("ab") as stream:
            stream.write(b"changed during staging")

    monkeypatch.setattr("moyu_harmony_pilot.actions.apps.shutil.copyfile", changed_copy)
    for _ in range(2):
        outcome = runner.run(device, "app.install", params, idempotency_key="staging-change")
        assert outcome.status == "unknown"
    assert transport.submissions == 0
