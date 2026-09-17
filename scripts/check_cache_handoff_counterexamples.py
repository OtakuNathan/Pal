"""Run the handoff abstraction and assert that each deliberately broken rule fails.

Usage: python scripts/check_cache_handoff_counterexamples.py path/to/tla2tools.jar
No provider access. Generated TLC traces stay in a temporary directory.
"""
import pathlib
import subprocess
import sys
import tempfile

root = pathlib.Path(__file__).resolve().parents[1]
jar = pathlib.Path(sys.argv[1]).resolve()
base = (root / "spec/llm/PromptCacheHandoff.cfg").read_text()
mutations = {
    "DropProtection": "Protection", "Optimistic": "SoundAck", "StaleOwner": "OwnerIsolation",
    "IgnoreM": "SoundAck", "ClearThird": "ThirdResponse", "HiddenMarker": "SoundAck",
    "CircularEvidence": "SoundAck",
}
with tempfile.TemporaryDirectory(prefix="cache-handoff-tlc-") as tmp:
    (pathlib.Path(tmp) / "PromptCacheHandoff.tla").write_text((root / "spec/llm/PromptCacheHandoff.tla").read_text())
    for mutation, invariant in mutations.items():
        cfg = base.replace(f"{mutation} = FALSE", f"{mutation} = TRUE")
        cfg = cfg[:cfg.index("INVARIANTS")] + f"INVARIANT {invariant}\nCHECK_DEADLOCK FALSE\n"
        path = pathlib.Path(tmp) / f"{mutation}.cfg"
        path.write_text(cfg)
        result = subprocess.run(["java", "-XX:+UseParallelGC", "-cp", str(jar), "tlc2.TLC",
            "-cleanup", "-config", path.name, "PromptCacheHandoff.tla"],
            cwd=tmp, capture_output=True, text=True)
        if f"Invariant {invariant} is violated" not in result.stdout:
            raise SystemExit(f"{mutation}: expected counterexample missing\n{result.stdout}\n{result.stderr}")
        print(f"{mutation}: expected {invariant} counterexample")

base = (root / "spec/llm/PromptCacheSettlement.cfg").read_text()
with tempfile.TemporaryDirectory(prefix="cache-settlement-tlc-") as tmp:
    (pathlib.Path(tmp) / "PromptCacheSettlement.tla").write_text(
        (root / "spec/llm/PromptCacheSettlement.tla").read_text())
    for mutation, invariant in {"ClearAllOnAck": "Conservation", "DropLateEstimate": "Conservation",
                                "DoubleCharge": "BillingIdempotent", "ClearOnAnchor": "Conservation"}.items():
        cfg = base.replace(f"{mutation} = FALSE", f"{mutation} = TRUE")
        cfg = cfg[:cfg.index("INVARIANTS")] + f"INVARIANT {invariant}\nCHECK_DEADLOCK FALSE\n"
        path = pathlib.Path(tmp) / f"{mutation}.cfg"
        path.write_text(cfg)
        result = subprocess.run(["java", "-XX:+UseParallelGC", "-cp", str(jar), "tlc2.TLC",
            "-cleanup", "-config", path.name, "PromptCacheSettlement.tla"],
            cwd=tmp, capture_output=True, text=True)
        if f"Invariant {invariant} is violated" not in result.stdout:
            raise SystemExit(f"{mutation}: expected counterexample missing\n{result.stdout}\n{result.stderr}")
        print(f"{mutation}: expected {invariant} counterexample")

base = (root / "spec/llm/PromptCacheFixedAnchors.cfg").read_text()
with tempfile.TemporaryDirectory(prefix="cache-fixed-tlc-") as tmp:
    (pathlib.Path(tmp) / "PromptCacheFixedAnchors.tla").write_text(
        (root / "spec/llm/PromptCacheFixedAnchors.tla").read_text())
    for mutation, invariant in {"SkipFixed": "FixedMarkers", "UnprovenBase": "CoveredBase"}.items():
        cfg = base.replace(f"{mutation} = FALSE", f"{mutation} = TRUE")
        cfg = cfg[:cfg.index("INVARIANTS")] + f"INVARIANT {invariant}\nCHECK_DEADLOCK FALSE\n"
        path = pathlib.Path(tmp) / f"{mutation}.cfg"
        path.write_text(cfg)
        result = subprocess.run(["java", "-XX:+UseParallelGC", "-cp", str(jar), "tlc2.TLC",
            "-cleanup", "-config", path.name, "PromptCacheFixedAnchors.tla"],
            cwd=tmp, capture_output=True, text=True)
        if f"Invariant {invariant} is violated" not in result.stdout:
            raise SystemExit(f"{mutation}: expected counterexample missing\n{result.stdout}\n{result.stderr}")
        print(f"{mutation}: expected {invariant} counterexample")
