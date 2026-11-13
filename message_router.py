import logging

log = logging.getLogger("Router")

# tipos de mensagem e seus respectivos handlers em GameHost
HOST_HANDLERS = {
    "GAME_LIST": "handle_game_list",
    "JOIN": "handle_join",
    "LEAVE": "handle_leave",
    "GUESS": "handle_guess",
}

# tipos de mensagem e seus respectivos handlers em GamePlayer
# TODO: confirmar os nomes no game_player
PLAYER_HANDLERS = {
    "GAME_LIST_OK": "handle_game_list_ok",
    "JOIN_OK": "handle_join_ok",
    "JOIN_ERR": "handle_join_err",
    "LEAVE_OK": "handle_leave_ok",
    "GAME_START": "handle_game_start",
    "STATE": "handle_state",
    "GUESS_RESULT": "handle_guess_result",
    "GAME_OVER": "handle_game_over",
}


class MessageRouter:
    def __init__(self, host, player, keep_alive):
        """
        host: GameHost
        player: GamePlayer (pode ser None)
        keep_alive: KeepAlive
        """

        self.host = host
        self.player = player
        self.ka = keep_alive

    def handle(self, conn, msg: dict) -> None:
        # recebe mensagens do PeerServer
        tipo = msg["type"]  # o PeerConnection garante q eh uma str

        # 1 - trata mensagens de keep-alive
        if tipo in ("PING", "PONG"):
            self.ka.tratar(conn, msg)
            return

        # 2 - identifica o handler da mensagem
        if tipo in HOST_HANDLERS:
            destino, nome = self.host, HOST_HANDLERS[tipo]
        elif tipo in PLAYER_HANDLERS:
            destino, nome = self.player, PLAYER_HANDLERS[tipo]
        else:
            log.warning("tipo desconhecido %r de %s", tipo, conn.peer_id)
            conn.enviar_erro("unknown_type", f"tipo desconhecido: {tipo}",
                             msg.get("msg_id"))
            return

        # 3 - valida o remetente da mensagem
        if not self._src_confere(conn, msg):
            return

        # 4 - lobby e partida só para quem anunciou a feature hangman
        if not conn.suporta_forca:
            log.debug("%s de %s ignorada: peer não anunciou hangman", tipo, conn.peer_id)
            return

        log.debug("%s de %s", tipo, conn.peer_id)
        self._despachar(destino, nome, conn, msg)

    def _src_confere(self, conn, msg: dict) -> bool:
        # verifica se o remetente corresponde à conexão
        src = msg.get("src")
        if isinstance(src, str) and src == conn.peer_id:
            return True

        log.warning("%s de %s com src inválido (%r), descartada",
                    msg["type"], conn.peer_id, src)
        conn.enviar_erro("bad_request", "src diferente do peer desta conexão",
                         msg.get("msg_id"))
        return False

    def _despachar(self, destino, nome_metodo: str, conn, msg: dict) -> None:
        # executa o handler correspondente à mensagem
        metodo = getattr(destino, nome_metodo, None) if destino is not None else None

        if metodo is None:
            log.warning("sem handler para %s (%s)", msg["type"], nome_metodo)
            conn.enviar_erro("invalid_state", f"{msg['type']} não pode ser tratada agora",
                             msg.get("msg_id"))
            return

        metodo(msg)