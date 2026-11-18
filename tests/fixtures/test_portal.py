import pyotp
from fastapi.testclient import TestClient

from rapa.fixtures.portal import (
    DEFAULT_PASSWORD,
    DEFAULT_TOTP_SECRET,
    DEFAULT_USER,
    cpf_valido,
    create_app,
)

CPF_VALIDO = "529.982.247-25"


def logged_in(variant: str = "v1") -> TestClient:
    client = TestClient(create_app(variant))  # type: ignore[arg-type]
    response = client.post("/login", data={"usuario": DEFAULT_USER, "senha": DEFAULT_PASSWORD})
    assert response.status_code == 200
    assert "Código de verificação" in response.text
    code = pyotp.TOTP(DEFAULT_TOTP_SECRET).now()
    response = client.post("/login/codigo", data={"codigo": code})
    assert "Olá" in response.text
    return client


def test_cpf_check_digits() -> None:
    assert cpf_valido(CPF_VALIDO)
    assert not cpf_valido("111.111.111-11")
    assert not cpf_valido("529.982.247-24")


def test_wrong_password_is_rejected() -> None:
    client = TestClient(create_app())
    response = client.post("/login", data={"usuario": DEFAULT_USER, "senha": "errada"})
    assert "incorretos" in response.text


def test_pages_require_login() -> None:
    client = TestClient(create_app())
    response = client.get("/notas", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_pagination_and_download() -> None:
    client = logged_in()
    page = client.get("/notas?pagina=3").text
    assert "Página 3 de 3" in page
    response = client.get("/notas/001000/download")
    assert response.headers["content-type"] == "application/pdf"
    assert 'filename="nota-001000.pdf"' in response.headers["content-disposition"]


def test_issue_protocol_is_recorded() -> None:
    client = logged_in()
    response = client.post(
        "/protocolo", data={"nome": "Maria", "cpf": CPF_VALIDO, "uf": "SP", "declaro": "sim"}
    )
    assert "PRT-2026-000001" in response.text
    state = client.get("/_teste/estado").json()
    assert state["protocolos"][0]["nome"] == "Maria"


def test_v2_changes_labels() -> None:
    client = logged_in("v2")
    assert "Emitir documento" in client.get("/protocolo").text
    assert "Download" in client.get("/notas").text
