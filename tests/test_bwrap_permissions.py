"""Directory grants can be used without a Bunshin invocation or editor."""
import shutil
import subprocess

import pytest

from pal.foundation.bwrap_permissions import DirectoryPermissions


def test_directory_grants_work_with_an_ordinary_process(tmp_path):
    bwrap = shutil.which("bwrap")
    if not bwrap:
        pytest.skip("bubblewrap is not available")
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "contract").write_text("interface")
    (tmp_path / "sibling").write_text("readable")
    policy = DirectoryPermissions(
        read_only=True,
        writable_directories=("shared",),
        editable_files=("single", "shared/member", "shared/contract"),
        protected_paths=("shared/contract",),
    )
    args = [bwrap, "--unshare-all", "--die-with-parent", "--ro-bind", "/", "/"]
    assert policy.mount_into(args, tmp_path) == {"single": tmp_path / "single"}
    result = subprocess.run([
        *args, "--chdir", str(tmp_path), "--", "/bin/sh", "-ec",
        'test "$(cat sibling)" = readable\n'
        'echo change > shared/member\n'
        'for path in sibling single shared/contract; do\n'
        '    if (echo change > "$path") 2>/dev/null; then exit 1; fi\n'
        'done',
    ], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert (shared / "member").read_text() == "change\n"
    assert (shared / "contract").read_text() == "interface"
    assert not (tmp_path / "single").exists()


@pytest.mark.parametrize("invalid", ["../outside", "/absolute", "link/child"])
def test_invalid_grant_is_rejected_before_creating_directories(tmp_path, invalid):
    (tmp_path / "link").symlink_to(tmp_path, target_is_directory=True)
    policy = DirectoryPermissions(
        read_only=True, writable_directories=("new",), editable_files=(invalid,),
    )
    args = []
    with pytest.raises(ValueError):
        policy.mount_into(args, tmp_path)
    assert not args
    assert not (tmp_path / "new").exists()
