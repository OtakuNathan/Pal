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
        ("pal.minion", "family_templates/general.toml"),
        ("pal.minion", "family_templates/software_engineering.toml"),
        ("pal.minion", "family_templates/lifestyle.toml"),
        ("pal.minion", "profile_templates/generic.toml"),
        ("pal.minion", "profile_templates/general/requirements_analyst.toml"),
        ("pal.minion", "profile_templates/general/researcher.toml"),
        ("pal.minion", "profile_templates/general/contract_planner.toml"),
        ("pal.minion", "profile_templates/general/architecture_reviewer.toml"),
        ("pal.minion", "profile_templates/general/verifier.toml"),
        ("pal.minion", "profile_templates/software_engineering/v2_requirements_analyst.toml"),
        ("pal.minion", "profile_templates/software_engineering/v2_researcher.toml"),
        ("pal.minion", "profile_templates/software_engineering/v2_contract_planner.toml"),
        ("pal.minion", "profile_templates/software_engineering/v2_architecture_reviewer.toml"),
        ("pal.minion", "profile_templates/software_engineering/v2_coder.toml"),
        ("pal.minion", "profile_templates/software_engineering/v2_verifier.toml"),
        ("pal.minion", "profile_templates/software_engineering/v2_reviewer.toml"),
        ("pal.minion", "profile_templates/lifestyle/requirements_analyst.toml"),
        ("pal.minion", "profile_templates/lifestyle/researcher.toml"),
        ("pal.minion", "profile_templates/lifestyle/contract_planner.toml"),
        ("pal.minion", "profile_templates/lifestyle/architecture_reviewer.toml"),
        ("pal.minion", "profile_templates/lifestyle/nutrition_checkin_producer.toml"),
        ("pal.minion", "profile_templates/lifestyle/verifier.toml"),
        ("pal.plugins_builtin.lsp", "plugin.toml"),
        ("pal.plugins_builtin.lsp", "runtime.py"),
        ("pal.plugins_builtin.minion", "plugin.toml"),
    )

    for package, relative_path in required_files:
        assert resources.files(package).joinpath(relative_path).is_file()

    assert not resources.files("pal.minion").joinpath("profile_templates/software_engineering/planner.toml").is_file()
