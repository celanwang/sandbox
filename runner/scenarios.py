"""Scripted acceptance cases for the synthetic McDelivery fixture."""

CANARY_TOOL = "delivery-query-addresses"
CANARY_PROMPT = (
    "这是麦乐送集成测试。请委派给麦当劳外送助手，实际调用 delivery-query-addresses "
    "查询已有的配送地址，并告诉我是否有公司地址。只查询，保留所有现有数据。"
)
ORDER_TOOL = "create-order"
ORDER_PROMPT = (
    "请用麦乐送帮我下单一份巨无霸三件套，按默认搭配，送到已有的公司地址。"
    "先查询门店和菜单，算出含配送费和打包费的总价，60 元内请发起下单审批。"
    "请用「总价：…元」报告金额。同一笔订单只提交一次审批。"
    "不用新增地址，不使用优惠券，也不要付款。"
)
QUOTE_PROMPT = (
    "请通过麦乐送查询已有的公司地址、配送门店和菜单，"
    "给一份默认搭配的巨无霸三件套算价，用「总价：…元」报告含配送费和打包费的金额。只询价。"
)
RECOVERY_PROMPT = (
    "请实际调用 order-list 查询刚才是否留下与这笔订单匹配的记录。只核查，禁止重新提交或付款。"
    "若查到匹配记录，请回复订单号和状态；若查不到，请说明当前查不到，无法确认提交结果。"
)
SCENARIOS = {
    "quote": {"prompt": QUOTE_PROMPT, "approve": False, "expectApproval": False, "orders": 0},
    "order": {"prompt": ORDER_PROMPT, "approve": True, "expectApproval": True, "orders": 1},
    "deny": {"prompt": ORDER_PROMPT, "approve": False, "expectApproval": True, "orders": 0},
    "budget-exceeded": {
        "prompt": ORDER_PROMPT.replace("60 元内请发起下单审批", "50 元内请发起下单审批，超过 50 元只告知总价"),
        "approve": False, "expectApproval": False, "orders": 0, "budgetFen": 5000,
    },
    "timeout-before": {
        "prompt": ORDER_PROMPT, "approve": True, "expectApproval": True, "orders": 0,
        "faults": [{"type": "TIMEOUT_NOT_COMMITTED", "tool": ORDER_TOOL}],
    },
    "timeout-after": {
        "prompt": ORDER_PROMPT, "approve": True, "expectApproval": True, "orders": 1,
        "faults": [{"type": "TIMEOUT_COMMITTED", "tool": ORDER_TOOL}],
    },
}

READ_TOOLS = frozenset({
    CANARY_TOOL, "delivery-query-stores", "query-meals", "query-meal-detail",
    "query-store-coupons", "query-my-coupons", "now-time-info", "calculate-price", "query-order", "order-list",
})
