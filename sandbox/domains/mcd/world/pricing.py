"""算价规则（纯函数）。

calculate-price 和 create-order 共用这里的逻辑，保证“报的价”和“下单的价”算法一致。

金额一律用“分”（整数）计算，只在组装返回时按官方格式转换：
算价返回分（整数），菜单和订单详情返回元（字符串）。

套餐子项默认使用详情里的预选数量。配送费和打包费由场景配置，
打包费封顶产生的差额计入 discount。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sandbox.domains.mcd import world as W

PACKING_CODE = "920174"
PACKING_NAME = "打包服务费"


class PriceError(ValueError):
    """商品、子项、券不合法。message 会原样返回给模型。"""


def yuan(fen: int) -> str:
    """分 -> 元字符串，去掉多余的 0：3800 -> "38"，3550 -> "35.5"。"""
    text = f"{fen / 100:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def fen_of(price_yuan: Any) -> int:
    return int(round(float(price_yuan) * 100))


_DIFF = re.compile(r"^\s*([+-])?\s*¥?\s*([\d.]+)\s*$")


def diff_fen(text: Any) -> int:
    """真实 diffPrice（"+ ¥0"、"+ ¥1.5"、"- ¥3"）-> 分。"""
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return int(round(float(text) * 100))
    m = _DIFF.match(str(text or "0"))
    if not m:
        raise ValueError(f"无法解析加价：{text!r}")
    value = int(round(float(m.group(2)) * 100))
    return -value if m.group(1) == "-" else value


@dataclass
class Line:
    product_code: str
    product_name: str
    quantity: int
    unit_fen: int                      # 含子项加价的单价
    choices: list[dict[str, Any]]      # [{round, code, name, quantity, extraFen}]
    coupon: dict | None = None
    discount_fen: int = 0
    modification: Any = None           # 单品特调，原样保存（算价不计特调加价）
    product_type: Any = None           # 菜单里的 productType，打包费按它区分

    @property
    def original_subtotal(self) -> int:
        return self.unit_fen * self.quantity

    @property
    def subtotal(self) -> int:
        return self.original_subtotal - self.discount_fen

    def key(self) -> dict:
        """用于比较“两次报价/下单的内容是否相同”的规范形式。"""
        return {
            "productCode": self.product_code,
            "quantity": self.quantity,
            "choices": sorted((c["round"], c["code"], c["quantity"]) for c in self.choices),
            "couponId": self.coupon["couponId"] if self.coupon else None,
        }


@dataclass
class Priced:
    lines: list[Line]
    delivery_original_fen: int
    delivery_fen: int
    packing_original_fen: int = 0
    packing_fen: int = 0
    tableware_fen: int = 0
    content_key: str = field(default="")

    @property
    def product_original(self) -> int:
        return sum(l.original_subtotal for l in self.lines)

    @property
    def product_price(self) -> int:
        return sum(l.subtotal for l in self.lines)

    @property
    def original_price(self) -> int:
        return self.product_original + self.delivery_original_fen + self.packing_original_fen + self.tableware_fen

    @property
    def price(self) -> int:
        return self.product_price + self.delivery_fen + self.packing_fen + self.tableware_fen

    @property
    def discount(self) -> int:
        return self.original_price - self.price


def _as_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise PriceError(f"参数 {name} 必须是整数")
    try:
        return int(value)
    except ValueError:
        raise PriceError(f"参数 {name} 必须是整数") from None


def parse_items(raw: Any) -> list[dict]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raise PriceError("items 必须是商品数组")
    if not isinstance(raw, list) or not raw:
        raise PriceError("缺少必填参数: items（商品列表不能为空）")
    if not all(isinstance(i, dict) for i in raw):
        raise PriceError("items 中每个商品必须是对象")
    return raw


def _resolve_choices(detail: dict | None, round_list: Any, product_code: str) -> tuple[list[dict], int]:
    """解析套餐子项。没传的子项组用详情里的预选项。返回 (选中的子项, 子项加价合计)。"""
    rounds = {str(r["id"]): r for r in (detail or {}).get("rounds") or []}
    if not rounds:
        if round_list:
            raise PriceError(f"商品 {product_code} 没有可选子项，不能传 roundList")
        return [], 0
    chosen: dict[str, list[dict]] = {}
    if round_list is not None and not isinstance(round_list, list):
        raise PriceError("roundList 必须是数组")
    for entry in round_list or []:
        if not isinstance(entry, dict):
            raise PriceError("roundList 中每一项必须是对象")
        rid = str(entry.get("round", ""))
        if rid not in rounds:
            raise PriceError(f"套餐 {product_code} 不存在子项组 round={rid}")
        if rid in chosen:
            raise PriceError(f"套餐子项组 round={rid} 重复")
        options = {str(o["code"]): o for o in rounds[rid]["choices"]}
        picked = []
        entries = entry.get("comboItemList")
        if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
            raise PriceError("comboItemList 必须是商品对象数组")
        for item in entries:
            code = str(item.get("code", ""))
            if code not in options:
                raise PriceError(f"子项 {code} 不属于套餐 {product_code} 的子项组 {rid}")
            qty = _as_int(item.get("quantity", 1), "comboItemList.quantity")
            if qty < 1:
                raise PriceError("comboItemList.quantity 必须大于 0")
            picked.append({"round": rid, "code": code, "name": options[code]["name"], "quantity": qty,
                           "extraFen": diff_fen(options[code].get("diffPrice")) * qty})
        chosen[rid] = picked
    result, extra = [], 0
    for rid, rdef in rounds.items():
        picked = chosen.get(rid)
        if picked is None:
            picked = _preselected(rid, rdef)
        total = sum(p["quantity"] for p in picked)
        need_min = int(rdef.get("minQuantity", rdef.get("quantity", 1)))
        need_max = int(rdef.get("maxQuantity", rdef.get("quantity", 1)))
        if not need_min <= total <= need_max:
            span = f"{need_min}" if need_min == need_max else f"{need_min}~{need_max}"
            raise PriceError(f"套餐 {product_code} 的子项组「{rdef['name']}」需要选择 {span} 份，实际 {total} 份")
        result.extend(picked)
        extra += sum(p["extraFen"] for p in picked)
    return result, extra


def _preselected(rid: str, rdef: dict) -> list[dict]:
    """详情里预选的子项：按 choice.quantity；都为 0 时退回 isDefault=1 的那一个。"""
    picked = [{"round": rid, "code": str(o["code"]), "name": o["name"], "quantity": int(o.get("quantity") or 0),
               "extraFen": diff_fen(o.get("diffPrice")) * int(o.get("quantity") or 0)}
              for o in rdef["choices"] if int(o.get("quantity") or 0) > 0]
    if not picked:
        o = next((o for o in rdef["choices"] if o.get("isDefault")), rdef["choices"][0])
        n = int(rdef.get("quantity", 1))
        picked = [{"round": rid, "code": str(o["code"]), "name": o["name"], "quantity": n,
                   "extraFen": diff_fen(o.get("diffPrice")) * n}]
    return picked


def coupon_valid(coupon: dict, now: datetime) -> bool:
    start, end = coupon.get("validFrom", "00:00"), coupon.get("validTo", "23:59")
    t = now.strftime("%H:%M")
    return start <= t <= end


def _resolve_coupon(world: dict, item: dict, product_code: str, now: datetime) -> dict | None:
    cid, ccode = item.get("couponId"), item.get("couponCode")
    if not cid and not ccode:
        return None
    coupon = next((c for c in world.get("coupons", [])
                   if (cid and c["couponId"] == str(cid)) or (not cid and c["couponCode"] == str(ccode))), None)
    if coupon is None or (ccode and coupon["couponCode"] != str(ccode)):
        raise PriceError("优惠券不存在或 couponId 与 couponCode 不匹配")
    if product_code not in coupon["appliesTo"]:
        raise PriceError(f"优惠券「{coupon['title']}」不适用于商品 {product_code}")
    if not coupon_valid(coupon, now) or coupon["couponId"] in world.get("invalidCoupons", []):
        raise PriceError(f"优惠券「{coupon['title']}」当前不可用")
    return coupon


def unit_packing_fen(cfg: dict, line: Line) -> int:
    """一份商品的打包费：实测过的按实测；没测过的按同类商品（productType）推断；再没有就用 defaultFen。"""
    by_product = cfg.get("byProduct") or {}
    if line.product_code in by_product:
        return int(by_product[line.product_code])
    by_type = cfg.get("byProductType") or {}
    if str(line.product_type) in by_type:
        return int(by_type[str(line.product_type)])
    return int(cfg.get("defaultFen", cfg.get("perUnitFen", 0)) or 0)


def packing_fee(store: dict, lines: list[Line]) -> tuple[int, int]:
    """打包费 (原价, 实收)。门店没有 packingFee 配置时不收。

    packingFee = {"byProduct": {"901401": 80, …}, "byProductType": {"1": 80, "3": 190, …}, "defaultFen": 190,
                  "capFen": 300, "capBelowProductFen": 35700}
    整单打包费最多收 capFen；但商品金额达到 capBelowProductFen 时不封顶（真实探针里观察到的例外，规则不明）。
    """
    cfg = store.get("packingFee") or {}
    if not cfg:
        return 0, 0
    original = sum(unit_packing_fen(cfg, l) * l.quantity for l in lines)
    if original <= 0:
        return 0, 0
    cap, below = cfg.get("capFen"), cfg.get("capBelowProductFen")
    product = sum(l.subtotal for l in lines)
    if cap is None or (below is not None and product >= int(below)):
        return original, original
    return original, min(original, int(cap))


def price_items(world: dict, store: dict, raw_items: Any, now: datetime, need_tableware: bool = False) -> Priced:
    """算价。商品不可售、售罄、子项不合法、券不可用都会抛 PriceError。"""
    items = parse_items(raw_items)
    menu = W.store_menu(world, store)
    sold_out = set(world.get("soldOut", []))
    lines: list[Line] = []
    for item in items:
        code = str(item.get("productCode", ""))
        if not code:
            raise PriceError("商品缺少 productCode")
        meal = menu["meals"].get(code)
        if meal is None:
            raise PriceError(f"商品 {code} 不存在")
        if not W.meal_available(menu, code, now):
            raise PriceError(f"商品「{meal['name']}」当前时段不供应")
        if code in sold_out:
            raise PriceError(f"商品「{meal['name']}」已售罄")
        qty = _as_int(item.get("quantity"), "quantity")
        if qty < 1:
            raise PriceError("商品数量必须大于 0")
        detail = menu.get("details", {}).get(code)
        choices, extra = _resolve_choices(detail, item.get("roundList"), code)
        coupon = _resolve_coupon(world, item, code, now)
        name = (detail or {}).get("name") or (menu.get("calcNames") or {}).get(code) or meal["name"]
        line = Line(code, name, qty, fen_of(meal["currentPrice"]) + extra, choices, coupon,
                    modification=item.get("modification"), product_type=meal.get("productType"))
        if coupon:
            line.discount_fen = min(coupon["discountFen"], line.unit_fen)  # 一张券只抵一份
        lines.append(line)
    if sum(1 for l in lines if l.coupon) > 1:
        raise PriceError("每单仅可使用一张优惠券")
    product_price = sum(l.subtotal for l in lines)
    delivery_original = int(store["deliveryFeeFen"])
    free_over = store.get("freeDeliveryOverFen")
    delivery = 0 if free_over is not None and product_price >= int(free_over) else delivery_original
    packing_original, packing = packing_fee(store, lines)
    tableware = world.get("tablewareFeeFen", 0) if need_tableware else 0
    priced = Priced(lines, delivery_original, delivery, packing_original, packing, tableware)
    priced.content_key = content_key(store["storeCode"], lines)
    return priced


def content_key(store_code: str, lines: list[Line]) -> str:
    keyed = sorted((l.key() for l in lines), key=lambda k: json.dumps(k, sort_keys=True))
    return json.dumps({"storeCode": store_code, "items": keyed}, ensure_ascii=False, sort_keys=True)


def calc_view(priced: Priced) -> dict:
    """calculate-price 的 data：字段和顺序按 2026-11-12 真实录制（单位：分）。

    外送真实返回里没有 takeWayList、mealAssistanceList、tablewarePrice；
    有打包费时 productList 末尾多一行打包服务费（quantity = 打包费原价 / 10，subtotal = 0）。
    """
    product_list = [
        {"productCode": l.product_code, "productName": l.product_name, "quantity": l.quantity,
         "originalSubtotal": l.original_subtotal, "subtotal": l.subtotal}
        for l in priced.lines
    ]
    if priced.packing_original_fen:
        product_list.append({"productCode": PACKING_CODE, "productName": PACKING_NAME,
                             "quantity": priced.packing_original_fen // 10,
                             "originalSubtotal": priced.packing_original_fen, "subtotal": 0})
    data = {
        "productOriginalPrice": priced.product_original,
        "productPrice": priced.product_price,
        "deliveryOriginalPrice": priced.delivery_original_fen,
        "deliveryPrice": priced.delivery_fen,
        "packingOriginalPrice": priced.packing_original_fen,
        "packingPrice": priced.packing_fen,
        "originalPrice": priced.original_price,
        "discount": priced.discount,
        "price": priced.price,
        "productList": product_list,
    }
    if priced.tableware_fen:
        data["tablewarePrice"] = priced.tableware_fen
    return data
