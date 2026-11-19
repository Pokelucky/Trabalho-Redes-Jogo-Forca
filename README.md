# PyP2P-Forca — Jogo da Forca Peer-to-Peer

Cliente P2P descentralizado de Jogo da Forca desenvolvido em Python puro, utilizando sockets TCP.

Cada nó peer registra-se em um servidor **Rendezvous** para descoberta dinâmica na rede, mantém uma malha de conexões TCP persistentes com vizinhos no mesmo *namespace* e pode atuar tanto como **host** (coordenador temporário de partida) quanto como **jogador**, sem depender de um servidor central de jogos.

---

## Arquitetura e Funcionalidades

- **Descoberta Dinâmica e Reconciliação (`rendezvous_connection.py`, `peer_table.py`):** Registro automático (`REGISTER`), renovação periódica de TTL (`ttl / 2`), descoberta de vizinhos (`DISCOVER`), encerramento limpo (`UNREGISTER`), reconexão com *backoff* exponencial e detecção automática de peers locais no mesmo IP público (`127.0.0.1`).
- **Malha TCP e Sessão (`peer_connection.py`, `keep_alive.py`):** *Handshake* (`HELLO`/`HELLO_OK`) com negociação de *features* (`hangman`), desempate determinístico de conexões simultâneas duplicadas (menor `peer_id` lexicográfico), encerramento gracioso (`BYE`/`BYE_OK`) e *keep-alive* (`PING`/`PONG`) com medição de RTT médio e detecção de nós `STALE`.
- **Estrela da Partida e Regras (`game_host.py`, `game_player.py`, `hangman.py`):**
  - *Host* autoritativo com controle de turnos em *round-robin*, *timeout* de jogada configurável e difusão de estado sequencial (`STATE` com `seq` monotônico).
  - Comprometimento criptográfico da palavra secreta via `SHA-256` (`word_hash` no `GAME_START`) verificado automaticamente pelos jogadores no `GAME_OVER`.
  - Tolerância a falhas com remoção automática de jogadores desconectados, suporte a reconexão em partida ativa (`rejoin`) e aborto limpo caso o *host* caia.
- **Interface CLI Interativa (`cli.py`):** Controle completo de rede, *lobby* e partidas pelo terminal.

---

## Estrutura do Projeto

```text
.
├── main.py                   # Ponto de entrada, leitura de config/CLI e setup de logs
├── p2p_client.py             # Orquestração dos módulos e ciclo de vida da aplicação
├── rendezvous_connection.py  # Cliente e worker periódico do servidor Rendezvous
├── peer_connection.py        # Servidor/cliente TCP, framing JSON, HELLO/BYE e desempate
├── peer_table.py             # Tabela de peers, estados, backoff e reconciliação
├── keep_alive.py             # Envio periódico de PING/PONG, RTT e detecção de STALE
├── message_router.py         # Validação de envelope e roteamento de mensagens
├── game_host.py              # Máquina de estados e regras da partida no papel de host
├── game_player.py            # Máquina de estados, lobby e validação no papel de jogador
├── hangman.py                # Lógica pura do Jogo da Forca (isolada da rede)
├── cli.py                    # Interface de comandos no terminal
├── config.json               # Configurações padrão do cliente
└── words.txt                 # Banco de palavras para /create --random
```

---

## Requisitos e Instalação

- **Sistema Operacional:** Linux (ou qualquer SO com suporte padrão a sockets TCP/IP)
- **Python:** `3.10` ou superior (utiliza *type hints* com união `X | Y`)
- **Dependências Externas:** **Nenhuma.** O projeto utiliza exclusivamente a Biblioteca Padrão do Python (`socket`, `threading`, `json`, `hashlib`, `unicodedata`, `argparse`, `logging`, etc.).

```bash
git clone [https://github.com/](https://github.com/)<seu-usuario>/Trabalho-Redes-Jogo-Forca.git
cd Trabalho-Redes-Jogo-Forca
```

---

## Como Executar

### 1. Configuração
Os parâmetros padrão ficam em `config.json` e podem ser sobrescritos via linha de comando:

```json
{
  "rdv_host": "pyp2p.mfcaetano.cc",
  "rdv_port": 8080,
  "namespace": "CIC",
  "name": "alice",
  "listen_port": 8081,
  "register_ttl": 7200,
  "discover_interval": 20,
  "ping_interval": 30,
  "ping_miss_limit": 3,
  "max_reconnect_attempts": 5,
  "base_backoff": 2,
  "max_backoff": 60,
  "lives": 6,
  "max_players": 4,
  "turn_timeout": 30,
  "words_file": "words.txt",
  "log_file": null
}
```

### 2. Iniciando Peers (Exemplo Local em Dois Terminais)

**Terminal 1 (Host):**
```bash
python3 main.py --name alice --namespace CIC --listen-port 8081 --log-file alice.log
```

**Terminal 2 (Jogador):**
```bash
python3 main.py --name bob --namespace CIC --listen-port 8082 --log-file bob.log
```

*(Opcional)* Para apontar para um arquivo de configuração customizado, utilize `--config outro_config.json`.

---

## Comandos da CLI

| Comando | Descrição |
| :--- | :--- |
| `/peers [* \| #namespace]` | Descobre e lista peers (padrão: namespace local) |
| `/conn` | Lista conexões TCP ativas, direção (`inbound`/`outbound`) e RTT |
| `/rtt` | Exibe o RTT último e médio por peer conectado |
| `/reconnect` | Força uma reconciliação imediata com o Rendezvous |
| `/games` | Consulta todos os vizinhos conectados e lista partidas abertas |
| `/create <palavra> [max_players]` | Cria uma partida com a palavra informada e torna-se host |
| `/create --random [max_players]` | Cria uma partida sorteando uma palavra de `words.txt` |
| `/start` | Inicia a partida hospedada (exige ao menos 1 jogador) |
| `/cancel` | Cancela a partida hospedada |
| `/join <host_peer_id>` | Entra na partida aberta pelo host indicado |
| `/leave` | Sai da partida atual |
| `/guess <letra>` | Envia um palpite de letra (`a`–`z`) |
| `/word <palavra>` | Envia um palpite da palavra inteira |
| `/status` | Exibe o estado atual da partida (palavra mascarada, vidas, turno) |
| `/log <DEBUG\|INFO\|WARNING\|ERROR>` | Altera o nível de log exibido no terminal |
| `/help` | Lista todos os comandos disponíveis |
| `/quit` | Encerra a sessão de forma ordenada (`GAME_OVER`/`LEAVE` → `BYE` → `UNREGISTER`) |

---
