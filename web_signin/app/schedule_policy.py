import json
from decimal import Decimal, InvalidOperation


def parse_times(times_json):
    try:
        values = json.loads(times_json)
    except (ValueError, TypeError):
        return []
    if not isinstance(values, list):
        return []
    result = []
    for value in values:
        if not isinstance(value, str):
            continue
        parts = value.split(":")
        if len(parts) == 2:
            parts.append("00")
        if len(parts) != 3:
            continue
        try:
            hour, minute, second = map(int, parts)
        except ValueError:
            continue
        if 0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60:
            result.append(f"{hour:02d}:{minute:02d}:{second:02d}")
    return result


def policy_fields(task):
    return {
        "random_enabled": bool(task.random_enabled),
        "random_minutes": task.random_minutes,
        "fallback_time": task.fallback_time,
        "periods": task.periods or [],
    }


def _normalize_location(location, period_name):
    if location is None:
        return None
    if not isinstance(location, dict):
        raise ValueError(f"{period_name}：签到地点格式不正确")
    name = location.get("name")
    if not isinstance(name, str) or not name.strip() or not name.isprintable() or len(name.strip()) > 40:
        raise ValueError(f"{period_name}：请填写 1～40 个字符的地点名称")

    normalized = {"name": name.strip()}
    for field, label, limit in [("lat", "纬度", 90), ("lng", "经度", 180)]:
        error = f"{period_name}：{label}必须是 -{limit} 到 {limit} 之间的数字"
        raw = location.get(field)
        if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
            raise ValueError(error)
        text = str(raw).strip()
        if not text or len(text) > 32:
            raise ValueError(error)
        try:
            value = Decimal(text)
        except InvalidOperation:
            raise ValueError(error) from None
        if not value.is_finite() or not -limit <= value <= limit:
            raise ValueError(error)
        normalized[field] = text
    return normalized


def normalize_periods(raw):
    if not isinstance(raw, list):
        raise ValueError("签到时段必须为列表")
    periods = []
    names = set()
    for item in raw:
        name = item.get("name") if isinstance(item, dict) else None
        if not isinstance(name, str) or not name.strip() or not name.isprintable() or len(name.strip()) > 40:
            raise ValueError("每个时段需要填写 1～40 个字符的名称")
        name = name.strip()
        if name in names:
            raise ValueError(f"签到时段名称重复：{name}")
        names.add(name)
        raw_times = item.get("times")
        times = parse_times(json.dumps(raw_times)) if isinstance(raw_times, list) else []
        if not times or len(times) != len(raw_times):
            raise ValueError(f"{name}：请填写有效的普通时间（HH:MM 或 HH:MM:SS）")
        fallback = parse_times(json.dumps([item.get("fallback_time")]))
        if not fallback or max(times) >= fallback[0]:
            raise ValueError(f"{name}：兜底时间必填，且必须晚于本时段的所有普通时间")
        period = {"name": name, "times": sorted(set(times)), "fallback_time": fallback[0]}
        location = _normalize_location(item.get("location"), name)
        if location is not None:
            period["location"] = location
        periods.append(period)
    periods.sort(key=lambda period: period["times"][0])
    for previous, current in zip(periods, periods[1:]):
        if previous["fallback_time"] >= current["times"][0]:
            raise ValueError(f"{previous['name']}的兜底时间必须早于{current['name']}的第一个普通时间")
    return periods


def period_times(periods):
    return [value for period in periods for value in period["times"]]


def validate_policy(data, times, task=None):
    fields = policy_fields(task) if task else {"random_enabled": False, "random_minutes": 5,
                                               "fallback_time": None, "periods": []}
    fields.update({key: data[key] for key in fields if key in data})
    if not isinstance(fields["random_enabled"], bool):
        raise ValueError("random_enabled 必须为布尔值")
    minutes = fields["random_minutes"]
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 0 <= minutes <= 60:
        raise ValueError("随机浮动分钟数必须为 0～60 的整数")
    periods = normalize_periods(fields["periods"])
    fields["periods"] = periods
    if periods:
        fields["random_enabled"] = True
        fields["fallback_time"] = periods[-1]["fallback_time"]
        times = period_times(periods)
    fallback = fields["fallback_time"]
    if fallback:
        normalized = parse_times(json.dumps([fallback]))
        if not normalized:
            raise ValueError("兜底时间格式应为 HH:MM 或 HH:MM:SS")
        fields["fallback_time"] = normalized[0]
    else:
        fields["fallback_time"] = None
    if fields["random_enabled"]:
        if not fields["fallback_time"]:
            raise ValueError("随机签到必须设置最后兜底时间")
        if not times or any(value >= fields["fallback_time"] for value in times):
            raise ValueError("每个普通时间点都必须早于兜底时间")
        wx_key = data.get("wx_key", task.wx_key if task else "") or ""
        qq_key = data.get("qq_key", task.qq_key if task else "") or ""
        if not isinstance(wx_key, str) or not isinstance(qq_key, str):
            raise ValueError("通知 Key 必须为字符串")
        if not wx_key.strip() and not qq_key.strip():
            raise ValueError("随机签到请至少配置一种微信或 QQ 通知 Key，以接收失败提醒")
    return fields
