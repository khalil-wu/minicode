from backend.config import PermissionSettings
from backend.permissions.checker import PermissionChecker, check_permission_level
from backend.permissions.context import PermissionContext
from backend.tools.base import PermissionLevel


def test_python_dependency_install_stays_confirm_even_with_broad_allow_rule():
    checker = PermissionChecker(
        PermissionSettings(content_allow_rules=["run_command(*)"]),
    )

    assert check_permission_level(
        checker,
        "run_command",
        args={"command": "pip install torch"},
        context=PermissionContext(mode="auto"),
    ) == PermissionLevel.AUTO

    assert check_permission_level(
        checker,
        "run_command",
        args={"command": "pip list"},
        context=PermissionContext(mode="auto"),
    ) == PermissionLevel.AUTO
