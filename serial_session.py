# -*- coding: utf-8 -*-
"""Сеанс работы с коммутатором. Транспорт сменный: USB-COM (SerialSession) или
сеть по Telnet (TelnetSession). Вся логика входа/команд/сброса — общая, в _Session."""

import re
import time

import serial
from serial.tools import list_ports

try:
    import telnetlib            # для сетевого режима (в Python 3.11 присутствует)
except Exception:               # на будущее: в 3.13 telnetlib удалён
    telnetlib = None


class SwitchError(Exception):
    pass


def list_com_ports():
    """[('COM3', 'Prolific USB-to-Serial'), ...]"""
    out = []
    for p in list_ports.comports():
        out.append((p.device, p.description or ""))
    return sorted(out)


# =====================================================================
#  Базовый класс — вся логика поверх сменного транспорта (_t_*)
# =====================================================================
class _Session(object):

    def __init__(self, log=print, profile=None):
        self.log = log
        self.profile = profile or {}
        self.prompts = self.profile.get("prompts", ["#", ">"])
        self.error_patterns = self.profile.get("error_patterns", [])
        self.auto_answers = self.profile.get("auto_answers", [])

    # ---- транспорт: реализуется в наследниках -------------------------
    def _conn_desc(self):        raise NotImplementedError
    def _t_open(self):           raise NotImplementedError
    def _t_close(self):          raise NotImplementedError
    def _is_open(self):          raise NotImplementedError
    def _t_read(self, n):        raise NotImplementedError      # -> bytes (b"" если пусто)
    def _t_write_bytes(self, b): raise NotImplementedError
    def _t_reset_input(self):    raise NotImplementedError

    # ---- служебное ----------------------------------------------------
    def open(self):
        self.log("Открываю %s..." % self._conn_desc())
        self._t_open()

    def close(self):
        try:
            if self._is_open():
                self._t_close()
                self.log("Соединение закрыто.")
        except Exception:
            pass

    def _write(self, text):
        # ВАЖНО: отправляем ТОЛЬКО "\r" (CR), без "\n". Консоли D-Link (и многих
        # других) трактуют "\r" и "\n" как ДВА нажатия Enter — из-за лишнего "\n"
        # на запрос пароля уходил пустой ввод и логин срывался.
        self._t_write_bytes((text + "\r").encode("ascii", "ignore"))

    def _read_until(self, patterns, timeout=8.0):
        """Читает до появления одного из шаблонов или таймаута. Возвращает текст."""
        deadline = time.time() + timeout
        buf = ""
        while time.time() < deadline:
            chunk = self._t_read(4096)
            if chunk:
                buf += chunk.decode("utf-8", "replace")
                tail = buf[-200:]
                # Пейджер: D-Link ("SPACE Next Page  a All  q Quit") или "--More--".
                # Отвечаем 'a' (All) — вывалить всё разом.
                if ("Next Page" in tail or "Next Entry" in tail
                        or "--More--" in tail or "More: <space>" in tail
                        or "Quit" in tail):
                    self._t_write_bytes(b"a")
                    time.sleep(0.05)
                    continue
                for pat in patterns:
                    if pat in tail:
                        return buf
            else:
                time.sleep(0.05)
        return buf

    def _handle_auto_answers(self, text):
        answered = False
        tail = text[-200:]
        for rule in self.auto_answers:
            if re.search(rule["match"], tail, re.IGNORECASE):
                self.log("   ? авто-ответ: %s" % rule["send"])
                self._write(rule["send"])
                self._read_until(self.prompts, timeout=10)
                answered = True
        return answered

    def _looks_like_prompt(self, tail):
        low = tail.lower()
        return (any(p in tail for p in self.prompts)
                and "assword" not in low
                and "sername" not in low and "ogin:" not in low)

    def wake_up(self, username, password, timeout=10):
        """Будит консоль и логинится, устойчиво к остаточному состоянию строки.
        Возвращает весь собранный текст (баннер + приглашение) — для определения
        модели. Сначала ЧИТАЕМ, что на экране, и «будим» Enter только при тишине."""
        wake = self.prompts + ["ogin:", "sername:", "assword:"]
        self._t_reset_input()

        out = self._read_until(wake, timeout=3)
        if not out.strip():
            self._write("")
            out = self._read_until(wake, timeout=timeout)
        full = out
        got_any = bool(out.strip())

        sent_user = sent_pass = 0
        deadline = time.time() + timeout * 3
        while time.time() < deadline:
            tail = out[-200:]
            if out.strip():
                got_any = True
            if self._looks_like_prompt(tail):
                break
            if re.search(r"(ogin|sername)\s*:", tail):
                if sent_user >= 4:
                    break
                if sent_user == 0:
                    self.log("Запрошен логин, ввожу учётные данные...")
                self._write(username)
                sent_user += 1
            elif "assword" in tail.lower():
                if sent_pass >= 4:
                    break
                self._write(password)
                sent_pass += 1
            else:
                self._write("")
            out = self._read_until(wake, timeout=6)
            full += out

        if not got_any:
            raise SwitchError(
                "Коммутатор не отвечает. Проверьте подключение (кабель/порт/скорость "
                "для COM или IP/сеть для Telnet).")
        if not self._looks_like_prompt(full[-200:]) and not any(p in full[-200:] for p in self.prompts):
            raise SwitchError("Не удалось войти в CLI. Возможно, неверный логин/пароль.")

        for cmd in self.profile.get("enable", []):
            self._write(cmd)
            out = self._read_until(self.prompts + ["assword:"], timeout=timeout)
            full += out
            if "assword" in out[-120:]:
                self._write(password)
                full += self._read_until(self.prompts, timeout=timeout)

        self.log("Связь с коммутатором установлена.")
        return full

    def identify(self, username, password, profiles):
        """Определяет марку/модель по баннеру и, если нужно, выводу show-команд.
        Возвращает (ключ_профиля | None, собранный_текст)."""
        self.open()
        try:
            text = self.wake_up(username, password) or ""
            key = self._match_profile(text, profiles)
            if not key:
                for cmd in ("show version", "show switch", "show system"):
                    try:
                        text += "\n" + self.capture(cmd, timeout=8)
                    except Exception:
                        pass
                    key = self._match_profile(text, profiles)
                    if key:
                        break
            return key, text
        finally:
            self.close()

    @staticmethod
    def _match_profile(text, profiles):
        low = (text or "").lower()
        for key, prof in profiles.items():
            for pat in (prof.get("detect") or []):
                if pat and pat.lower() in low:
                    return key
        return None

    def send(self, cmd, replies=None, timeout=15, secret=None):
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

    def capture(self, cmd, timeout=60, idle=1.5):
        """Читает ВЕСЬ вывод show-команды (в т.ч. многостраничный) — завершается по
        ТИШИНЕ (нет новых данных idle секунд), а не по первому приглашению. Так
        корректно снимается длинный конфиг старого D-Link (строки '#---' раньше
        принимались за приглашение). На пейджер отвечаем 'a'."""
        self.log("> %s" % cmd)
        self._write(cmd)
        buf = ""
        deadline = time.time() + timeout
        last = time.time()
        while time.time() < deadline:
            chunk = self._t_read(4096)
            if chunk:
                buf += chunk.decode("utf-8", "replace")
                last = time.time()
                tail = buf[-200:]
                if ("Next Page" in tail or "Next Entry" in tail
                        or "--More--" in tail or "Quit" in tail):
                    self._t_write_bytes(b"a")
                    time.sleep(0.05)
            else:
                if buf and (time.time() - last) > idle:
                    break
                time.sleep(0.05)
        return buf

    def run_plan(self, plan, username, password, on_progress=None,
                 backup_cmd=None, verify_cmd=None, facts_cmd=None):
        result = {"backup": "", "verify": "", "facts": ""}
        self.open()
        try:
            self.wake_up(username, password)

            if facts_cmd:
                try:
                    result["facts"] = self.capture(facts_cmd, timeout=20)
                except Exception as exc:
                    self.log("Не удалось прочитать факты: %s" % exc)
            if backup_cmd:
                self.log("Сохраняю текущий конфиг в бэкап...")
                try:
                    result["backup"] = self.capture(backup_cmd, timeout=40)
                except Exception as exc:
                    self.log("Не удалось сделать бэкап: %s" % exc)

            total = len(plan)
            for idx, step in enumerate(plan, 1):
                self.send(step["cmd"], step.get("replies"), secret=password,
                          timeout=step.get("timeout") or 15)
                if on_progress:
                    on_progress(idx, total)
            self.log("")
            self.log("=== Настройка завершена, конфигурация сохранена ===")

            if verify_cmd:
                self.log("Проверяю применённые настройки...")
                try:
                    result["verify"] = self.capture(verify_cmd, timeout=20)
                except Exception as exc:
                    self.log("Не удалось выполнить проверку: %s" % exc)
        finally:
            self.close()
        return result

    def read_config(self, username, password, cmd):
        self.open()
        try:
            self.wake_up(username, password)
            return self.capture(cmd, timeout=40)
        finally:
            self.close()

    def reset_factory(self, username, password, reset_cmds):
        self.open()
        try:
            self.wake_up(username, password)
            for cmd in (reset_cmds or []):
                self.log("> %s" % cmd)
                self._write(cmd)
                # Ждём ИМЕННО вопрос-подтверждение (не приглашение), затем "y".
                self._read_until(
                    ["y/n", "y / n", "(y", "yes/no", "proceed", "sure", "confirm",
                     "will be", "continue", "reset"],
                    timeout=12)
                for _ in range(2):          # подтверждаем на случай двойного запроса
                    self._write("y")
                    time.sleep(0.4)
                self.log("   подтверждение отправлено (y)")
                time.sleep(0.5)
            self.log("")
            self.log("=== Команда сброса отправлена и подтверждена. Коммутатор "
                     "перезагружается — вернётся к заводским. ===")
        finally:
            self.close()


