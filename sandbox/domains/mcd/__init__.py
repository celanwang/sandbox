"""Synthetic McDelivery adapter."""

import json
from pathlib import Path

from sandbox.episodes import Episode, Fault, Outcome
from sandbox.domains.mcd.handlers import RawText, dispatch
from sandbox.domains.mcd.world import list_scenes, load_scene
from sandbox.domains.mcd.world.variants import apply_variant


class McDelivery:
    name = "mcd"
    tools = json.loads(Path(__file__).with_name("tools.json").read_text(encoding="utf-8"))
    business_faults = frozenset({"SOLD_OUT", "STORE_CLOSED", "COUPON_INVALID"})

    def scenes(self) -> list[str]:
        return list_scenes()

    def open(self, scene: str, seed: int) -> dict:
        world = apply_variant(load_scene(scene), seed)
        world.update(orders=[], quotes=[], invalidCoupons=[], soldOut=[])
        return world

    def state(self, world: dict) -> dict[str, dict]:
        return {
            "clock": {"now": world["clock"]},
            "addresses": {row["addressId"]: row for row in world["addresses"]},
            "stores": {row["storeCode"]: row for row in world["stores"]},
            "menus": world["menus"],
            "coupons": {row["couponId"]: row for row in world["coupons"]},
            "soldOut": dict.fromkeys(world["soldOut"], True),
            "invalidCoupons": dict.fromkeys(world["invalidCoupons"], True),
            "quotes": {str(row["callSeq"]): row for row in world["quotes"]},
            "orders": {row["orderId"]: row for row in world["orders"]},
        }

    def fault(self, world: dict, fault: Fault) -> None:
        target = fault.target
        if fault.type == "SOLD_OUT" and any(target in menu["meals"] for menu in world["menus"].values()):
            if target not in world["soldOut"]:
                world["soldOut"].append(target)
            return
        if fault.type == "STORE_CLOSED":
            for store in world["stores"]:
                if store["storeCode"] == target:
                    store["forceClosed"] = True
                    return
        if fault.type == "COUPON_INVALID" and any(coupon["couponId"] == target for coupon in world["coupons"]):
            if target not in world["invalidCoupons"]:
                world["invalidCoupons"].append(target)
            return
        raise ValueError(f"unknown target for {fault.type}: {target!r}")

    def call(self, episode: Episode, tool: str, arguments: dict) -> Outcome:
        result = dispatch(episode, tool, arguments)
        if isinstance(result, RawText):
            return Outcome(result.success, result.text, result.text)
        return Outcome(result["success"], result, json.dumps(result, ensure_ascii=False, separators=(",", ":")))
