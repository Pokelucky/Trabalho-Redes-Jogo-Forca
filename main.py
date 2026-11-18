import argparse
import json
import logging
import os

from p2p_client import P2PForcaApp

DEFAULTS = {
    "rdv_host": "pyp2p.mfcaetano.cc", "rdv_port": 8080,
    "namespace": "CIC", "name": "alice", "listen_port": 8081,
    "register_ttl": 7200, "discover_interval": 20,
    "ping_interval": 30, "ping_miss_limit": 3,
    "max_reconnect_attempts": 5, "base_backoff": 2, "max_backoff": 60,
    "lives": 6, "max_players": 4, "turn_timeout": 30,
    "words_file": "words.txt", "log_file": None,
}


def carregar_config() -> dict:
    """DEFAULTS < config.json < linha de comando (--listen-port 8082, --log-file x.log...)."""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default="config.json")
    caminho = pre.parse_known_args()[0].config

    cfg = dict(DEFAULTS)
    if os.path.exists(caminho):
        with open(caminho, encoding="utf-8") as f:
            cfg.update(json.load(f))

    parser = argparse.ArgumentParser(description="PyP2P-Forca")
    parser.add_argument("--config", default=caminho, help="arquivo de configuração")
    for chave, padrao in DEFAULTS.items():
        tipo = str if padrao is None else type(padrao)
        parser.add_argument("--" + chave.replace("_", "-"), dest=chave, type=tipo, default=None)
    args = parser.parse_args()
    for chave in DEFAULTS:
        valor = getattr(args, chave)
        if valor is not None:
            cfg[chave] = valor
    return cfg


def configurar_logging(log_file):
    """Tela: INFO. Arquivo (--log-file): tudo, inclusive o JSON bruto em DEBUG."""
    fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")
    raiz = logging.getLogger()
    raiz.setLevel(logging.DEBUG)
    tela = logging.StreamHandler()
    tela.setLevel(logging.INFO)
    tela.setFormatter(fmt)
    raiz.addHandler(tela)
    if log_file:
        arquivo = logging.FileHandler(log_file, encoding="utf-8")
        arquivo.setLevel(logging.DEBUG)
        arquivo.setFormatter(fmt)
        raiz.addHandler(arquivo)


def main():
    cfg = carregar_config()
    configurar_logging(cfg["log_file"])
    app = P2PForcaApp(cfg)
    try:
        app.iniciar()
    except OSError as e:
        print(f"Não consegui escutar na porta {cfg['listen_port']}: {e}")
        return
    app.cli.run()


if __name__ == "__main__":
    main()