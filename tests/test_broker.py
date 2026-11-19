"""Broker URL parsing."""

from __future__ import annotations

import unittest

import _support  # noqa: F401  (registers the package shell)

from custom_components.emotionkit.broker import (
    DEFAULT_WEBSOCKET_PATH,
    BrokerAddress,
    InvalidBrokerURL,
    derive_broker_url,
    parse_broker_url,
)


class ParseBrokerUrlTest(unittest.TestCase):
    def test_plain_tcp_schemes(self) -> None:
        for scheme in ("tcp", "mqtt"):
            with self.subTest(scheme=scheme):
                address = parse_broker_url(f"{scheme}://mqtt.example.com:1884")
                self.assertEqual(address.host, "mqtt.example.com")
                self.assertEqual(address.port, 1884)
                self.assertFalse(address.tls)
                self.assertEqual(address.transport, "tcp")
                self.assertIsNone(address.websocket_path)
                self.assertFalse(address.is_websocket)

    def test_plain_tcp_default_port(self) -> None:
        for scheme in ("tcp", "mqtt"):
            with self.subTest(scheme=scheme):
                self.assertEqual(parse_broker_url(f"{scheme}://broker.test").port, 1883)

    def test_tls_schemes(self) -> None:
        # mqtts:// used to fall back to an unencrypted connection.
        for scheme in ("tls", "ssl", "mqtts"):
            with self.subTest(scheme=scheme):
                address = parse_broker_url(f"{scheme}://mqtt.example.com")
                self.assertTrue(address.tls)
                self.assertEqual(address.port, 8883)
                self.assertEqual(address.transport, "tcp")
                self.assertEqual(
                    parse_broker_url(f"{scheme}://mqtt.example.com:8884").port, 8884
                )

    def test_websocket_plain(self) -> None:
        address = parse_broker_url("ws://broker.test:9001/mqtt")
        self.assertEqual(
            address,
            BrokerAddress(
                scheme="ws",
                host="broker.test",
                port=9001,
                tls=False,
                transport="websockets",
                websocket_path="/mqtt",
            ),
        )
        self.assertTrue(address.is_websocket)

    def test_websocket_defaults(self) -> None:
        plain = parse_broker_url("ws://broker.test")
        self.assertEqual((plain.port, plain.tls), (80, False))
        self.assertEqual(plain.websocket_path, DEFAULT_WEBSOCKET_PATH)

        secure = parse_broker_url("wss://example.org")
        self.assertEqual((secure.port, secure.tls), (443, True))
        self.assertEqual(secure.transport, "websockets")
        self.assertEqual(secure.websocket_path, "/mqtt")

    def test_websocket_secure_behind_reverse_proxy(self) -> None:
        # The form production is expected to move to.
        address = parse_broker_url("wss://example.org/mqtt")
        self.assertEqual(address.host, "example.org")
        self.assertEqual(address.port, 443)
        self.assertTrue(address.tls)
        self.assertEqual(address.websocket_path, "/mqtt")

    def test_websocket_keeps_custom_path_port_and_query(self) -> None:
        address = parse_broker_url("wss://example.org:8443/broker/ws?tenant=a")
        self.assertEqual(address.port, 8443)
        self.assertEqual(address.websocket_path, "/broker/ws?tenant=a")
        self.assertEqual(parse_broker_url("ws://h/").websocket_path, "/")

    def test_scheme_is_case_insensitive_and_whitespace_is_ignored(self) -> None:
        address = parse_broker_url("  TLS://Mqtt.Example.com:8883\n")
        self.assertTrue(address.tls)
        self.assertEqual(address.host, "mqtt.example.com")

    def test_ipv6_and_trailing_slash(self) -> None:
        address = parse_broker_url("tcp://[::1]:1883/")
        self.assertEqual(address.host, "::1")
        self.assertEqual(address.port, 1883)
        self.assertEqual(address.describe(), "tcp://[::1]:1883")

    def test_describe(self) -> None:
        self.assertEqual(
            parse_broker_url("wss://example.org").describe(),
            "wss://example.org:443/mqtt",
        )
        self.assertEqual(
            parse_broker_url("tcp://example.org").describe(), "tcp://example.org:1883"
        )

    def test_rejects_what_cannot_work(self) -> None:
        bad = [
            "",
            "   ",
            None,
            1883,
            "mqtt.example.com:1883",  # no scheme
            "localhost",
            "http://example.org",  # wrong protocol
            "https://example.org/mqtt",
            "mqtt+ssl://example.org",
            "tsl://example.org:8883",  # typo must not end in plaintext
            "tcp://",  # no host
            "tcp://:1883",
            "tcp://example.org:notaport",
            "tcp://example.org:0",
            "tcp://example.org:70000",
            "tcp://example.org:1883/some/path",
            "tls://example.org/mqtt",  # path needs ws:// or wss://
            "tcp://example.org?x=1",
            "wss://example.org/mqtt#frag",
            "tcp://[::1",
        ]
        for url in bad:
            with self.subTest(url=url):
                with self.assertRaises(InvalidBrokerURL):
                    parse_broker_url(url)

    def test_error_is_a_value_error_with_a_helpful_message(self) -> None:
        with self.assertRaises(ValueError) as caught:
            parse_broker_url("http://example.org")
        message = str(caught.exception)
        self.assertIn("http", message)
        self.assertIn("wss://", message)

    def test_credentials_are_rejected_and_never_echoed(self) -> None:
        with self.assertRaises(InvalidBrokerURL) as caught:
            parse_broker_url("tcp://user:s3cret@example.org:1883")
        self.assertNotIn("s3cret", str(caught.exception))
        self.assertNotIn("user", str(caught.exception))


class DeriveBrokerUrlTest(unittest.TestCase):
    def test_https_control_plane(self) -> None:
        self.assertEqual(derive_broker_url("https://emotionkit.de"), "tls://emotionkit.de:8883")

    def test_http_control_plane(self) -> None:
        self.assertEqual(derive_broker_url("http://localhost:8085"), "tcp://localhost:1883")

    def test_result_is_always_parseable(self) -> None:
        for url in ("https://emotionkit.de", "http://[::1]:8085", "", "nonsense"):
            with self.subTest(url=url):
                parse_broker_url(derive_broker_url(url))


if __name__ == "__main__":
    unittest.main()
