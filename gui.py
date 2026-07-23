# -*- coding: utf-8 -*-
"""UTECH-switch master — мастер настройки коммутаторов (v1.1, CustomTkinter)."""

import os
import re
import csv
import queue
import threading
import datetime

import tkinter as tk
from tkinter import messagebox, filedialog

import customtkinter as ctk

import config_builder as cb
import serial_session as ss

APP_VERSION = "1.1"
APP_TITLE = "UTECH-switch master"
BAUD_RATES = ["9600", "19200", "38400", "57600", "115200"]

ACCENT = "#2FA572"
ACCENT_HOVER = "#268A5E"
DANGER = "#C0504D"
DANGER_HOVER = "#9E3B38"
CARD_PAD = 8

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class App(ctk.CTk):

    def __init__(self):
        super().__init__()
        self.title("%s  v%s" % (APP_TITLE, APP_VERSION))
        self.geometry("980x960")
        self.minsize(920, 820)

        self.log_queue = queue.Queue()
        self.worker = None
        self.vlans = []
        self.detected_key = None       # профиль, подтверждённый «Проверить связь»

        try:
            self.defaults, self.profiles = cb.load_config()
        except Exception as exc:
            messagebox.showerror("Ошибка", "Не удалось прочитать profiles.yaml:\n%s" % exc)
            raise SystemExit(1)

        self.profile_keys = list(self.profiles.keys())

        self._build_ui()
        self._seed_default_vlan()
        self.after(100, self._drain_log)

    # ====================================================================
    #  Вёрстка
    # ====================================================================
    def _build_ui(self):
        # адаптивный размер под экран (без прокрутки — всё на одном экране)
        try:
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            w = min(1180, max(900, sw - 100))
            h = min(1000, max(640, sh - 100))
            self.geometry("%dx%d+%d+%d" % (w, h, 30, 20))
        except Exception:
            pass

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        root = ctk.CTkFrame(self, fg_color="transparent")
        root.grid(row=0, column=0, sticky="nsew", padx=12, pady=(8, 4))
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(8, weight=1)   # журнал растягивается, остальное фиксировано

        self._build_header(root)
        self._build_connection(root)
        self._build_params(root)
        self._build_vlans(root)
        self._build_security(root)
        self._build_actions(root)
        self._build_log(root)

        self.refresh_ports()
        self._model_changed()

    def _build_header(self, parent):
        head = ctk.CTkFrame(parent, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", pady=(0, 2))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(head, text="%s" % APP_TITLE,
                     font=ctk.CTkFont(size=20, weight="bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(head, text="настройка коммутаторов через USB-COM · v%s" % APP_VERSION,
                     font=ctk.CTkFont(size=12), text_color=("gray40", "gray60"))\
            .grid(row=0, column=1, sticky="w", padx=(10, 0))

    def _card(self, parent, row, title):
        card = ctk.CTkFrame(parent, corner_radius=10)
        card.grid(row=row, column=0, sticky="ew", pady=3)
        ctk.CTkLabel(card, text=title, font=ctk.CTkFont(size=14, weight="bold"))\
            .grid(row=0, column=0, columnspan=6, sticky="w", padx=CARD_PAD, pady=(6, 1))
        return card

    def _entry(self, parent, row, col, label, default="", width=170, show=None):
        ctk.CTkLabel(parent, text=label).grid(
            row=row, column=col, sticky="w", padx=(CARD_PAD, 4), pady=3)
        e = ctk.CTkEntry(parent, width=width, height=28, show=show)
        if default:
            e.insert(0, default)
        e.grid(row=row, column=col + 1, sticky="w", padx=(0, CARD_PAD), pady=3)
        return e

    # -------------------------------------------------------- подключение
    def _build_connection(self, parent):
        card = self._card(parent, 1, "1.  Подключение")

        ctk.CTkLabel(card, text="COM-порт:").grid(row=1, column=0, sticky="w", padx=(CARD_PAD, 4), pady=6)
        self.opt_port = ctk.CTkOptionMenu(card, width=240, values=["—"])
        self.opt_port.grid(row=1, column=1, sticky="w", pady=6)
        ctk.CTkButton(card, text="Обновить", width=90, command=self.refresh_ports)\
            .grid(row=1, column=2, sticky="w", padx=8, pady=6)

        ctk.CTkLabel(card, text="Модель:").grid(row=1, column=3, sticky="w", padx=(CARD_PAD, 4), pady=6)
        self.opt_model = ctk.CTkOptionMenu(
            card, width=280,
            values=[self.profiles[k].get("title", k) for k in self.profile_keys],
            command=self._model_changed)
        self.opt_model.grid(row=1, column=4, sticky="w", pady=6)

        ctk.CTkLabel(card, text="Скорость:").grid(row=2, column=0, sticky="w", padx=(CARD_PAD, 4), pady=6)
        self.opt_baud = ctk.CTkOptionMenu(card, width=120, values=BAUD_RATES)
        self.opt_baud.grid(row=2, column=1, sticky="w", pady=6)

        self.e_login_user = self._entry(card, 2, 2, "Вход (логин):",
                                        self.defaults["login_username"], width=140)
        self.e_login_pass = self._entry(card, 2, 4, "пароль:",
                                        self.defaults["login_password"], width=140, show="*")

        self.e_operator = self._entry(card, 3, 0, "Оператор:", "", width=180)
        ctk.CTkLabel(card, text="(для реестра)", text_color=("gray40", "gray60"),
                     font=ctk.CTkFont(size=11)).grid(row=3, column=2, sticky="w", padx=0, pady=(0, 8))

    # -------------------------------------------------------- параметры
    def _build_params(self, parent):
        card = self._card(parent, 2, "2.  Параметры коммутатора")

        self.e_hostname = self._entry(card, 1, 0, "Имя устройства:", "SW-OFFICE-01")
        self.e_ip = self._entry(card, 1, 2, "IP управления:", "10.79.")
        self.e_mask = self._entry(card, 1, 4, "Маска:", "255.255.255.0")

        self.e_gw = self._entry(card, 2, 0, "Шлюз (авто):", "")
        self.e_mgmt_vlan = self._entry(card, 2, 2, "VLAN управления:", "10", width=100)
        self.e_user = self._entry(card, 2, 4, "Новый логин:", self.defaults["username"])

        self.e_pass = self._entry(card, 3, 0, "Новый пароль:", self.defaults["password"], show="*")
        self.e_snmp = self._entry(card, 3, 2, "SNMP community:", self.defaults["snmp_ro"])

        self.var_indiv = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(card, text="Индивидуальный пароль на коммутатор",
                        variable=self.var_indiv, command=self._toggle_indiv)\
            .grid(row=3, column=4, columnspan=2, sticky="w", padx=CARD_PAD, pady=6)

        self.e_ip.bind("<KeyRelease>", self._autofill_gateway)
        self.e_ip.bind("<FocusOut>", self._autofill_gateway)

        opts = ctk.CTkFrame(card, fg_color="transparent")
        opts.grid(row=4, column=0, columnspan=6, sticky="w", padx=CARD_PAD, pady=(4, CARD_PAD))
        self.var_ssh = tk.BooleanVar(value=True)
        self.var_snmp = tk.BooleanVar(value=True)
        self.var_user = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(opts, text="Включить SSH", variable=self.var_ssh)\
            .grid(row=0, column=0, padx=(0, 18))
        self.chk_snmp = ctk.CTkCheckBox(opts, text="Настроить SNMP", variable=self.var_snmp)
        self.chk_snmp.grid(row=0, column=1, padx=(0, 18))
        ctk.CTkCheckBox(opts, text="Создать пользователя", variable=self.var_user)\
            .grid(row=0, column=2, padx=(0, 18))

        if self.defaults.get("snmp_mandatory"):
            self.chk_snmp.configure(state="disabled")
        if self.defaults.get("lock_credentials"):
            for e in (self.e_user, self.e_pass, self.e_snmp):
                e.configure(state="disabled")

    # -------------------------------------------------------- VLAN
    def _build_vlans(self, parent):
        card = self._card(parent, 3, "3.  VLAN")
        card.grid_columnconfigure(0, weight=1)

        hint = ("Заполните поля и нажмите «Добавить». Порты, не указанные ни в одном "
                "VLAN, остаются в дефолтном VLAN 1 (абоненты).")
        ctk.CTkLabel(card, text=hint, font=ctk.CTkFont(size=12),
                     text_color=("gray40", "gray60"), justify="left")\
            .grid(row=1, column=0, columnspan=6, sticky="w", padx=CARD_PAD, pady=(0, 6))

        form = ctk.CTkFrame(card, fg_color="transparent")
        form.grid(row=2, column=0, columnspan=6, sticky="ew", padx=CARD_PAD, pady=(0, 8))

        def field(col, label, width, placeholder=""):
            box = ctk.CTkFrame(form, fg_color="transparent")
            box.grid(row=0, column=col, padx=(0, 10), sticky="w")
            ctk.CTkLabel(box, text=label, font=ctk.CTkFont(size=12),
                         text_color=("gray40", "gray60")).pack(anchor="w")
            e = ctk.CTkEntry(box, width=width, placeholder_text=placeholder)
            e.pack()
            return e

        self.e_vname = field(0, "Имя VLAN", 150, "MNGNMT_10")
        self.e_vid = field(1, "Номер VLAN", 90, "10")
        self.e_vports = field(2, "Порты", 130, "25-28")

        mbox = ctk.CTkFrame(form, fg_color="transparent")
        mbox.grid(row=0, column=3, padx=(0, 10), sticky="w")
        ctk.CTkLabel(mbox, text="Режим", font=ctk.CTkFont(size=12),
                     text_color=("gray40", "gray60")).pack(anchor="w")
        self.opt_mode = ctk.CTkOptionMenu(mbox, width=110, values=["tag", "untag"])
        self.opt_mode.pack()

        abox = ctk.CTkFrame(form, fg_color="transparent")
        abox.grid(row=0, column=4, sticky="w")
        ctk.CTkLabel(abox, text=" ", font=ctk.CTkFont(size=12)).pack(anchor="w")
        ctk.CTkButton(abox, text="+  Добавить", width=120, command=self._add_vlan).pack()

        ctk.CTkLabel(card, text="Добавленные VLAN",
                     font=ctk.CTkFont(size=12, weight="bold"))\
            .grid(row=3, column=0, columnspan=6, sticky="w", padx=CARD_PAD, pady=(4, 2))
        self.vlan_list = ctk.CTkFrame(card, fg_color=("gray92", "gray17"))
        self.vlan_list.grid(row=4, column=0, columnspan=6, sticky="ew",
                            padx=CARD_PAD, pady=(0, CARD_PAD))
        self.vlan_list.grid_columnconfigure(0, weight=1)

    # -------------------------------------------------------- безопасность
    def _build_security(self, parent):
        card = self._card(parent, 4, "4.  Безопасность и доступ")
        card.grid_columnconfigure(5, weight=1)

        self.e_uplink = self._entry(card, 1, 0,
                                    "Аплинк-порты:", "", width=140)
        ctk.CTkLabel(card, text="пусто = авто (из tag-портов)",
                     text_color=("gray40", "gray60"), font=ctk.CTkFont(size=11))\
            .grid(row=1, column=2, columnspan=3, sticky="w", pady=6)

        opts = ctk.CTkFrame(card, fg_color="transparent")
        opts.grid(row=2, column=0, columnspan=6, sticky="w", padx=CARD_PAD, pady=(2, CARD_PAD))
        self.var_seg = tk.BooleanVar(value=True)
        self.var_lbd = tk.BooleanVar(value=True)
        self.var_storm = tk.BooleanVar(value=True)
        self.var_web = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(opts, text="Изолировать абонентов (traffic segmentation)",
                        variable=self.var_seg).grid(row=0, column=0, sticky="w", padx=(0, 18), pady=4)
        ctk.CTkCheckBox(opts, text="Защита от петель (Loop protection)",
                        variable=self.var_lbd).grid(row=0, column=1, sticky="w", padx=(0, 18), pady=4)
        ctk.CTkCheckBox(opts, text="Storm control",
                        variable=self.var_storm).grid(row=1, column=0, sticky="w", padx=(0, 18), pady=4)
        ctk.CTkCheckBox(opts, text="Включить веб-интерфейс (HTTP/HTTPS)",
                        variable=self.var_web).grid(row=1, column=1, sticky="w", padx=(0, 18), pady=4)

    # -------------------------------------------------------- кнопки
    def _build_actions(self, parent):
        bar = ctk.CTkFrame(parent, fg_color="transparent")
        bar.grid(row=5, column=0, sticky="ew", pady=(4, 4))
        bar.grid_columnconfigure(5, weight=1)

        ctk.CTkButton(bar, text="Проверить связь", width=140, command=self.test_link)\
            .grid(row=0, column=0, padx=(0, 8))
        ctk.CTkButton(bar, text="Показать команды", width=150,
                      fg_color="transparent", border_width=1, command=self.preview)\
            .grid(row=0, column=1, padx=(0, 8))
        self.btn_apply = ctk.CTkButton(
            bar, text="НАСТРОИТЬ КОММУТАТОР", width=220, height=40,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=ACCENT, hover_color=ACCENT_HOVER, command=self.apply)
        self.btn_apply.grid(row=0, column=2, padx=(0, 8))
        ctk.CTkButton(bar, text="Прочитать настройки", width=160,
                      fg_color="transparent", border_width=1, command=self.do_read_config)\
            .grid(row=0, column=3, padx=(0, 8))
        ctk.CTkButton(bar, text="Сохранить лог", width=120,
                      fg_color="transparent", border_width=1, command=self.save_log)\
            .grid(row=0, column=6, sticky="e")

        # вторая строка — опасное действие отдельно
        bar2 = ctk.CTkFrame(parent, fg_color="transparent")
        bar2.grid(row=6, column=0, sticky="ew", pady=(0, 4))
        ctk.CTkButton(bar2, text="Сбросить к заводским", width=180,
                      fg_color=DANGER, hover_color=DANGER_HOVER, command=self.do_reset)\
            .grid(row=0, column=0, sticky="w")

        self.progress = ctk.CTkProgressBar(parent)
        self.progress.set(0)
        self.progress.grid(row=7, column=0, sticky="ew", pady=(6, 4))

    def _build_log(self, parent):
        card = ctk.CTkFrame(parent, corner_radius=10)
        card.grid(row=8, column=0, sticky="nsew", pady=(3, 4))
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(card, text="Журнал", font=ctk.CTkFont(size=14, weight="bold"))\
            .grid(row=0, column=0, sticky="w", padx=CARD_PAD, pady=(6, 1))
        self.txt_log = ctk.CTkTextbox(card, height=110,
                                      font=ctk.CTkFont(family="Consolas", size=12))
        self.txt_log.grid(row=1, column=0, sticky="nsew", padx=CARD_PAD, pady=(0, CARD_PAD))

    # ====================================================================
    #  VLAN — данные
    # ====================================================================
    def _seed_default_vlan(self):
        self.vlans = [{"name": "MNGNMT_10", "id": "10", "ports": "25-28", "mode": "tag"}]
        self._refresh_vlan_list()

    def _add_vlan(self):
        name = self.e_vname.get().strip()
        vid = self.e_vid.get().strip()
        ports = self.e_vports.get().strip()
        mode = self.opt_mode.get().strip().lower()
        mode = "tag" if mode.startswith("tag") else "untag"
        if not name or not vid or not ports:
            messagebox.showwarning("Заполните поля", "Укажите имя, номер и порты VLAN.")
            return
        if not vid.isdigit() or not (1 <= int(vid) <= 4094):
            messagebox.showwarning("Номер VLAN", "Номер VLAN должен быть числом 1–4094.")
            return
        self.vlans.append({"name": name.replace(" ", "_"), "id": vid,
                           "ports": ports, "mode": mode})
        self.e_vname.delete(0, "end")
        self.e_vid.delete(0, "end")
        self.e_vports.delete(0, "end")
        self._refresh_vlan_list()

    def _remove_vlan(self, index):
        if 0 <= index < len(self.vlans):
            del self.vlans[index]
            self._refresh_vlan_list()

    def _refresh_vlan_list(self):
        for w in self.vlan_list.winfo_children():
            w.destroy()
        if not self.vlans:
            ctk.CTkLabel(self.vlan_list,
                         text="VLAN не добавлены — абоненты будут в дефолтном VLAN 1.",
                         text_color=("gray40", "gray60"))\
                .grid(row=0, column=0, sticky="w", padx=6, pady=6)
            return
        for i, v in enumerate(self.vlans):
            row = ctk.CTkFrame(self.vlan_list)
            row.grid(row=i, column=0, sticky="ew", padx=2, pady=3)
            row.grid_columnconfigure(0, weight=1)
            badge = "TAG" if v["mode"] == "tag" else "UNTAG"
            text = "  %s      VLAN %s      порты %s      %s" % (
                v["name"], v["id"], v["ports"], badge)
            ctk.CTkLabel(row, text=text, anchor="w", font=ctk.CTkFont(size=13))\
                .grid(row=0, column=0, sticky="w", padx=6, pady=4)
            ctk.CTkButton(row, text="✕", width=32, fg_color=DANGER, hover_color=DANGER_HOVER,
                          command=lambda idx=i: self._remove_vlan(idx))\
                .grid(row=0, column=1, padx=6, pady=4)

    def _vlan_table_text(self):
        return "\n".join("%s %s %s %s" % (v["name"], v["id"], v["ports"], v["mode"])
                         for v in self.vlans)

    # ====================================================================
    #  Мелкие обработчики
    # ====================================================================
    def _toggle_indiv(self):
        if self.var_indiv.get():
            self.e_pass.configure(show="")
            self.e_pass.delete(0, "end")
            self.e_pass.insert(0, cb.gen_password())
        else:
            self.e_pass.configure(show="*")
            self.e_pass.delete(0, "end")
            self.e_pass.insert(0, self.defaults["password"])

    def _autofill_gateway(self, _event=None):
        parts = self.e_ip.get().strip().split(".")
        if len(parts) == 4 and all(p.isdigit() for p in parts):
            gw = ".".join(parts[:3] + ["254"])
            self.e_gw.delete(0, "end")
            self.e_gw.insert(0, gw)

    def _model_changed(self, _event=None):
        self.opt_baud.set(str(self._profile().get("baudrate", 115200)))
        # ручная смена модели сбрасывает подтверждённое определение
        if self.detected_key and self._selected_key() != self.detected_key:
            self.detected_key = None
            self.log("Модель изменена вручную — определение сброшено. "
                     "Нажмите «Проверить связь» перед настройкой.")

    def _selected_key(self):
        title = self.opt_model.get()
        for k in self.profile_keys:
            if self.profiles[k].get("title", k) == title:
                return k
        return None

    def _profile(self):
        return self.profiles[self._selected_key() or self.profile_keys[0]]

    def refresh_ports(self):
        ports = ss.list_com_ports()
        values = ["%s — %s" % (p, d) for p, d in ports]
        if values:
            self.opt_port.configure(values=values)
            self.opt_port.set(values[0])
        else:
            self.opt_port.configure(values=["Порты не найдены"])
            self.opt_port.set("Порты не найдены")
            self.log("COM-порты не найдены. Подключите USB-COM переходник и нажмите «Обновить».")

    def _selected_port(self):
        raw = self.opt_port.get()
        if not raw or raw in ("—", "Порты не найдены"):
            raise ValueError("Не выбран COM-порт")
        return raw.split(" — ")[0]

    def log(self, text=""):
        self.log_queue.put(str(text))

    def _drain_log(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                self.txt_log.insert("end", line + "\n")
                try:
                    self.txt_log.see("end")
                except Exception:
                    pass
        except queue.Empty:
            pass
        self.after(100, self._drain_log)

    # ====================================================================
    #  Сбор данных и настройка
    # ====================================================================
    def _collect(self):
        form = {
            "login_username": self.e_login_user.get().strip(),
            "login_password": self.e_login_pass.get(),
            "hostname": self.e_hostname.get().strip(),
            "mgmt_ip": self.e_ip.get().strip(),
            "mgmt_mask": self.e_mask.get().strip(),
            "gateway": self.e_gw.get().strip(),
            "mgmt_vlan": self.e_mgmt_vlan.get().strip(),
            "username": self.e_user.get().strip(),
            "password": self.e_pass.get(),
            "snmp_ro": self.e_snmp.get().strip(),
            "enable_ssh": self.var_ssh.get(),
            "enable_snmp": self.var_snmp.get(),
            "enable_user": self.var_user.get(),
            "enable_web": self.var_web.get(),
            "enable_segmentation": self.var_seg.get(),
            "enable_loopback": self.var_lbd.get(),
            "enable_storm": self.var_storm.get(),
            "uplink_ports": self.e_uplink.get().strip(),
            "vlan_table_text": self._vlan_table_text(),
        }
        variables = cb.prepare_vars(form)
        plan = cb.build_commands(self._profile(), variables)
        return variables, plan

    def preview(self):
        try:
            variables, plan = self._collect()
        except Exception as exc:
            messagebox.showerror("Проверьте данные", str(exc))
            return
        text = cb.plan_to_text(plan, mask_password=variables["password"])
        self._show_text_window("Команды, которые будут отправлены (%d шт.)" % len(plan), text)

    def _show_text_window(self, title, text):
        win = ctk.CTkToplevel(self)
        win.title(title)
        win.geometry("680x640")
        box = ctk.CTkTextbox(win, font=ctk.CTkFont(family="Consolas", size=12))
        box.pack(fill="both", expand=True, padx=10, pady=10)
        box.insert("1.0", text)
        win.after(250, win.lift)

    def test_link(self):
        try:
            port = self._selected_port()
        except Exception as exc:
            messagebox.showerror("Ошибка", str(exc))
            return
        baud = self.opt_baud.get()
        user, pw = self.e_login_user.get().strip(), self.e_login_pass.get()
        profiles = self.profiles
        # универсальный профиль для определения (модель пока неизвестна)
        detect_profile = {"prompts": ["#", ">", ":admin#"],
                          "enable": [], "error_patterns": [], "auto_answers": []}

        def job():
            sess = ss.SerialSession(port, baud, self.log, detect_profile)
            try:
                key, _text = sess.identify(user, pw, profiles)
                if key:
                    title = profiles[key].get("title", key)
                    self.detected_key = key
                    self.log("✓ Связь есть. Определён коммутатор → %s" % title)
                    self.log("  Модель распознана — «Настроить коммутатор» разблокирована.")
                    self.after(0, lambda t=title: (self.opt_model.set(t), self._model_changed()))
                else:
                    self.detected_key = None
                    self.log("✓ Связь есть, но МОДЕЛЬ НЕ ОПРЕДЕЛЕНА — настройка заблокирована.")
                    self.log("  Проверьте кабель/скорость и повторите «Проверить связь». "
                             "Если модель поддерживается, но не распознаётся — пришлите лог, "
                             "добавим её признаки.")
            except Exception as exc:
                self.log("ОШИБКА: %s" % exc)
        self._run_async(job)

    def apply(self):
        if not self.detected_key:
            messagebox.showwarning(
                "Сначала определите модель",
                "Настройка возможна только после определения модели.\n"
                "Нажмите «Проверить связь» — программа сама определит коммутатор.")
            return
        try:
            port = self._selected_port()
            variables, plan = self._collect()
        except Exception as exc:
            messagebox.showerror("Проверьте данные", str(exc))
            return

        if not messagebox.askyesno(
                "Подтверждение",
                "Будет отправлено %d команд на %s.\nIP управления: %s\nПродолжить?"
                % (len(plan), port, variables["mgmt_ip"])):
            return

        self.progress.set(0)
        self.log("")
        self.log("=== %s | %s ===" % (
            datetime.datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
            self._profile().get("title")))
        total = len(plan)
        baud, prof = self.opt_baud.get(), self._profile()
        login_user, login_pass = variables["login_username"], variables["login_password"]
        operator = self.e_operator.get().strip()
        model_title = prof.get("title", "")
        backup_cmd = prof.get("show_running")
        verify_cmd = prof.get("verify")
        facts_cmd = prof.get("facts")

        def progress(idx, _total):
            self.after(0, lambda i=idx: self.progress.set(i / max(total, 1)))

        def job():
            sess = ss.SerialSession(port, baud, self.log, prof)
            try:
                result = sess.run_plan(plan, login_user, login_pass, progress,
                                       backup_cmd=backup_cmd, verify_cmd=verify_cmd,
                                       facts_cmd=facts_cmd)
                self._post_apply(variables, result, model_title, operator)
                self.log("Подключайтесь по SSH: ssh %s@%s"
                         % (variables["username"], variables["mgmt_ip"]))
            except Exception as exc:
                self.log("")
                self.log("ОШИБКА: %s" % exc)
                self.log("Настройка остановлена. Конфигурация НЕ сохранена — "
                         "коммутатор можно перезагрузить без записи.")
        self._run_async(job)

    def _post_apply(self, variables, result, model_title, operator):
        # бэкап
        if result.get("backup"):
            try:
                path = self._save_backup(variables["hostname"], result["backup"], "before")
                self.log("Бэкап конфига сохранён: %s" % path)
            except Exception as exc:
                self.log("Не удалось сохранить бэкап: %s" % exc)
        # проверка IP
        if result.get("verify"):
            if variables["mgmt_ip"] in result["verify"]:
                self.log("✓ Проверка: IP %s применён и виден в коммутаторе." % variables["mgmt_ip"])
            else:
                self.log("⚠ Проверка: IP %s не найден в выводе — проверьте вручную "
                         "(show ip interface brief)." % variables["mgmt_ip"])
        # реестр
        try:
            mac = self._parse_mac((result.get("facts", "") or "") + (result.get("verify", "") or ""))
            path = self._append_registry({
                "Дата": datetime.datetime.now().strftime("%d.%m.%Y %H:%M"),
                "Имя": variables["hostname"],
                "IP": variables["mgmt_ip"],
                "Модель": model_title,
                "Логин": variables["username"],
                "Пароль": variables["password"],
                "MAC": mac,
                "Оператор": operator,
            })
            self.log("Запись добавлена в реестр: %s" % path)
        except Exception as exc:
            self.log("Не удалось записать в реестр: %s" % exc)

    # ---- файлы: бэкап и реестр (рядом с программой) ----
    def _base_dir(self):
        return cb.base_dir()

    def _save_backup(self, name, text, suffix=""):
        folder = os.path.join(self._base_dir(), "backups")
        os.makedirs(folder, exist_ok=True)
        safe = re.sub(r"[^\w\-.]+", "_", name or "switch")
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        fname = "%s-%s%s.txt" % (safe, stamp, ("-" + suffix if suffix else ""))
        path = os.path.join(folder, fname)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def _append_registry(self, row):
        path = os.path.join(self._base_dir(), "registry.csv")
        fields = ["Дата", "Имя", "IP", "Модель", "Логин", "Пароль", "MAC", "Оператор"]
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, delimiter=";")
            if new:
                w.writeheader()
            w.writerow(row)
        return path

    def _parse_mac(self, text):
        m = re.search(r"([0-9A-Fa-f]{2}[-:]){5}[0-9A-Fa-f]{2}", text or "")
        return m.group(0) if m else ""

    # ---- Прочитать настройки ----
    def do_read_config(self):
        try:
            port = self._selected_port()
        except Exception as exc:
            messagebox.showerror("Ошибка", str(exc))
            return
        prof = self._profile()
        cmd = prof.get("show_running")
        if not cmd:
            messagebox.showinfo("Нет команды", "Для этой модели не задана команда чтения конфига.")
            return
        baud = self.opt_baud.get()
        user, pw = self.e_login_user.get().strip(), self.e_login_pass.get()
        host = self.e_hostname.get().strip() or "switch"

        def job():
            sess = ss.SerialSession(port, baud, self.log, prof)
            try:
                text = sess.read_config(user, pw, cmd)
                try:
                    path = self._save_backup(host, text, "read")
                    self.log("Конфиг прочитан и сохранён: %s" % path)
                except Exception as exc:
                    self.log("Не удалось сохранить файл: %s" % exc)
                self.after(0, lambda t=text: self._show_text_window("Текущий конфиг коммутатора", t))
            except Exception as exc:
                self.log("ОШИБКА: %s" % exc)
        self._run_async(job)

    # ---- Сброс к заводским ----
    def do_reset(self):
        try:
            port = self._selected_port()
        except Exception as exc:
            messagebox.showerror("Ошибка", str(exc))
            return
        prof = self._profile()
        reset_cmds = prof.get("reset")
        if not reset_cmds:
            messagebox.showinfo("Нет команды", "Для этой модели не задана команда сброса.")
            return
        if not messagebox.askyesno(
                "Сброс к заводским",
                "ВНИМАНИЕ: все настройки коммутатора будут стёрты, устройство перезагрузится.\n"
                "Продолжить сброс к заводским?"):
            return
        baud = self.opt_baud.get()
        user, pw = self.e_login_user.get().strip(), self.e_login_pass.get()

        def job():
            sess = ss.SerialSession(port, baud, self.log, prof)
            try:
                sess.reset_factory(user, pw, reset_cmds)
            except Exception as exc:
                self.log("ОШИБКА: %s" % exc)
        self._run_async(job)

    # ---- общий запуск фоновой операции ----
    def _run_async(self, job):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Подождите", "Предыдущая операция ещё выполняется.")
            return
        self.btn_apply.configure(state="disabled")

        def wrapper():
            try:
                job()
            finally:
                self.after(0, lambda: self.btn_apply.configure(state="normal"))
        self.worker = threading.Thread(target=wrapper, daemon=True)
        self.worker.start()

    def save_log(self):
        name = filedialog.asksaveasfilename(
            defaultextension=".txt",
            initialfile="switch-%s.txt" % datetime.datetime.now().strftime("%Y%m%d-%H%M"))
        if not name:
            return
        with open(name, "w", encoding="utf-8") as fh:
            fh.write(self.txt_log.get("1.0", "end"))
        self.log("Лог сохранён: %s" % name)


def main():
    App().mainloop()
