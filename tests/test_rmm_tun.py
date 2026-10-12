"""Validation coverage for the server-owned TUN frontend."""

import subprocess
import unittest
from unittest.mock import MagicMock, patch

from rmm_tun import DEFAULT_TUN_ADDRESS, ServerTunRelay, TunError, normalize_tun_config


class TunConfigTests(unittest.TestCase):
    """Verify operator supplied TUN values are bounded before OS commands run."""

    def test_defaults_are_stable(self):
        config = normalize_tun_config({"device": "tun://rmm0"})

        self.assertEqual(config["device"], "tun://rmm0")
        self.assertEqual(config["tun_address"], DEFAULT_TUN_ADDRESS)
        self.assertEqual(config["routes"], [])
        self.assertEqual(config["route"], "")
        self.assertEqual(config["mtu"], 1500)
        self.assertEqual(config["udp_timeout_seconds"], 300)

    def test_rejects_default_route_and_device_query(self):
        for body in (
            {"device": "tun://rmm0", "route": "0.0.0.0/0"},
            {"device": "tun://rmm0?route=10.0.0.0/8"},
            {"device": "tun://rmm0", "route": "198.18.10.0/24"},
        ):
            with self.subTest(body=body):
                with self.assertRaises(ValueError):
                    normalize_tun_config(body)

    def test_normalizes_multiple_routes_and_keeps_legacy_alias(self):
        config = normalize_tun_config(
            {
                "device": "tun://rmm0",
                "routes": ["10.10.1.0/24", "10.20.0.7/16", "10.10.1.0/24"],
            }
        )

        self.assertEqual(config["routes"], ["10.10.1.0/24", "10.20.0.0/16"])
        self.assertEqual(config["route"], "10.10.1.0/24")

    def test_rejects_non_array_routes(self):
        with self.assertRaisesRegex(ValueError, "routes must be an array"):
            normalize_tun_config({"device": "tun://rmm0", "routes": {"route": "10.0.0.0/8"}})


class TunRelayTests(unittest.TestCase):
    """Verify helper and bridge lifecycle without requiring host TUN access."""

    def test_start_and_stop_configures_linux_device_and_route(self):
        config = normalize_tun_config(
            {
                "device": "tun://rmm0",
                "routes": ["10.10.0.0/16", "10.20.0.0/16"],
            }
        )
        socks = MagicMock()
        socks.start.return_value.port = 42123
        process = MagicMock()
        process.poll.return_value = None
        relay = ServerTunRelay("session-1", config, "/tmp/rmm-tun", socks)

        with patch("rmm_tun.platform.system", return_value="Linux"):
            with patch("rmm_tun.os.path.isfile", return_value=True):
                with patch("rmm_tun.os.access", return_value=True):
                    with patch("rmm_tun.shutil.which", return_value="/sbin/ip"):
                        with patch("rmm_tun.subprocess.run") as run:
                            with patch("rmm_tun.subprocess.Popen", return_value=process):
                                with patch("rmm_tun.time.sleep"):
                                    with patch("rmm_tun.os.killpg"):
                                        run.return_value.returncode = 1
                                        relay.start()
                                        relay.stop()

        socks.start.assert_called_once_with(
            "session-1",
            port=0,
            bind_host="127.0.0.1",
            udp_idle_timeout=300,
        )
        socks.stop.assert_called_once_with("session-1")
        self.assertFalse(relay.alive())
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["ip", "tuntap", "add", "dev", "rmm0", "mode", "tun"], commands)
        self.assertIn(["ip", "route", "replace", "10.10.0.0/16", "dev", "rmm0"], commands)
        self.assertIn(["ip", "route", "replace", "10.20.0.0/16", "dev", "rmm0"], commands)
        self.assertIn(["ip", "route", "del", "10.10.0.0/16", "dev", "rmm0"], commands)
        self.assertIn(["ip", "route", "del", "10.20.0.0/16", "dev", "rmm0"], commands)

    def test_reports_missing_linux_tun_device(self):
        failure = subprocess.CalledProcessError(
            1,
            ["ip", "tuntap", "add", "dev", "rmm0", "mode", "tun"],
            stderr="open: No such file or directory",
        )
        with patch("rmm_tun.subprocess.run", side_effect=failure):
            with self.assertRaisesRegex(TunError, "/dev/net/tun is missing or inaccessible"):
                from rmm_tun import _run_system_command

                _run_system_command(["ip", "tuntap", "add", "dev", "rmm0", "mode", "tun"])

    def test_start_rejects_existing_linux_device(self):
        config = normalize_tun_config({"device": "tun://rmm0"})
        socks = MagicMock()
        socks.start.return_value.port = 42123
        relay = ServerTunRelay("session-1", config, "/tmp/rmm-tun", socks)

        with patch("rmm_tun.platform.system", return_value="Linux"):
            with patch("rmm_tun.os.path.isfile", return_value=True):
                with patch("rmm_tun.os.access", return_value=True):
                    with patch("rmm_tun.shutil.which", return_value="/sbin/ip"):
                        with patch("rmm_tun.subprocess.run") as run:
                            run.return_value.returncode = 0
                            with self.assertRaisesRegex(TunError, "already exists"):
                                relay.start()

        socks.stop.assert_called_once_with("session-1")

    def test_rejects_linux_device_name_on_macos(self):
        config = normalize_tun_config({"device": "tun://rmm0"})
        socks = MagicMock()
        relay = ServerTunRelay("session-1", config, "/tmp/rmm-tun", socks)

        with patch("rmm_tun.platform.system", return_value="Darwin"):
            with patch("rmm_tun.os.path.isfile", return_value=True):
                with patch("rmm_tun.os.access", return_value=True):
                    with self.assertRaisesRegex(TunError, "utun<N>"):
                        relay.start()

        socks.start.assert_not_called()

    def test_rejects_ipv6_and_unsafe_interface(self):
        for body in (
            {"device": "tun://rmm0", "tun_address": "2001:db8::1/64"},
            {"device": "tun://rmm0", "interface": "eth0;id"},
        ):
            with self.subTest(body=body):
                with self.assertRaises(ValueError):
                    normalize_tun_config(body)


if __name__ == "__main__":
    unittest.main()
