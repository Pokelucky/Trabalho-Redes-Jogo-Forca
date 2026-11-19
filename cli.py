"""
Projeto: Jogo da Forca P2P (PyP2P-Forca)
Grupo: 13
Integrantes:
  - Lucas Galvão Wosiach -190128828
  - Samuel Henrique Fernandes Silva - 252001163 
"""


import logging
import random

from game_host import HostState
from hangman import Hangman

log = logging.getLogger("CLI")

NIVEIS = ("DEBUG", "INFO", "WARNING", "ERROR")


def _seguro(texto) -> str:
    # texto que veio da rede vai pro terminal: troca caractere de controle (evita escape ansi)
    return "".join(c if c.isprintable() else "?" for c in str(texto))


class Cli:
    def __init__(self, host, player, server, ka, table, rdv, worker, on_quit=None,
                 words_file="words.txt", default_max_players=4,
                 input_fn=input, out=print, sortear=random.choice):
        self.host = host
        self.player = player
        self.server = server
        self.ka = ka
        self.table = table
        self.rdv = rdv
        self.worker = worker
        self.on_quit = on_quit              # sem isso o /quit so avisa host e jogador
        self.words_file = words_file
        self.default_max_players = default_max_players
        self._input = input_fn
        self._out = out
        self._sortear = sortear
        self._sair = False

        # nome -> (método, uso, descrição)
        self._comandos = {
            "peers": (self.cmd_peers, "/peers [* | #namespace]", "descobre e lista peers (padrão: namespace local)"),
            "conn": (self.cmd_conn, "/conn", "lista as conexões ativas"),
            "rtt": (self.cmd_rtt, "/rtt", "RTT médio por peer"),
            "reconnect": (self.cmd_reconnect, "/reconnect", "força a reconexão com o Rendezvous"),
            "games": (self.cmd_games, "/games", "lista as partidas abertas dos vizinhos"),
            "create": (self.cmd_create, "/create <palavra> [max_players]", "cria uma partida e vira host"),
            "start": (self.cmd_start, "/start", "inicia a partida que você hosteia"),
            "cancel": (self.cmd_cancel, "/cancel", "cancela a partida que você hosteia"),
            "join": (self.cmd_join, "/join <host_peer_id>", "entra na partida de um host"),
            "leave": (self.cmd_leave, "/leave", "sai da partida atual"),
            "guess": (self.cmd_guess, "/guess <letra>", "palpite de letra"),
            "word": (self.cmd_word, "/word <palavra>", "palpite de palavra inteira"),
            "status": (self.cmd_status, "/status", "estado da partida"),
            "log": (self.cmd_log, "/log <DEBUG|INFO|WARNING|ERROR>", "muda o nível de log da tela"),
            "help": (self.cmd_help, "/help", "lista os comandos"),
            "quit": (self.cmd_quit, "/quit", "encerra o programa"),
        }

    # loop principal
    def run(self) -> None:
        self._say("CLI", f"{self.host.my_id} pronto. Use /help para ver os comandos.")
        while not self._sair:
            try:
                linha = self._input("> ")
            except (EOFError, KeyboardInterrupt):
                self.executar("/quit")
                break
            self.executar(linha)

    def executar(self, linha: str) -> bool:
        # roda uma linha. devolve False quando o programa deve sair
        linha = linha.strip()
        if not linha:
            return not self._sair
        if not linha.startswith("/"):
            self._say("CLI", "Use /help para ver os comandos.")
            return not self._sair

        partes = linha[1:].split(None, 1)
        nome = partes[0].lower() if partes else ""
        arg = partes[1].strip() if len(partes) > 1 else ""
        comando = self._comandos.get(nome)
        if comando is None:
            self._say("CLI", f"Comando desconhecido: /{nome}. Use /help.")
            return not self._sair

        try:
            comando[0](arg)
        except ValueError as e:                 # erro de uso ou de regra: só mostra a mensagem
            self._say("CLI", str(e))
        except Exception as e:                  # bug: vai pro log e o cli continua de pé
            log.exception("erro em /%s", nome)
            self._say("CLI", f"Erro inesperado em /{nome}: {e}")
        return not self._sair

    # rede
    def cmd_peers(self, arg: str) -> None:
        if arg == "":
            namespace = None
        elif arg == "*":
            namespace = "*"
        elif arg.startswith("#") and len(arg) > 1 and " " not in arg:
            namespace = arg[1:]
        else:
            raise ValueError("Uso: /peers [* | #namespace]")

        try:
            peers = self.rdv.discover(namespace)
        except Exception as e:
            raise ValueError(f"Falha ao consultar o Rendezvous: {e}")

        estados = {e["peer_id"]: e["estado"] for e in self.table.listar(self.ka)}
        if not peers:
            self._say("CLI", "Nenhum peer encontrado.")
            return
        self._say("CLI", f"{len(peers)} peer(s) encontrado(s):")
        for p in sorted(peers, key=lambda p: (p["namespace"], p["name"])):
            pid = f"{p['name']}@{p['namespace']}"
            self._say("CLI", f"  {_seguro(pid)}  {_seguro(p['ip'])}:{p['port']}  "
                             f"estado={estados.get(pid, '—')}  expira em {p.get('expires_in', '?')} s")

    def cmd_conn(self, arg: str) -> None:
        conns = sorted(self.server.conexoes(), key=lambda c: c.peer_id)
        estados = {e["peer_id"]: e["estado"] for e in self.table.listar(self.ka)}
        if not conns:
            self._say("CLI", "Nenhuma conexão ativa.")
            return
        self._say("CLI", f"{len(conns)} conexão(ões) ativa(s):")
        for c in conns:
            forca = "hangman" if c.suporta_forca else "sem-hangman"
            self._say("CLI", f"  {_seguro(c.peer_id)}  {c.direcao}  "
                             f"{estados.get(c.peer_id, 'CONNECTED')}  {forca}  RTT {self._rtt_texto(c)}")

    def cmd_rtt(self, arg: str) -> None:
        conns = sorted(self.server.conexoes(), key=lambda c: c.peer_id)
        if not conns:
            self._say("CLI", "Nenhuma conexão ativa.")
            return
        for c in conns:
            r = self.ka.rtt(c)
            texto = "sem medição ainda" if r is None else f"média {r[1]:.1f} ms (último {r[0]:.1f} ms)"
            self._say("CLI", f"  {_seguro(c.peer_id)}  {texto}")

    def cmd_reconnect(self, arg: str) -> None:
        self.worker.forcar()
        self._say("CLI", "Reconexão com o Rendezvous solicitada.")

    # lobby e partida como jogador
    def cmd_games(self, arg: str) -> None:
        jogos = self.player.list_games()
        if not jogos:
            self._say("CLI", "Nenhuma partida aberta.")
            return
        self._say("CLI", "Partidas abertas:")
        for j in jogos:
            jogadores = j.get("players")
            n = len(jogadores) if isinstance(jogadores, list) else 0
            linha = (f"  {_seguro(j.get('game_id'))}  host={_seguro(j.get('host'))}  "
                     f"{n}/{_seguro(j.get('max_players', '?'))} jogadores  "
                     f"{_seguro(j.get('word_length', '?'))} letras  {_seguro(j.get('lives', '?'))} vidas")
            if j.get("status") not in (None, "OPEN"):
                linha += f"  [{_seguro(j['status'])}]"
            self._say("CLI", linha)

    def cmd_join(self, arg: str) -> None:
        if len(arg.split()) != 1:
            raise ValueError("Uso: /join <host_peer_id>")
        if self._hospedando():
            raise ValueError("Você está hospedando uma partida: use /cancel antes.")
        if arg == self.host.my_id:
            raise ValueError("Você não pode entrar na própria partida.")
        self.player.join(arg)
        self._say("CLI", f"Pedido de entrada enviado a {arg}.")

    def cmd_leave(self, arg: str) -> None:
        if self._hospedando():
            raise ValueError("Você é o host: use /cancel para encerrar a partida.")
        self.player.leave()

    def cmd_guess(self, arg: str) -> None:
        if len(arg.split()) != 1:
            raise ValueError("Uso: /guess <letra>")
        self._so_jogador()
        self.player.guess_letter(arg)

    def cmd_word(self, arg: str) -> None:
        if len(arg.split()) != 1:
            raise ValueError("Uso: /word <palavra>")
        self._so_jogador()
        self.player.guess_word(arg)

    # partida como host
    def cmd_create(self, arg: str) -> None:
        partes = arg.split()
        if not partes or len(partes) > 2:
            raise ValueError("Uso: /create <palavra> [max_players]  ou  /create --random [max_players]")
        if self.player.ocupado:
            raise ValueError("Você está em uma partida como jogador: use /leave antes.")

        max_players = self.default_max_players
        if len(partes) == 2:
            try:
                max_players = int(partes[1])
            except ValueError:
                raise ValueError("max_players deve ser um número inteiro.") from None

        sorteada = partes[0] == "--random"
        palavra = self._palavra_sorteada() if sorteada else partes[0]
        game_id = self.host.create(palavra, max_players)

        letras = len(self.host.describe()["masked"])
        # a palavra sorteada não aparece, pra não estragar o jogo de quem também joga em outro peer
        origem = "palavra sorteada, " if sorteada else ""
        self._say("CLI", f"Partida {game_id} criada ({origem}{letras} letras, até {max_players} jogadores). "
                         "Use /start quando os jogadores entrarem.")

    def cmd_start(self, arg: str) -> None:
        self.host.start()
        d = self.host.describe()
        self._say("CLI", f"Partida iniciada com {len(d['players'])} jogador(es). Vez de {d['turn']}.")

    def cmd_cancel(self, arg: str) -> None:
        self.host.cancel()
        self._say("CLI", "Partida cancelada.")

    # estado e utilidades
    def cmd_status(self, arg: str) -> None:
        d = self.host.describe()
        if d["state"] != HostState.IDLE.value:
            linhas = self._linhas_host(d)
        else:
            linhas = self.player.status_lines()
        for linha in linhas:
            self._say("Game", linha)

    def cmd_log(self, arg: str) -> None:
        root = logging.getLogger()
        console = [h for h in root.handlers
                   if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)]
        if not arg:
            atual = logging.getLevelName(console[0].level) if console else logging.getLevelName(root.level)
            self._say("CLI", f"Nível de log da tela: {atual}. Uso: /log <DEBUG|INFO|WARNING|ERROR>")
            return
        nivel = arg.strip().upper()
        if nivel not in NIVEIS:
            raise ValueError("Uso: /log <DEBUG|INFO|WARNING|ERROR>")
        n = getattr(logging, nivel)
        # o logger raiz tem que deixar passar, senão o handler nunca recebe o DEBUG
        if root.level > n:
            root.setLevel(n)
        for h in console:
            h.setLevel(n)
        self._say("CLI", f"Nível de log da tela: {nivel}.")

    def cmd_help(self, arg: str) -> None:
        self._say("CLI", "Comandos:")
        for uso, desc in ((u, d) for _, u, d in self._comandos.values()):
            self._say("CLI", f"  {uso:<36} {desc}")

    def cmd_quit(self, arg: str) -> None:
        self._sair = True                       # sai do laço mesmo se o on_quit falhar
        self._say("CLI", "Encerrando...")
        if self.on_quit:
            self.on_quit()
        else:
            self.host.shutdown()
            self.player.shutdown()

    # auxiliares
    def _say(self, tag: str, texto: str) -> None:
        self._out(f"[{tag}] {texto}")

    def _hospedando(self) -> bool:
        return self.host.state in (HostState.OPEN, HostState.RUNNING)

    def _so_jogador(self) -> None:
        if self._hospedando():
            raise ValueError("O host não joga.")

    def _rtt_texto(self, conn) -> str:
        r = self.ka.rtt(conn)
        return "—" if r is None else f"{r[0]:.1f} ms (média {r[1]:.1f} ms)"

    def _palavra_sorteada(self) -> str:
        try:
            with open(self.words_file, encoding="utf-8") as f:
                linhas = f.read().splitlines()
        except OSError:
            raise ValueError(f"Não consegui ler {self.words_file}.") from None

        validas = []
        for linha in linhas:
            linha = linha.strip()
            if not linha or linha.startswith("#"):
                continue
            try:
                Hangman.normalize_word(linha)   # pula o que o host recusaria
            except ValueError:
                continue
            validas.append(linha)
        if not validas:
            raise ValueError(f"{self.words_file} não tem nenhuma palavra válida.")
        return self._sortear(validas)

    def _linhas_host(self, d: dict) -> list[str]:
        # mesmo formato do /status do jogador, mas o host não joga
        l1 = f"{d['game_id']}  (host: {self.host.my_id})  status: {d['state']}"
        palavra = " ".join(d["masked"])
        erradas = ", ".join(d["wrong_letters"]) or "—"
        l2 = f"Palavra: {palavra}     Vidas: {d['lives']} / {d['initial_lives']}     Erradas: {erradas}"

        l3 = f"Jogadores: {', '.join(d['players']) or '—'}"
        if d["state"] == HostState.OPEN.value:
            l3 += f"   {len(d['players'])}/{d['max_players']}, aguardando /start"
        elif d["state"] == HostState.FINISHED.value:
            l3 += "   partida encerrada"
        elif d["turn"]:
            l3 += f"   Vez de: {d['turn']}"
            if d["seconds_left"] is not None:
                l3 += f"  — {int(d['seconds_left'])} s restantes"
        return [l1, l2, l3]