import collections
import logging
import threading
import time
from datetime import datetime, timezone

from peer_connection import criar_msg

log = logging.getLogger("KeepAlive")


def agora_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _Estado:
    def __init__(self):
        self.pendentes = {}                      # msg_id -> instante do envio
        self.faltas = 0                          # PINGs seguidos sem PONG
        self.rtts = collections.deque(maxlen=10)  # últimos 10 RTTs (ms)


class KeepAlive:
    def __init__(self, server, ping_interval=30, miss_limit=3, on_stale=None):
        self.server = server
        self.ping_interval = ping_interval
        self.miss_limit = miss_limit
        self.on_stale = on_stale                 # on_stale(conn), chamado antes de fechar
        self._estados = {}                       # conn -> _Estado
        self._lock = threading.Lock()
        self._parar = threading.Event()

    def start(self):
        threading.Thread(target=self._laco, daemon=True, name="keep-alive").start()

    def parar(self):
        self._parar.set()

    def _laco(self):
        while not self._parar.wait(self.ping_interval):
            try:
                self._tick()
            except Exception:
                log.exception("erro no ciclo de keep-alive")

    def _tick(self):
        conns = self.server.conexoes()
        with self._lock:
            for c in list(self._estados):
                if c not in conns:
                    del self._estados[c]
        for conn in conns:
            msg = None
            with self._lock:
                est = self._estados.setdefault(conn, _Estado())
                if est.faltas < self.miss_limit:
                    msg = criar_msg("PING", timestamp=agora_iso())
                    est.pendentes[msg["msg_id"]] = time.monotonic()
                    est.faltas += 1
            if msg is None:
                log.warning("%s missed %d PINGs — marking STALE", conn.peer_id, self.miss_limit)
                if self.on_stale:
                    self.on_stale(conn)
                conn.fechar()
            else:
                conn.enviar(msg)

    def tratar(self, conn, msg):
        """Chamar com toda mensagem PING ou PONG recebida."""
        tipo = msg.get("type")
        if tipo == "PING":
            if not isinstance(msg.get("timestamp"), str):
                conn.enviar_erro("bad_request", "PING sem timestamp", msg.get("msg_id"))
                return
            conn.enviar(criar_msg("PONG", msg_id=msg["msg_id"], timestamp=msg["timestamp"]))
        elif tipo == "PONG":
            with self._lock:
                est = self._estados.get(conn)
                t0 = est.pendentes.pop(msg.get("msg_id"), None) if est else None
                if t0 is None:
                    log.debug("PONG sem PING correspondente de %s, ignorado", conn.peer_id)
                    return
                rtt = (time.monotonic() - t0) * 1000
                est.faltas = 0
                est.pendentes.clear()
                est.rtts.append(rtt)
                media = sum(est.rtts) / len(est.rtts)
            log.info("%s RTT=%.1f ms (avg %.1f ms)", conn.peer_id, rtt, media)

    def rtt(self, conn):
        """(último, média dos últimos 10) em ms, ou None. Para /rtt e /conn."""
        with self._lock:
            est = self._estados.get(conn)
            if not est or not est.rtts:
                return None
            return est.rtts[-1], sum(est.rtts) / len(est.rtts)