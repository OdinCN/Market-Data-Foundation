"""JOLTS strategy retains execution results and reports actual process lifecycle."""

from market_data_foundation.historical.cpi_auto_strategy import CpiAutoStrategy
from market_data_foundation.historical.jolts_release_schedule import POLICY


class JoltsAutoStrategy(CpiAutoStrategy):
    def get(self):
        return {**super().get(), "policy": POLICY}

    def tick(self):
        if not self.get()["enabled"]:
            return {"action": "paused", **self.heartbeat("paused")}
        self.heartbeat("running")
        try:
            result = self.scheduler.due_check()
        except Exception:
            self.heartbeat("error")
            raise
        self.heartbeat("idle")
        return {**result, "strategy": self.get()}
