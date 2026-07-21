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


def parse_vlan_table(text):
    """Единый формат ввода: одна строка = «Имя  Номер  Порты  tag|untag».
    Пример корпоративного стандарта:  MNGNMT_10 10 25-28 tag

    Возвращает (vlans, port_groups):
      vlans       — [{'vlan_id': int, 'vlan_name': str}, ...] в порядке появления
      port_groups — [{'mode': 'access'|'trunk', 'port_range': str,
                      'port_list': [int], 'vlan': str, 'vlans_csv': str}, ...]

    Порты, не указанные ни в одной строке, НЕ трогаются и остаются в дефолтном
    VLAN 1 (абоненты). tag  -> порт(ы) trunk, VLAN тегированный;
                       untag -> порт(ы) access, VLAN нетегированный.
    Несколько строк tag на один и тот же диапазон портов объединяются в один
    trunk с перечнем VLAN через запятую.
    """
    vlan_names = {}          # 'id' -> name
    vlan_order = []          # ['10', '20', ...]
    access_groups = []
    trunk_ranges = {}        # port_range -> {'port_list': [...], 'vlans': ['10', ...]}
    trunk_order = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 4:
            raise ValueError(
                "Строка VLAN должна быть вида «Имя Номер Порты tag|untag», "
                "например «MNGNMT_10 10 25-28 tag»: %r" % raw)
        name, vid, port_range, mode = parts[0], parts[1], parts[2], parts[3].lower()

        if not vid.isdigit() or not (1 <= int(vid) <= 4094):
            raise ValueError("Некорректный номер VLAN (1-4094): %r" % raw)
        name = re.sub(r"\s+", "_", name)

        if mode in ("tag", "tagged", "trunk"):
            tagged = True
        elif mode in ("untag", "untagged", "access"):
            tagged = False
        else:
            raise ValueError("Последнее слово должно быть tag или untag: %r" % raw)

        try:
            port_list = expand_ports(port_range)
        except ValueError:
            raise ValueError("Не удалось разобрать номера портов: %r" % raw)
        if not port_list:
            raise ValueError("Не указаны порты: %r" % raw)

        if vid not in vlan_names:
            vlan_names[vid] = name
            vlan_order.append(vid)

        if tagged:
            grp = trunk_ranges.get(port_range)
            if grp is None:
                grp = {"port_list": port_list, "vlans": []}
                trunk_ranges[port_range] = grp
                trunk_order.append(port_range)
            if vid not in grp["vlans"]:
                grp["vlans"].append(vid)
        else:
            access_groups.append({
                "mode": "access", "port_range": port_range,
                "port_list": port_list, "vlan": vid, "vlans_csv": vid,
            })

    vlans = [{"vlan_id": int(v), "vlan_name": vlan_names[v]} for v in vlan_order]

    trunk_groups = []
    for r in trunk_order:
        g = trunk_ranges[r]
        trunk_groups.append({
            "mode": "trunk", "port_range": r, "port_list": g["port_list"],
            "vlan": g["vlans"][0], "vlans_csv": ",".join(g["vlans"]),
        })

    return vlans, access_groups + trunk_groups


def derive_gateway(ip):
    """Корпоративный стандарт: шлюз всегда X.Y.Z.254 в той же /24, что и IP.
    '10.79.253.232' -> '10.79.253.254'. Пустая строка -> '' (без вычисления)."""
    ip = (ip or "").strip()
    try:
        octets = str(ipaddress.IPv4Address(ip)).split(".")
    except Exception:
        return ""
    return ".".join(octets[:3] + ["254"])


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

    # Корпоративный стандарт: шлюз всегда 10.79.X.254 в той же /24, что и IP.
    # Если поле шлюза пустое — вычисляем автоматически из IP.
    gateway = (form.get("gateway") or "").strip()
    if not gateway:
        gateway = derive_gateway(form["mgmt_ip"])
    form = dict(form, gateway=gateway)
    ip_ok(gateway, "шлюз")

    try:
        prefix = mask_to_prefix(form["mgmt_mask"])
    except Exception:
        errors.append("Некорректная маска: %s" % form["mgmt_mask"])
        prefix = 24

    try:
        vlans, ports = parse_vlan_table(form.get("vlan_table_text", ""))
    except ValueError as exc:
        errors.append(str(exc))
        vlans, ports = [], []

    mgmt_vlan = str(form.get("mgmt_vlan") or "1")
    mgmt_vlan_name = "default"
    for v in vlans:
        if str(v["vlan_id"]) == mgmt_vlan:
            mgmt_vlan_name = v["vlan_name"]

    # VLAN управления (кроме дефолтного 1) должен быть описан в таблице VLAN,
    # иначе команде interface vlan N не на чем работать.
    if mgmt_vlan != "1" and mgmt_vlan not in {str(v["vlan_id"]) for v in vlans}:
        errors.append(
            "VLAN управления %s не описан в таблице VLAN — добавьте строку "
            "с этим номером (например «MNGNMT_%s %s 25-28 tag»)."
            % (mgmt_vlan, mgmt_vlan, mgmt_vlan))

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
