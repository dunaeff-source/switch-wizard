# -*- coding: utf-8 -*-
"""Работа с консольным портом коммутатора через USB-COM."""

import re
import time

import serial
from serial.tools import list_ports


class SwitchError(Exception):
    pass


def list_com_ports():
    """[('COM3', 'Prolific USB-to-Serial'), ...]"""
    out = []
    for p in list_ports.comports():
        out.append((p.device, p.description or ""))
    return sorted(out)


class SerialSession(object):

    def __init__(self, port, baudrate=115200, log=print, profile=None):
        self.port_name = port
        self.baudrate = int(baudrate)
        self.log = log
        self.profile = profile or {}
        self.ser = None
        self.prompts = self.profile.get("prompts", ["#", ">"])
        self.error_patterns = self.profile.get("error_patterns", [])
        self.auto_answers = self.profile.get("auto_answers", [])

    # ---------------------------------------------------------------- служебное
    def open(self):
        self.log("Открываю %s на %s бод..." % (self.port_name, self.baudrate))
        self.ser = serial.Serial(
            port=self.port_name,
            baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.2,
            write_timeout=5,
        )
        time.sleep(0.3)
        self.ser.reset_input_buffer()

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()
            self.log("Порт закрыт.")

    def _write(self, text):
        # ВАЖНО: отправляем ТОЛЬКО "\r" (CR), без "\n". Консоли D-Link (и многих
        # других вендоров) трактуют "\r" и "\n" как ДВА отдельных нажатия Enter.
        # Из-за лишнего "\n" на запрос пароля уходил пустой ввод и логин срывался,
        # хотя вручную в PuTTY (где шлётся только "\r") тот же admin/admin проходил.
        self.ser.write((text + "\r").encode("ascii", "ignore"))
        self.ser.flush()

    def _read_until(self, patterns, timeout=8.0):
        """Читает до появления одного из шаблонов или таймаута. Возвращает текст."""
        deadline = time.time() + timeout
        buf = ""
        while time.time() < deadline:
            chunk = self.ser.read(4096)
            if chunk:
                buf += chunk.decode("utf-8", "replace")
                # пейджер "--More--"
                if "More" in buf[-40:]:
                    self.ser.write(b" ")
                    continue
                tail = buf[-120:]
                for pat in patterns:
                    if pat in tail:
                        return buf
            else:
                time.sleep(0.05)
        return buf

    def _handle_auto_answers(self, text):
        """Отвечает на подтверждения вида 'Are you sure? (y/n)'."""
        answered = False
        tail = text[-200:]
        for rule in self.auto_answers:
            if re.search(rule["match"], tail, re.IGNORECASE):
                self.log("   ? авто-ответ: %s" % rule["send"])
                self._write(rule["send"])
                self._read_until(self.prompts, timeout=10)
                answered = True
        return answered

    # ------------------------------------------------------------------- сценарий
    def wake_up(self, username, password, timeout=10):
        """Будит консоль и при необходимости логинится."""
        self.ser.reset_input_buffer()
        self._write("")
        out = self._read_until(self.prompts + ["ogin:", "sername:", "assword:"], timeout=timeout)

        if not out.strip():
            raise SwitchError(
                "Коммутатор не отвечает. Проверьте кабель, номер COM-порта и "
                "скорость (частые значения: 115200, 38400, 9600)."
            )

        if re.search(r"(ogin|sername)\s*:", out[-120:]):
            self.log("Запрошен логин, ввожу учётные данные...")
            self._write(username)
            out = self._read_until(["assword:"] + self.prompts, timeout=timeout)
        if "assword" in out[-120:]:
            self._write(password)
            out = self._read_until(self.prompts, timeout=timeout)

        if not any(p in out[-120:] for p in self.prompts):
            raise SwitchError("Не удалось получить приглашение CLI. Возможно, неверный пароль.")

        for cmd in self.profile.get("enable", []):
            self._write(cmd)
            out = self._read_until(self.prompts + ["assword:"], timeout=timeout)
            if "assword" in out[-120:]:
                self._write(password)
                self._read_until(self.prompts, timeout=timeout)

        self.log("Связь с коммутатором установлена.")
        return True

    def send(self, cmd, replies=None, timeout=15, secret=None):
        """Отправляет команду, ждёт приглашение, возвращает ответ устройства."""
        shown = cmd.replace(secret, "********") if secret else cmd
        self.log("> %s" % shown)
        self._write(cmd)
        out = self._read_until(self.prompts + ["assword:", "onfirm"], timeout=timeout)

        for rep in (replies or []):
            self._write(rep)
            out += self._read_until(self.prompts + ["assword:", "onfirm"], timeout=timeout)

        if self._handle_auto_answers(out):
            out += self._read_until(self.prompts, timeout=timeout)

        for pat in self.error_patterns:
            if pat.lower() in out.lower():
                raise SwitchError("Коммутатор отклонил команду:\n%s\nОтвет: %s"
                                  % (shown, out.strip()[-300:]))
        return out

    def run_plan(self, plan, username, password, on_progress=None):
        """Выполняет весь список команд. Возвращает полный лог сессии."""
        self.open()
        try:
            self.wake_up(username, password)
            total = len(plan)
            for idx, step in enumerate(plan, 1):
                self.send(step["cmd"], step.get("replies"), secret=password,
                          timeout=step.get("timeout") or 15)
                if on_progress:
                    on_progress(idx, total)
            self.log("")
            self.log("=== Настройка завершена, конфигурация сохранена ===")
        finally:
            self.close()
