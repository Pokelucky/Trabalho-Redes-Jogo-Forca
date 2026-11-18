import json
import logging
import socket
import threading
import time

log = logging.getLogger("RDV")


class RendezvousError(Exception):
    """O servidor respondeu status ERROR."""


def enviar_comando(host, porta, comando, timeout=5):
    """Abre uma conexão, envia 1 comando JSON, lê a resposta e fecha."""
    with socket.create_connection((host, porta), timeout=timeout) as s:
        linha = json.dumps(comando)
        log.debug("-> %s:%s %s", host, porta, linha)
        s.sendall((linha + "\n").encode("utf-8"))
        resposta = s.makefile("r", encoding="utf-8").readline()
    log.debug("<- %s:%s %s", host, porta, resposta.rstrip())
    return json.loads(resposta)


class RendezvousClient:
    def __init__(self, host, porta, namespace, name, listen_port, ttl=7200):
        self.host = host
        self.porta = porta
        self.namespace = namespace
        self.name = name
        self.listen_port = listen_port
        self.ttl = ttl
        self.meu_ip = None

    def _chamar(self, comando):
        resposta = enviar_comando(self.host, self.porta, comando)
        if resposta.get("status") != "OK":
            raise RendezvousError(resposta.get("message", "erro desconhecido"))
        return resposta

    def register(self):
        """Registra e também serve de refresh do TTL."""
        r = self._chamar({
            "type": "REGISTER",
            "namespace": self.namespace,
            "name": self.name,
            "port": self.listen_port,
            "ttl": self.ttl,
        })

        if self.meu_ip is None:
            log.info("NAT: %s é o IP visto pelo servidor; conexões de entrada só "
                     "funcionam com a porta %s acessível/encaminhada", r["ip"], self.listen_port)
            
        self.meu_ip = r["ip"]
        log.info("registered %s@%s as %s:%s (ttl=%s)",
                 self.name, self.namespace, r["ip"], r["port"], r["ttl"])
        return r

    def discover(self, namespace=None):
        """Lista os peers, já sem você mesmo."""
        comando = {"type": "DISCOVER"}
        ns = namespace or self.namespace
        if ns != "*":
            comando["namespace"] = ns
        r = self._chamar(comando)
        return [p for p in r["peers"]
                if not (p["name"] == self.name and p["namespace"] == self.namespace)]

    def unregister(self):
        self._chamar({
            "type": "UNREGISTER",
            "namespace": self.namespace,
            "name": self.name,
            "port": self.listen_port,
        })
        log.info("unregistered %s@%s", self.name, self.namespace)


"""WORKER DO RENDEZVOUS"""

class RendezvousWorker(threading.Thread):
    """Refresh do registro + DISCOVER periódico."""

    def __init__(self, client, discover_interval, on_peers):
        super().__init__(daemon=True, name="rdv-worker")
        self.client = client
        self.discover_interval = discover_interval
        self.on_peers = on_peers
        self._parar = threading.Event()
        self._forcar = threading.Event()

    def run(self):
        proximo_refresh = 0
        while not self._parar.is_set():
            try:
                agora = time.monotonic()
                if agora >= proximo_refresh:
                    self.client.register()
                    proximo_refresh = agora + self.client.ttl / 2
                self.on_peers(self.client.discover())
            except RendezvousError as e:
                log.warning("servidor respondeu erro: %s", e)
                if str(e) == "peer_not_registered":
                    proximo_refresh = 0
            except (OSError, ValueError) as e:
                log.warning("falha de rede no ciclo: %s", e)
            except Exception:
                log.exception("erro inesperado no ciclo")
            self._forcar.wait(self.discover_interval)
            self._forcar.clear()

    def forcar(self):
        self._forcar.set()

    def parar(self):
        self._parar.set()
        self._forcar.set()