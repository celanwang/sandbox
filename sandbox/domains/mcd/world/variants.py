"""按种子生成场景变体。

同一个场景，种子不同，价格、配送费、券面额就不同；种子相同，结果完全一样。
目的是让模型学会“按工具返回的数字算”，而不是背下沙箱里的固定价格。

种子为 0 时不做任何改动：预设题目和测试题单里写的金额都基于种子 0。

变体保留场景定义的打包费和免配送费规则。
"""
from __future__ import annotations

import random
import re

from sandbox.domains.mcd.world.pricing import fen_of, yuan

PRICE_STEPS_FEN = [-200, -100, -50, 0, 50, 100, 200]
DELIVERY_FEES_FEN = [500, 600, 700, 800, 900, 1000]
COUPON_DISCOUNTS_FEN = [300, 400, 500, 600]


def apply_variant(world: dict, seed: int) -> dict:
    if not seed:
        return world
    rng = random.Random(f"{world.get('scene_id')}:{seed}")
    changes = {"seed": seed, "prices": {}, "stores": {}, "coupons": {}}
    steps: dict[str, int] = {}   # 同一餐品在各门店按同样幅度变，保持“门店之间价格关系”不变
    for menu_id in sorted(world.get("menus", {})):
        menu = world["menus"][menu_id]
        for code in sorted(menu["meals"]):
            meal = menu["meals"][code]
            step = steps.setdefault(code, rng.choice(PRICE_STEPS_FEN))
            old = fen_of(meal["currentPrice"])
            new = max(100, old + step)
            meal["currentPrice"] = yuan(new)
            if "originalPrice" in meal:
                meal["originalPrice"] = yuan(max(new, fen_of(meal["originalPrice"]) + (new - old)))
            changes["prices"].setdefault(code, [yuan(old), yuan(new)])
    for store in world.get("stores", []):
        store["deliveryFeeFen"] = rng.choice(DELIVERY_FEES_FEN)
        changes["stores"][store["storeCode"]] = {"deliveryFeeFen": store["deliveryFeeFen"]}
    for coupon in world.get("coupons", []):
        coupon["discountFen"] = rng.choice(COUPON_DISCOUNTS_FEN)
        coupon["title"] = re.sub(r"立减[\d.]+元", f"立减{yuan(coupon['discountFen'])}元", coupon["title"])
        changes["coupons"][coupon["couponId"]] = coupon["discountFen"]
    world["variant"] = changes
    return world
