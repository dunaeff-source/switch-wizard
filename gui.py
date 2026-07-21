# -*- coding: utf-8 -*-
"""Мастер настройки коммутаторов — графический интерфейс."""

import os
import queue
import threading
import datetime
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import config_builder as cb
import serial_session as ss

APP_TITLE = "Мастер настройки коммутаторов"
BAUD_RATES = ["9600", "19200", "38400", "57600", "115200"]


class App(tk.Tk):

    def __init__(self):
        tk.Tk.__init__(self)
        self.title(APP_TITLE)
        self.geometry("980x720")
        self.minsize(880, 640)

        self.log_queue = queue.Queue()
        self.worker = None

        try:
            self.defaults, self.profiles = cb.load_config()
        except Exception as exc:
            messagebox.showerror("Ошибка", "Не удалось прочитать profiles.yaml:\n%s" % exc)
            raise SystemExit(1)

        self.profile_keys = list(self.profiles.keys())
        self._build_ui()
        self.after(100, self._drain_log)

    # ------------------------------------------------------------------ вёрстка
    def _build_ui(self):
        pad = {"padx": 6, "pady": 4}
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)

        # --- Подключение
        conn = ttk.LabelFrame(root, text="1. Подключение", padding=8)
        conn.pack(fill="x")

        ttk.Label(conn, text="COM-порт:").grid(row=0, column=0, sticky="w", **pad)
        self.cmb_port = ttk.Combobox(conn, width=28, state="readonly")
        self.cmb_port.grid(row=0, column=1, sticky="w", **pad)
        ttk.Button(conn, text="Обновить", command=self.refresh_ports).grid(row=0, column=2, **pad)

        ttk.Label(conn, text="Модель:").grid(row=0, column=3, sticky="w", **pad)
        self.cmb_model = ttk.Combobox(
            conn, width=34, state="readonly",
            values=[self.profiles[k].get("title", k) for k in self.profile_keys])
        self.cmb_model.current(0)
        self.cmb_model.grid(row=0, column=4, sticky="w", **pad)
        self.cmb_model.bind("<<ComboboxSelected>>", self._model_changed)

        ttk.Label(conn, text="Скорость:").grid(row=0, column=5, sticky="w", **pad)
        self.cmb_baud = ttk.Combobox(conn, width=9, state="readonly", values=BAUD_RATES)
        self.cmb_baud.grid(row=0, column=6, sticky="w", **pad)

        ttk.Label(conn, text="Вход в коммутатор (заводские) — логин:")\
            .grid(row=1, column=0, columnspan=2, sticky="w", **pad)
        self.e_login_user = ttk.Entry(conn, width=14)
        self.e_login_user.insert(0, self.defaults["login_username"])
        self.e_login_user.grid(row=1, column=2, sticky="w", **pad)
        ttk.Label(conn, text="пароль:").grid(row=1, column=3, sticky="e", **pad)
        self.e_login_pass = ttk.Entry(conn, width=14, show="*")
        self.e_login_pass.insert(0, self.defaults["login_password"])
        self.e_login_pass.grid(row=1, column=4, sticky="w", **pad)

        # --- Параметры
        params = ttk.LabelFrame(root, text="2. Параметры коммутатора", padding=8)
        params.pack(fill="x", pady=(10, 0))

        self.e_hostname = self._field(params, "Имя устройства:", 0, 0, "SW-OFFICE-01")
        self.e_ip = self._field(params, "IP управления:", 0, 2, "192.168.1.10")
        self.e_mask = self._field(params, "Маска:", 0, 4, "255.255.255.0")
        self.e_gw = self._field(params, "Шлюз:", 1, 0, "192.168.1.1")
        self.e_mgmt_vlan = self._field(params, "VLAN управления:", 1, 2, "1", width=8)
        self.e_user = self._field(params, "Новый логин:", 1, 4, self.defaults["username"])
        self.e_pass = self._field(params, "Новый пароль:", 2, 0,
                                  self.defaults["password"], show="*")
        self.e_snmp = self._field(params, "SNMP community:", 2, 2, self.defaults["snmp_ro"])

        self.var_ssh = tk.BooleanVar(value=True)
        self.var_snmp = tk.BooleanVar(value=True)
        self.var_user = tk.BooleanVar(value=True)
        ttk.Checkbutton(params, text="Включить SSH", variable=self.var_ssh)\
            .grid(row=2, column=4, sticky="w", **pad)
        chk_snmp = ttk.Checkbutton(params, text="Настроить SNMP", variable=self.var_snmp)
        chk_snmp.grid(row=2, column=5, sticky="w", **pad)
        ttk.Checkbutton(params, text="Создать пользователя", variable=self.var_user)\
            .grid(row=3, column=4, sticky="w", **pad)

        # SNMP обязателен по корпоративному стандарту — галочку снять нельзя
        if self.defaults.get("snmp_mandatory"):
            chk_snmp.state(["disabled"])

        # Опциональная блокировка учётных данных от правки оператором
        if self.defaults.get("lock_credentials"):
            for entry in (self.e_user, self.e_pass, self.e_snmp):
                entry.state(["readonly"])

        # --- VLAN и порты
        tables = ttk.Frame(root)
        tables.pack(fill="both", expand=False, pady=(10, 0))

        vlan_box = ttk.LabelFrame(tables, text="3. VLAN — «номер имя», по одному в строке", padding=8)
        vlan_box.pack(side="left", fill="both", expand=True)
        self.txt_vlans = tk.Text(vlan_box, height=7, width=32)
        self.txt_vlans.pack(fill="both", expand=True)
        self.txt_vlans.insert("1.0", "10 Office\n20 VoIP\n30 Guest")

        port_box = ttk.LabelFrame(
            tables, text="4. Порты — «диапазон режим VLAN»", padding=8)
        port_box.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self.txt_ports = tk.Text(port_box, height=7, width=42)
        self.txt_ports.pack(fill="both", expand=True)
        self.txt_ports.insert("1.0", "1-20 access 10\n21-22 access 20\n23-24 trunk 10,20,30")

        # --- Кнопки
        btns = ttk.Frame(root)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="Проверить связь", command=self.test_link).pack(side="left")
        ttk.Button(btns, text="Показать команды", command=self.preview).pack(side="left", padx=6)
        self.btn_apply = ttk.Button(btns, text="НАСТРОИТЬ КОММУТАТОР", command=self.apply)
        self.btn_apply.pack(side="left", padx=6)
        ttk.Button(btns, text="Сохранить лог", command=self.save_log).pack(side="right")

        self.progress = ttk.Progressbar(root, mode="determinate")
        self.progress.pack(fill="x", pady=(8, 0))

        # --- Лог
        log_box = ttk.LabelFrame(root, text="Журнал", padding=6)
        log_box.pack(fill="both", expand=True, pady=(8, 0))
        self.txt_log = tk.Text(log_box, height=12, bg="#101418", fg="#d6e2ec",
                               insertbackground="#d6e2ec")
        scroll = ttk.Scrollbar(log_box, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.txt_log.pack(fill="both", expand=True)

        self.refresh_ports()
        self._model_changed()

    def _field(self, parent, label, row, col, default="", width=20, show=None):
        ttk.Label(parent, text=label).grid(row=row, column=col, sticky="w", padx=6, pady=4)
        entry = ttk.Entry(parent, width=width, show=show)
        entry.insert(0, default)
        entry.grid(row=row, column=col + 1, sticky="w", padx=6, pady=4)
        return entry

    # ------------------------------------------------------------------ действия
    def _model_changed(self, _event=None):
        prof = self._profile()
        self.cmb_baud.set(str(prof.get("baudrate", 115200)))

    def _profile_key(self):
        return self.profile_keys[self.cmb_model.current()]

    def _profile(self):
        return self.profiles[self._profile_key()]

    def refresh_ports(self):
        ports = ss.list_com_ports()
        values = ["%s — %s" % (p, d) for p, d in ports]
        self.cmb_port["values"] = values
        if values:
            self.cmb_port.current(0)
        else:
            self.cmb_port.set("")
            self.log("COM-порты не найдены. Подключите USB-COM переходник и нажмите «Обновить».")

    def _selected_port(self):
        raw = self.cmb_port.get()
        if not raw:
            raise ValueError("Не выбран COM-порт")
        return raw.split(" — ")[0]

    def log(self, text=""):
        self.log_queue.put(str(text))

    def _drain_log(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                self.txt_log.insert("end", line + "\n")
                self.txt_log.see("end")
        except queue.Empty:
            pass
        self.after(100, self._drain_log)

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
            "vlans_text": self.txt_vlans.get("1.0", "end"),
            "ports_text": self.txt_ports.get("1.0", "end"),
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
        win = tk.Toplevel(self)
        win.title("Команды, которые будут отправлены (%d шт.)" % len(plan))
        win.geometry("640x600")
        box = tk.Text(win)
        box.pack(fill="both", expand=True)
        box.insert("1.0", text)

    def test_link(self):
        try:
            port = self._selected_port()
        except Exception as exc:
            messagebox.showerror("Ошибка", str(exc))
            return

        def job():
            sess = ss.SerialSession(port, self.cmb_baud.get(), self.log, self._profile())
            try:
                sess.open()
                sess.wake_up(self.e_login_user.get().strip(), self.e_login_pass.get())
            except Exception as exc:
                self.log("ОШИБКА: %s" % exc)
            finally:
                sess.close()

        self._run_async(job)

    def apply(self):
        try:
            port = self._selected_port()
            variables, plan = self._collect()
        except Exception as exc:
            messagebox.showerror("Проверьте данные", str(exc))
            return

        if not messagebox.askyesno(
                "Подтверждение",
                "Будет отправлено %d команд на %s.\n"
                "IP управления: %s\nПродолжить?" % (len(plan), port, variables["mgmt_ip"])):
            return

        self.progress["maximum"] = len(plan)
        self.progress["value"] = 0
        self.log("")
        self.log("=== %s | %s ===" % (datetime.datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
                                      self._profile().get("title")))

        def progress(idx, total):
            self.progress["value"] = idx

        def job():
            sess = ss.SerialSession(port, self.cmb_baud.get(), self.log, self._profile())
            try:
                sess.run_plan(plan, variables["login_username"],
                              variables["login_password"], progress)
                self.log("Подключайтесь по SSH: ssh %s@%s"
                         % (variables["username"], variables["mgmt_ip"]))
            except Exception as exc:
                self.log("")
                self.log("ОШИБКА: %s" % exc)
                self.log("Настройка остановлена. Конфигурация НЕ сохранена — "
                         "коммутатор можно перезагрузить без записи.")

        self._run_async(job)

    def _run_async(self, job):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Подождите", "Предыдущая операция ещё выполняется.")
            return
        self.btn_apply.state(["disabled"])

        def wrapper():
            try:
                job()
            finally:
                self.btn_apply.state(["!disabled"])

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
