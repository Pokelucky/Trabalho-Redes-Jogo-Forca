import json
import logging
import socket
import threading
import uuid

log = logging.getLogger("PeerServer")

MAX_LINHA = 32768
HANDSHAKE_TIMEOUT = 5
VERSAO = "1.0"
FEATURES = ["hangman"]


def criar_msg(tipo, msg_id=None, **campos):
    """Monta uma mensagem peer-peer com o envelope padrão (ttl=1).
    Provisório: quando existir o message.py (que não é meu), trocar por ele."""
    msg = {"type": tipo, "msg_id": msg_id or str(uuid.uuid4()), "ttl": 1}
    msg.update(campos)
    return msg


class PeerConnection:
    """Uma conexão TCP com um vizinho (já pós-handshake, ou em handshake)."""

    def __init__(self, server, sock, direcao, endereco):
        self.server = server
        self.sock = sock
        self.direcao = direcao            # "inbound" ou "outbound"
        self.endereco = endereco          # (ip, porta) do outro lado
        self.peer_id = None
        self.features = []
        self.suporta_forca = False        # False => não enviar lobby/partida
        self._arq = sock.makefile("rb")
        self._lock_envio = threading.Lock()
        self._lock_fechar = threading.Lock()
        self._fechada = threading.Event()
        self._bye_ok = threading.Event()

    @property
    def fechada(self):
        return self._fechada.is_set()

    # ---------- envio ----------
    def enviar(self, msg):
        linha = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        if len(linha) > MAX_LINHA:
            log.error("mensagem %s maior que 32 KiB, não enviada", msg.get("type"))
            return False
        try:
            with self._lock_envio:
                self.sock.sendall(linha)
        except OSError as e:
            log.warning("falha ao enviar para %s: %s", self.peer_id or self.endereco, e)
            self.fechar()
            return False
        log.debug("→ %s %s", self.peer_id or self.endereco, linha.decode("utf-8").rstrip())
        return True

    def enviar_erro(self, code, message, ref=None):
        campos = {"src": self.server.meu_id, "code": code, "message": message}
        if self.peer_id:
            campos["dst"] = self.peer_id
        if ref:
            campos["ref_msg_id"] = ref
        self.enviar(criar_msg("ERROR", **campos))

    # ---------- leitura ----------
    def _ler_msg(self):
        """Lê uma linha JSON. Devolve dict, ou None se a conexão deve acabar."""
        bruto = self._arq.readline(MAX_LINHA + 1)
        if not bruto:
            return None                                       # EOF
        if not bruto.endswith(b"\n"):
            if len(bruto) >= MAX_LINHA:
                self.enviar_erro("bad_json", "linha acima de 32 KiB")
            return None
        log.debug("← %s %s", self.peer_id or self.endereco,
                  bruto.decode("utf-8", errors="replace").rstrip())
        try:
            msg = json.loads(bruto.decode("utf-8"))
            if not isinstance(msg, dict):
                raise ValueError("não é objeto")
        except ValueError:
            self.enviar_erro("bad_json", "linha não é um objeto JSON válido")
            return None
        return msg

    # ---------- handshake ----------
    def _guardar_identidade(self, msg):
        pid, feats = msg.get("peer_id"), msg.get("features")
        if (msg.get("ttl") != 1 or not isinstance(msg.get("msg_id"), str)
                or not isinstance(pid, str) or "@" not in pid
                or not isinstance(feats, list)):
            self.enviar_erro("bad_request", "handshake inválido", msg.get("msg_id"))
            return False
        if pid == self.server.meu_id:
            log.warning("conexão com o próprio peer_id, fechando")
            return False
        if msg.get("version") != VERSAO:
            log.warning("%s usa versão %s (esperado %s)", pid, msg.get("version"), VERSAO)
        self.peer_id = pid
        self.features = [f for f in feats if isinstance(f, str)]
        self.suporta_forca = "hangman" in self.features
        return True

    def handshake_entrada(self):
        """Quem aceita: espera HELLO e responde HELLO_OK."""
        self.sock.settimeout(HANDSHAKE_TIMEOUT)
        msg = self._ler_msg()
        if msg is None:
            return False
        if msg.get("type") != "HELLO":
            self.enviar_erro("invalid_state", "esperado HELLO", msg.get("msg_id"))
            return False
        if not self._guardar_identidade(msg):
            return False
        self.enviar(criar_msg("HELLO_OK", msg_id=msg["msg_id"], peer_id=self.server.meu_id,
                              version=VERSAO, features=FEATURES))
        self.sock.settimeout(None)
        return True

    def handshake_saida(self):
        """Quem abre: envia HELLO e espera HELLO_OK."""
        hello = criar_msg("HELLO", peer_id=self.server.meu_id, version=VERSAO, features=FEATURES)
        self.sock.settimeout(HANDSHAKE_TIMEOUT)
        if not self.enviar(hello):
            return False
        msg = self._ler_msg()
        if msg is None or msg.get("type") != "HELLO_OK" or msg.get("msg_id") != hello["msg_id"]:
            return False
        if not self._guardar_identidade(msg):
            return False
        self.sock.settimeout(None)
        return True

    # ---------- laço principal ----------
    def rodar(self):
        try:
            while not self._fechada.is_set():
                msg = self._ler_msg()
                if msg is None or not self._tratar(msg):
                    break
        except OSError as e:
            log.debug("erro de leitura com %s: %s", self.peer_id, e)
        finally:
            self.fechar()
            self.server._conexao_encerrada(self)

    def _tratar(self, msg):
        """Devolve False para encerrar a conexão."""
        if msg.get("ttl") != 1:
            log.debug("mensagem com ttl != 1 descartada")
            return True
        tipo = msg.get("type")
        if not isinstance(tipo, str) or not isinstance(msg.get("msg_id"), str):
            self.enviar_erro("bad_request", "type/msg_id ausente ou inválido")
            return True
        
        if tipo == "ERROR":
            log.warning("ERROR de %s: %s (%s)", self.peer_id, msg.get("code"), msg.get("message"))
            return True
        
        dst = msg.get("dst")
        if dst is not None and dst != self.server.meu_id:
            self.enviar_erro("not_for_me", f"mensagem endereçada a {dst}", msg["msg_id"])
            return True
        
        if tipo == "BYE":
            log.info("%s sent BYE (%s)", self.peer_id, msg.get("reason"))
            self.enviar(criar_msg("BYE_OK", msg_id=msg["msg_id"],
                                  src=self.server.meu_id, dst=self.peer_id))
            return False
        if tipo == "BYE_OK":
            self._bye_ok.set()
            return False
        if tipo in ("HELLO", "HELLO_OK"):
            self.enviar_erro("invalid_state", "handshake já concluído", msg["msg_id"])
            return True
        try:
            self.server.on_message(self, msg)
        except Exception:
            log.exception("erro ao tratar %s de %s", tipo, self.peer_id)
        return True

    # ---------- encerramento ----------
    def bye(self, reason="shutdown", esperar=2.0):
        """BYE, espera BYE_OK por até 2 s e fecha o socket de qualquer jeito."""
        if self.fechada:
            return
        self.enviar(criar_msg("BYE", src=self.server.meu_id, dst=self.peer_id, reason=reason))
        self._bye_ok.wait(esperar)
        self.fechar()

    def fechar(self):
        with self._lock_fechar:
            if self._fechada.is_set():
                return
            self._fechada.set()
        for op in (lambda: self.sock.shutdown(socket.SHUT_RDWR), self.sock.close):
            try:
                op()
            except OSError:
                pass


