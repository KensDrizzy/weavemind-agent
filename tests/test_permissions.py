from permissions.policy import PermissionPolicy
from permissions.modes import PermissionMode


def test_default_mode_allows_all():
    policy = PermissionPolicy()
    assert policy.can_use("Read", PermissionMode.DEFAULT)
    assert policy.can_use("Bash", PermissionMode.DEFAULT)


def test_disallowed_tool():
    policy = PermissionPolicy(disallowed=["Bash"])
    assert not policy.can_use("Bash", PermissionMode.DEFAULT)


def test_permit_mode():
    policy = PermissionPolicy(allowed=["Read"])
    assert policy.can_use("Read", PermissionMode.PERMIT)
    assert not policy.can_use("Bash", PermissionMode.PERMIT)


def test_bypass_mode():
    policy = PermissionPolicy(disallowed=["Bash"])
    assert policy.can_use("Bash", PermissionMode.BYPASS)
