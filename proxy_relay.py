"""
Async SOCKS5 relay with hot-switchable upstream proxy.

All browsers connect to localhost:RELAY_PORT (no auth). Traffic is forwarded
through the currently active upstream SOCKS5 proxy. When upstream dies, call
set_upstream() to switch — new connections go to new upstream instantly.

Architecture:
    [Browser tabs] → localhost:RELAY → upstream SOCKS5 → bet365.com

On proxy failure:
    1. Watchdog detects via check_health() (every 3s)
    2. set_upstream() switches to backup proxy
    3. Old connections die naturally (upstream is already dead)
    4. Bet365 JS auto-reconnects → new connection → relay → new upstream
    5. Workers that didn't auto-reconnect get page.reload()
    6. No browser restart needed — recovery in seconds, not minutes
"""

import asyncio
import struct
import sys
from datetime import datetime, timezone

# SOCKS5 protocol constants (RFC 1928)
SOCKS5 = 0x05
AUTH_NONE = 0x00
AUTH_USERPASS = 0x02
CMD_CONNECT = 0x01
ATYP_IPV4 = 0x01
ATYP_DOMAIN = 0x03
ATYP_IPV6 = 0x04

HEALTH_CHECK_HOST = "www.bet365.com"
HEALTH_CHECK_PORT = 443


def _log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] [Relay] {msg}", file=sys.stderr, flush=True)


