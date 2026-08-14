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
        ("pal.bunshin", "family_templates/general.toml"),
        ("pal.bunshin", "family_templates/software_engineering.toml"),
        ("pal.bunshin", "family_templates/lifestyle.toml"),
        ("pal.bunshin", "profile_templates/generic.toml"),
        ("pal.bunshin", "profile_templates/general/architect.toml"),
        ("pal.bunshin", "profile_templates/general/reviewer.toml"),
        ("pal.bunshin", "profile_templates/general/verifier.toml"),
        ("pal.bunshin", "profile_templates/software_engineering/v2_architect.toml"),
        ("pal.bunshin", "profile_templates/software_engineering/v2_coder.toml"),
        ("pal.bunshin", "profile_templates/software_engineering/v2_verifier.toml"),
        ("pal.bunshin", "profile_templates/software_engineering/v2_reviewer.toml"),
        ("pal.bunshin", "profile_templates/lifestyle/architect.toml"),
        ("pal.bunshin", "profile_templates/lifestyle/nutritionist.toml"),
        ("pal.bunshin", "profile_templates/lifestyle/reviewer.toml"),
        ("pal.bunshin", "architecture_templates/base/schema.json"),
        ("pal.bunshin", "architecture_templates/base/architect.yaml.j2"),
        ("pal.bunshin", "architecture_specializations/general.v1/specialization.json"),
        ("pal.bunshin", "architecture_specializations/general.v1/preamble.j2"),
        ("pal.bunshin", "architecture_specializations/general.v1/context.j2"),
        ("pal.bunshin", "architecture_specializations/general.v1/module_definition.j2"),
        ("pal.bunshin", "architecture_specializations/lifestyle.nutrition_checkin.v1/specialization.json"),
        ("pal.bunshin", "architecture_specializations/lifestyle.nutrition_checkin.v1/preamble.j2"),
        ("pal.bunshin", "architecture_specializations/lifestyle.nutrition_checkin.v1/context.j2"),
        ("pal.bunshin", "architecture_specializations/lifestyle.nutrition_checkin.v1/module_definition.j2"),
        ("pal.bunshin", "architecture_specializations/software_engineering.v1/specialization.json"),
        ("pal.bunshin", "architecture_specializations/software_engineering.v1/preamble.j2"),
        ("pal.bunshin", "architecture_specializations/software_engineering.v1/context.j2"),
        ("pal.bunshin", "architecture_specializations/software_engineering.v1/module_definition.j2"),
        ("pal.plugins_builtin.lsp", "plugin.toml"),
        ("pal.plugins_builtin.lsp", "runtime.py"),
        ("pal.plugins_builtin.bunshin", "plugin.toml"),
    )

    for package, relative_path in required_files:
        assert resources.files(package).joinpath(relative_path).is_file()

    assert not resources.files("pal.bunshin").joinpath("profile_templates/software_engineering/planner.toml").is_file()
    assert not resources.files("pal.bunshin").joinpath(
        "profile_templates/software_engineering/v2_architecture_reviewer.toml"
    ).is_file()
