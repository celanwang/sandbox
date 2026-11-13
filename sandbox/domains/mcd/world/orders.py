"""订单：建单、状态、以及 create-order / query-order / order-list 的返回结构。

订单记录（world["orders"] 里的每一项）同时服务两件事：
1. 生成返回给模型的内容（按官方示例的字段）；
2. 给判定器用的快照字段（商品、子项、券、金额、下单前最后一次报价是否一致）。
"""
from __future__ import annotations

import hashlib
from typing import Any

from sandbox.domains.mcd.world.pricing import Priced, yuan

STATUS_PENDING = "待支付"
STATUS_CANCELLED = "已取消"
# order-list 的官方示例里状态文字带“订单”前缀
LIST_STATUS = {STATUS_PENDING: "待支付", STATUS_CANCELLED: "订单已取消"}
CANCEL_REASONS = {1: "改主意了", 2: "重复下单", 3: "点错了/点多了/点少了",
                  4: "地址/电话填错了", 5: "送达时间选错了", -1: "其它原因"}


def digits(seed: str, n: int) -> str:
    """由种子生成固定的数字串：同样的输入永远得到同样的结果。"""
    out, i = "", 0
    while len(out) < n:
        out += "".join(str(int(c, 16) % 10) for c in hashlib.sha256(f"{seed}:{i}".encode()).hexdigest())
        i += 1
    return out[:n]


def new_order_id(scene_id: str, seed: int, n: int) -> str:
    """28 位数字订单号，格式与真实订单号相同；同一场景、同一种子下的第 n 笔订单永远相同。"""
    return "1030" + digits(f"order:{scene_id}:{seed}:{n}", 24)


def build_order(*, order_id: str, pay_id: str, store: dict, address: dict, priced: Priced,
                create_time: str, remark: str = "", last_quote: dict | None = None,
                status: str = STATUS_PENDING) -> dict:
    return {
        "orderId": order_id,
        "payId": pay_id,
        "payH5Url": f"https://payment.sandbox.invalid/mcp/scanToPay?orderId={order_id}",
        "status": status,
        "storeCode": store["storeCode"],
        "beCode": store["beCode"],
        "storeName": store["storeName"],
        "storeAddress": store.get("address", ""),
        "addressId": address["addressId"],
        "addressTag": address.get("tag"),
        "fullAddress": address["fullAddress"],
        "contactName": address["contactName"],
        "phone": address["phone"],
        "createTime": create_time,
        "remark": remark,
        "items": [
            {"productCode": l.product_code, "productName": l.product_name, "quantity": l.quantity,
             "choices": [{"round": c["round"], "code": c["code"], "name": c["name"], "quantity": c["quantity"]}
                         for c in l.choices],
             "couponId": l.coupon["couponId"] if l.coupon else None,
             "modification": l.modification,
             "subtotalFen": l.subtotal}
            for l in priced.lines
        ],
        "couponIds": [l.coupon["couponId"] for l in priced.lines if l.coupon],
        "contentKey": priced.content_key,
        "amounts": {
            "productOriginalFen": priced.product_original, "productFen": priced.product_price,
            "deliveryOriginalFen": priced.delivery_original_fen, "deliveryFen": priced.delivery_fen,
            "packingOriginalFen": priced.packing_original_fen, "packingFen": priced.packing_fen,
            "originalFen": priced.original_price, "discountFen": priced.discount, "totalFen": priced.price,
        },
        # 下单前最近一条与门店、商品和总价一致的报价。
        "lastQuote": last_quote,
        "quoteMatchesOrder": bool(last_quote and last_quote["contentKey"] == priced.content_key
                                  and last_quote["priceFen"] == priced.price),
        "cancelReason": None,
    }


def _product_list(order: dict, with_image: bool = False) -> list[dict]:
    out = []
    for item in order["items"]:
        entry: dict[str, Any] = {"productName": item["productName"]}
        if with_image:
            entry["productImage"] = ""
        entry.update({
            "quantity": item["quantity"],
            "price": yuan(item["subtotalFen"]),
            "comboItemList": [{"itemName": c["name"], "itemQuantity": c["quantity"]} for c in item["choices"]],
        })
        out.append(entry)
    return out


def _detail_common(order: dict) -> dict:
    a = order["amounts"]
    reservation = order.get("reservationDate")
    return {
        "totalAmount": yuan(a["originalFen"]),
        "realTotalAmount": yuan(a["totalFen"]),
        "totalDiscountAmount": yuan(a["discountFen"]),
        "couponList": [{"couponId": cid} for cid in order["couponIds"]],
        "deliveryInfo": {
            "deliveryType": "预约配送" if reservation else "立即送出",
            "deliveryAddress": order["fullAddress"],
            "addressDetail": "",
            "customerNickname": order["contactName"],
            "mobilePhone": order["phone"],
            "expectDeliveryTime": reservation or "",
        },
        "createTime": order["createTime"],
        "deliveryPrice": yuan(a["deliveryOriginalFen"]),
        "realDeliveryPrice": yuan(a["deliveryFen"]),
        "productPrice": yuan(a["productFen"]),
        "takeWay": "",
        "pickupCode": "",
        "lockerCode": "",
        "mealAssistance": {"code": "", "name": "", "items": []},
        "lockerQrCode": "",
        "remark": order["remark"],
    }


def create_view(order: dict) -> dict:
    """create-order 的 data。"""
    detail = {"orderStatus": order["status"], "storeName": order["storeName"],
              "storeAddress": order["storeAddress"], "orderProductList": _product_list(order)}
    detail.update(_detail_common(order))
    return {"orderId": order["orderId"], "payId": order["payId"], "payH5Url": order["payH5Url"], "orderDetail": detail}


def query_view(order: dict) -> dict:
    """query-order 的 data。"""
    data = {"orderId": order["orderId"], "orderStatus": order["status"], "storeName": order["storeName"],
            "orderProductList": _product_list(order, with_image=True)}
    data.update(_detail_common(order))
    return data


def list_view(order: dict) -> dict:
    """order-list 中的一项。"""
    return {
        "orderId": order["orderId"],
        "orderType": "2",
        "createTime": order["createTime"],
        "beType": "2",
        "beCode": order["beCode"],
        "storeCode": order["storeCode"],
        "storeName": order["storeName"],
        "orderStatus": LIST_STATUS.get(order["status"], order["status"]),
        "orderProductList": [
            {"productCode": i["productCode"], "productName": i["productName"], "quantity": i["quantity"],
             "comboItemList": [{"productCode": c["code"], "name": c["name"], "quantity": c["quantity"]}
                               for c in i["choices"]]}
            for i in order["items"]
        ],
        "realTotalAmount": yuan(order["amounts"]["totalFen"]),
    }
