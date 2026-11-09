import logging
import threading
import time

log = logging.getLogger("PeerTable")

DISCOVERED, CONNECTING, CONNECTED, STALE, GONE = (
    "DISCOVERED", "CONNECTING", "CONNECTED", "STALE", "GONE")


class _Entrada:
    def __init__(self, ip, port):
        self.ip = ip
        self.port = port              # None = só conhecemos por conexão de entrada
        self.estado = DISCOVERED
        self.tentativas = 0
        self.proxima = 0.0            # instante (monotonic) da próxima tentativa
        self.ja_conectou = False


class PeerTable:
    def __init__(self, server, base_backoff=2, max_backoff=60,
                 max_attempts=5, meu_ip=None):
        self.server = server
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.max_attempts = max_attempts
        self.meu_ip = meu_ip          # função que devolve meu IP público (ou None)
        self._peers = {}              # peer_id -> _Entrada
        self._lock = threading.Lock()
        self._parar = threading.Event()

    def start(self):
        threading.Thread(target=self._laco, daemon=True, name="peer-table").start()

    def parar(self):
        self._parar.set()

    def _set(self, pid, e, novo):
        if e.estado != novo:
            log.info("%s: %s -> %s", pid, e.estado, novo)
            e.estado = novo

    # ---------- reconciliação (callback do worker do Rendezvous) ----------
    def reconciliar(self, peers):
        vistos, para_fechar = set(), []
        with self._lock:
            for p in peers:
                pid = f"{p['name']}@{p['namespace']}"
                if pid == self.server.meu_id:
                    continue
                vistos.add(pid)
                e = self._peers.get(pid)
                if e is None:
                    self._peers[pid] = _Entrada(p["ip"], p["port"])
                    log.info("%s discovered at %s:%s", pid, p["ip"], p["port"])
                else:
                    e.ip, e.port = p["ip"], p["port"]
                    if e.estado == GONE:             # reapareceu no DISCOVER
                        e.tentativas, e.proxima = 0, 0.0
                        self._set(pid, e, DISCOVERED)
            for pid, e in self._peers.items():
                if pid not in vistos and e.port is not None and e.estado != GONE:
                    self._set(pid, e, GONE)          # sumiu do Rendezvous
                    para_fechar.append(pid)
        for pid in para_fechar:
            conn = self.server.get(pid)
            if conn:
                threading.Thread(target=conn.bye, args=("gone",), daemon=True).start()

    # ---------- tentativas de conexão ----------
    def _laco(self):
        while not self._parar.wait(1.0):
            agora = time.monotonic()
            tentar = []
            with self._lock:
                for pid, e in self._peers.items():
                    if (e.estado in (DISCOVERED, STALE) and e.port is not None
                            and agora >= e.proxima):
                        self._set(pid, e, CONNECTING)
                        tentar.append((pid, e.ip, e.port))
            for t in tentar:
                threading.Thread(target=self._tentar, args=t, daemon=True).start()

    def _tentar(self, pid, ip, port):
        meu = self.meu_ip() if self.meu_ip else None
        if meu and ip == meu:
            ip = "127.0.0.1"          # mesmo IP público = mesma máquina/rede
        conn = self.server.conectar(ip, port, pid)
        if conn:
            with self._lock:
                e = self._peers.get(pid)
                if e and e.estado == CONNECTING and self.server.get(pid):
                    e.tentativas = 0
                    e.ja_conectou = True
                    self._set(pid, e, CONNECTED)
            return                    # on_connect marca CONNECTED
        with self._lock:
            e = self._peers.get(pid)
            if not e or e.estado != CONNECTING:
                return
            if self.server.get(pid):  # conectou por outro caminho (entrada)
                self._set(pid, e, CONNECTED)
                e.tentativas = 0
                return
            e.tentativas += 1
            if e.tentativas >= self.max_attempts:
                log.warning("%s: %d tentativas falharam", pid, e.tentativas)
                self._set(pid, e, GONE)
                return
            atraso = min(self.base_backoff * 2 ** (e.tentativas - 1), self.max_backoff)
            e.proxima = time.monotonic() + atraso
            log.info("%s: tentativa %d falhou, nova em %.0f s", pid, e.tentativas, atraso)
            self._set(pid, e, STALE if e.ja_conectou else DISCOVERED)

    # ---------- callbacks do PeerServer ----------
    def on_connect(self, conn):
        with self._lock:
            e = self._peers.get(conn.peer_id)
            if e is None:
                e = self._peers[conn.peer_id] = _Entrada(None, None)
            e.tentativas, e.ja_conectou = 0, True
            self._set(conn.peer_id, e, CONNECTED)

    def on_disconnect(self, conn):
        with self._lock:
            e = self._peers.get(conn.peer_id)
            if e is None or e.estado == GONE:
                return
            if e.port is None:        # só existia pela conexão de entrada
                del self._peers[conn.peer_id]
                return
            e.tentativas = 0
            e.proxima = time.monotonic() + self.base_backoff
            self._set(conn.peer_id, e, STALE)

    # ---------- consulta (para /peers, /conn e /rtt) ----------
    def listar(self, ka=None):
        with self._lock:
            itens = [(pid, e.estado, e.tentativas, e.ip, e.port)
                     for pid, e in self._peers.items()]
        saida = []
        for pid, estado, tent, ip, port in itens:
            conn = self.server.get(pid)
            rtt = ka.rtt(conn) if (ka and conn) else None
            saida.append({"peer_id": pid, "estado": estado, "tentativas": tent,
                          "endereco": f"{ip}:{port}",
                          "direcao": conn.direcao if conn else None,
                          "rtt_ms": round(rtt[1], 1) if rtt else None})
        return saida