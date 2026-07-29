from __future__ import annotations

from types import SimpleNamespace
import unittest

from pal.control.contracts import ControlRoute
from pal.control.interactions import build_think_panel_interaction
from pal.llm.contracts import ThinkingChoice, ThinkingContract
from pal.llm.llm_adaptor.openai_chat import CodexBridgeProvider
from pal.llm.llm_adaptor.zai_glm import ZaiGLMProvider


def _glm_endpoint(*, thinking_contract: object | None = None) -> SimpleNamespace:
    capabilities: dict[str, object] = {"supports_thinking": True}
    if thinking_contract is not None:
        capabilities["thinking_contract"] = thinking_contract
    return SimpleNamespace(
        endpoint_id="glm",
        provider="zhipu",
        model_id="glm-5.2",
        api_mode="openai_chat",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        supports_reasoning=True,
        capabilities_blob=capabilities,
    )


class ThinkingContractTests(unittest.TestCase):
    def test_glm_exposes_only_effective_provider_choices(self) -> None:
        contract = ZaiGLMProvider(_glm_endpoint()).thinking_contract()

        self.assertIsNotNone(contract)
        assert contract is not None
        self.assertEqual([choice.choice_id for choice in contract.choices], ["off", "high", "max"])
        self.assertEqual(contract.resolve("balanced"), "high")
        self.assertEqual(contract.resolve("xhigh"), "max")

    def test_endpoint_can_narrow_but_not_extend_provider_choices(self) -> None:
        narrowed = ZaiGLMProvider(
            _glm_endpoint(
                thinking_contract={
                    "default": "high",
                    "choices": [
                        "off",
                        {"id": "high", "label": "focused", "aliases": ["careful"]},
                    ],
                }
            )
        ).thinking_contract()

        self.assertIsNotNone(narrowed)
        assert narrowed is not None
        self.assertEqual([choice.label for choice in narrowed.choices], ["off", "focused"])
        self.assertEqual(narrowed.resolve("careful"), "high")
        with self.assertRaisesRegex(ValueError, "not supported"):
            ZaiGLMProvider(
                _glm_endpoint(
                    thinking_contract={
                        "default": "ultra",
                        "choices": ["ultra"],
                    }
                )
            ).thinking_contract()

    def test_openai_max_choice_is_model_or_endpoint_capability_driven(self) -> None:
        base = {
            "endpoint_id": "codex",
            "provider": "codex_cli",
            "api_mode": "openai_chat",
            "base_url": "codex://cli",
            "supports_reasoning": True,
        }
        ordinary = CodexBridgeProvider(
            SimpleNamespace(**base, model_id="gpt-5.5", capabilities_blob={})
        ).thinking_contract()
        max_model = CodexBridgeProvider(
            SimpleNamespace(**base, model_id="gpt-5.6", capabilities_blob={})
        ).thinking_contract()
        explicit = CodexBridgeProvider(
            SimpleNamespace(
                **base,
                model_id="custom",
                capabilities_blob={"supports_max_reasoning_effort": True},
            )
        ).thinking_contract()

        assert ordinary is not None
        assert max_model is not None
        assert explicit is not None
        self.assertIsNone(ordinary.resolve("max"))
        self.assertEqual(max_model.resolve("max"), "max")
        self.assertEqual(explicit.resolve("maximum"), "max")

    def test_contract_rejects_ambiguous_aliases(self) -> None:
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            ThinkingContract(
                choices=(
                    ThinkingChoice("low", "low", aliases=("fast",)),
                    ThinkingChoice("high", "high", aliases=("fast",)),
                ),
                default_choice_id="low",
            )

    def test_think_panel_renders_provider_choices_only(self) -> None:
        route = ControlRoute(
            endpoint_id="socket",
            channel_kind="socket",
            reply_target={},
            control_scope_key="scope",
        )
        panel = build_think_panel_interaction(
            route,
            {
                "endpoint_id": "glm",
                "current": "max",
                "choices": [
                    {"id": "off", "label": "off"},
                    {"id": "high", "label": "high"},
                    {"id": "max", "label": "max"},
                ],
            },
        )

        buttons = [button for row in panel.buttons for button in row]
        self.assertEqual([button.action_args.get("think_level") for button in buttons[:-1]], ["off", "high", "max"])
        self.assertEqual(buttons[2].label, "> max")


if __name__ == "__main__":
    unittest.main()
