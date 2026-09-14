"""Certificado autoassinado para servir em HTTPS.

Existe porque `navigator.share` e `navigator.clipboard` exigem contexto seguro.
Do celular, o servidor é alcançado pelo IP da rede — que em HTTP não é contexto
seguro —, então sem HTTPS os dois botões ficam mortos justamente no aparelho
onde o WhatsApp está.
"""

import ipaddress
import shutil
import subprocess

import pytest

from memegen import tls

pytestmark = pytest.mark.skipif(
    not shutil.which("openssl"), reason="openssl não disponível")


@pytest.fixture
def certificado(monkeypatch, tmp_path):
    monkeypatch.setattr(tls, "CERT", tmp_path / "cert.pem")
    monkeypatch.setattr(tls, "KEY", tmp_path / "key.pem")
    return tls.gerar()


def _texto(cert):
    return subprocess.run(
        ["openssl", "x509", "-in", str(cert), "-noout", "-text"],
        check=True, capture_output=True, text=True).stdout


def test_gera_par_utilizavel(certificado):
    cert, chave = certificado
    assert cert.exists() and chave.exists()
    assert "BEGIN CERTIFICATE" in cert.read_text()
    assert oct(chave.stat().st_mode)[-3:] == "600"   # a chave não é legível por outros


def test_cobre_localhost_e_os_ips_da_maquina(certificado):
    """Sem os IPs no SAN, o celular reclamaria de nome divergente além do
    certificado autoassinado — dois avisos em vez de um."""
    texto = _texto(certificado[0])
    assert "DNS:localhost" in texto
    assert "IP Address:127.0.0.1" in texto
    for ip in tls.ips_locais():
        assert f"IP Address:{ip}" in texto


def test_nao_e_autoridade_certificadora(certificado):
    assert "CA:FALSE" in _texto(certificado[0])


def test_reaproveita_o_certificado_existente(certificado):
    cert, chave = certificado
    antes = (cert.read_bytes(), chave.read_bytes())
    assert tls.gerar() == (cert, chave)
    assert (cert.read_bytes(), chave.read_bytes()) == antes   # não regerou
    tls.gerar(forcar=True)
    assert cert.read_bytes() != antes[0]                      # --forcar regera


def test_ips_locais_sao_validos():
    ips = tls.ips_locais()
    assert "127.0.0.1" in ips
    for ip in ips:
        ipaddress.ip_address(ip)          # não levanta


def test_san_ignora_entrada_invalida():
    san = tls._san(["127.0.0.1", "nao-e-um-ip", "192.168.1.5"])
    assert san == "DNS:localhost,IP:127.0.0.1,IP:192.168.1.5"
