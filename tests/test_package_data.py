from __future__ import annotations

from importlib import resources


def test_required_runtime_package_data_is_available() -> None:
    required_files = (
        ("pal.core", "tool_surface.toml"),
        ("pal.lsp", "server_templates/clangd.toml"),
        ("pal.lsp", "server_templates/csharp.toml"),
        ("pal.lsp", "server_templates/css.toml"),
        ("pal.lsp", "server_templates/go.toml"),
        ("pal.lsp", "server_templates/html.toml"),
        ("pal.lsp", "server_templates/java.toml"),
        ("pal.lsp", "server_templates/json.toml"),
        ("pal.lsp", "server_templates/lua.toml"),
        ("pal.lsp", "server_templates/pyright.toml"),
        ("pal.lsp", "server_templates/rust.toml"),
        ("pal.lsp", "server_templates/shell.toml"),
        ("pal.lsp", "server_templates/typescript.toml"),
        ("pal.lsp", "server_templates/yaml.toml"),
        ("pal.mcp", "templates/stdio_server.toml"),
        ("pal.minion", "profile_templates/generic.toml"),
        ("pal.minion", "profile_templates/software_engineering/writer.toml"),
        ("pal.plugins_builtin.lsp", "plugin.toml"),
        ("pal.plugins_builtin.lsp", "runtime.py"),
        ("pal.plugins_builtin.minion", "plugin.toml"),
    )

    for package, relative_path in required_files:
        assert resources.files(package).joinpath(relative_path).is_file()
