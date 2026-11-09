import json
import logging
import socket

log = logging.getLogger("RDV")


class RendezvousError(Exception):
    """O servidor respondeu status ERROR."""


def enviar_comando(host, porta, comando, timeout=5):
    """Abre uma conexão, envia 1 comando JSON, lê a resposta e fecha."""
    with socket.create_connection((host, porta), timeout=timeout) as s:
        s.sendall((json.dumps(comando) + "\n").encode("utf-8"))
        resposta = s.makefile("r", encoding="utf-8").readline()
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