# =====================================================================
#  Транспорт 1: USB-COM
# =====================================================================
class SerialSession(_Session):

    def __init__(self, port, baudrate=115200, log=print, profile=None):
        super(SerialSession, self).__init__(log, profile)
        self.port_name = port
        self.baudrate = int(baudrate)
        self.ser = None

    def _conn_desc(self):
        return "%s на %s бод" % (self.port_name, self.baudrate)

    def _t_open(self):
        self.ser = serial.Serial(
            port=self.port_name, baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE, timeout=0.2, write_timeout=5)
        time.sleep(0.3)
        self.ser.reset_input_buffer()

    def _t_close(self):
        self.ser.close()

    def _is_open(self):
        return bool(self.ser and self.ser.is_open)

    def _t_read(self, n):
        return self.ser.read(n)

    def _t_write_bytes(self, data):
        self.ser.write(data)
        self.ser.flush()

    def _t_reset_input(self):
        self.ser.reset_input_buffer()


# =====================================================================
#  Транспорт 2: сеть по Telnet (для коммутаторов без консоли, напр. DGS-1100)
# =====================================================================
class TelnetSession(_Session):

    def __init__(self, host, port=23, log=print, profile=None):
        super(TelnetSession, self).__init__(log, profile)
        self.host = host
        self.port = int(port or 23)
        self.tn = None

    def _conn_desc(self):
        return "%s:%s (Telnet)" % (self.host, self.port)

    def _t_open(self):
        if telnetlib is None:
            raise SwitchError("Telnet недоступен в этой сборке Python.")
        self.tn = telnetlib.Telnet(self.host, self.port, timeout=8)
        time.sleep(0.3)

    def _t_close(self):
        try:
            self.tn.close()
        finally:
            self.tn = None

    def _is_open(self):
        return self.tn is not None

    def _t_read(self, n):
        try:
            return self.tn.read_very_eager()   # неблокирующе; IAC обрабатывается сам
        except EOFError:
            return b""
        except Exception:
            return b""

    def _t_write_bytes(self, data):
        self.tn.write(data)                    # telnetlib сам экранирует IAC

    def _t_reset_input(self):
        try:
            self.tn.read_very_eager()
        except Exception:
            pass
