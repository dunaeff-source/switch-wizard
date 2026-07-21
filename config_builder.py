# -*- coding: utf-8 -*-
"""Сборка списка команд из профиля вендора и данных, введённых оператором."""

import os
import re
import sys
import ipaddress

import yaml

PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")

# Порядок применения секций
SECTION_ORDER = [
    "enter_config",
    "hostname",
    "vlans",
    "mgmt",
    "user",
    "ssh",
    "snmp",
    "ports_access",
    "ports_trunk",
    "save",
]


def base_dir():
    """Каталог рядом с .exe (PyInstaller) или со скриптом."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


RESERVED_KEYS = ("defaults",)

DEFAULTS_FALLBACK = {
    "username": "admin",
    "password": "",
    "snmp_ro": "public",
    "snmp_mandatory": False,
    "lock_credentials": False,
}


def load_config(path=None):
    """Возвращает (defaults, profiles) из profiles.yaml."""
    path = path or os.path.join(base_dir(), "profiles.yaml")
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    defaults = dict(DEFAULTS_FALLBACK)
    defaults.update(data.get("defaults") or {})
    profiles = {k: v for k, v in data.items() if k not in RESERVED_KEYS}
    if not profiles:
        raise ValueError("В profiles.yaml не найдено ни одного профиля коммутатора")
    return defaults, profiles


def load_profiles(path=None):
    """Совместимость со старым вызовом."""
    return load_config(path)[1]


# --------------------------------------------------------------------------
# Разбор пользовательского ввода
# --------------------------------------------------------------------------

def parse_vlans(text):
    """'10 Office' / '20;VoIP' / '30' -> [{'vlan_id': 10, 'vlan_name': 'VLAN30'}]"""
    result = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"[\s;,]+", line, maxsplit=1)
        vid = parts[0]
        if not vid.isdigit() or not (1 <= int(vid) <= 4094):
            raise ValueError("Некорректный VLAN ID: %r" % raw)
        name = parts[1].strip() if len(parts) > 1 and parts[1].strip() else "VLAN%s" % vid
        name = re.sub(r"\s+", "_", name)
        result.append({"vlan_id": int(vid), "vlan_name": name})
    return result


def expand_ports(spec):
    """'1-4,7' -> [1, 2, 3, 4, 7]"""
    ports = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            a, b = chunk.split("-", 1)
            a, b = int(a), int(b)
            if a > b:
                a, b = b, a
            ports.extend(range(a, b + 1))
        else:
            ports.append(int(chunk))
    return sorted(set(ports))


def parse_ports(text):
    """
    '1-20 access 10'      -> группа access
    '21-24 trunk 10,20'   -> группа trunk
    """
    groups = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 3:
            raise ValueError("Строка портов должна быть вида '1-8 access 10': %r" % raw)
        port_range, mode, vlan_spec = parts[0], parts[1].lower(), parts[2]
        if mode not in ("access", "trunk"):
            raise ValueError("Режим должен быть access или trunk: %r" % raw)
        try:
            port_list = expand_ports(port_range)
        except ValueError:
            raise ValueError("Не удалось разобрать номера портов: %r" % raw)
        vlans = [v.strip() for v in vlan_spec.split(",") if v.strip()]
        for v in vlans:
            if not v.isdigit():
                raise ValueError("Некорректный VLAN в строке портов: %r" % raw)
        groups.append({
            "mode": mode,
            "port_range": port_range,
            "port_list": port_list,
            "vlan": vlans[0],
            "vlans_csv": ",".join(vlans),
        })
    return groups


def mask_to_prefix(mask):
    return ipaddress.IPv4Network("0.0.0.0/%s" % mask).prefixlen


# --------------------------------------------------------------------------
# Валидация и подготовка переменных
# --------------------------------------------------------------------------

def prepare_vars(form):
    """form — словарь с полями из GUI. Возвращает плоский набор переменных."""
    errors = []

    def ip_ok(value, label):
        try:
            ipaddress.IPv4Address(value)
            return True
        except Exception:
            errors.append("Некорректный %s: %s" % (label, value))
            return False

    ip_ok(form["mgmt_ip"], "IP управления")
    ip_ok(form["gateway"], "шлюз")
    try:
        prefix = mask_to_prefix(form["mgmt_mask"])
    except Exception:
        errors.append("Некорректная маска: %s" % form["mgmt_mask"])
        prefix = 24

    vlans = parse_vlans(form.get("vlans_text", ""))
    ports = parse_ports(form.get("ports_text", ""))

    mgmt_vlan = str(form.get("mgmt_vlan") or "1")
    mgmt_vlan_name = "default"
    for v in vlans:
        if str(v["vlan_id"]) == mgmt_vlan:
            mgmt_vlan_name = v["vlan_name"]

    # Проверка: все VLAN, используемые на портах, объявлены
    declared = {str(v["vlan_id"]) for v in vlans} | {"1"}
    for g in ports:
        for v in g["vlans_csv"].split(","):
            if v not in declared:
                errors.append("VLAN %s используется на портах, но не создан в списке VLAN" % v)

    if not form.get("password"):
        errors.append("Не задан пароль администратора")
    if form.get("enable_snmp", True) and not (form.get("snmp_ro") or "").strip():
        errors.append("Не заполнено имя SNMP community")

    if errors:
        raise ValueError("\n".join(sorted(set(errors))))

    return {
        "login_username": form.get("login_username") or "admin",
        "login_password": form.get("login_password", ""),
        "hostname": form.get("hostname", "switch"),
        "mgmt_ip": form["mgmt_ip"],
        "mgmt_mask": form["mgmt_mask"],
        "mgmt_prefix": str(prefix),
        "gateway": form["gateway"],
        "mgmt_vlan": mgmt_vlan,
        "mgmt_vlan_name": mgmt_vlan_name,
        "username": form.get("username") or "admin",
        "password": form["password"],
        "snmp_ro": form.get("snmp_ro") or "public",
        "_vlans": vlans,
        "_ports": ports,
        "_flags": {
            "hostname": bool(form.get("hostname")),
            "ssh": bool(form.get("enable_ssh", True)),
            "snmp": bool(form.get("enable_snmp", True)),
            "user": bool(form.get("enable_user", True)),
        },
    }


# --------------------------------------------------------------------------
# Рендеринг
# --------------------------------------------------------------------------

def render(text, scope):
    def repl(m):
        key = m.group(1)
        if key not in scope:
            raise KeyError("В шаблоне используется неизвестная переменная {{%s}}" % key)
        return str(scope[key])
    return PLACEHOLDER.sub(repl, text)


def _norm(item):
    if isinstance(item, str):
        return {"cmd": item, "replies": [], "timeout": None}
    return {"cmd": item["cmd"], "replies": list(item.get("replies", [])),
            "timeout": item.get("timeout")}


def _contiguous_runs(numbers):
    runs, start, prev = [], None, None
    for n in numbers:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            runs.append((start, prev))
            start = prev = n
    if start is not None:
        runs.append((start, prev))
    return runs


def _iface_range(port_list, prefix):
    """[1..20] + '1/0/' -> '1/0/1-1/0/20'; [1,2,7] -> '1/0/1-1/0/2,1/0/7'"""
    parts = []
    for a, b in _contiguous_runs(port_list):
        if a == b:
            parts.append("%s%d" % (prefix, a))
        else:
            parts.append("%s%d-%s%d" % (prefix, a, prefix, b))
    return ",".join(parts)


def _iter_scopes(profile, foreach, base):
    """Разворачивает цикл секции в набор областей видимости."""
    vlans, ports = base["_vlans"], base["_ports"]
    iface_tpl = profile.get("iface_template", "{{port}}")

    if foreach == "vlans":
        for v in vlans:
            yield dict(base, **v)

    elif foreach in ("ports_each_access", "ports_each_trunk"):
        mode = "access" if foreach.endswith("access") else "trunk"
        for g in ports:
            if g["mode"] != mode:
                continue
            for p in g["port_list"]:
                scope = dict(base, port=p, vlan=g["vlan"], vlans_csv=g["vlans_csv"],
                             port_range=g["port_range"])
                scope["iface"] = render(iface_tpl, scope)
                yield scope

    elif foreach in ("port_groups_access", "port_groups_trunk"):
        mode = "access" if foreach.endswith("access") else "trunk"
        prefix = profile.get("port_prefix", "")
        for g in ports:
            if g["mode"] == mode:
                yield dict(base, port_range=g["port_range"], vlan=g["vlan"],
                           vlans_csv=g["vlans_csv"],
                           iface_range=_iface_range(g["port_list"], prefix))
    else:
        raise ValueError("Неизвестный foreach: %s" % foreach)


def build_commands(profile, variables):
    """Возвращает список {'section':..., 'cmd':..., 'replies': [...]}"""
    sections = profile.get("sections", {})
    flags = variables["_flags"]
    plan = []

    for sec_id in SECTION_ORDER:
        if sec_id in flags and not flags[sec_id]:
            continue
        spec = sections.get(sec_id)
        if not spec:
            continue

        if isinstance(spec, dict) and "foreach" in spec:
            items = [_norm(c) for c in spec["commands"]]
            for scope in _iter_scopes(profile, spec["foreach"], variables):
                for it in items:
                    plan.append({
                        "section": sec_id,
                        "cmd": render(it["cmd"], scope),
                        "replies": [render(r, scope) for r in it["replies"]],
                        "timeout": it["timeout"],
                    })
        else:
            for it in [_norm(c) for c in spec]:
                plan.append({
                    "section": sec_id,
                    "cmd": render(it["cmd"], variables),
                    "replies": [render(r, variables) for r in it["replies"]],
                    "timeout": it["timeout"],
                })
    return plan


def plan_to_text(plan, mask_password=None):
    lines, current = [], None
    for step in plan:
        if step["section"] != current:
            current = step["section"]
            lines.append("")
            lines.append("! --- %s ---" % current)
        cmd = step["cmd"]
        if mask_password:
            cmd = cmd.replace(mask_password, "********")
        lines.append(cmd)
    return "\n".join(lines).strip()