class PeerServer:
    """Escuta conexões, abre conexões e guarda uma conexão por vizinho."""

    def __init__(self, meu_id, listen_port, on_message, on_connect=None, on_disconnect=None):
        self.meu_id = meu_id
        self.listen_port = listen_port
        self.on_message = on_message            # on_message(conn, msg)
        self.on_connect = on_connect            # on_connect(conn)
        self.on_disconnect = on_disconnect      # on_disconnect(conn)
        self._conns = {}                        # peer_id -> PeerConnection
        self._lock = threading.Lock()
        self._conectando = set()
        self._parar = threading.Event()
        self._srv = None

    # ---------- servidor ----------
    def start(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("0.0.0.0", self.listen_port))
        self._srv.listen()
        self._srv.settimeout(1.0)
        threading.Thread(target=self._aceitar, daemon=True, name="peer-accept").start()
        log.info("listening on port %s as %s", self.listen_port, self.meu_id)

    def _aceitar(self):
        while not self._parar.is_set():
            try:
                sock, addr = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn = PeerConnection(self, sock, "inbound", addr)
            threading.Thread(target=self._atender, args=(conn,), daemon=True).start()

    def _atender(self, conn):
        try:
            ok = conn.handshake_entrada()
        except OSError:
            ok = False
        if not ok:
            conn.fechar()
            return
        self._iniciar(conn)

    # ---------- cliente ----------
    def conectar(self, ip, porta, peer_id=None):
        """Abre conexão e faz o handshake (bloqueia até 5 s). Devolve a conexão ou None."""
        if peer_id and self.get(peer_id):
            return self.get(peer_id)
        chave = (ip, porta)
        with self._lock:
            if chave in self._conectando:
                return None
            self._conectando.add(chave)
        conn = None
        try:
            sock = socket.create_connection((ip, porta), timeout=HANDSHAKE_TIMEOUT)
            conn = PeerConnection(self, sock, "outbound", (ip, porta))
            if not conn.handshake_saida():
                conn.fechar()
                return None
        except OSError as e:
            log.warning("não conectou a %s:%s: %s", ip, porta, e)
            if conn:
                conn.fechar()
            return None
        finally:
            with self._lock:
                self._conectando.discard(chave)
        threading.Thread(target=self._iniciar, args=(conn,), daemon=True).start()
        return conn

    def conectar_em_thread(self, ip, porta, peer_id=None):
        threading.Thread(target=self.conectar, args=(ip, porta, peer_id), daemon=True).start()

    # ---------- registro e duplicadas ----------
    def _iniciar(self, conn):
        if self._registrar(conn):
            log.info("%s connected: %s (features=%s)",
                     "Inbound" if conn.direcao == "inbound" else "Outbound",
                     conn.peer_id, ",".join(conn.features))
            if self.on_connect:
                try:
                    self.on_connect(conn)
                except Exception:
                    log.exception("erro em on_connect")
        conn.rodar()

    def _registrar(self, conn):
        """Guarda a conexão. Duplicada: vale a aberta pelo peer de menor peer_id."""
        manter = "outbound" if self.meu_id < conn.peer_id else "inbound"
        perdedora, substituiu = None, False
        with self._lock:
            antiga = self._conns.get(conn.peer_id)
            if antiga is None or antiga.fechada:
                self._conns[conn.peer_id] = conn
            elif antiga.direcao == conn.direcao:
                # mesma direção: a antiga provavelmente morreu (peer reiniciou)
                self._conns[conn.peer_id] = conn
                perdedora, substituiu = antiga, True
            elif conn.direcao == manter:
                self._conns[conn.peer_id] = conn
                perdedora = antiga
            else:
                perdedora = conn
        if substituiu:
            log.info("%s reconnected — replacing old connection", conn.peer_id)
        elif perdedora is not None:
            log.info("Duplicate connection with %s — keeping %s (lower peer_id)",
                     conn.peer_id, manter)
        if perdedora is not None:
            threading.Thread(target=perdedora.bye, args=("duplicate",), daemon=True).start()
        return perdedora is not conn

    def _conexao_encerrada(self, conn):
        with self._lock:
            era_a_ativa = self._conns.get(conn.peer_id) is conn
            if era_a_ativa:
                del self._conns[conn.peer_id]
        if era_a_ativa:
            log.info("Disconnected: %s", conn.peer_id)
            if self.on_disconnect:
                try:
                    self.on_disconnect(conn)
                except Exception:
                    log.exception("erro em on_disconnect")

    # ---------- API para os outros módulos ----------
    def get(self, peer_id):
        with self._lock:
            c = self._conns.get(peer_id)
        return c if c and not c.fechada else None

    def conexoes(self):
        with self._lock:
            return [c for c in self._conns.values() if not c.fechada]

    def enviar(self, peer_id, msg):
        c = self.get(peer_id)
        return c.enviar(msg) if c else False

    def encerrar(self, reason="shutdown"):
        """BYE em paralelo para todos, depois fecha o servidor."""
        self._parar.set()
        ts = [threading.Thread(target=c.bye, args=(reason,)) for c in self.conexoes()]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        if self._srv:
            self._srv.close()