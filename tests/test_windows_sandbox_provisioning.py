"""Offline patch/provisioning-contract tests. Never provisions machine accounts."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "windows_owner_patcher", ROOT / "desktop/scripts/patch-windows-sandbox-source.py",
)
patcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patcher)


@pytest.fixture(scope="module")
def patched():
    # These unchanged pinned sources are tracked, so a clean offline checkout
    # exercises the same real anchors as the production patcher.
    fixture = ROOT / "tests/fixtures" / f"codex-windows-sandbox-v{patcher.UPSTREAM_VERSION}" / "src"
    source = Path("fixture/codex-rs/windows-sandbox-rs/src")
    texts = {source / path.relative_to(fixture): path.read_text(encoding="utf-8") for path in fixture.rglob("*.rs")}
    assert source / "setup.rs" in texts
    for path, original in list(texts.items()):
        text = original
        for old, new in patcher.IDENTITY_REPLACEMENTS:
            text = text.replace(old, new)
        if path.name in {"wfp.rs", "filter_specs.rs"}:
            def guid(match):
                seed = match.group(1).replace("_", "")
                return f"GUID::from_u128(0x{patcher.uuid.uuid5(patcher.NAMESPACE, f'windows-sandbox/wfp/{seed}').hex})" if len(seed) == 32 else match.group(0)
            text = patcher.GUID_RE.sub(guid, text)
        texts[path] = text
    texts[source / "wfp/filter_specs.rs"] = patcher._add_full_network_block(texts[source / "wfp/filter_specs.rs"])
    patcher._owner_namespace_edits(source, texts)
    return {p.relative_to(source).as_posix(): value for p, value in texts.items()}


def test_all_wfp_mutation_targets_are_owner_scoped(patched):
    code = patched["wfp.rs"]
    assert "delete_filter_if_present(engine.handle, &spec.key)" not in code
    assert "filterKey: spec.key" not in code
    assert code.count("delete_filter_if_present(engine.handle, &crate::owner_identity::owner().guid(spec.key))") == 2
    assert "filterKey: crate::owner_identity::owner().guid(spec.key)" in code
    assert "FwpmSubLayerDeleteByKey0(engine.handle, &sublayer_key())" in code
    assert "FwpmProviderDeleteByKey0(engine.handle, &provider_key())" in code
    assert "subLayerKey: sublayer_key()" in code
    assert patched["wfp/filter_specs.rs"].count("FilterSpec {") == 15  # type + 14 filters


def test_setup_cannot_reset_arbitrary_or_legacy_accounts(patched):
    code = patched["setup_provisioning.rs"]
    assert code.index("bind_for_user") < code.index("acquire_sandbox_setup_lock(INFINITE)")
    assert "setup payload accounts do not match its owner" in code
    assert code.index("save_installation(&crate::InstallationRecord") < code.index("run_payload(&payload)")
    users = patched["setup_provisioning/sandbox_users.rs"]
    assert users.index("status == NERR_UserExists") < users.index("let upd = NetUserSetInfo")
    assert users.index("existing local account does not belong to sandbox owner") < users.index("let upd = NetUserSetInfo")
    assert "MiniCode sandbox owner {}" in users
    assert "fn offline_username()" in patched["setup.rs"]
    assert "pub const OFFLINE_USERNAME" not in patched["setup.rs"]


def test_dpapi_acl_and_wfp_receipt_commit_order(patched):
    users = patched["setup_provisioning/sandbox_users.rs"]
    assert users.index("super::lock_persistent_sandbox_dirs(payload") < users.index("let offline_password = random_password")
    code = patched["setup_provisioning.rs"]
    secrets_acl = code[code.index("&sandbox_secrets_dir(&payload.codex_home)"):]
    assert secrets_acl.index("DaclInheritance::Protected") < secrets_acl.index('"lock sandbox secrets dir')
    assert code.index("wfp_result?;") < code.index("if repairing_disabled_accounts {", code.index("wfp_result?;"))
    assert "owner_id: crate::owner_identity::owner().owner_id.clone()" in users


def test_service_runner_registration_and_uninstall_share_namespace(patched):
    service = patched["service_identity.rs"]
    assert 'Ok("MiniCodeSandboxService".into())' not in service
    assert "crate::owner_identity::owner().service" in service
    assert "crate::owner_identity::owner().pipe" in service
    assert "pub owner_sid: String" in patched["elevated/ipc_framed.rs"]
    assert "IPC_PROTOCOL_VERSION: u8 = 7" in patched["elevated/ipc_framed.rs"]
    assert "bind_windows_sandbox_runner(&req.real_codex_home, &req.owner_sid)" in patched["bin/command_runner/win.rs"]
    for file in ["installation_record.rs", "runtime_ownership.rs"]:
        assert "SOFTWARE\\OpenAI\\Codex" not in patched[file]
    assert "firewall_name(base)" in patched["uninstall_windows/firewall.rs"]
    assert "cleanup home does not match sandbox owner" in patched["uninstall_windows.rs"]
    assert 'remove_sandbox_principal("MiniCodeSandboxUsers")' not in patched["uninstall_windows.rs"]
    assert 'name == crate::winutil::sandbox_users_group()' in patched["uninstall_windows/principals.rs"]
    assert "windows_sandbox_read_acl_mutex()" in patched["bin/command_runner/win.rs"]


def test_native_owner_authority_does_not_use_environment_or_legacy_fallback():
    source = (ROOT / "desktop/native-windows-sandbox/owner_identity.rs").read_text(encoding="utf-8")
    main = (ROOT / "desktop/native-windows-sandbox/src/main.rs").read_text(encoding="utf-8")
    assert 'std::env::var("USERNAME")' not in main
    assert "current_setup_user()?" in main
    assert "users.owner_id == identity.owner_id" in source
    assert "marker.owner_id == identity.owner_id" in source
    assert "runtime_ownership::current_setup_user()?" in source
    assert "std::env" not in source


def test_anchored_patch_is_idempotent_and_refuses_changed_contract(patched):
    source = Path("fixture/codex-rs/windows-sandbox-rs/src")
    texts = {source / name: code for name, code in patched.items()}
    before = dict(texts)
    patcher._owner_namespace_edits(source, texts)
    assert texts == before
    texts[source / "setup.rs"] = texts[source / "setup.rs"].replace(
        'pub const SETUP_VERSION: u32 = 6;', 'pub const SETUP_VERSION: u32 = 99;',
    )
    with pytest.raises(RuntimeError, match="Owner namespace anchor changed"):
        patcher._owner_namespace_edits(source, texts)
    # The edit planner operates only on a dictionary, never partial filesystem writes.
    assert all(not path.exists() for path in before)


def test_prepare_uses_queried_group_and_build_never_provisions():
    prepare = (ROOT / "scripts/prepare_windows_native_sandbox.py").read_text(encoding="utf-8")
    build = (ROOT / "desktop/scripts/prepare-windows-sandbox-runtime.ps1").read_text(encoding="utf-8")
    assert "_identity(home, runtime)['group']" in prepare
    assert '"MiniCodeSandboxUsers:(OI)(CI)(RX)"' not in prepare
    assert "--offline" in build
    assert "Remove-Item" not in build
    assert '"sandbox",' not in build
    assert '"setup",' not in build
