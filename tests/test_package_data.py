from __future__ import annotations

from importlib import resources


def test_required_runtime_package_data_is_available() -> None:
    required_files = (
        ("pal.core", "tool_surface.toml"),
        ("pal.mcp", "templates/stdio_server.toml"),
        ("pal.minion", "profile_templates/generic.toml"),
        ("pal.plugins_builtin.minion", "plugin.toml"),
    )

    for package, relative_path in required_files:
        assert resources.files(package).joinpath(relative_path).is_file()
