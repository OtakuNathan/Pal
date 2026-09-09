from pathlib import Path


def check(context):
    from pal.web_fetch.provisioning import inspect
    return {"ok": True, "dependencies": inspect(Path(context["runtime_root"]))}


def prepare(context):
    from pal.web_fetch.provisioning import prepare as prepare_browser
    return prepare_browser(Path(context["runtime_root"]))


def verify(context):
    from pal.web_fetch.provisioning import verify as verify_browser
    return verify_browser(Path(context["runtime_root"]))
