"""Dados reais do comprovante para pagamentos registrados manualmente."""
import re

ELETRONICOS = {"03", "04", "17"}
BANDEIRAS = (
    ("01", "Visa"), ("02", "Mastercard"), ("03", "American Express"),
    ("04", "Sorocred"), ("05", "Diners Club"), ("06", "Elo"),
    ("07", "Hipercard"), ("08", "Aura"), ("09", "Cabal"), ("99", "Outras"),
)
CAMPOS = (
    "pagamento_cnpj", "pagamento_bandeira", "pagamento_autorizacao",
    "pagamento_cnpj_recebedor", "pagamento_terminal",
)


def _texto(dados, chave):
    try:
        return str(dados[chave] or "").strip()
    except (KeyError, IndexError):
        return ""


def _cnpj(texto, rotulo):
    if not texto:
        return ""
    if re.search(r"[^0-9./\-\s]", texto):
        raise ValueError(f"{rotulo}: informe um CNPJ válido com 14 dígitos.")
    numero = re.sub(r"\D", "", texto)
    if len(numero) != 14 or len(set(numero)) == 1:
        raise ValueError(f"{rotulo}: informe um CNPJ válido com 14 dígitos.")
    base = numero[:12]
    for pesos in ((5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2),
                  (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)):
        resto = sum(int(n) * peso for n, peso in zip(base, pesos)) % 11
        base += str(0 if resto < 2 else 11 - resto)
    if base != numero:
        raise ValueError(f"{rotulo}: os dígitos verificadores do CNPJ são inválidos.")
    return numero


def validar(dados, tpag):
    """Campos vazios são opcionais; nunca inventa dados da operação."""
    campos = {chave: "" for chave in CAMPOS}
    if tpag not in ELETRONICOS:
        return campos
    campos.update({chave: _texto(dados, chave) for chave in CAMPOS})
    for chave, rotulo in (("pagamento_cnpj", "Instituição de pagamento"),
                           ("pagamento_cnpj_recebedor", "Beneficiário do pagamento")):
        campos[chave] = _cnpj(campos[chave], rotulo)
    if tpag == "17":
        campos["pagamento_bandeira"] = ""
    elif campos["pagamento_bandeira"] and campos["pagamento_bandeira"] not in dict(BANDEIRAS):
        raise ValueError("Escolha uma bandeira válida ou deixe sem informar.")
    for chave, limite, rotulo in (("pagamento_autorizacao", 128, "Código de autorização"),
                                  ("pagamento_terminal", 40, "Identificador do terminal")):
        valor = campos[chave]
        if len(valor) > limite or any(ord(c) < 32 or ord(c) == 127 for c in valor):
            raise ValueError(f"{rotulo}: use até {limite} caracteres, sem quebras de linha.")
    return campos


def salvar(db, venda_id, campos):
    db.execute("UPDATE vendas SET " + ", ".join(chave + "=?" for chave in CAMPOS) + " WHERE id=?",
               tuple(campos[chave] for chave in CAMPOS) + (venda_id,))


def payload(dados, tpag):
    if tpag not in ELETRONICOS:
        return {}
    campos = validar(dados, tpag)
    # O sistema não captura pagamentos por TEF/POS integrado.
    return {
        "tpIntegra": 2,
        "CNPJ": campos["pagamento_cnpj"] or None,
        "tBand": campos["pagamento_bandeira"] or None,
        "cAut": campos["pagamento_autorizacao"] or None,
        "CNPJReceb": campos["pagamento_cnpj_recebedor"] or None,
        "idTermPag": campos["pagamento_terminal"] or None,
    }