class ProxyRelay:
    """SOCKS5 relay server with hot-switchable upstream proxy."""

    def __init__(self):
        self.upstream_host: str = ""
        self.upstream_port: int = 0
        self.upstream_user: str | None = None
        self.upstream_pass: str | None = None
        self.listen_port: int = 0
        self._server = None
        self._active: int = 0

    @property
    def active_connections(self) -> int:
        return self._active

    @property
    def upstream_label(self) -> str:
        if not self.upstream_host:
            return "none"
        return f"{self.upstream_host}:{self.upstream_port}"

    def set_upstream(self, host: str, port: int,
                     username: str = None, password: str = None):
        """Switch upstream proxy. Takes effect on new connections immediately."""
        old = self.upstream_label
        self.upstream_host = host
        self.upstream_port = port
        self.upstream_user = username
        self.upstream_pass = password
        _log(f"Upstream: {old} → {host}:{port}")

    def set_upstream_from_url(self, proxy_url: str):
        """Parse 'socks5://user:pass@host:port' and set as upstream."""
        from urllib.parse import urlparse
        p = urlparse(proxy_url)
        self.set_upstream(p.hostname, p.port, p.username, p.password)

    async def start(self):
        """Start relay on a random available port."""
        self._server = await asyncio.start_server(
            self._handle_client, "127.0.0.1", 0
        )
        self.listen_port = self._server.sockets[0].getsockname()[1]
        _log(f"Listening on 127.0.0.1:{self.listen_port}")

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            _log("Stopped")

    async def check_health(self, timeout: float = 5.0) -> bool:
        """Test upstream: SOCKS5 handshake + CONNECT to bet365.com:443.

        This is an end-to-end test: proxy alive + upstream network working +
        can reach bet365. Takes ~0.5-2s for a typical mobile proxy.
        """
        if not self.upstream_host:
            return False
        uw = None
        try:
            ur, uw = await asyncio.wait_for(
                asyncio.open_connection(self.upstream_host, self.upstream_port),
                timeout=timeout,
            )

            # SOCKS5 greeting
            if self.upstream_user:
                uw.write(bytes([SOCKS5, 2, AUTH_NONE, AUTH_USERPASS]))
            else:
                uw.write(bytes([SOCKS5, 1, AUTH_NONE]))
            await uw.drain()

            resp = await asyncio.wait_for(ur.readexactly(2), timeout=timeout)
            if resp[0] != SOCKS5:
                return False

            # Username/password auth if required (RFC 1929)
            if resp[1] == AUTH_USERPASS:
                if not self.upstream_user:
                    return False
                uname = self.upstream_user.encode()
                passwd = (self.upstream_pass or "").encode()
                uw.write(bytes([0x01, len(uname)]) + uname
                         + bytes([len(passwd)]) + passwd)
                await uw.drain()
                auth_resp = await asyncio.wait_for(ur.readexactly(2), timeout=timeout)
                if auth_resp[1] != 0x00:
                    return False

            # CONNECT to bet365
            host_b = HEALTH_CHECK_HOST.encode()
            uw.write(bytes([SOCKS5, CMD_CONNECT, 0x00, ATYP_DOMAIN, len(host_b)])
                     + host_b + struct.pack("!H", HEALTH_CHECK_PORT))
            await uw.drain()

            # Read VER REP RSV ATYP — REP=0x00 means success
            resp = await asyncio.wait_for(ur.readexactly(4), timeout=timeout)
            return resp[1] == 0x00

        except Exception:
            return False
        finally:
            if uw:
                try:
                    uw.close()
                    await uw.wait_closed()
                except Exception:
                    pass

    # ── SOCKS5 server (browser-facing) ────────────────────────────────────

    async def _handle_client(self, cr: asyncio.StreamReader,
                             cw: asyncio.StreamWriter):
        """Handle one SOCKS5 connection from a browser."""
        self._active += 1
        uw = None
        try:
            # 1. Read browser's SOCKS5 greeting: VER NMETHODS METHODS...
            header = await asyncio.wait_for(cr.readexactly(2), timeout=10)
            if header[0] != SOCKS5:
                return
            await cr.readexactly(header[1])  # consume methods

            # Reply: no auth required on relay side
            cw.write(bytes([SOCKS5, AUTH_NONE]))
            await cw.drain()

            # 2. Read CONNECT request: VER CMD RSV ATYP ...
            req = await asyncio.wait_for(cr.readexactly(4), timeout=10)
            if req[0] != SOCKS5 or req[1] != CMD_CONNECT:
                cw.write(bytes([SOCKS5, 0x07, 0, ATYP_IPV4,
                                0, 0, 0, 0, 0, 0]))
                await cw.drain()
                return

            # Parse target address
            atyp = req[3]
            if atyp == ATYP_IPV4:
                addr_data = await cr.readexactly(4)
                connect_addr = bytes([ATYP_IPV4]) + addr_data
            elif atyp == ATYP_DOMAIN:
                dlen_b = await cr.readexactly(1)
                domain = await cr.readexactly(dlen_b[0])
                connect_addr = bytes([ATYP_DOMAIN]) + dlen_b + domain
            elif atyp == ATYP_IPV6:
                addr_data = await cr.readexactly(16)
                connect_addr = bytes([ATYP_IPV6]) + addr_data
            else:
                cw.write(bytes([SOCKS5, 0x08, 0, ATYP_IPV4,
                                0, 0, 0, 0, 0, 0]))
                await cw.drain()
                return

            port_data = await cr.readexactly(2)
            connect_payload = connect_addr + port_data

            # 3. Connect to target through upstream SOCKS5 proxy
            ur, uw = await self._connect_upstream(connect_payload)

            # 4. Reply success to browser
            cw.write(bytes([SOCKS5, 0x00, 0, ATYP_IPV4,
                            0, 0, 0, 0, 0, 0]))
            await cw.drain()

            # 5. Bidirectional data relay
            t1 = asyncio.create_task(self._pipe(cr, uw))
            t2 = asyncio.create_task(self._pipe(ur, cw))
            done, pending = await asyncio.wait(
                [t1, t2], return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()

        except Exception:
            # Connection error — browser will retry
            pass
        finally:
            self._active -= 1
            for w in (cw, uw):
                if w is not None:
                    try:
                        w.close()
                        await w.wait_closed()
                    except Exception:
                        pass

    # ── SOCKS5 client (upstream-facing) ───────────────────────────────────

    async def _connect_upstream(self, connect_payload: bytes
                                ) -> tuple[asyncio.StreamReader,
                                           asyncio.StreamWriter]:
        """Open SOCKS5 tunnel through upstream proxy.

        connect_payload: ATYP + ADDR + PORT bytes (forwarded from browser).
        Returns (reader, writer) connected to the final target.
        """
        ur, uw = await asyncio.wait_for(
            asyncio.open_connection(self.upstream_host, self.upstream_port),
            timeout=15,
        )

        try:
            # Greeting
            if self.upstream_user:
                uw.write(bytes([SOCKS5, 2, AUTH_NONE, AUTH_USERPASS]))
            else:
                uw.write(bytes([SOCKS5, 1, AUTH_NONE]))
            await uw.drain()

            resp = await asyncio.wait_for(ur.readexactly(2), timeout=10)
            if resp[0] != SOCKS5:
                raise ConnectionError("Upstream not SOCKS5")

            # Auth if upstream requires it
            if resp[1] == AUTH_USERPASS:
                if not self.upstream_user:
                    raise ConnectionError("Upstream requires auth")
                uname = self.upstream_user.encode()
                passwd = (self.upstream_pass or "").encode()
                uw.write(bytes([0x01, len(uname)]) + uname
                         + bytes([len(passwd)]) + passwd)
                await uw.drain()
                auth_resp = await asyncio.wait_for(
                    ur.readexactly(2), timeout=10)
                if auth_resp[1] != 0x00:
                    raise ConnectionError("Upstream auth failed")

            # CONNECT
            uw.write(bytes([SOCKS5, CMD_CONNECT, 0x00]) + connect_payload)
            await uw.drain()

            # Response header: VER REP RSV ATYP
            hdr = await asyncio.wait_for(ur.readexactly(4), timeout=15)
            if hdr[1] != 0x00:
                raise ConnectionError(
                    f"Upstream CONNECT refused (REP={hdr[1]:#04x})")

            # Consume BND.ADDR + BND.PORT (variable-length)
            bind_atyp = hdr[3]
            if bind_atyp == ATYP_IPV4:
                await ur.readexactly(6)   # 4 addr + 2 port
            elif bind_atyp == ATYP_DOMAIN:
                dlen = (await ur.readexactly(1))[0]
                await ur.readexactly(dlen + 2)
            elif bind_atyp == ATYP_IPV6:
                await ur.readexactly(18)  # 16 addr + 2 port
            else:
                await ur.readexactly(6)   # fallback

            return ur, uw

        except Exception:
            uw.close()
            await uw.wait_closed()
            raise

    # ── Data relay ────────────────────────────────────────────────────────

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader,
                    writer: asyncio.StreamWriter):
        """Relay data until EOF or error."""
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
