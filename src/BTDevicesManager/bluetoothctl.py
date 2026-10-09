# Based on ReachView code from Egor Fedorov (egor.fedorov@emlid.com)
# Updated for Python 3.6.8 on a Raspberry  Pi
# source: https://gist.github.com/castis/0b7a162995d0b465ba9c84728e60ec01#file-bluetoothctl-py
# Updated for enigma2 by jbleyel

# If you are interested in using ReachView code as a part of a
# closed source project, please contact Emlid Limited (info@emlid.com).

# This file is part of ReachView.

# ReachView is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# ReachView is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with ReachView.  If not, see <http://www.gnu.org/licenses/>.

from os import listdir
from pexpect import EOF, TIMEOUT, spawnu
from re import compile
from subprocess import check_output
from time import monotonic, sleep
import threading


class Bluetoothctl:
    """A wrapper for bluetoothctl utility."""

    max_start_attempts = 3
    retry_delay = 2
    retry_cooldown = 60
    prompt = compile(r"\[[^\]\r\n]+\][#>]\s*(?:\x1b\[[0-9;]*m)?")
    ansi_escape = compile(r'\x1B\[[0-?]*[ -/]*[@-~]')

    def __init__(self):
        self.process = None
        self.isReady = False
        self.isScanning = False
        self.passkey = None
        self.start_error = None
        self.next_retry_at = 0
        self.start_thread = None
        self.start_lock = threading.Lock()
        # Some drivers (Amlogic bt-dev) power-cycle the controller on every
        # unblock, which drops the firmware loaded by hciattach.
        if self._bluetooth_blocked():
            try:
                check_output("rfkill unblock bluetooth", shell=True)
            except Exception as e:
                self.start_error = f"rfkill unblock bluetooth failed: {e}"
                print(f"[BluetoothManager] {self.start_error}")
        self._start_thread()

    @staticmethod
    def _bluetooth_blocked():
        try:
            for name in listdir("/sys/class/rfkill"):
                path = f"/sys/class/rfkill/{name}"
                with open(f"{path}/type") as fd:
                    if fd.read().strip() != "bluetooth":
                        continue
                with open(f"{path}/soft") as fd:
                    if fd.read().strip() == "1":
                        return True
        except OSError:
            return True
        return False

    def _start_thread(self, force=False):
        now = monotonic()
        if not force and self.next_retry_at and now < self.next_retry_at:
            return False
        if self.start_thread and self.start_thread.is_alive():
            return True
        self.start_thread = threading.Thread(target=self._start_bluetoothctl, name="BTDevicesManager.bluetoothctl")
        self.start_thread.daemon = True
        self.start_thread.start()
        return True

    def _close_process(self, process=None):
        process = process or self.process
        if process is None:
            return
        try:
            process.close(force=True)
        except Exception:
            pass
        if process is self.process:
            self.process = None
            self.isReady = False

    def _format_start_error(self, error, process=None):
        before = getattr(process, "before", "")
        if before:
            before = before.replace("\r\n", "\n").strip()
            if before:
                return f"{error}: {before[-300:]}"
        return str(error)

    def _start_bluetoothctl(self):
        with self.start_lock:
            if self.isReady and self.process and self.process.isalive():
                return
            self._close_process()

            for attempt in range(1, self.max_start_attempts + 1):
                process = None
                print(f"Trying to start bluetoothctl ({attempt}/{self.max_start_attempts})...")
                try:
                    process = spawnu("bluetoothctl", echo=False, timeout=10)
                    result = process.expect(["Agent registered", self.prompt, EOF, TIMEOUT], timeout=10)
                    if result in (0, 1):
                        # BlueZ 5.87 emits its first prompt only after input; consume
                        # it so command output is not shifted by one request.
                        process.send("\n")
                        if process.expect([self.prompt, EOF, TIMEOUT], timeout=10):
                            raise RuntimeError("bluetoothctl prompt not available")
                        self.process = process
                        self.isReady = True
                        self.start_error = None
                        self.next_retry_at = 0
                        print("bluetoothctl is ready.")
                        return
                    self.start_error = "bluetoothctl exited before it was ready"
                    self._close_process(process)
                except Exception as e:
                    self.start_error = self._format_start_error(e, process)
                    self._close_process(process)
                    print(f"bluetoothctl start failed: {self.start_error}")
                sleep(self.retry_delay)

            self.next_retry_at = monotonic() + self.retry_cooldown
            print(f"bluetoothctl is not ready; retrying is paused for {self.retry_cooldown} seconds.")

    def is_ready(self, timeout=0):
        if self.isReady and self.process and self.process.isalive():
            return True

        self.isReady = False
        self._start_thread()
        end_time = monotonic() + timeout
        while timeout and monotonic() < end_time:
            if self.isReady and self.process and self.process.isalive():
                return True
            if self.start_thread and not self.start_thread.is_alive():
                break
            sleep(0.1)
        return self.isReady and self.process and self.process.isalive()

    def get_start_error(self):
        return self.start_error or "bluetoothctl is not ready"

    def send(self, command, pause=0):
        if not self.is_ready(timeout=5):
            raise Exception(self.get_start_error())

        try:
            self.process.send(f"{command}\n")
            sleep(pause)
            result = self.process.expect([self.prompt, EOF, TIMEOUT], timeout=10)
            if result:
                self.isReady = False
                if result == 1:
                    self._close_process()
                raise Exception(f"bluetoothctl failed after {command}")
        except Exception:
            if self.process is None or not self.process.isalive():
                self._close_process()
            raise

    def get_output(self, *args, **kwargs):
        """Run a command in bluetoothctl prompt, return output as a list of lines."""
        self.send(*args, **kwargs)
        return self.process.before.split("\r\n")

    def expectResult(self, patterns):
        """Read the command result, including output already consumed by send()."""
        # Confirmation needs an interactive user decision, not an automatic yes.
        confirmation = "Request confirmation"
        output = self.ansi_escape.sub("", self.process.before or "")
        if confirmation in output:
            raise RuntimeError("Bluetooth pairing requires confirmation; cancelling the attempt")
        for index, pattern in enumerate(patterns):
            if compile(pattern).search(output):
                return index
        result = self.process.expect(patterns + [confirmation], timeout=10)
        if result == len(patterns):
            raise RuntimeError("Bluetooth pairing requires confirmation; cancelling the attempt")
        return result

    def commandFailed(self, command, error):
        if isinstance(error, TIMEOUT):
            reason = "timeout waiting for bluetoothctl"
        elif isinstance(error, EOF):
            reason = "bluetoothctl exited unexpectedly"
        else:
            reason = str(error)
        print(f"[BluetoothManager] {command} failed: {reason}")
        self.passkey = None
        self.isReady = False
        self.isScanning = False
        # Discard pending agent questions and late responses before another try.
        self._close_process()
        return False

    def start_scan(self):
        """Start bluetooth scanning process."""
        try:
            self.send("scan on")
            self.isScanning = True
        except Exception as e:
            print(e)

    def stop_scan(self):
        """Start bluetooth scanning process."""
        try:
            self.send("scan off")
            self.isScanning = False
        except Exception as e:
            print(e)

    def scan(self, timeout=5):
        """Start and stop bluetooth scanning process."""
        self.start_scan()
        sleep(timeout)
        self.stop_scan()

    def make_discoverable(self):
        """Make device discoverable."""
        try:
            self.send("discoverable on")
        except Exception as e:
            print(e)

    def parse_device_info(self, info_string):
        """Parse a string corresponding to a device."""
        device = {}
        block_list = ["[\x1b[0;", "removed"]
        if not any(keyword in info_string for keyword in block_list):
            # BlueZ 5.87 wraps devices that advertise as non-discoverable in
            # COLOR_BOLDGRAY, which would otherwise end up in the name.
            info_string = self.ansi_escape.sub("", info_string)
            try:
                device_position = info_string.index("Device")
            except ValueError:
                pass
            else:
                if device_position > -1:
                    attribute_list = info_string[device_position:].split(" ", 2)
                    if len(attribute_list) == 3:
                        device = {
                            "mac_address": attribute_list[1],
                            "name": attribute_list[2]
                        }

        return device

    def get_available_devices(self):
        """Return a list of tuples of paired and discoverable devices."""
        available_devices = []
        try:
            out = self.get_output("devices")
        except Exception as e:
            print(e)
        else:
            for line in out:
                device = self.parse_device_info(line)
                if device:
                    available_devices.append(device)
        return available_devices

    def get_paired_devices(self):
        """Return a list of paired devices."""
        paired_devices = []

        try:
            # BlueZ >= 5.65
            out = self.get_output("devices Paired")
        except Exception:
            try:
                # BlueZ 5.50 / older versions
                out = self.get_output("paired-devices")
            except Exception as error:
                print(error)
                return paired_devices

        for line in out:
            device = self.parse_device_info(line)
            if device:
                paired_devices.append(device)

        return paired_devices

    def get_discoverable_devices(self):
        """Filter paired devices out of available."""
        available = self.get_available_devices()
        paired = self.get_paired_devices()

        return [d for d in available if d not in paired]

    def get_device_info(self, mac_address):
        """Get device info by mac address."""
        try:
            out = self.get_output(f"info {mac_address}")
        except Exception as e:
            print(e)
            return False
        else:
            return out

    def pair(self, mac_address):
        """Try to pair with a device by mac address."""
        self.passkey = None
        try:
            if mac_address in [x['mac_address'] for x in self.get_paired_devices()]:
                return True
            self.send(f"pair {mac_address}", 4)
            res = self.expectResult(["Failed to pair", "Pairing successful", "Passkey: ", "PIN code: ", "Request authorization"])
            if res == 1:
                return True
            elif res == 4:
                self.send("yes")
                return self.expectResult(["Failed to pair", "Pairing successful"]) == 1
            elif res in [2, 3]:
                output = self.ansi_escape.sub("", self.process.before or "")
                passkey = compile(r"(?:Passkey|PIN code):[ \t]*([^\r\n]*)").search(output)
                self.passkey = passkey.group(1).strip() if passkey else self.ansi_escape.sub('', str(self.process.buffer)).strip()
                return False
            else:
                print(f"Failed to pair. Res = {res}")
                return False
        except Exception as e:
            return self.commandFailed("Pair", e)

    def trust(self, mac_address):
        """Trust the device with the given MAC address"""
        try:
            self.get_output(f"trust {mac_address}")
            return self.expectResult([".*not available\r\n", "trust succe"]) == 1
        except Exception as e:
            return self.commandFailed("Trust", e)

    def remove(self, mac_address):
        """Remove paired device by mac address, return success of the operation."""
        try:
            self.send(f"remove {mac_address}", 3)
            return self.expectResult(["not available", "Device has been removed"]) == 1
        except Exception as e:
            return self.commandFailed("Remove", e)

    def connect(self, mac_address):
        """Try to connect to a device by mac address."""
        try:
            self.send(f"connect {mac_address}", 2)
            return self.expectResult(["Failed to connect", "Connection successful"]) == 1
        except Exception as e:
            return self.commandFailed("Connect", e)

    def disconnect(self, mac_address):
        """Try to disconnect to a device by mac address."""
        try:
            self.send(f"disconnect {mac_address}", 2)
            # BlueZ renamed the message after 5.70; keep both spellings.
            return self.expectResult(["Failed to disconnect", "Disconnection successful", "Successful disconnected"]) in (1, 2)
        except Exception as e:
            return self.commandFailed("Disconnect", e)

    def agent_noinputnooutput(self):
        """Start agent"""
        try:
            self.send("agent NoInputNoOutput")
        except Exception as e:
            print(e)

    def agent_off(self):
        """Stop agent"""
        try:
            self.send("agent off")
        except Exception as e:
            print(e)

    def default_agent(self):
        """Start default agent"""
        try:
            self.send("default-agent")
        except Exception as e:
            print(e)

    def pairable_on(self):
        """Enable Pairable"""
        try:
            self.send("pairable on")
        except Exception as e:
            print(e)

    def pairable_off(self):
        """Disbale Pairable"""
        try:
            self.send("pairable off")
        except Exception as e:
            print(e)


iBluetoothctl = Bluetoothctl()
