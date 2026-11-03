"""JOLTS strategy retains execution results and reports actual process lifecycle."""

import json
import os

from market_data_foundation.historical.cpi_auto_strategy import CpiAutoStrategy
from market_data_foundation.historical.jolts_release_schedule import POLICY


class JoltsAutoStrategy(CpiAutoStrategy):
    def get(self):
        value = super().get()
        path = self.strategy_path.with_name("jolts-worker.json")
        if path.exists():
            try:
                process = json.loads(path.read_text())
                value["worker"]["process"] = process
                if process["state"] == "stopped":
                    value["worker"]["state"] = "stopped"
                else:
                    try:
                        os.kill(process["pid"], 0)
                    except ProcessLookupError:
                        value["worker"]["state"] = "stopped"
            except OSError, ValueError, KeyError, TypeError:
                value["worker"]["state"] = "unknown"
        return {**value, "policy": POLICY}

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
