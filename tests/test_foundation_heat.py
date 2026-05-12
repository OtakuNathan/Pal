from __future__ import annotations

import unittest

from pal.foundation import HeatLevel, HeatPolicy, HeatStateMachine, HeatStateRegistry


class HeatStateMachineTests(unittest.TestCase):
    def test_hot_ghost_dormant_cycle(self) -> None:
        machine = HeatStateMachine(HeatPolicy(hot_ttl=1, ghost_ttl=1, max_renewal_count=3))

        promoted = machine.promote_to_hot("item")
        self.assertEqual(promoted.event, "hot_promoted")
        self.assertEqual(promoted.state.heat_level, HeatLevel.HOT)

        ghosted = machine.tick(promoted.state)
        self.assertEqual(ghosted.event, "hot_to_ghost")
        self.assertEqual(ghosted.state.heat_level, HeatLevel.GHOST)

        reactivated = machine.promote_to_hot("item", ghosted.state)
        self.assertEqual(reactivated.event, "ghost_reactivated")
        self.assertEqual(reactivated.state.renewal_count, 1)
        self.assertEqual(reactivated.state.heat_level, HeatLevel.HOT)

        dormant = machine.tick(machine.tick(reactivated.state).state)
        self.assertEqual(dormant.event, "ghost_to_dormant")
        self.assertIsNone(dormant.state)
        self.assertTrue(dormant.expired)

    def test_registry_tracks_hot_keys_and_expiry(self) -> None:
        registry = HeatStateRegistry(machine=HeatStateMachine(HeatPolicy(hot_ttl=1, ghost_ttl=1)))

        registry.promote_to_hot("a")
        registry.promote_to_hot("b")
        self.assertEqual(registry.hot_keys(), ("a", "b"))

        transitions = registry.tick()
        self.assertEqual([item.event for item in transitions], ["hot_to_ghost", "hot_to_ghost"])
        self.assertEqual(registry.hot_keys(), ())
        self.assertEqual(registry.get("a").heat_level, HeatLevel.GHOST)

        expired = registry.tick()
        self.assertEqual([item.key for item in expired if item.expired], ["a", "b"])
        self.assertIsNone(registry.get("a"))


if __name__ == "__main__":
    unittest.main()
