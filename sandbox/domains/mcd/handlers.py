"""工具处理层：每个工具一个函数，签名 (episode, arguments) -> 信封 dict 或 RawText。

处理函数只做三件事：校验参数、调用 world 里的规则函数、组装官方结构的 data。
记录调用、渲染文本由 server 统一完成。

覆盖配送地址、门店、菜单、优惠券、虚拟时钟、报价和订单。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sandbox.domains.mcd import world as W
from sandbox.episodes import Episode
from sandbox.domains.mcd.render import envelope
from sandbox.domains.mcd.world import orders as O
from sandbox.domains.mcd.world.pricing import PriceError, calc_view, coupon_valid, price_items, yuan

@dataclass
class RawText:
    """不套信封、不套模板，原样返回的文本（例如 query-my-coupons 官方就是返回 Markdown）。"""
    text: str
    success: bool = True


Result = dict | RawText
Handler = Callable[[Episode, dict], Result]


class ParamError(ValueError):
    pass


def _require(args: dict, *names: str) -> None:
    missing = [n for n in names if args.get(n) in (None, "", [])]
    if missing:
        raise ParamError(f"缺少必填参数: {', '.join(missing)}")


def _as_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ParamError(f"参数 {name} 必须是整数")
    try:
        return int(value)
    except ValueError:
        raise ParamError(f"参数 {name} 必须是整数") from None


def _fail(ep: Episode, message: str, code: int = 400, data: Any = None) -> dict:
    return envelope(ep, data, success=False, code=code, message=message)


def _delivery_mode(args: dict) -> None:
    """沙箱第一期只支持麦乐送。"""
    if "orderType" in args and _as_int(args["orderType"], "orderType") != 2:
        raise ParamError("沙箱当前只支持麦乐送（orderType=2，beType=2）")
    if _as_int(args.get("beType"), "beType") != 2:
        raise ParamError("沙箱当前只支持麦乐送（orderType=2，beType=2）")


def _store(ep: Episode, args: dict) -> dict:
    store = W.find_store(ep.world, args["storeCode"])
    if store is None:
        raise ParamError("门店不存在")
    if args.get("beCode") and str(args["beCode"]) != store["beCode"]:
        raise ParamError("beCode 与门店不匹配")
    return store


# ---------- 地址、门店、菜单 ----------

def delivery_query_addresses(ep: Episode, args: dict) -> Result:
    addresses = [{k: a[k] for k in ("addressId", "contactName", "phone", "fullAddress")}
                 for a in ep.world["addresses"]]
    return envelope(ep, {"addresses": addresses})


def delivery_create_address(ep: Episode, args: dict) -> Result:
    """新增配送地址。Iris 里这是免审批的写操作。

    新地址由哪些门店配送，由场景的 newAddressDeliverFrom 决定（默认所有门店）。
    """
    _require(args, "city", "contactName", "phone", "address", "addressDetail")
    phone = str(args["phone"]).strip()
    if not (phone.isdigit() and len(phone) == 11):
        raise ParamError("phone 必须是 11 位数字")
    n = sum(1 for a in ep.world["addresses"] if a.get("created")) + 1
    address_id = f"1000000000000000000001{n:06d}"
    full = f"{args['city']}{args['address']} {args['addressDetail']}"
    record = {"addressId": address_id, "tag": None, "contactName": str(args["contactName"]),
              "phone": f"{phone[:3]}****{phone[-4:]}", "fullAddress": full, "created": True,
              "createdAtCallSeq": ep.seq + 1}
    ep.world["addresses"].append(record)
    stores = ep.world.get("newAddressDeliverFrom") or [s["storeCode"] for s in ep.world["stores"]]
    for store in ep.world["stores"]:
        if store["storeCode"] in stores:
            store.setdefault("deliverTo", []).append(address_id)
    return envelope(ep, {k: record[k] for k in ("addressId", "contactName", "phone", "fullAddress")})


def delivery_query_stores(ep: Episode, args: dict) -> Result:
    _require(args, "addressId", "beType")
    be_type = _as_int(args["beType"], "beType")
    if be_type not in (2, 6):
        raise ParamError("beType 仅支持 2（麦乐送）或 6（企业团餐）")
    if be_type == 6:
        raise ParamError("沙箱当前只支持麦乐送（beType=2）")
    if W.find_address(ep.world, args["addressId"]) is None:
        return _fail(ep, "地址不存在，请先查询用户可配送地址列表", code=404)
    now = W.clock(ep.world)
    # 字段和顺序照真实录制：真实返回没有 address 字段（尽管说明文字里写了“门店地址”）
    stores = [{
        "storeCode": s["storeCode"], "beCode": s["beCode"], "storeName": s["storeName"],
        "reservation": bool(s.get("reservation")), "reservationTimeOptions": W.reservation_options(s, now),
        "businessStatus": W.store_open(s, now),
        "businessStartTime": s["businessStartTime"], "businessEndTime": s["businessEndTime"],
    } for s in W.stores_for_address(ep.world, args["addressId"])]
    return envelope(ep, stores)


def query_meals(ep: Episode, args: dict) -> Result:
    _require(args, "storeCode", "orderType", "beType")
    _delivery_mode(args)
    store = _store(ep, args)
    menu = W.menu_for_store(ep.world, store, W.clock(ep.world))
    # 结构照真实录制：categories[{name, meals[{code, tags?}]}]，meals{code: {name, image, productType, …}}
    return envelope(ep, {"categories": menu["categories"], "meals": menu["meals"]})


def query_meal_detail(ep: Episode, args: dict) -> Result:
    _require(args, "code", "storeCode", "orderType", "beType")
    _delivery_mode(args)
    store = _store(ep, args)
    menu = W.store_menu(ep.world, store)
    code = str(args["code"])
    meal = menu["meals"].get(code)
    if meal is None:
        return _fail(ep, "餐品不存在", code=404)
    detail = (menu.get("details") or {}).get(code)
    if detail is None:
        # 单品可以直接用菜单数据生成详情。
        detail = {"code": code, "name": meal["name"], "rounds": [], "image": meal.get("image", ""),
                  "supportModify": False}
    return envelope(ep, detail)


# ---------- 券与时间 ----------

def query_store_coupons(ep: Episode, args: dict) -> Result:
    _require(args, "storeCode", "orderType", "beType")
    _delivery_mode(args)
    store = _store(ep, args)
    now = W.clock(ep.world)
    menu = W.store_menu(ep.world, store)
    meals = menu["meals"]
    day = now.strftime("%Y-%m-%d")
    data = []
    for c in ep.world.get("coupons", []):
        if not coupon_valid(c, now) or c["couponId"] in ep.world.get("invalidCoupons", []):
            continue
        products = [{"productCode": p, "productName": meals[p]["name"]} for p in c["appliesTo"]
                    if p in meals and W.meal_available(menu, p, now)]
        if products:
            data.append({"title": c["title"], "couponId": c["couponId"], "couponCode": c["couponCode"],
                         "tradeDateTime": f"{day} {c['validFrom']}:00-{day} {c['validTo']}:59",
                         "products": products})
    return envelope(ep, data)


def query_my_coupons(ep: Episode, args: dict) -> Result:
    """保留优惠券列表的 Markdown 格式。"""
    now = W.clock(ep.world)
    coupons = [
        coupon for coupon in ep.world["coupons"]
        if coupon_valid(coupon, now) and coupon["couponId"] not in ep.world["invalidCoupons"]
    ]
    if not coupons:
        return RawText("# 您的优惠券列表\n\n暂无可用优惠券\n\n")
    day = ep.world["clock"][:10]
    lines = ["# 您的优惠券列表", f"共 {len(coupons)} 张可用优惠券"]
    for c in coupons:
        lines += [f"## {c['title']}",
                  f"- **优惠**: 立减¥{yuan(c['discountFen'])}",
                  f"- **有效期**: {day} {c['validFrom']}-{day} {c['validTo']}",
                  "- **领取时间**: 今日收到",
                  "- **标签**: 外送专用"]
    return RawText("\n".join(lines) + "\n")


def now_time_info(ep: Episode, args: dict) -> Result:
    now = W.clock(ep.world)
    aware = now.replace(tzinfo=timezone(timedelta(hours=8)))
    utc = aware.astimezone(timezone.utc)
    return envelope(ep, {
        "timestamp": int(aware.timestamp() * 1000),
        "datetime": now.strftime("%Y-%m-%dT%H:%M:%S.000"),
        "formatted": now.strftime("%Y-%m-%d %H:%M:%S"),
        "date": now.strftime("%Y-%m-%d"),
        "year": now.year, "month": now.month, "day": now.day,
        "dayOfWeek": now.strftime("%A").upper(),
        "timezone": "GMT+08:00", "offset": "+08:00",
        "utc": utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    })


# ---------- 算价与订单 ----------

def calculate_price(ep: Episode, args: dict) -> Result:
    _require(args, "storeCode", "orderType", "beType", "items")
    _delivery_mode(args)
    store = _store(ep, args)
    try:
        priced = price_items(ep.world, store, args["items"], W.clock(ep.world), bool(args.get("needTableware")))
    except PriceError as e:
        return _fail(ep, str(e))
    ep.world["quotes"].append({
        "callSeq": ep.seq + 1, "round": ep.round, "storeCode": store["storeCode"],
        "contentKey": priced.content_key, "items": [l.key() for l in priced.lines],
        "productFen": priced.product_price, "deliveryFen": priced.delivery_fen, "priceFen": priced.price,
    })
    return envelope(ep, calc_view(priced))


def create_order(ep: Episode, args: dict) -> Result:
    _require(args, "storeCode", "beType", "items")
    _delivery_mode(args)
    _require(args, "addressId")
    store = _store(ep, args)
    address = W.find_address(ep.world, args["addressId"])
    if address is None:
        return _fail(ep, "配送地址不存在")
    now = W.clock(ep.world)
    if str(args["addressId"]) not in store.get("deliverTo", []):
        return _fail(ep, f"门店「{store['storeName']}」不配送该地址")
    if not W.store_open(store, now):
        return _fail(ep, f"门店「{store['storeName']}」当前不在营业时间"
                         f"（{store['businessStartTime']}-{store['businessEndTime']}）")
    remark = str(args.get("remark") or "")
    if len(remark) > 50:
        return _fail(ep, "订单备注不能超过 50 字")
    reservation = str(args.get("reservationDate") or "").strip()
    if reservation:
        try:
            datetime.strptime(reservation, "%Y-%m-%d %H:%M")
        except ValueError:
            return _fail(ep, "reservationDate 格式应为 yyyy-MM-dd HH:mm")
    try:
        priced = price_items(ep.world, store, args["items"], now, bool(args.get("needTableware")))
    except PriceError as e:
        return _fail(ep, str(e))
    n = len(ep.world["orders"]) + 1
    order = O.build_order(
        order_id=O.new_order_id(ep.scene_id, ep.seed, n),
        pay_id=O.digits(f"pay:{ep.scene_id}:{ep.seed}:{n}", 20),
        store=store, address=address, priced=priced, create_time=ep.world["clock"], remark=remark,
        last_quote=dict(ep.world["quotes"][-1]) if ep.world["quotes"] else None,
    )
    order["reservationDate"] = reservation or None   # 沙箱只记录，不校验预约时段
    order["createdAtCallSeq"] = ep.seq + 1
    order["createdInRound"] = ep.round
    ep.world["orders"].append(order)
    return envelope(ep, O.create_view(order))


def _find_order(ep: Episode, order_id: Any) -> dict | None:
    return next((o for o in ep.world["orders"] if o["orderId"] == str(order_id)), None)


def query_order(ep: Episode, args: dict) -> Result:
    _require(args, "orderId")
    order = _find_order(ep, args["orderId"])
    if order is None:
        return _fail(ep, "订单不存在", code=404)
    return envelope(ep, O.query_view(order))


def order_list(ep: Episode, args: dict) -> Result:
    recent = sorted(ep.world["orders"], key=lambda o: (o["createTime"], o["orderId"]), reverse=True)[:10]
    return envelope(ep, {"list": [O.list_view(o) for o in recent]})


CANCEL_FAILED = {"cancelResult": False, "orderId": ""}


def cancel_order(ep: Episode, args: dict) -> Result:
    # 失败时也带上 cancelResult=false：Iris 账本按 /data/cancelResult 判定，
    # 缺这个字段会被判成 UNKNOWN 并锁住整个会话的写操作。
    _require(args, "orderId", "cancelReasonCode")
    reason = _as_int(args["cancelReasonCode"], "cancelReasonCode")
    if reason not in O.CANCEL_REASONS:
        raise ParamError("cancelReasonCode 取值应为 1、2、3、4、5 或 -1")
    order = _find_order(ep, args["orderId"])
    if order is None:
        return _fail(ep, "订单不存在", code=404, data=dict(CANCEL_FAILED))
    if order["status"] == O.STATUS_CANCELLED:
        return _fail(ep, "订单已取消，无需重复取消", data=dict(CANCEL_FAILED))
    order["status"] = O.STATUS_CANCELLED
    order["cancelReason"] = O.CANCEL_REASONS[reason]
    order["cancelledAtCallSeq"] = ep.seq + 1
    return envelope(ep, {"cancelResult": True, "orderId": ""})


HANDLERS: dict[str, Handler] = {
    "delivery-query-addresses": delivery_query_addresses,
    "delivery-create-address": delivery_create_address,
    "delivery-query-stores": delivery_query_stores,
    "query-meals": query_meals,
    "query-meal-detail": query_meal_detail,
    "query-store-coupons": query_store_coupons,
    "query-my-coupons": query_my_coupons,
    "now-time-info": now_time_info,
    "calculate-price": calculate_price,
    "create-order": create_order,
    "query-order": query_order,
    "order-list": order_list,
    "cancel-order": cancel_order,
}


def dispatch(ep: Episode, tool: str, args: dict) -> Result:
    handler = HANDLERS.get(tool)
    if handler is None:
        return _fail(ep, "沙箱暂不支持该工具", code=501)
    failed_data = dict(CANCEL_FAILED) if tool == "cancel-order" else None
    try:
        return handler(ep, args)
    except ParamError as e:
        return _fail(ep, str(e), data=failed_data)
