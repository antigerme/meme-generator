"""Certificado autoassinado para servir em HTTPS.

Existe por um motivo específico: `navigator.share` e `navigator.clipboard` só
funcionam em contexto seguro. `localhost` conta como seguro, mas
`http://192.168.0.10:8000` não — e é exatamente assim que o celular acessa o
servidor da sua máquina. Sem HTTPS, compartilhar no WhatsApp é impossível no
aparelho onde o WhatsApp está.

O certificado é gerado uma vez e cobre localhost, 127.0.0.1 e os IPs de rede
local detectados, para que o celular não reclame também de nome divergente. O
navegador ainda vai avisar que o certificado é autoassinado: é aceitar uma vez
por aparelho.
"""

from __future__ import annotations

import ipaddress
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

from . import config

CERT = config.DATA / "cert.pem"
KEY = config.DATA / "key.pem"


def ips_locais() -> list[str]:
    """IPs desta máquina que um celular na mesma rede conseguiria alcançar."""
    achados = {"127.0.0.1"}
    try:
        # não envia nada; só faz o SO escolher a interface de saída
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            achados.add(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            achados.add(info[4][0])
    except OSError:
        pass
    return sorted(achados)


def _san(ips: list[str]) -> str:
    """Lista de nomes alternativos no formato que o openssl espera."""
    partes = ["DNS:localhost"]
    n = 1
    for ip in ips:
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            continue
        partes.append(f"IP:{ip}")
        n += 1
    return ",".join(partes)


def gerar(forcar: bool = False) -> tuple[Path, Path]:
    """Gera o par certificado/chave se ainda não existir. Devolve os caminhos.

    Usa o openssl da máquina em vez da biblioteca `cryptography`: é uma
    dependência a menos num projeto que já tem bastante, e o binário existe por
    padrão em Linux e macOS (no Windows vem com o Git).
    """
    if CERT.exists() and KEY.exists() and not forcar:
        return CERT, KEY

    if not shutil.which("openssl"):
        raise RuntimeError(
            "openssl não encontrado — necessário para gerar o certificado de "
            "HTTPS. Instale-o, ou rode sem --https (o compartilhamento só "
            "funcionará em localhost)."
        )

    config.ensure_dirs()
    ips = ips_locais()
    with tempfile.NamedTemporaryFile("w", suffix=".cnf", delete=False) as cnf:
        cnf.write(
            "[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n"
            "[dn]\nCN=memegen local\n"
            f"[ext]\nsubjectAltName={_san(ips)}\nbasicConstraints=critical,CA:FALSE\n"
        )
        caminho_cnf = cnf.name

    try:
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-days", "825", "-keyout", str(KEY), "-out", str(CERT),
             "-config", caminho_cnf],
            check=True, capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"openssl falhou ao gerar o certificado: {e.stderr.decode()[:300]}"
        ) from e
    finally:
        Path(caminho_cnf).unlink(missing_ok=True)

    KEY.chmod(0o600)
    return CERT, KEY
