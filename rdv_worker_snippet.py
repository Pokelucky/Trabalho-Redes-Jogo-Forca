import logging
import threading
import time

from rendezvous_connection import RendezvousError

log = logging.getLogger("RDV")


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