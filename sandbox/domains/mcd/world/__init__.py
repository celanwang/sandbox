"""世界层：加载场景，提供只依赖输入的规则函数。

规则函数都是纯函数：输入世界数据和参数，返回结果，不读写任何全局状态，
方便单独测试，也方便以后调整业务规则。
"""
from __future__ import annotations

import json
from datetime import datetime, time
from pathlib import Path
from typing import Any

SCENE_DIR = Path(__file__).resolve().parent.parent / "scenes"
CLOCK_FORMAT = "%Y-%m-%d %H:%M:%S"


class SceneError(ValueError):
    pass


def load_scene(scene_id: str) -> dict[str, Any]:
    """读取内置场景 JSON。"""
    if scene_id not in list_scenes():
        raise SceneError(f"场景不存在: {scene_id}")
    return json.loads((SCENE_DIR / f"{scene_id}.json").read_text(encoding="utf-8"))


def list_scenes() -> list[str]:
    return sorted(p.stem for p in SCENE_DIR.glob("*.json"))


# ---------- 时间 ----------

def clock(world: dict) -> datetime:
    return datetime.strptime(world["clock"], CLOCK_FORMAT)


def _hm(text: str) -> time:
    return datetime.strptime(text, "%H:%M").time()


def store_open(store: dict, now: datetime) -> bool:
    if store.get("forceClosed"):  # 异常引擎注入的“门店打烊”
        return False
    start, end = _hm(store["businessStartTime"]), _hm(store["businessEndTime"])
    t = now.time().replace(second=0, microsecond=0)
    return start <= t <= end


# ---------- 地址与门店 ----------

def find_address(world: dict, address_id: str) -> dict | None:
    return next((a for a in world["addresses"] if a["addressId"] == str(address_id)), None)


def find_store(world: dict, store_code: str) -> dict | None:
    return next((s for s in world["stores"] if s["storeCode"] == str(store_code)), None)


def stores_for_address(world: dict, address_id: str) -> list[dict]:
    """能配送到该地址的门店（含已打烊的，营业状态由 businessStatus 体现）。"""
    return [s for s in world["stores"] if str(address_id) in s.get("deliverTo", [])]


# ---------- 菜单 ----------
#
# 场景菜单结构：
#   menus[<菜单号>] = {
#     "categories": [...],          # 真实 query-meals 的 data.categories，原样
#     "meals": {code: {...}},       # 真实 query-meals 的 data.meals，原样
#     "details": {code: {...}},     # 真实 query-meal-detail 的 data，原样（没录到的餐品没有）
#     "calcNames": {code: "…"},     # 没录到详情的餐品在算价返回里的名字（来自算价探针，可选）
#     "availability": {code: {"until": "10:30"}}   # 沙箱自己的供应时段（可选）
#   }

def store_menu(world: dict, store: dict) -> dict:
    return world["menus"][store.get("menu", "default")]


def meal_available(menu: dict, code: str, now: datetime) -> bool:
    until = (menu.get("availability") or {}).get(code, {}).get("until")
    if until and now.time() >= _hm(until):
        return False
    return True


def menu_for_store(world: dict, store: dict, now: datetime) -> dict:
    """按虚拟时钟过滤后的菜单：去掉不在供应时段的餐品，售罄的在分类里加“售罄”标签。

    返回的 categories / meals 结构与真实 query-meals 一致，可以直接放进返回。
    """
    menu = store_menu(world, store)
    sold_out = set(world.get("soldOut", []))
    meals = {code: m for code, m in menu["meals"].items() if meal_available(menu, code, now)}
    categories = []
    for cat in menu["categories"]:
        entries = []
        for e in cat.get("meals") or []:
            if e["code"] not in meals:
                continue
            e = dict(e)
            if e["code"] in sold_out:
                e["tags"] = list(e.get("tags") or []) + ["售罄"]   # 售罄的真实写法未录到，沿用假设
            entries.append(e)
        if entries:
            categories.append({**{k: v for k, v in cat.items() if k != "meals"}, "meals": entries})
    return {"categories": categories, "meals": meals, "soldOut": sold_out}


# ---------- 门店预约 ----------

def reservation_options(store: dict, now: datetime, days: int = 3) -> list[dict]:
    """按虚拟时钟生成 reservationTimeOptions。

    store["reservationPeriods"] 形如 [["夜市", ["00:00", "00:29"], ["17:30", "23:59"]], ["早餐", ["07:30", "10:59"]], ...]，
    今天的时段还需满足场景配置的最短提前量。
    """
    from datetime import timedelta
    periods = store.get("reservationPeriods") or []
    if not store.get("reservation") or not periods:
        return []
    out = []
    for i in range(days):
        day = (now + timedelta(days=i)).date()
        parts = []
        for name, *ranges in periods:
            if i == 0:
                # 今天只列结束时间晚于当前时间加提前量的时段。
                cutoff = (now + timedelta(minutes=int(store.get("reservationLeadMinutes", 30)))).time()
                late = now.date() != (now + timedelta(minutes=int(store.get("reservationLeadMinutes", 30)))).date()
                ranges = [] if late else [r for r in ranges if _hm(r[1]) > cutoff]
            if ranges:
                parts.append((ranges[0][0], f"{name}(" + ",".join(f"{a}至{b}" for a, b in ranges) + ")"))
        parts.sort(key=lambda x: x[0])   # 真实写法：按剩余第一个时间段的开始时间排序
        if parts:
            out.append({"date": day.strftime("%Y-%m-%d"), "today": i == 0,
                        "reservationOptionText": ",".join(t for _, t in parts)})
    return out
