
import os
import sqlite3
import shutil
import hashlib
import base64
import json
import subprocess
import xml.etree.ElementTree as ET
from io import BytesIO
from datetime import datetime, date
from zoneinfo import ZoneInfo
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, session, send_file, jsonify, send_from_directory, make_response
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.lib.utils import ImageReader
from PIL import Image, ImageOps

APP_NAME = "CENTRALVET Agropecuária"
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "centralvet.db")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
FISCAL_DIR = os.path.join(DATA_DIR, "fiscal")
FISCAL_ORIGINAL_DIR = os.path.join(FISCAL_DIR, "originais")
FISCAL_NFE_DIR = os.path.join(FISCAL_DIR, "nfe")
FISCAL_NFCE_DIR = os.path.join(FISCAL_DIR, "nfce")
os.makedirs(BACKUP_DIR, exist_ok=True)
os.makedirs(FISCAL_ORIGINAL_DIR, exist_ok=True)
os.makedirs(FISCAL_NFE_DIR, exist_ok=True)
os.makedirs(FISCAL_NFCE_DIR, exist_ok=True)

# Segunda cópia de segurança em /app/certs.
# No Coolify, /app/certs já precisa ser persistente para manter o A1; por isso
# usamos esse volume como paraquedas caso /app/data seja recriado por engano.
CERT_PATH_ENV = os.environ.get("CERTIFICADO_PATH", "/app/certs/centralvet_a1.pfx")
CERTS_DIR = os.path.dirname(CERT_PATH_ENV) or "/app/certs"
PERSIST_MIRROR_DIR = os.environ.get("PERSIST_MIRROR_DIR", os.path.join(CERTS_DIR, "centralvet-persistence"))
MIRROR_DB_PATH = os.path.join(PERSIST_MIRROR_DIR, "centralvet.db")
os.makedirs(PERSIST_MIRROR_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
app.secret_key = os.environ.get("SECRET_KEY", "centralvet-veltrix-2026")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "1234")

# Dados fiscais oficiais/base da CENTRALVET.
# São usados para preencher automaticamente a configuração fiscal
# quando o banco estiver vazio após redeploy ou primeira instalação.
FISCAL_DEFAULTS = {
    "razao_social": "CENTRALVET AGROPECUARIA LTDA",
    "nome_fantasia": "CENTRALVET AGROPECUARIA",
    "cnpj": "68.690.225/0001-50",
    "inscricao_estadual": "005626088.00-49",
    "endereco": "R MANOEL FRANCISCO DE CASTRO, 21, B, CENTRO",
    "municipio": "ORIZANIA",
    "uf": "MG",
    "cep": "36.828-000",
    "telefone": "(31) 3875-1342",
    "email": "CONTABILIDADEREIS01@HOTMAIL.COM",
    "regime": "SIMPLES NACIONAL",
    "ambiente": "Produção",
    "certificado_nome": "A1 CENTRALVET válido até 05/09/2027",
    "certificado_path": "/app/certs/centralvet_a1.pfx",
    "certificado_senha_env": "CERTIFICADO_SENHA",
    "nf_serie": "1",
    "nf_numero_inicial": "1",
    "nfce_serie": "1",
    "nfce_numero_inicial": "1",
    "csc_id": "000001",
    "csc_token": "4b24675b696846632d2ed5e0519baa7b",
    "cfop_padrao": "5102",
    "cst_csosn_padrao": "102",
    "observacao": "Emissão direta NF-e pela SEFAZ/MG. Dados cadastrais base fixados no sistema.",
    "logradouro": "R MANOEL FRANCISCO DE CASTRO",
    "numero": "21",
    "complemento": "B",
    "bairro": "CENTRO",
    "codigo_municipio": "3145877",
    "cnae": "4683400",
    "crt": 1,
}


@app.route("/manifest.json")
def manifest_json():
    return send_from_directory(os.path.join(app.root_path, "static"), "manifest.json", mimetype="application/manifest+json")

@app.route("/sw.js")
def service_worker():
    response = make_response(send_from_directory(os.path.join(app.root_path, "static"), "sw.js", mimetype="application/javascript"))
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


# ---------------- Helpers ----------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def money_to_float(v):
    if v is None:
        return 0.0
    s = str(v).strip()
    if not s:
        return 0.0
    s = s.replace("R$", "").replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except Exception:
        return 0.0

def num_to_float(v):
    return money_to_float(v)

UNIDADES_PADRAO = [
    "UN", "CX", "PCT", "FD", "SC", "KG", "G", "L", "ML",
    "FR", "AMP", "GL", "M", "M2", "M3", "PAR", "KIT"
]

def normalizar_unidade(v):
    """Evita confundir quantidade com unidade de medida.

    Valores numéricos (ex.: '1') são inválidos como unidade e viram UN.
    Unidades alfabéticas vindas de XML são preservadas em maiúsculas.
    """
    s = str(v or "").strip().upper()
    if not s:
        return "UN"
    # Se o usuário digitou quantidade no campo unidade (1, 1.0, 0,001 etc.), corrige para UN.
    try:
        float(s.replace(",", "."))
        return "UN"
    except Exception:
        pass
    # Evita lixo/valores muito longos no campo. Mantém unidades usuais ou abreviações de XML.
    if len(s) > 10:
        return "UN"
    return s

def today_str():
    return date.today().isoformat()

def br_date(s):
    if not s:
        return ""
    try:
        if "/" in str(s):
            return s
        y, m, d = str(s)[:10].split("-")
        return f"{d}/{m}/{y}"
    except Exception:
        return str(s)

def fmt_money(v):
    try:
        v = float(v or 0)
    except Exception:
        v = 0
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def fmt_num(v):
    try:
        v = float(v or 0)
    except Exception:
        v = 0
    txt = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return txt

app.jinja_env.filters["money"] = fmt_money
app.jinja_env.filters["num"] = fmt_num
app.jinja_env.filters["brdate"] = br_date

def danfe_share_serializer():
    return URLSafeTimedSerializer(app.secret_key, salt="centralvet-danfe-whatsapp")

def make_danfe_share_token(devolucao_id):
    return danfe_share_serializer().dumps({"devolucao_id": int(devolucao_id)})

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if session.get("logged"):
            return fn(*args, **kwargs)
        return redirect(url_for("login"))
    return wrapper

def exec_sql(sql, params=()):
    with get_db() as db:
        db.execute(sql, params)
        db.commit()

def fetch_one(sql, params=()):
    with get_db() as db:
        return db.execute(sql, params).fetchone()

def fetch_all(sql, params=()):
    with get_db() as db:
        return db.execute(sql, params).fetchall()

def _sqlite_snapshot(source_path, dest_path):
    """Cria uma cópia consistente do SQLite, inclusive se o app estiver em uso."""
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    src = sqlite3.connect(source_path, timeout=30)
    dst = sqlite3.connect(dest_path, timeout=30)
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()

def _db_business_score(path):
    """Pontuação simples para distinguir um banco vazio de um banco com dados reais."""
    if not os.path.exists(path) or os.path.getsize(path) < 1024:
        return -1
    try:
        conn = sqlite3.connect(path)
        score = 0
        for table in ("produtos", "clientes", "fornecedores", "vendas", "financeiro", "devolucoes", "xml_imports"):
            try:
                score += int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            except Exception:
                pass
        conn.close()
        return score
    except Exception:
        return -1

def restore_db_from_persistent_mirror():
    """Restaura o banco espelho quando /app/data nasceu vazio após um redeploy."""
    if not os.path.exists(MIRROR_DB_PATH):
        return False
    primary_score = _db_business_score(DB_PATH)
    mirror_score = _db_business_score(MIRROR_DB_PATH)
    # Restaura quando o banco principal não existe/é inválido ou parece uma instalação nova
    # e o espelho possui dados reais. Nunca substitui silenciosamente um banco com mais dados.
    if primary_score < 0 or (primary_score == 0 and mirror_score > 0):
        os.makedirs(DATA_DIR, exist_ok=True)
        shutil.copy2(MIRROR_DB_PATH, DB_PATH)
        print(f"[CENTRALVET] Banco restaurado do espelho persistente: {MIRROR_DB_PATH}", flush=True)
        return True
    return False

def sync_persistent_mirror():
    if not os.path.exists(DB_PATH):
        return None
    try:
        _sqlite_snapshot(DB_PATH, MIRROR_DB_PATH)
        return MIRROR_DB_PATH
    except Exception as exc:
        print(f"[CENTRALVET] Aviso: não foi possível atualizar espelho persistente: {exc}", flush=True)
        return None

def backup_db(reason="manual"):
    if not os.path.exists(DB_PATH):
        return None
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    name = f"backup-centralvet-{reason}-{stamp}.db"
    dest = os.path.join(BACKUP_DIR, name)
    _sqlite_snapshot(DB_PATH, dest)
    # Atualiza também o espelho no volume do certificado. Assim um redeploy não apaga cadastros
    # mesmo se /app/data estiver configurado incorretamente no servidor.
    sync_persistent_mirror()
    # keep latest 40 backups
    files = sorted([os.path.join(BACKUP_DIR, f) for f in os.listdir(BACKUP_DIR) if f.endswith(".db")])
    for old in files[:-40]:
        try:
            os.remove(old)
        except Exception:
            pass
    return dest

def add_column(db, table, column, ddl):
    cols = [r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def xml_local(tag):
    return tag.split('}', 1)[-1] if '}' in tag else tag

def xml_first_text(node, name, default=""):
    if node is None:
        return default
    for child in node.iter():
        if xml_local(child.tag) == name:
            return (child.text or "").strip()
    return default

def xml_first(node, name):
    if node is None:
        return None
    for child in node.iter():
        if xml_local(child.tag) == name:
            return child
    return None

def xml_all(root, name):
    return [child for child in root.iter() if xml_local(child.tag) == name]

def only_digits(v):
    return ''.join(ch for ch in str(v or '') if ch.isdigit())

def xml_float(v):
    try:
        return float(str(v or '0').replace(',', '.'))
    except Exception:
        return 0.0

ST_CST_CODES = {"10", "30", "60", "70"}
ST_CSOSN_CODES = {"201", "202", "203", "500"}
ST_VALUE_FIELDS = [
    "vBCST", "vICMSST", "vBCSTRet", "vICMSSTRet",
    "vBCFCPST", "vFCPST", "vBCFCPSTRet", "vFCPSTRet"
]
ST_CFOP_COMPRA = {"1403", "2403"}

def get_product_tax(det):
    for key in ["CSOSN", "CST"]:
        val = xml_first_text(det, key, "")
        if val:
            return val
    return ""

def analisar_st_produto(det, cfop_origem=""):
    """Detecta se o item da nota veio com ICMS-ST.

    A detecção usa o XML original da compra: CST/CSOSN, valores próprios de ST
    e CFOPs comuns de compra com ST. Isso evita perguntar ao contador em cada
    devolução, mas mantém o campo editável para exceções.
    """
    cst = xml_first_text(det, "CST", "").strip()
    csosn = xml_first_text(det, "CSOSN", "").strip()
    cfop = str(cfop_origem or "").strip()
    motivos = []

    if cst in ST_CST_CODES:
        motivos.append(f"CST {cst}")
    if csosn in ST_CSOSN_CODES:
        motivos.append(f"CSOSN {csosn}")
    if cfop in ST_CFOP_COMPRA:
        motivos.append(f"CFOP origem {cfop}")

    campos_valor = []
    for campo in ST_VALUE_FIELDS:
        valor_txt = xml_first_text(det, campo, "")
        if valor_txt != "" and xml_float(valor_txt) > 0:
            campos_valor.append(campo)
    if campos_valor:
        motivos.append("valores de ST: " + ", ".join(campos_valor))

    tem_st = bool(motivos)
    return {
        "tem_st": tem_st,
        "modo_st": "com_st" if tem_st else "sem_st",
        "status_st": "Com ST" if tem_st else "Sem ST / comum",
        "motivo_st": "; ".join(motivos) if motivos else "Não foram encontrados CST/CSOSN ou valores de ICMS-ST no item.",
    }

def parse_nfe_xml(xml_bytes):
    """Extrai os principais dados de XML NF-e/NFC-e de entrada."""
    root = ET.fromstring(xml_bytes)
    inf = xml_first(root, "infNFe")
    chave = ""
    if inf is not None:
        chave = (inf.attrib.get("Id") or "").replace("NFe", "").strip()
    ide = xml_first(root, "ide")
    emit = xml_first(root, "emit")
    total_node = xml_first(root, "ICMSTot")
    dest = xml_first(root, "dest")
    fornecedor_nome = xml_first_text(emit, "xNome") or xml_first_text(emit, "xFant")
    fornecedor_cnpj = xml_first_text(emit, "CNPJ") or xml_first_text(emit, "CPF")
    ender_emit = xml_first(emit, "enderEmit")
    ender_dest = xml_first(dest, "enderDest")
    dados = {
        "chave": chave,
        "numero": xml_first_text(ide, "nNF"),
        "serie": xml_first_text(ide, "serie"),
        "emissao": (xml_first_text(ide, "dhEmi") or xml_first_text(ide, "dEmi"))[:10],
        "fornecedor_nome": fornecedor_nome,
        "fornecedor_cnpj": fornecedor_cnpj,
        "fornecedor_uf": xml_first_text(ender_emit, "UF"),
        "destinatario_nome": xml_first_text(dest, "xNome"),
        "destinatario_cnpj": xml_first_text(dest, "CNPJ") or xml_first_text(dest, "CPF"),
        "destinatario_uf": xml_first_text(ender_dest, "UF"),
        "natureza": xml_first_text(ide, "natOp"),
        "modelo": xml_first_text(ide, "mod"),
        "total": xml_float(xml_first_text(total_node, "vNF")),
        "itens": []
    }
    for det in xml_all(root, "det"):
        prod = xml_first(det, "prod")
        if prod is None:
            continue
        cfop_origem = xml_first_text(prod, "CFOP")
        st_info = analisar_st_produto(det, cfop_origem)
        item = {
            "item_origem_idx": int(det.attrib.get("nItem") or (len(dados["itens"]) + 1)),
            "codigo": xml_first_text(prod, "cProd"),
            "ean": xml_first_text(prod, "cEAN") or xml_first_text(prod, "cEANTrib"),
            "nome": xml_first_text(prod, "xProd"),
            "ncm": xml_first_text(prod, "NCM"),
            "cest": xml_first_text(prod, "CEST"),
            "cfop": cfop_origem,
            "unidade": xml_first_text(prod, "uCom") or xml_first_text(prod, "uTrib") or "UN",
            "quantidade": xml_float(xml_first_text(prod, "qCom") or xml_first_text(prod, "qTrib")),
            "valor_unitario": xml_float(xml_first_text(prod, "vUnCom") or xml_first_text(prod, "vUnTrib")),
            "valor_total": xml_float(xml_first_text(prod, "vProd")),
            "cst_csosn": get_product_tax(det),
            "tem_st": st_info["tem_st"],
            "modo_st": st_info["modo_st"],
            "status_st": st_info["status_st"],
            "motivo_st": st_info["motivo_st"],
        }
        if item["nome"]:
            dados["itens"].append(item)
    return dados

def sugerir_cfop_devolucao(tipo_operacao="mesmo_estado", substituicao="sem_st"):
    """Sugere CFOP de devolução de compra para mercadoria de revenda.
    A regra serve para acelerar a devolução; o campo fica editável por item.
    """
    tipo = (tipo_operacao or "mesmo_estado").strip()
    st = (substituicao or "sem_st").strip()
    if st == "com_st":
        return "6411" if tipo == "outro_estado" else "5411"
    return "6202" if tipo == "outro_estado" else "5202"

def cert_runtime_status():
    path = os.environ.get("CERTIFICADO_PATH", "").strip()
    if not path:
        try:
            cfg = fetch_one("SELECT certificado_path FROM fiscal_config WHERE id=1")
            path = (cfg["certificado_path"] if cfg and cfg["certificado_path"] else "").strip()
        except Exception:
            path = ""
    path = path or FISCAL_DEFAULTS["certificado_path"]
    has_pass = bool(os.environ.get("CERTIFICADO_SENHA", "").strip())
    exists = bool(path and os.path.exists(path))
    return {"path": path, "has_password": has_pass, "exists": exists}


def cfg_val(cfg, key):
    try:
        return str(cfg[key] or '').strip()
    except Exception:
        return ''

def fiscal_checklist(cfg, cert_status):
    checks = [
        {"item": "Certificado A1 no servidor", "ok": bool(cert_status.get("exists")), "obs": cert_status.get("path") or "Verifique o upload do A1"},
        {"item": "Senha do certificado no Coolify", "ok": bool(cert_status.get("has_password")), "obs": "Variável CERTIFICADO_SENHA"},
        {"item": "CNPJ da empresa", "ok": bool(cfg_val(cfg, "cnpj")), "obs": cfg_val(cfg, "cnpj") or "Preencher CNPJ"},
        {"item": "Inscrição Estadual", "ok": bool(cfg_val(cfg, "inscricao_estadual")), "obs": cfg_val(cfg, "inscricao_estadual") or "Preencher IE"},
        {"item": "Regime tributário", "ok": bool(cfg_val(cfg, "regime")), "obs": cfg_val(cfg, "regime") or "Confirmar com contador"},
        {"item": "Série e número inicial NF-e", "ok": bool(cfg_val(cfg, "nf_serie") and cfg_val(cfg, "nf_numero_inicial")), "obs": f"Série {cfg_val(cfg,'nf_serie') or '-'} • Nº {cfg_val(cfg,'nf_numero_inicial') or '-'}"},
        {"item": "Série e número inicial NFC-e", "ok": bool(cfg_val(cfg, "nfce_serie") and cfg_val(cfg, "nfce_numero_inicial")), "obs": f"Série {cfg_val(cfg,'nfce_serie') or '-'} • Nº {cfg_val(cfg,'nfce_numero_inicial') or '-'}"},
        {"item": "CSC/Token da NFC-e", "ok": bool(cfg_val(cfg, "csc_token")), "obs": "Preenchido" if cfg_val(cfg, "csc_token") else "Obrigatório para NFC-e em produção"},
        {"item": "CFOP padrão", "ok": bool(cfg_val(cfg, "cfop_padrao")), "obs": cfg_val(cfg, "cfop_padrao") or "Pode usar provisório em homologação"},
        {"item": "CST/CSOSN padrão", "ok": bool(cfg_val(cfg, "cst_csosn_padrao")), "obs": cfg_val(cfg, "cst_csosn_padrao") or "Pode usar provisório em homologação"},
    ]
    return checks

def fiscal_prod_ready(cfg, cert_status):
    required = ["cnpj", "inscricao_estadual", "regime", "nf_serie", "nf_numero_inicial", "nfce_serie", "nfce_numero_inicial", "csc_token", "cfop_padrao", "cst_csosn_padrao"]
    missing = [r for r in required if not cfg_val(cfg, r)]
    if not cert_status.get("exists"):
        missing.append("certificado_a1")
    if not cert_status.get("has_password"):
        missing.append("senha_certificado")
    return (len(missing) == 0, missing)


def fiscal_issuer_payload(cfg):
    return {
        "razao_social": cfg_val(cfg, "razao_social"),
        "nome_fantasia": cfg_val(cfg, "nome_fantasia"),
        "cnpj": only_digits(cfg_val(cfg, "cnpj")),
        "inscricao_estadual": only_digits(cfg_val(cfg, "inscricao_estadual")),
        "logradouro": cfg_val(cfg, "logradouro") or FISCAL_DEFAULTS["logradouro"],
        "numero": cfg_val(cfg, "numero") or FISCAL_DEFAULTS["numero"],
        "complemento": cfg_val(cfg, "complemento") or FISCAL_DEFAULTS["complemento"],
        "bairro": cfg_val(cfg, "bairro") or FISCAL_DEFAULTS["bairro"],
        "codigo_municipio": only_digits(cfg_val(cfg, "codigo_municipio") or FISCAL_DEFAULTS["codigo_municipio"]),
        "municipio": cfg_val(cfg, "municipio") or FISCAL_DEFAULTS["municipio"],
        "uf": (cfg_val(cfg, "uf") or "MG").upper(),
        "cep": only_digits(cfg_val(cfg, "cep")),
        "telefone": only_digits(cfg_val(cfg, "telefone")),
        "cnae": only_digits(cfg_val(cfg, "cnae") or FISCAL_DEFAULTS["cnae"]),
        "crt": int(cfg_val(cfg, "crt") or FISCAL_DEFAULTS["crt"]),
        "csc_id": cfg_val(cfg, "csc_id"),
        "csc_token": cfg_val(cfg, "csc_token"),
    }


def fiscal_tpamb(cfg):
    return 1 if cfg_val(cfg, "ambiente").lower().startswith("produ") else 2


def run_fiscal_engine(payload, timeout=90):
    cli = os.path.join(app.root_path, "fiscal_engine", "nfe_cli.php")
    if not os.path.isfile(cli):
        return {"ok": False, "authorized": False, "error": "Motor fiscal interno não encontrado no servidor."}
    try:
        proc = subprocess.run(
            ["php", cli],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=timeout,
            cwd=app.root_path,
        )
    except FileNotFoundError:
        return {"ok": False, "authorized": False, "error": "PHP/NFePHP não instalado. Faça o deploy usando o Dockerfile desta versão."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "authorized": False, "error": "A SEFAZ não respondeu dentro do tempo limite. Tente novamente; o sistema manterá a mesma numeração."}
    raw = (proc.stdout or "").strip()
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        detail = (proc.stderr or raw or "sem retorno").strip()[-1200:]
        return {"ok": False, "authorized": False, "error": f"Retorno inválido do motor fiscal: {detail}"}
    if not isinstance(data, dict):
        return {"ok": False, "authorized": False, "error": "Retorno inválido do motor fiscal."}
    if proc.returncode != 0 and not data.get("error"):
        data["error"] = (proc.stderr or "Falha no motor fiscal.").strip()[-1200:]
        data["ok"] = False
    return data


def fiscal_engine_base_payload(cfg):
    cert_path = os.environ.get("CERTIFICADO_PATH", "").strip() or cfg_val(cfg, "certificado_path") or FISCAL_DEFAULTS["certificado_path"]
    return {
        "issuer": fiscal_issuer_payload(cfg),
        "tpAmb": fiscal_tpamb(cfg),
        "cert_path": cert_path,
        "cert_password": os.environ.get("CERTIFICADO_SENHA", ""),
    }


def fiscal_safe_file(path):
    if not path:
        return None
    try:
        rp = os.path.realpath(path)
        base = os.path.realpath(FISCAL_DIR) + os.sep
        if not rp.startswith(base) or not os.path.isfile(rp):
            return None
        return rp
    except Exception:
        return None


def apply_devolucao_stock_once(db, devolucao_id):
    dev = db.execute("SELECT * FROM devolucoes WHERE id=?", (devolucao_id,)).fetchone()
    if not dev or int(dev["baixou_estoque"] or 0) == 1 or int(dev["baixar_estoque_solicitado"] or 0) != 1:
        return
    itens = db.execute("SELECT * FROM devolucao_itens WHERE devolucao_id=?", (devolucao_id,)).fetchall()
    for i in itens:
        if i["produto_id"]:
            qtd = float(i["quantidade_devolver"] or 0)
            valor_unit = float(i["valor_unitario"] or 0)
            db.execute("UPDATE produtos SET estoque=estoque-? WHERE id=?", (qtd, i["produto_id"]))
            db.execute("""
                INSERT INTO estoque_mov (data, produto_id, tipo, quantidade, custo_unit, valor_total, origem, observacao)
                VALUES (?, ?, 'Saída', ?, ?, ?, ?, ?)
            """, (dev["data"] or today_str(), i["produto_id"], qtd, valor_unit, qtd * valor_unit,
                  f"DEVOLUCAO #{devolucao_id}", f"Saída após autorização SEFAZ. NF origem {dev['numero_origem'] or ''}. Chave {dev['chave_origem'] or ''}"))
    db.execute("UPDATE devolucoes SET baixou_estoque=1 WHERE id=?", (devolucao_id,))


def emitir_devolucao_sefaz_internal(devolucao_id):
    cfg = fetch_one("SELECT * FROM fiscal_config WHERE id=1")
    dev = fetch_one("SELECT * FROM devolucoes WHERE id=?", (devolucao_id,))
    itens = fetch_all("SELECT * FROM devolucao_itens WHERE devolucao_id=? ORDER BY id", (devolucao_id,))
    if not cfg or not dev or not itens:
        return {"ok": False, "authorized": False, "error": "Devolução ou configuração fiscal não encontrada."}
    if cfg_val(dev, "status") == "Autorizada SEFAZ" and cfg_val(dev, "protocolo_autorizacao"):
        return {"ok": True, "authorized": True, "cStat": cfg_val(dev, "sefaz_cstat") or "100", "xMotivo": cfg_val(dev, "sefaz_motivo") or "Autorizada", "chave": cfg_val(dev, "nf_devolucao_chave"), "protocolo": cfg_val(dev, "protocolo_autorizacao"), "xml_path": cfg_val(dev, "xml_autorizado_path"), "danfe_path": cfg_val(dev, "danfe_path")}

    cert = cert_runtime_status()
    if not cert.get("exists"):
        return {"ok": False, "authorized": False, "error": f"Certificado A1 não encontrado em {cert.get('path') or 'CERTIFICADO_PATH'}."}
    if not cert.get("has_password"):
        return {"ok": False, "authorized": False, "error": "A variável CERTIFICADO_SENHA não está configurada no Coolify."}
    original_path = fiscal_safe_file(dev["xml_original_path"])
    if not original_path:
        return {"ok": False, "authorized": False, "error": "XML original não foi encontrado no armazenamento persistente. Importe a nota novamente."}

    serie = int(only_digits(cfg_val(cfg, "nf_serie") or "1") or 1)
    current_num = int(only_digits(cfg_val(cfg, "nf_numero_inicial") or "1") or 1)
    if cfg_val(dev, "nf_devolucao_numero"):
        nnf = int(only_digits(cfg_val(dev, "nf_devolucao_numero")) or current_num)
    else:
        nnf = current_num
        exec_sql("UPDATE devolucoes SET nf_devolucao_numero=? WHERE id=?", (str(nnf), devolucao_id))
    issue_dt = cfg_val(dev, "emissao_tentativa_em")
    if not issue_dt:
        issue_dt = datetime.now(ZoneInfo("America/Sao_Paulo")).isoformat(timespec="seconds")
        exec_sql("UPDATE devolucoes SET emissao_tentativa_em=? WHERE id=?", (issue_dt, devolucao_id))
    cNF = str(int(hashlib.sha256(f"{only_digits(cfg_val(cfg,'cnpj'))}:{serie}:{nnf}:{devolucao_id}".encode()).hexdigest()[:12], 16) % 100000000).zfill(8)
    output_dir = os.path.join(FISCAL_NFE_DIR, str(devolucao_id))
    os.makedirs(output_dir, exist_ok=True)

    payload = fiscal_engine_base_payload(cfg)
    payload.update({
        "action": "emit_return",
        "original_xml_path": original_path,
        "chave_origem": dev["chave_origem"],
        "serie": serie,
        "nNF": nnf,
        "cNF": cNF,
        "issue_datetime": issue_dt,
        "lote_id": f"{devolucao_id}{nnf}",
        "motivo": dev["motivo"] or "Devolução de mercadoria",
        "output_dir": output_dir,
        "items": [{
            "item_origem_idx": int(i["item_origem_idx"] or 0),
            "codigo": i["codigo"], "ean": i["ean"], "nome": i["nome"], "ncm": i["ncm"], "cest": i["cest"],
            "cfop_devolucao": i["cfop_devolucao"], "unidade": i["unidade"],
            "quantidade_original": float(i["quantidade_original"] or 0),
            "quantidade_devolver": float(i["quantidade_devolver"] or 0),
            "valor_unitario": float(i["valor_unitario"] or 0),
        } for i in itens],
    })
    result = run_fiscal_engine(payload, timeout=120)
    now = datetime.now(ZoneInfo("America/Sao_Paulo")).isoformat(timespec="seconds")
    if result.get("authorized"):
        with get_db() as db:
            db.execute("""
                UPDATE devolucoes SET status='Autorizada SEFAZ', nf_devolucao_chave=?, protocolo_autorizacao=?,
                    xml_assinado_path=?, xml_autorizado_path=?, danfe_path=?, sefaz_cstat=?, sefaz_motivo=?,
                    autorizado_em=?, ambiente_emissao=? WHERE id=?
            """, (result.get("chave") or "", result.get("protocolo") or "", result.get("signed_xml_path") or "",
                  result.get("xml_path") or "", result.get("danfe_path") or "", result.get("cStat") or "100",
                  result.get("xMotivo") or "Autorizado o uso da NF-e", now, cfg_val(cfg, "ambiente"), devolucao_id))
            apply_devolucao_stock_once(db, devolucao_id)
            next_num = max(current_num, nnf + 1)
            db.execute("UPDATE fiscal_config SET nf_numero_inicial=? WHERE id=1", (str(next_num),))
            db.commit()
        backup_db("nfe-devolucao-autorizada")
    else:
        motivo = result.get("xMotivo") or result.get("error") or "Falha não identificada na autorização."
        cstat = str(result.get("cStat") or "")
        with get_db() as db:
            db.execute("""
                UPDATE devolucoes SET status=?, xml_assinado_path=COALESCE(NULLIF(?,''), xml_assinado_path),
                    sefaz_cstat=?, sefaz_motivo=?, ambiente_emissao=? WHERE id=?
            """, ("Rejeitada SEFAZ" if cstat else "Falha antes do envio à SEFAZ", result.get("signed_xml_path") or "", cstat, motivo, cfg_val(cfg, "ambiente"), devolucao_id))
            db.commit()
    return result


def _nfce_payment_code(forma):
    txt = _pos5890u_ascii(forma or '').lower()
    if 'pix' in txt:
        return '17', None
    if 'credito' in txt or 'crédito' in str(forma or '').lower():
        return '03', None
    if 'debito' in txt or 'débito' in str(forma or '').lower():
        return '04', None
    if 'dinheiro' in txt or 'especie' in txt:
        return '01', None
    if 'cheque' in txt:
        return '02', None
    if 'crediario' in txt or 'crediário' in str(forma or '').lower():
        return '05', None
    if 'boleto' in txt:
        return '15', None
    return '99', str(forma or 'Outros')[:60] or 'Outros'


def _nfce_sale_tax(prod, cfg):
    """Sugere a tributação de saída do Simples a partir do cadastro do produto.

    O cadastro pode ter vindo de XML de compra (CST do fornecedor). Para a saída
    da CENTRALVET, CST 60/10/30/70 ou CSOSN 500 indicam mercadoria já alcançada
    por ST e são convertidos para CSOSN 500. Demais itens usam o padrão 102.
    """
    raw = only_digits(prod['cst_csosn'] if prod else '')
    allowed = {'101','102','103','201','202','203','300','400','500','900'}
    if raw in allowed:
        csosn = raw
    elif raw in {'10','30','60','70'}:
        csosn = '500'
    else:
        cfg_default = only_digits(cfg_val(cfg, 'cst_csosn_padrao')) or '102'
        csosn = cfg_default if cfg_default in allowed else '102'
    manual_cfop = only_digits(prod['cfop'] if prod else '')
    if len(manual_cfop) == 4 and manual_cfop.startswith('5'):
        cfop = manual_cfop
    else:
        cfop = '5405' if csosn == '500' else '5102'
    return csosn, cfop


def emitir_nfce_sefaz_internal(venda_id):
    cfg = fetch_one("SELECT * FROM fiscal_config WHERE id=1")
    venda = fetch_one("SELECT * FROM vendas WHERE id=?", (venda_id,))
    itens = fetch_all("""
        SELECT vi.*, p.codigo, p.unidade, p.ncm, p.cfop, p.cst_csosn, p.ean, p.cest
        FROM venda_itens vi LEFT JOIN produtos p ON p.id=vi.produto_id
        WHERE vi.venda_id=? ORDER BY vi.id
    """, (venda_id,))
    if not cfg or not venda or not itens:
        return {'ok': False, 'authorized': False, 'error': 'Venda ou configuração fiscal não encontrada.'}
    if cfg_val(venda, 'nf_status') == 'Autorizada SEFAZ' and cfg_val(venda, 'nf_protocolo'):
        return {
            'ok': True, 'authorized': True, 'cStat': cfg_val(venda, 'nf_cstat') or '100',
            'xMotivo': cfg_val(venda, 'nf_motivo') or 'Autorizada', 'chave': cfg_val(venda, 'nf_chave'),
            'protocolo': cfg_val(venda, 'nf_protocolo'), 'xml_path': cfg_val(venda, 'nf_xml_path'),
            'danfe_path': cfg_val(venda, 'nf_danfe_path')
        }

    cert = cert_runtime_status()
    if not cert.get('exists'):
        return {'ok': False, 'authorized': False, 'error': f"Certificado A1 não encontrado em {cert.get('path') or 'CERTIFICADO_PATH'}."}
    if not cert.get('has_password'):
        return {'ok': False, 'authorized': False, 'error': 'A variável CERTIFICADO_SENHA não está configurada no Coolify.'}
    if not cfg_val(cfg, 'csc_id') or not cfg_val(cfg, 'csc_token'):
        return {'ok': False, 'authorized': False, 'error': 'CSC/ID Token da NFC-e não estão configurados.'}

    serie = int(only_digits(cfg_val(cfg, 'nfce_serie') or '1') or 1)
    current_num = int(only_digits(cfg_val(cfg, 'nfce_numero_inicial') or '1') or 1)
    saved_num = only_digits(cfg_val(venda, 'nf_numero'))
    nnf = int(saved_num) if saved_num else current_num
    issue_dt = cfg_val(venda, 'nf_emissao_tentativa_em')
    if not issue_dt:
        issue_dt = datetime.now(ZoneInfo('America/Sao_Paulo')).isoformat(timespec='seconds')
        exec_sql("UPDATE vendas SET nf_emissao_tentativa_em=?, nf_numero=?, nf_serie=?, nf_tipo='NFC-e' WHERE id=?",
                 (issue_dt, str(nnf), str(serie), venda_id))
    else:
        exec_sql("UPDATE vendas SET nf_numero=?, nf_serie=?, nf_tipo='NFC-e' WHERE id=?", (str(nnf), str(serie), venda_id))

    subtotal = sum(float(i['total'] or 0) for i in itens)
    desconto_total = max(0.0, float(venda['desconto'] or 0))
    descontos = []
    restante = round(desconto_total, 2)
    for idx, i in enumerate(itens):
        if idx == len(itens)-1:
            d = restante
        else:
            base = float(i['total'] or 0)
            d = round(desconto_total * (base / subtotal), 2) if subtotal > 0 else 0.0
            d = min(d, restante)
            restante = round(restante - d, 2)
        descontos.append(max(0.0, d))

    fiscal_items = []
    for idx, i in enumerate(itens):
        prod = i
        ncm = only_digits(i['ncm'] or '')
        if len(ncm) not in (2,8):
            return {'ok': False, 'authorized': False, 'error': f"O produto '{i['produto_nome']}' está sem NCM válido. Edite o produto antes de emitir a NFC-e."}
        csosn, cfop = _nfce_sale_tax(prod, cfg)
        fiscal_items.append({
            'codigo': i['codigo'] or str(i['produto_id'] or idx+1),
            'nome': i['produto_nome'], 'ean': i['ean'] or '', 'ncm': ncm, 'cest': i['cest'] or '',
            'cfop': cfop, 'csosn': csosn, 'orig': '0', 'unidade': normalizar_unidade(i['unidade']),
            'quantidade': float(i['quantidade'] or 0), 'valor_unitario': float(i['preco_unit'] or 0),
            'valor_produto': float(i['total'] or 0), 'desconto': descontos[idx],
        })

    customer = {}
    if venda['cliente_id']:
        cli = fetch_one("SELECT * FROM clientes WHERE id=?", (venda['cliente_id'],))
        if cli:
            customer = {'nome': cli['nome'] or venda['cliente_nome'], 'documento': cli['cpf_cnpj'] or '', 'email': cli['email'] or ''}

    tpag, xpag = _nfce_payment_code(venda['forma_pagamento'])
    cNF = str(int(hashlib.sha256(f"NFCE:{only_digits(cfg_val(cfg,'cnpj'))}:{serie}:{nnf}:{venda_id}".encode()).hexdigest()[:12], 16) % 100000000).zfill(8)
    output_dir = os.path.join(FISCAL_NFCE_DIR, str(venda_id))
    os.makedirs(output_dir, exist_ok=True)
    payload = fiscal_engine_base_payload(cfg)
    payload.update({
        'action': 'emit_nfce', 'serie': serie, 'nNF': nnf, 'cNF': cNF, 'issue_datetime': issue_dt,
        'lote_id': f"65{venda_id}{nnf}", 'output_dir': output_dir, 'customer': customer,
        'tPag': tpag, 'xPag': xpag, 'indPag': 0, 'valor_pago': float(venda['total'] or 0), 'troco': 0,
        'informacoes_complementares': f"Venda #{venda_id} - CENTRALVET Agropecuaria",
        'items': fiscal_items,
    })
    result = run_fiscal_engine(payload, timeout=120)
    now = datetime.now(ZoneInfo('America/Sao_Paulo')).isoformat(timespec='seconds')
    if result.get('authorized'):
        with get_db() as db:
            db.execute("""
                UPDATE vendas SET nf_tipo='NFC-e', nf_status='Autorizada SEFAZ', nf_numero=?, nf_serie=?,
                    nf_chave=?, nf_protocolo=?, nf_xml_path=?, nf_danfe_path=?, nf_cstat=?, nf_motivo=?,
                    nf_autorizado_em=?, nf_obs=? WHERE id=?
            """, (str(nnf), str(serie), result.get('chave') or '', result.get('protocolo') or '',
                  result.get('xml_path') or '', result.get('danfe_path') or '', result.get('cStat') or '100',
                  result.get('xMotivo') or 'Autorizado o uso da NFC-e', now,
                  'NFC-e autorizada diretamente pela SEFAZ/MG.', venda_id))
            db.execute("UPDATE fiscal_config SET nfce_numero_inicial=? WHERE id=1", (str(max(current_num, nnf + 1)),))
            db.commit()
        backup_db('nfce-autorizada')
    else:
        motivo = result.get('xMotivo') or result.get('error') or 'Falha não identificada na autorização da NFC-e.'
        cstat = str(result.get('cStat') or '')
        with get_db() as db:
            db.execute("""
                UPDATE vendas SET nf_tipo='NFC-e', nf_status=?, nf_numero=?, nf_serie=?, nf_cstat=?, nf_motivo=?, nf_obs=? WHERE id=?
            """, ('Rejeitada SEFAZ' if cstat else 'Falha antes do envio à SEFAZ', str(nnf), str(serie), cstat, motivo, motivo, venda_id))
            db.commit()
        backup_db('nfce-falha')
    return result


def init_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS clientes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            cpf_cnpj TEXT,
            telefone TEXT,
            email TEXT,
            endereco TEXT,
            observacao TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS fornecedores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            cpf_cnpj TEXT,
            telefone TEXT,
            email TEXT,
            endereco TEXT,
            observacao TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS produtos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo TEXT,
            nome TEXT NOT NULL,
            categoria TEXT,
            unidade TEXT DEFAULT 'UN',
            preco_custo REAL DEFAULT 0,
            preco_venda REAL DEFAULT 0,
            estoque REAL DEFAULT 0,
            estoque_minimo REAL DEFAULT 0,
            ncm TEXT,
            cfop TEXT,
            cst_csosn TEXT,
            aliquota REAL DEFAULT 0,
            ativo INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS estoque_mov (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT,
            produto_id INTEGER,
            tipo TEXT,
            quantidade REAL DEFAULT 0,
            custo_unit REAL DEFAULT 0,
            valor_total REAL DEFAULT 0,
            origem TEXT,
            observacao TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS vendas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT,
            cliente_id INTEGER,
            cliente_nome TEXT,
            forma_pagamento TEXT,
            status TEXT,
            subtotal REAL DEFAULT 0,
            desconto REAL DEFAULT 0,
            total REAL DEFAULT 0,
            lucro REAL DEFAULT 0,
            observacao TEXT,
            nf_tipo TEXT,
            nf_status TEXT DEFAULT 'Não emitida',
            nf_numero TEXT,
            nf_obs TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS venda_itens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            venda_id INTEGER,
            produto_id INTEGER,
            produto_nome TEXT,
            quantidade REAL DEFAULT 0,
            preco_unit REAL DEFAULT 0,
            custo_unit REAL DEFAULT 0,
            total REAL DEFAULT 0,
            lucro REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS financeiro (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT,
            tipo TEXT,
            descricao TEXT,
            valor REAL DEFAULT 0,
            forma TEXT,
            status TEXT DEFAULT 'Pago',
            vencimento TEXT,
            pessoa TEXT,
            referencia_tipo TEXT,
            referencia_id INTEGER,
            observacao TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS fiscal_config (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            razao_social TEXT,
            nome_fantasia TEXT,
            cnpj TEXT,
            inscricao_estadual TEXT,
            endereco TEXT,
            municipio TEXT,
            uf TEXT,
            cep TEXT,
            telefone TEXT,
            email TEXT,
            regime TEXT,
            ambiente TEXT DEFAULT 'Homologação',
            certificado_nome TEXT,
            certificado_path TEXT,
            certificado_senha_env TEXT,
            nf_serie TEXT,
            nf_numero_inicial TEXT,
            nfce_serie TEXT,
            nfce_numero_inicial TEXT,
            csc_id TEXT,
            csc_token TEXT,
            cfop_padrao TEXT,
            cst_csosn_padrao TEXT,
            observacao TEXT
        );
        CREATE TABLE IF NOT EXISTS xml_imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chave TEXT UNIQUE,
            xml_hash TEXT UNIQUE,
            numero TEXT,
            serie TEXT,
            emissao TEXT,
            fornecedor_nome TEXT,
            fornecedor_cnpj TEXT,
            total REAL DEFAULT 0,
            itens_qtd INTEGER DEFAULT 0,
            observacao TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS devolucoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT,
            fornecedor_nome TEXT,
            fornecedor_cnpj TEXT,
            destinatario_nome TEXT,
            destinatario_cnpj TEXT,
            chave_origem TEXT,
            numero_origem TEXT,
            serie_origem TEXT,
            emissao_origem TEXT,
            total_original REAL DEFAULT 0,
            motivo TEXT,
            status TEXT DEFAULT 'Gerada',
            baixou_estoque INTEGER DEFAULT 0,
            nf_devolucao_numero TEXT,
            nf_devolucao_chave TEXT,
            xml_hash TEXT,
            observacao TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS devolucao_itens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            devolucao_id INTEGER,
            produto_id INTEGER,
            codigo TEXT,
            ean TEXT,
            nome TEXT,
            ncm TEXT,
            cest TEXT,
            cfop_original TEXT,
            cfop_devolucao TEXT,
            cst_csosn TEXT,
            unidade TEXT,
            quantidade_original REAL DEFAULT 0,
            quantidade_devolver REAL DEFAULT 0,
            valor_unitario REAL DEFAULT 0,
            valor_total REAL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)
        # default fiscal config based on CENTRALVET documents and SIARE/NFC-e data
        db.execute("""
            INSERT OR IGNORE INTO fiscal_config
            (id, razao_social, nome_fantasia, cnpj, inscricao_estadual, endereco, municipio, uf, cep, telefone, email, regime, ambiente,
             certificado_nome, certificado_path, certificado_senha_env, nf_serie, nf_numero_inicial, nfce_serie, nfce_numero_inicial,
             csc_id, csc_token, cfop_padrao, cst_csosn_padrao, observacao)
            VALUES (1, :razao_social, :nome_fantasia, :cnpj, :inscricao_estadual, :endereco, :municipio, :uf, :cep, :telefone, :email, :regime, :ambiente,
             :certificado_nome, :certificado_path, :certificado_senha_env, :nf_serie, :nf_numero_inicial, :nfce_serie, :nfce_numero_inicial,
             :csc_id, :csc_token, :cfop_padrao, :cst_csosn_padrao, :observacao)
        """, FISCAL_DEFAULTS)
        for column, ddl in [
            ('ean', 'TEXT'), ('cest', 'TEXT'), ('ultima_chave_xml', 'TEXT'), ('fornecedor_id', 'INTEGER')
        ]:
            add_column(db, 'produtos', column, ddl)
        # Corrige cadastros antigos em que a quantidade foi digitada por engano no campo Unidade
        # (ex.: estoque 0,00 + unidade '1' aparecia visualmente como '0,001').
        for prod in db.execute("SELECT id, unidade FROM produtos").fetchall():
            unidade_corrigida = normalizar_unidade(prod["unidade"])
            if unidade_corrigida != (prod["unidade"] or ""):
                db.execute("UPDATE produtos SET unidade=? WHERE id=?", (unidade_corrigida, prod["id"]))
        # Migrações de compatibilidade com bancos de versões antigas.
        # CREATE TABLE IF NOT EXISTS não adiciona colunas novas em uma tabela já existente,
        # então cada coluna usada por vendas/relatórios precisa ser garantida explicitamente.
        for column, ddl in [
            ('data', 'TEXT'), ('cliente_id', 'INTEGER'), ('cliente_nome', 'TEXT'), ('forma_pagamento', 'TEXT'),
            ('status', "TEXT DEFAULT 'Pago'"), ('subtotal', 'REAL DEFAULT 0'), ('desconto', 'REAL DEFAULT 0'),
            ('total', 'REAL DEFAULT 0'), ('lucro', 'REAL DEFAULT 0'), ('observacao', 'TEXT'), ('nf_tipo', 'TEXT'),
            ('nf_status', "TEXT DEFAULT 'Não emitida'"), ('nf_numero', 'TEXT'), ('nf_obs', 'TEXT'),
            ('nf_serie', 'TEXT'), ('nf_chave', 'TEXT'), ('nf_protocolo', 'TEXT'), ('nf_xml_path', 'TEXT'),
            ('nf_danfe_path', 'TEXT'), ('nf_cstat', 'TEXT'), ('nf_motivo', 'TEXT'), ('nf_autorizado_em', 'TEXT'),
            ('nf_emissao_tentativa_em', 'TEXT'), ('created_at', 'TEXT')
        ]:
            add_column(db, 'vendas', column, ddl)
        for column, ddl in [
            ('venda_id', 'INTEGER'), ('produto_id', 'INTEGER'), ('produto_nome', 'TEXT'),
            ('quantidade', 'REAL DEFAULT 0'), ('preco_unit', 'REAL DEFAULT 0'), ('custo_unit', 'REAL DEFAULT 0'),
            ('total', 'REAL DEFAULT 0'), ('lucro', 'REAL DEFAULT 0')
        ]:
            add_column(db, 'venda_itens', column, ddl)
        for column, ddl in [
            ('data', 'TEXT'), ('tipo', 'TEXT'), ('descricao', 'TEXT'), ('valor', 'REAL DEFAULT 0'),
            ('forma', 'TEXT'), ('status', "TEXT DEFAULT 'Pago'"), ('vencimento', 'TEXT'), ('pessoa', 'TEXT'),
            ('referencia_tipo', 'TEXT'), ('referencia_id', 'INTEGER'), ('observacao', 'TEXT'), ('created_at', 'TEXT')
        ]:
            add_column(db, 'financeiro', column, ddl)
        for column, ddl in [
            ('chave', 'TEXT'), ('xml_hash', 'TEXT'), ('numero', 'TEXT'), ('serie', 'TEXT'), ('emissao', 'TEXT'),
            ('fornecedor_nome', 'TEXT'), ('fornecedor_cnpj', 'TEXT'), ('total', 'REAL DEFAULT 0'),
            ('itens_qtd', 'INTEGER DEFAULT 0'), ('observacao', 'TEXT'), ('created_at', 'TEXT'),
            ('xml_path', 'TEXT'), ('tipo', "TEXT DEFAULT 'Entrada'"), ('devolucao_id', 'INTEGER')
        ]:
            add_column(db, 'xml_imports', column, ddl)

        for column, ddl in [
            ('certificado_path', 'TEXT'), ('certificado_senha_env', 'TEXT'), ('nf_serie', 'TEXT'), ('nf_numero_inicial', 'TEXT'),
            ('nfce_serie', 'TEXT'), ('nfce_numero_inicial', 'TEXT'), ('csc_id', 'TEXT'), ('csc_token', 'TEXT'),
            ('cfop_padrao', 'TEXT'), ('cst_csosn_padrao', 'TEXT'), ('logradouro', 'TEXT'), ('numero', 'TEXT'),
            ('complemento', 'TEXT'), ('bairro', 'TEXT'), ('codigo_municipio', 'TEXT'), ('cnae', 'TEXT'), ('crt', 'INTEGER DEFAULT 1')
        ]:
            add_column(db, 'fiscal_config', column, ddl)
        for column, ddl in [
            ('xml_original_path', 'TEXT'), ('xml_assinado_path', 'TEXT'), ('xml_autorizado_path', 'TEXT'), ('danfe_path', 'TEXT'),
            ('protocolo_autorizacao', 'TEXT'), ('sefaz_cstat', 'TEXT'), ('sefaz_motivo', 'TEXT'), ('autorizado_em', 'TEXT'),
            ('ambiente_emissao', 'TEXT'), ('baixar_estoque_solicitado', 'INTEGER DEFAULT 1'), ('emissao_tentativa_em', 'TEXT')
        ]:
            add_column(db, 'devolucoes', column, ddl)
        add_column(db, 'devolucao_itens', 'item_origem_idx', 'INTEGER')
        db.execute("""UPDATE fiscal_config SET
            logradouro=COALESCE(NULLIF(logradouro,''), :logradouro), numero=COALESCE(NULLIF(numero,''), :numero),
            complemento=COALESCE(NULLIF(complemento,''), :complemento), bairro=COALESCE(NULLIF(bairro,''), :bairro),
            codigo_municipio=COALESCE(NULLIF(codigo_municipio,''), :codigo_municipio), cnae=COALESCE(NULLIF(cnae,''), :cnae),
            crt=COALESCE(crt, :crt) WHERE id=1""", FISCAL_DEFAULTS)
        # Fill empty fiscal fields on existing deployments, preserving manual changes already made in the system.
        db.execute("""
            UPDATE fiscal_config SET
              razao_social=COALESCE(NULLIF(razao_social,''), :razao_social),
              nome_fantasia=COALESCE(NULLIF(nome_fantasia,''), :nome_fantasia),
              cnpj=COALESCE(NULLIF(cnpj,''), :cnpj),
              inscricao_estadual=COALESCE(NULLIF(inscricao_estadual,''), :inscricao_estadual),
              endereco=COALESCE(NULLIF(endereco,''), :endereco),
              municipio=COALESCE(NULLIF(municipio,''), :municipio),
              uf=COALESCE(NULLIF(uf,''), :uf),
              cep=COALESCE(NULLIF(cep,''), :cep),
              telefone=COALESCE(NULLIF(telefone,''), :telefone),
              email=COALESCE(NULLIF(email,''), :email),
              regime=COALESCE(NULLIF(regime,''), :regime),
              ambiente=COALESCE(NULLIF(ambiente,''), :ambiente),
              certificado_nome=COALESCE(NULLIF(certificado_nome,''), :certificado_nome),
              certificado_path=COALESCE(NULLIF(certificado_path,''), :certificado_path),
              certificado_senha_env=COALESCE(NULLIF(certificado_senha_env,''), :certificado_senha_env),
              nf_serie=COALESCE(NULLIF(nf_serie,''), :nf_serie),
              nf_numero_inicial=COALESCE(NULLIF(nf_numero_inicial,''), :nf_numero_inicial),
              nfce_serie=COALESCE(NULLIF(nfce_serie,''), :nfce_serie),
              nfce_numero_inicial=COALESCE(NULLIF(nfce_numero_inicial,''), :nfce_numero_inicial),
              csc_id=COALESCE(NULLIF(csc_id,''), :csc_id),
              csc_token=COALESCE(NULLIF(csc_token,''), :csc_token),
              cfop_padrao=COALESCE(NULLIF(cfop_padrao,''), :cfop_padrao),
              cst_csosn_padrao=COALESCE(NULLIF(cst_csosn_padrao,''), :cst_csosn_padrao),
              observacao=COALESCE(NULLIF(observacao,''), :observacao)
            WHERE id=1
        """, FISCAL_DEFAULTS)
        db.commit()

# Antes de criar as tabelas, tenta recuperar a cópia espelho persistente.
# Isso é propositalmente executado antes do init_db(), para um banco vazio do novo container
# não esconder a cópia com os cadastros anteriores.
restore_db_from_persistent_mirror()
init_db()
sync_persistent_mirror()
print(f"[CENTRALVET] Banco ativo: {DB_PATH} | espelho: {MIRROR_DB_PATH}", flush=True)


def _xml_local(node, name):
    for child in node.iter():
        if child.tag.split('}')[-1] == name:
            return child
    return None


def _xml_text(node, name, default=""):
    found = _xml_local(node, name) if node is not None else None
    return (found.text or "").strip() if found is not None and found.text else default


def _parse_authorized_return_xml(path):
    """Extrai os dados necessários de uma NF-e de devolução já autorizada."""
    try:
        root = ET.parse(path).getroot()
        inf = None
        prot = None
        for node in root.iter():
            lname = node.tag.split('}')[-1]
            if lname == 'infNFe' and inf is None:
                inf = node
            elif lname == 'infProt' and prot is None:
                prot = node
        if inf is None or prot is None:
            return None
        ide = _xml_local(inf, 'ide')
        emit = _xml_local(inf, 'emit')
        dest = _xml_local(inf, 'dest')
        total = _xml_local(inf, 'ICMSTot')
        inf_adic = _xml_local(inf, 'infAdic')
        cstat = _xml_text(prot, 'cStat')
        if cstat not in ('100', '150'):
            return None
        emit_cnpj = only_digits(_xml_text(emit, 'CNPJ'))
        if emit_cnpj and emit_cnpj != only_digits(FISCAL_DEFAULTS['cnpj']):
            return None
        natop = _xml_text(ide, 'natOp').upper()
        finnfe = _xml_text(ide, 'finNFe')
        if 'DEVOL' not in natop and finnfe != '4':
            return None
        ref_key = ''
        nfref = _xml_local(ide, 'NFref')
        if nfref is not None:
            ref_key = only_digits(_xml_text(nfref, 'refNFe'))
        chave = only_digits(_xml_text(prot, 'chNFe'))
        if not chave:
            chave = only_digits(inf.attrib.get('Id', '').replace('NFe', ''))
        infcpl = _xml_text(inf_adic, 'infCpl')
        motivo = 'Devolução de mercadoria'
        if 'Motivo:' in infcpl:
            motivo = infcpl.split('Motivo:', 1)[1].strip() or motivo
        items = []
        for det in [n for n in inf if n.tag.split('}')[-1] == 'det']:
            prod = _xml_local(det, 'prod')
            imposto = _xml_local(det, 'imposto')
            icms = _xml_local(imposto, 'ICMS') if imposto is not None else None
            cst = ''
            if icms is not None:
                cst = _xml_text(icms, 'CSOSN') or _xml_text(icms, 'CST')
            q = money_to_float(_xml_text(prod, 'qCom'))
            vu = money_to_float(_xml_text(prod, 'vUnCom'))
            vt = money_to_float(_xml_text(prod, 'vProd')) or (q * vu)
            items.append({
                'codigo': _xml_text(prod, 'cProd'), 'ean': _xml_text(prod, 'cEAN'), 'nome': _xml_text(prod, 'xProd'),
                'ncm': _xml_text(prod, 'NCM'), 'cest': _xml_text(prod, 'CEST'), 'cfop_original': '',
                'cfop_devolucao': _xml_text(prod, 'CFOP'), 'cst_csosn': cst, 'unidade': _xml_text(prod, 'uCom') or 'UN',
                'quantidade_original': q, 'quantidade_devolver': q, 'valor_unitario': vu, 'valor_total': vt,
            })
        return {
            'data': (_xml_text(ide, 'dhEmi') or _xml_text(ide, 'dEmi') or '')[:10],
            'fornecedor_nome': _xml_text(dest, 'xNome'),
            'fornecedor_cnpj': _xml_text(dest, 'CNPJ') or _xml_text(dest, 'CPF'),
            'chave_origem': ref_key,
            'numero_origem': str(int(ref_key[25:34])) if len(ref_key) == 44 and ref_key[25:34].isdigit() else '',
            'serie_origem': str(int(ref_key[22:25])) if len(ref_key) == 44 and ref_key[22:25].isdigit() else '',
            'total': money_to_float(_xml_text(total, 'vNF')),
            'motivo': motivo, 'nf_devolucao_numero': str(int(_xml_text(ide, 'nNF') or '0')),
            'serie': str(int(_xml_text(ide, 'serie') or '1')), 'chave': chave, 'protocolo': _xml_text(prot, 'nProt'),
            'cStat': cstat, 'xMotivo': _xml_text(prot, 'xMotivo') or 'Autorizado o uso da NF-e',
            'autorizado_em': _xml_text(prot, 'dhRecbto') or _xml_text(ide, 'dhEmi'), 'items': items,
        }
    except Exception:
        return None


def _insert_recovered_return(data, xml_path='', danfe_path='', signed_path=''):
    chave = only_digits(data.get('chave'))
    if not chave:
        return None
    existing = fetch_one('SELECT * FROM devolucoes WHERE nf_devolucao_chave=? LIMIT 1', (chave,))
    if existing:
        updates = []
        params = []
        for col, val in [('xml_autorizado_path', xml_path), ('danfe_path', danfe_path), ('xml_assinado_path', signed_path)]:
            if val and os.path.isfile(val) and not cfg_val(existing, col):
                updates.append(f'{col}=?')
                params.append(val)
        if updates:
            params.append(existing['id'])
            exec_sql(f"UPDATE devolucoes SET {', '.join(updates)} WHERE id=?", params)
        count = fetch_one('SELECT COUNT(*) c FROM devolucao_itens WHERE devolucao_id=?', (existing['id'],))['c']
        if not count and data.get('items'):
            with get_db() as db:
                for idx, item in enumerate(data['items'], start=1):
                    prod = db.execute('SELECT id FROM produtos WHERE codigo=? LIMIT 1', (item.get('codigo') or '',)).fetchone()
                    db.execute("""INSERT INTO devolucao_itens
                        (devolucao_id, produto_id, item_origem_idx, codigo, ean, nome, ncm, cest, cfop_original, cfop_devolucao, cst_csosn,
                         unidade, quantidade_original, quantidade_devolver, valor_unitario, valor_total)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (existing['id'], prod['id'] if prod else None, idx, item.get('codigo'), item.get('ean'), item.get('nome'), item.get('ncm'),
                         item.get('cest'), item.get('cfop_original'), item.get('cfop_devolucao'), item.get('cst_csosn'), item.get('unidade') or 'UN',
                         item.get('quantidade_original') or item.get('quantidade_devolver') or 0, item.get('quantidade_devolver') or 0,
                         item.get('valor_unitario') or 0, item.get('valor_total') or 0))
                db.commit()
        return existing['id']
    with get_db() as db:
        cur = db.execute("""INSERT INTO devolucoes
            (data, fornecedor_nome, fornecedor_cnpj, destinatario_nome, destinatario_cnpj, chave_origem, numero_origem, serie_origem,
             total_original, motivo, status, baixou_estoque, baixar_estoque_solicitado, nf_devolucao_numero, nf_devolucao_chave,
             xml_hash, observacao, xml_assinado_path, xml_autorizado_path, danfe_path, protocolo_autorizacao, sefaz_cstat, sefaz_motivo,
             autorizado_em, ambiente_emissao)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Autorizada SEFAZ', 1, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Produção')""",
            (data.get('data') or today_str(), data.get('fornecedor_nome'), data.get('fornecedor_cnpj'),
             FISCAL_DEFAULTS['razao_social'], FISCAL_DEFAULTS['cnpj'], data.get('chave_origem'), data.get('numero_origem'), data.get('serie_origem'),
             data.get('total') or 0, data.get('motivo') or 'Devolução de mercadoria', data.get('nf_devolucao_numero'), chave,
             hashlib.sha256((chave + '|recuperada').encode()).hexdigest(),
             'NF-e autorizada recuperada automaticamente após atualização do sistema. Nenhuma nova emissão foi feita.',
             signed_path or '', xml_path or '', danfe_path or '', data.get('protocolo') or '', data.get('cStat') or '100',
             data.get('xMotivo') or 'Autorizado o uso da NF-e', data.get('autorizado_em') or ''))
        devolucao_id = cur.lastrowid
        for idx, item in enumerate(data.get('items') or [], start=1):
            prod = db.execute('SELECT id FROM produtos WHERE codigo=? LIMIT 1', (item.get('codigo') or '',)).fetchone()
            db.execute("""INSERT INTO devolucao_itens
                (devolucao_id, produto_id, item_origem_idx, codigo, ean, nome, ncm, cest, cfop_original, cfop_devolucao, cst_csosn,
                 unidade, quantidade_original, quantidade_devolver, valor_unitario, valor_total)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (devolucao_id, prod['id'] if prod else None, idx, item.get('codigo'), item.get('ean'), item.get('nome'), item.get('ncm'),
                 item.get('cest'), item.get('cfop_original'), item.get('cfop_devolucao'), item.get('cst_csosn'), item.get('unidade') or 'UN',
                 item.get('quantidade_original') or item.get('quantidade_devolver') or 0, item.get('quantidade_devolver') or 0,
                 item.get('valor_unitario') or 0, item.get('valor_total') or 0))
        cfg = db.execute('SELECT nf_numero_inicial FROM fiscal_config WHERE id=1').fetchone()
        atual = int(only_digits(cfg['nf_numero_inicial'] if cfg else '1') or 1)
        numero_rec = int(only_digits(data.get('nf_devolucao_numero')) or 0)
        if numero_rec and atual <= numero_rec:
            db.execute('UPDATE fiscal_config SET nf_numero_inicial=? WHERE id=1', (str(numero_rec + 1),))
        db.commit()
    return devolucao_id


def _find_sibling_danfe(xml_path):
    folder = os.path.dirname(xml_path)
    if not os.path.isdir(folder):
        return ''
    candidates = [os.path.join(folder, n) for n in os.listdir(folder) if n.lower().endswith('.pdf') and 'danfe' in n.lower()]
    return sorted(candidates)[-1] if candidates else ''


def recover_authorized_returns_from_files():
    try:
        for root_dir, _, names in os.walk(FISCAL_NFE_DIR):
            for name in names:
                if not name.lower().endswith('-autorizado.xml'):
                    continue
                xml_path = os.path.join(root_dir, name)
                data = _parse_authorized_return_xml(xml_path)
                if not data:
                    continue
                guess = xml_path[:-len('-autorizado.xml')] + '-assinado.xml'
                signed = guess if os.path.isfile(guess) else ''
                _insert_recovered_return(data, xml_path, _find_sibling_danfe(xml_path), signed)
    except Exception:
        pass


def recover_bundled_authorized_returns():
    recovery_dir = os.path.join(app.root_path, 'recovery')
    if not os.path.isdir(recovery_dir):
        return
    for name in os.listdir(recovery_dir):
        if not name.endswith('.json'):
            continue
        try:
            with open(os.path.join(recovery_dir, name), 'r', encoding='utf-8') as f:
                data = json.load(f)
            chave = only_digits(data.get('chave'))
            existing = fetch_one('SELECT * FROM devolucoes WHERE nf_devolucao_chave=? LIMIT 1', (chave,))
            bundle_pdf = os.path.join(app.root_path, data.get('danfe_bundle') or '')
            if existing:
                current_danfe = fiscal_safe_file(cfg_val(existing, 'danfe_path'))
                if bundle_pdf and os.path.isfile(bundle_pdf) and not current_danfe:
                    folder = os.path.join(FISCAL_NFE_DIR, str(existing['id']))
                    os.makedirs(folder, exist_ok=True)
                    dest = os.path.join(folder, f"NFe-{int(data.get('serie') or 1)}-{int(data.get('nf_devolucao_numero') or 1)}-DANFE.pdf")
                    if not os.path.isfile(dest):
                        shutil.copy2(bundle_pdf, dest)
                    exec_sql('UPDATE devolucoes SET danfe_path=? WHERE id=?', (dest, existing['id']))
                refreshed = fetch_one('SELECT * FROM devolucoes WHERE id=?', (existing['id'],))
                _insert_recovered_return(data, cfg_val(refreshed, 'xml_autorizado_path') or '', cfg_val(refreshed, 'danfe_path') or '', cfg_val(refreshed, 'xml_assinado_path') or '')
                continue
            devolucao_id = _insert_recovered_return(data)
            if devolucao_id and bundle_pdf and os.path.isfile(bundle_pdf):
                folder = os.path.join(FISCAL_NFE_DIR, str(devolucao_id))
                os.makedirs(folder, exist_ok=True)
                dest = os.path.join(folder, f"NFe-{int(data.get('serie') or 1)}-{int(data.get('nf_devolucao_numero') or 1)}-DANFE.pdf")
                shutil.copy2(bundle_pdf, dest)
                exec_sql('UPDATE devolucoes SET danfe_path=? WHERE id=?', (dest, devolucao_id))
        except Exception:
            continue


def recover_devolucao_files():
    """Reconecta arquivos fiscais e recria NF-es autorizadas que sumiram do histórico."""
    try:
        rows = fetch_all("SELECT id, xml_assinado_path, xml_autorizado_path, danfe_path FROM devolucoes ORDER BY id")
        for row in rows:
            folder = os.path.join(FISCAL_NFE_DIR, str(row["id"]))
            if not os.path.isdir(folder):
                continue
            names = os.listdir(folder)
            def pick(suffix):
                matches = [os.path.join(folder, n) for n in names if n.lower().endswith(suffix.lower())]
                return sorted(matches)[-1] if matches else ""
            signed = row["xml_assinado_path"] if row["xml_assinado_path"] and os.path.isfile(row["xml_assinado_path"]) else pick("-assinado.xml")
            authorized = row["xml_autorizado_path"] if row["xml_autorizado_path"] and os.path.isfile(row["xml_autorizado_path"]) else pick("-autorizado.xml")
            danfe = row["danfe_path"] if row["danfe_path"] and os.path.isfile(row["danfe_path"]) else pick("-danfe.pdf")
            if signed or authorized or danfe:
                exec_sql("""UPDATE devolucoes SET
                    xml_assinado_path=COALESCE(NULLIF(?,''), xml_assinado_path),
                    xml_autorizado_path=COALESCE(NULLIF(?,''), xml_autorizado_path),
                    danfe_path=COALESCE(NULLIF(?,''), danfe_path)
                    WHERE id=?""", (signed, authorized, danfe, row["id"]))
    except Exception:
        pass
    recover_authorized_returns_from_files()
    recover_bundled_authorized_returns()


recover_devolucao_files()


# ---------------- Auth ----------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("usuario") == ADMIN_USER and request.form.get("senha") == ADMIN_PASSWORD:
            session["logged"] = True
            session["user"] = ADMIN_USER
            return redirect(url_for("index"))
        flash("Usuário ou senha inválidos.", "erro")
    return render_template("login.html", app_name=APP_NAME)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------- Dashboard ----------------

@app.route("/")
@login_required
def index():
    hoje = today_str()
    total_produtos = fetch_one("SELECT COUNT(*) c FROM produtos WHERE ativo=1")["c"]
    baixo = fetch_one("SELECT COUNT(*) c FROM produtos WHERE ativo=1 AND estoque <= estoque_minimo")["c"]
    estoque_valor = fetch_one("SELECT COALESCE(SUM(estoque * preco_custo),0) v FROM produtos WHERE ativo=1")["v"]
    vendas_mes = fetch_one("SELECT COALESCE(SUM(total),0) v FROM vendas WHERE substr(data,1,7)=substr(?,1,7)", (hoje,))["v"]
    lucro_mes = fetch_one("SELECT COALESCE(SUM(lucro),0) v FROM vendas WHERE substr(data,1,7)=substr(?,1,7)", (hoje,))["v"]
    a_receber = fetch_one("SELECT COALESCE(SUM(valor),0) v FROM financeiro WHERE tipo='Entrada' AND status!='Pago'")["v"]
    a_pagar = fetch_one("SELECT COALESCE(SUM(valor),0) v FROM financeiro WHERE tipo='Saída' AND status!='Pago'")["v"]
    saldo_caixa = fetch_one("""
        SELECT COALESCE(SUM(CASE WHEN tipo='Entrada' THEN valor ELSE -valor END),0) v
        FROM financeiro WHERE status='Pago'
    """)["v"]
    ult_vendas = fetch_all("SELECT * FROM vendas ORDER BY id DESC LIMIT 6")
    estoque_baixo = fetch_all("SELECT * FROM produtos WHERE ativo=1 AND estoque <= estoque_minimo ORDER BY estoque ASC LIMIT 8")
    pendencias = []
    if baixo:
        pendencias.append(f"{baixo} produto(s) com estoque baixo")
    if a_receber:
        pendencias.append(f"{fmt_money(a_receber)} a receber")
    if a_pagar:
        pendencias.append(f"{fmt_money(a_pagar)} a pagar")
    return render_template("index.html", **locals())


# ---------------- Pessoas ----------------

def salvar_pessoa(table):
    nome = request.form.get("nome", "").strip()
    if not nome:
        flash("Informe o nome.", "erro")
        return None
    data = (
        nome,
        request.form.get("cpf_cnpj", "").strip(),
        request.form.get("telefone", "").strip(),
        request.form.get("email", "").strip(),
        request.form.get("endereco", "").strip(),
        request.form.get("observacao", "").strip()
    )
    with get_db() as db:
        cur = db.execute(f"""
            INSERT INTO {table} (nome, cpf_cnpj, telefone, email, endereco, observacao)
            VALUES (?, ?, ?, ?, ?, ?)
        """, data)
        db.commit()
        return cur.lastrowid

def atualizar_pessoa(table, item_id):
    nome = request.form.get("nome", "").strip()
    if not nome:
        flash("Informe o nome.", "erro")
        return
    data = (
        nome,
        request.form.get("cpf_cnpj", "").strip(),
        request.form.get("telefone", "").strip(),
        request.form.get("email", "").strip(),
        request.form.get("endereco", "").strip(),
        request.form.get("observacao", "").strip(),
        item_id
    )
    exec_sql(f"""
        UPDATE {table} SET nome=?, cpf_cnpj=?, telefone=?, email=?, endereco=?, observacao=?
        WHERE id=?
    """, data)

@app.route("/clientes", methods=["GET", "POST"])
@login_required
def clientes():
    if request.method == "POST":
        salvar_pessoa("clientes")
        backup_db("cliente")
        flash("Cliente salvo.", "ok")
        return redirect(url_for("clientes"))
    q = request.args.get("q", "").strip()
    if q:
        rows = fetch_all("SELECT * FROM clientes WHERE nome LIKE ? OR cpf_cnpj LIKE ? OR telefone LIKE ? ORDER BY nome", (f"%{q}%", f"%{q}%", f"%{q}%"))
    else:
        rows = fetch_all("SELECT * FROM clientes ORDER BY id DESC LIMIT 80")
    return render_template("pessoas.html", titulo="Clientes", rows=rows, tipo="clientes", q=q)

@app.route("/clientes/<int:item_id>/editar", methods=["GET", "POST"])
@login_required
def editar_cliente(item_id):
    row = fetch_one("SELECT * FROM clientes WHERE id=?", (item_id,))
    if not row:
        flash("Cliente não encontrado.", "erro")
        return redirect(url_for("clientes"))
    if request.method == "POST":
        atualizar_pessoa("clientes", item_id)
        backup_db("cliente-editado")
        flash("Cliente atualizado.", "ok")
        return redirect(url_for("clientes"))
    return render_template("pessoa_form.html", titulo="Editar cliente", row=row, tipo="clientes")

@app.route("/fornecedores", methods=["GET", "POST"])
@login_required
def fornecedores():
    if request.method == "POST":
        salvar_pessoa("fornecedores")
        backup_db("fornecedor")
        flash("Fornecedor salvo.", "ok")
        return redirect(url_for("fornecedores"))
    q = request.args.get("q", "").strip()
    if q:
        rows = fetch_all("SELECT * FROM fornecedores WHERE nome LIKE ? OR cpf_cnpj LIKE ? OR telefone LIKE ? ORDER BY nome", (f"%{q}%", f"%{q}%", f"%{q}%"))
    else:
        rows = fetch_all("SELECT * FROM fornecedores ORDER BY id DESC LIMIT 80")
    return render_template("pessoas.html", titulo="Fornecedores", rows=rows, tipo="fornecedores", q=q)

@app.route("/fornecedores/<int:item_id>/editar", methods=["GET", "POST"])
@login_required
def editar_fornecedor(item_id):
    row = fetch_one("SELECT * FROM fornecedores WHERE id=?", (item_id,))
    if not row:
        flash("Fornecedor não encontrado.", "erro")
        return redirect(url_for("fornecedores"))
    if request.method == "POST":
        atualizar_pessoa("fornecedores", item_id)
        backup_db("fornecedor-editado")
        flash("Fornecedor atualizado.", "ok")
        return redirect(url_for("fornecedores"))
    return render_template("pessoa_form.html", titulo="Editar fornecedor", row=row, tipo="fornecedores")


# ---------------- Produtos / Estoque ----------------

@app.route("/produtos", methods=["GET", "POST"])
@login_required
def produtos():
    if request.method == "POST":
        with get_db() as db:
            db.execute("""
                INSERT INTO produtos
                (codigo,nome,categoria,unidade,preco_custo,preco_venda,estoque,estoque_minimo,ncm,cfop,cst_csosn,aliquota,ean,cest)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                request.form.get("codigo","").strip(),
                request.form.get("nome","").strip(),
                request.form.get("categoria","").strip(),
                normalizar_unidade(request.form.get("unidade", "UN")),
                money_to_float(request.form.get("preco_custo")),
                money_to_float(request.form.get("preco_venda")),
                num_to_float(request.form.get("estoque")),
                num_to_float(request.form.get("estoque_minimo")),
                request.form.get("ncm","").strip(),
                request.form.get("cfop","").strip(),
                request.form.get("cst_csosn","").strip(),
                money_to_float(request.form.get("aliquota")),
                request.form.get("ean","").strip(),
                request.form.get("cest","").strip(),
            ))
            db.commit()
        backup_db("produto")
        flash("Produto salvo.", "ok")
        return redirect(url_for("produtos"))
    q = request.args.get("q","").strip()
    if q:
        rows = fetch_all("SELECT * FROM produtos WHERE ativo=1 AND (nome LIKE ? OR codigo LIKE ? OR categoria LIKE ?) ORDER BY nome", (f"%{q}%", f"%{q}%", f"%{q}%"))
    else:
        rows = fetch_all("SELECT * FROM produtos WHERE ativo=1 ORDER BY id DESC LIMIT 100")
    return render_template("produtos.html", rows=rows, q=q)

@app.route("/produtos/<int:item_id>/editar", methods=["GET", "POST"])
@login_required
def editar_produto(item_id):
    row = fetch_one("SELECT * FROM produtos WHERE id=?", (item_id,))
    if not row:
        flash("Produto não encontrado.", "erro")
        return redirect(url_for("produtos"))
    if request.method == "POST":
        exec_sql("""
            UPDATE produtos SET codigo=?, nome=?, categoria=?, unidade=?, preco_custo=?, preco_venda=?,
            estoque=?, estoque_minimo=?, ncm=?, cfop=?, cst_csosn=?, aliquota=?, ean=?, cest=? WHERE id=?
        """, (
            request.form.get("codigo","").strip(),
            request.form.get("nome","").strip(),
            request.form.get("categoria","").strip(),
            normalizar_unidade(request.form.get("unidade", "UN")),
            money_to_float(request.form.get("preco_custo")),
            money_to_float(request.form.get("preco_venda")),
            num_to_float(request.form.get("estoque")),
            num_to_float(request.form.get("estoque_minimo")),
            request.form.get("ncm","").strip(),
            request.form.get("cfop","").strip(),
            request.form.get("cst_csosn","").strip(),
            money_to_float(request.form.get("aliquota")),
            request.form.get("ean","").strip(),
            request.form.get("cest","").strip(),
            item_id
        ))
        backup_db("produto-editado")
        flash("Produto atualizado.", "ok")
        return redirect(url_for("produtos"))
    return render_template("produto_form.html", row=row)

@app.route("/produtos/<int:item_id>/excluir", methods=["POST"])
@login_required
def excluir_produto(item_id):
    exec_sql("UPDATE produtos SET ativo=0 WHERE id=?", (item_id,))
    backup_db("produto-excluido")
    flash("Produto removido.", "ok")
    return redirect(url_for("produtos"))

@app.route("/estoque", methods=["GET", "POST"])
@login_required
def estoque():
    produtos_list = fetch_all("SELECT * FROM produtos WHERE ativo=1 ORDER BY nome")
    if request.method == "POST":
        produto_id = int(request.form.get("produto_id") or 0)
        tipo = request.form.get("tipo") or "Entrada"
        qtd = num_to_float(request.form.get("quantidade"))
        custo = money_to_float(request.form.get("custo_unit"))
        valor_total = qtd * custo
        prod = fetch_one("SELECT * FROM produtos WHERE id=?", (produto_id,))
        if not prod or qtd <= 0:
            flash("Informe produto e quantidade válida.", "erro")
            return redirect(url_for("estoque"))
        sinal = 1 if tipo in ["Entrada", "Ajuste +"] else -1
        with get_db() as db:
            db.execute("UPDATE produtos SET estoque=estoque+? WHERE id=?", (sinal*qtd, produto_id))
            if tipo in ["Entrada", "Ajuste +"] and custo > 0:
                db.execute("UPDATE produtos SET preco_custo=? WHERE id=?", (custo, produto_id))
            db.execute("""
                INSERT INTO estoque_mov (data, produto_id, tipo, quantidade, custo_unit, valor_total, origem, observacao)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (request.form.get("data") or today_str(), produto_id, tipo, qtd, custo, valor_total, request.form.get("origem",""), request.form.get("observacao","")))
            db.commit()
        backup_db("estoque")
        flash("Movimentação salva.", "ok")
        return redirect(url_for("estoque"))
    movs = fetch_all("""
        SELECT m.*, p.nome produto_nome, p.codigo codigo FROM estoque_mov m
        LEFT JOIN produtos p ON p.id=m.produto_id
        ORDER BY m.id DESC LIMIT 100
    """)
    return render_template("estoque.html", produtos=produtos_list, movs=movs, hoje=today_str())


# ---------------- Vendas ----------------

@app.route("/vendas")
@login_required
def vendas():
    q = request.args.get("q","").strip()
    if q:
        rows = fetch_all("SELECT * FROM vendas WHERE cliente_nome LIKE ? OR id LIKE ? ORDER BY id DESC", (f"%{q}%", f"%{q}%"))
    else:
        rows = fetch_all("SELECT * FROM vendas ORDER BY id DESC LIMIT 100")
    return render_template("vendas.html", rows=rows, q=q)

@app.route("/vendas/nova", methods=["GET","POST"])
@login_required
def nova_venda():
    clientes_list = fetch_all("SELECT * FROM clientes ORDER BY nome")
    produtos_list = fetch_all("SELECT * FROM produtos WHERE ativo=1 ORDER BY nome")
    if request.method == "POST":
        cliente_id = int(request.form.get("cliente_id") or 0)
        cliente_nome = request.form.get("cliente_nome","").strip()
        if cliente_id:
            c = fetch_one("SELECT * FROM clientes WHERE id=?", (cliente_id,))
            cliente_nome = c["nome"] if c else cliente_nome
        elif cliente_nome:
            with get_db() as db:
                cur = db.execute("INSERT INTO clientes (nome) VALUES (?)", (cliente_nome,))
                cliente_id = cur.lastrowid
                db.commit()
        forma = request.form.get("forma_pagamento","Dinheiro")
        status = request.form.get("status","Pago")
        desconto = money_to_float(request.form.get("desconto"))
        prod_ids = request.form.getlist("produto_id[]")
        qtds = request.form.getlist("quantidade[]")
        precos = request.form.getlist("preco_unit[]")
        itens = []
        subtotal = lucro_total = 0.0
        for pid, q, pu in zip(prod_ids, qtds, precos):
            if not pid:
                continue
            qtd = num_to_float(q)
            preco = money_to_float(pu)
            if qtd <= 0:
                continue
            prod = fetch_one("SELECT * FROM produtos WHERE id=?", (int(pid),))
            if not prod:
                continue
            if qtd > float(prod["estoque"] or 0):
                flash(f"Estoque insuficiente para {prod['nome']}. Disponível: {fmt_num(prod['estoque'])}", "erro")
                return redirect(url_for("nova_venda"))
            custo = float(prod["preco_custo"] or 0)
            total = qtd * preco
            lucro = (preco - custo) * qtd
            subtotal += total
            lucro_total += lucro
            itens.append((int(pid), prod["nome"], qtd, preco, custo, total, lucro))
        if not itens:
            flash("Adicione pelo menos um produto.", "erro")
            return redirect(url_for("nova_venda"))
        total = max(0, subtotal - desconto)
        with get_db() as db:
            cur = db.execute("""
                INSERT INTO vendas (data, cliente_id, cliente_nome, forma_pagamento, status, subtotal, desconto, total, lucro, observacao, nf_tipo)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (request.form.get("data") or today_str(), cliente_id, cliente_nome, forma, status, subtotal, desconto, total, lucro_total, request.form.get("observacao",""), request.form.get("nf_tipo","NFC-e")))
            venda_id = cur.lastrowid
            for pid, nome, qtd, preco, custo, total_item, lucro in itens:
                db.execute("""
                    INSERT INTO venda_itens (venda_id, produto_id, produto_nome, quantidade, preco_unit, custo_unit, total, lucro)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (venda_id, pid, nome, qtd, preco, custo, total_item, lucro))
                db.execute("UPDATE produtos SET estoque=estoque-? WHERE id=?", (qtd, pid))
                db.execute("""
                    INSERT INTO estoque_mov (data, produto_id, tipo, quantidade, custo_unit, valor_total, origem, observacao)
                    VALUES (?, ?, 'Saída', ?, ?, ?, 'Venda', ?)
                """, (request.form.get("data") or today_str(), pid, qtd, custo, qtd*custo, f"Venda #{venda_id}"))
            db.execute("""
                INSERT INTO financeiro (data, tipo, descricao, valor, forma, status, pessoa, referencia_tipo, referencia_id)
                VALUES (?, 'Entrada', ?, ?, ?, ?, ?, 'Venda', ?)
            """, (request.form.get("data") or today_str(), f"Venda #{venda_id}", total, forma, "Pago" if status=="Pago" else "Pendente", cliente_nome, venda_id))
            db.commit()
        backup_db("venda")
        flash("Venda salva e estoque baixado automaticamente.", "ok")
        return redirect(url_for("recibo_venda", venda_id=venda_id))
    return render_template("venda_form.html", clientes=clientes_list, produtos=produtos_list, hoje=today_str())

@app.route("/vendas/<int:venda_id>/recibo")
@login_required
def recibo_venda(venda_id):
    venda = fetch_one("SELECT * FROM vendas WHERE id=?", (venda_id,))
    if not venda:
        flash("Venda não encontrada.", "erro")
        return redirect(url_for("vendas"))
    itens = fetch_all("SELECT * FROM venda_itens WHERE venda_id=?", (venda_id,))
    return render_template("recibo_venda.html", venda=venda, itens=itens)

def _thermal_wrap(text, font_name, font_size, max_width):
    text = str(text or "").strip()
    if not text:
        return [""]
    words = text.split()
    lines = []
    current = ""
    for word in words:
        candidate = word if not current else current + " " + word
        if stringWidth(candidate, font_name, font_size) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
        # Quebra palavras/códigos maiores que a largura do papel.
        chunk = ""
        for ch in word:
            test = chunk + ch
            if stringWidth(test, font_name, font_size) <= max_width:
                chunk = test
            else:
                if chunk:
                    lines.append(chunk)
                chunk = ch
        current = chunk
    if current:
        lines.append(current)
    return lines or [""]

def _thermal_money(value):
    try:
        value = float(value or 0)
    except Exception:
        value = 0.0
    return "R$ " + f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def _thermal_num(value):
    try:
        value = float(value or 0)
    except Exception:
        value = 0.0
    if abs(value - round(value)) < 0.000001:
        return str(int(round(value)))
    return (f"{value:.3f}".rstrip("0").rstrip(".")).replace(".", ",")

def _thermal_date(value):
    raw = str(value or "")
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        return raw


def _pos5890u_ascii(value):
    """Texto seguro para a POS-5890U em modo ESC/POS raw.

    A POS-5890U e clones costumam variar no mapa de code pages. Para evitar
    caracteres quebrados no caixa, normalizamos acentos para ASCII. Isso nao
    altera valores/codigos e deixa a impressao previsivel em qualquer revisao.
    """
    import unicodedata
    text = str(value or "")
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def _pos5890u_wrap(text, width=32):
    text = _pos5890u_ascii(text).strip()
    if not text:
        return [""]
    words = text.split()
    lines, current = [], ""
    for word in words:
        if len(word) > width:
            if current:
                lines.append(current)
                current = ""
            while len(word) > width:
                lines.append(word[:width])
                word = word[width:]
            current = word
            continue
        candidate = word if not current else current + " " + word
        if len(candidate) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def _pos5890u_lr(left, right, width=32):
    left = _pos5890u_ascii(left)
    right = _pos5890u_ascii(right)
    if len(right) >= width:
        return right[-width:]
    max_left = max(0, width - len(right) - 1)
    left = left[:max_left]
    return left + (" " * max(1, width - len(left) - len(right))) + right


def _escpos_raster_logo(image_path=None, max_width=300, threshold=172):
    """Converte a logo da CENTRALVET para raster ESC/POS monocromatico.

    A POS-5890U imprime 384 pontos por linha. Usamos no maximo 300 pontos para
    manter margem lateral e boa legibilidade do simbolo/texto.
    """
    if image_path is None:
        image_path = os.path.join(os.path.dirname(__file__), "static", "centralvet_logo.png")
    try:
        im = Image.open(image_path).convert("RGBA")
        alpha = im.getchannel("A")
        bbox = alpha.getbbox()
        if bbox:
            im = im.crop(bbox)
        bg = Image.new("RGBA", im.size, "white")
        bg.alpha_composite(im)
        gray = ImageOps.grayscale(bg.convert("RGB"))
        gray = ImageOps.autocontrast(gray)
        if gray.width > max_width:
            ratio = max_width / float(gray.width)
            new_h = max(1, int(round(gray.height * ratio)))
            gray = gray.resize((max_width, new_h), Image.Resampling.LANCZOS)
        # Limiar fixo deixa a marca nítida em cabeça térmica de 203 dpi.
        bw = gray.point(lambda p: 0 if p < threshold else 255, mode="1")
        width = bw.width
        height = bw.height
        width_bytes = (width + 7) // 8
        data = bytearray()
        pix = bw.load()
        for y in range(height):
            for xb in range(width_bytes):
                byte = 0
                for bit in range(8):
                    x = xb * 8 + bit
                    if x < width and pix[x, y] == 0:
                        byte |= (1 << (7 - bit))
                data.append(byte)
        GS = b"\x1d"
        header = GS + b"v0" + b"\x00" + bytes([
            width_bytes & 0xff, (width_bytes >> 8) & 0xff,
            height & 0xff, (height >> 8) & 0xff,
        ])
        return header + bytes(data)
    except Exception:
        # A venda nunca deve falhar por causa de uma imagem de cabecalho.
        return b""


def _escpos_qr(data, module=4):
    """Comandos ESC/POS QR Code Model 2, compatíveis com a maioria dos POS-58."""
    raw = str(data or '').encode('utf-8')
    if not raw:
        return b''
    GS = b'\x1d'
    out = bytearray()
    out += GS + b'(k' + bytes([4,0,49,65,50,0])       # model 2
    out += GS + b'(k' + bytes([3,0,49,67,max(1,min(8,int(module)))])
    out += GS + b'(k' + bytes([3,0,49,69,49])         # ECC M
    length = len(raw) + 3
    out += GS + b'(k' + bytes([length & 0xff, (length >> 8) & 0xff, 49,80,48]) + raw
    out += GS + b'(k' + bytes([3,0,49,81,48])
    return bytes(out)


def _nfce_xml_value(root, tag, default=''):
    if root is None:
        return default
    for el in root.iter():
        if el.tag.split('}')[-1] == tag:
            return (el.text or '').strip()
    return default


def build_nfce_pos5890u_escpos(xml_path):
    root = ET.parse(xml_path).getroot()
    def first(parent, tag):
        if parent is None: return None
        for el in parent.iter():
            if el.tag.split('}')[-1] == tag: return el
        return None
    inf = first(root, 'infNFe')
    emit = first(inf, 'emit')
    ide = first(inf, 'ide')
    total = first(inf, 'ICMSTot')
    dest = first(inf, 'dest')
    prot = first(root, 'infProt')
    qr = _nfce_xml_value(root, 'qrCode', '')
    key = ((inf.attrib.get('Id') or '') if inf is not None else '').replace('NFe','')
    nNF = _nfce_xml_value(ide, 'nNF', '')
    serie = _nfce_xml_value(ide, 'serie', '')
    dhEmi = _nfce_xml_value(ide, 'dhEmi', '')
    vNF = _nfce_xml_value(total, 'vNF', '0')
    protocolo = _nfce_xml_value(prot, 'nProt', '')
    xNome = _nfce_xml_value(emit, 'xNome', 'CENTRALVET AGROPECUARIA')
    cnpj = _nfce_xml_value(emit, 'CNPJ', '')
    ie = _nfce_xml_value(emit, 'IE', '')
    ender = first(emit, 'enderEmit')
    endereco = ' '.join(x for x in [
        _nfce_xml_value(ender,'xLgr',''), _nfce_xml_value(ender,'nro',''),
        _nfce_xml_value(ender,'xBairro',''), _nfce_xml_value(ender,'xMun',''), _nfce_xml_value(ender,'UF','')
    ] if x)
    consumer_doc = _nfce_xml_value(dest, 'CPF', '') or _nfce_xml_value(dest, 'CNPJ', '')
    consumer_name = _nfce_xml_value(dest, 'xNome', '')

    dets=[]
    for el in inf.iter() if inf is not None else []:
        if el.tag.split('}')[-1] != 'det': continue
        prod=first(el,'prod')
        dets.append({
            'nome':_nfce_xml_value(prod,'xProd','PRODUTO'), 'qtd':_nfce_xml_value(prod,'qCom','0'),
            'unit':_nfce_xml_value(prod,'vUnCom','0'), 'total':_nfce_xml_value(prod,'vProd','0'),
            'desc':_nfce_xml_value(prod,'vDesc','0')
        })
    pays=[]
    for el in inf.iter() if inf is not None else []:
        if el.tag.split('}')[-1]=='detPag':
            pays.append((_nfce_xml_value(el,'tPag',''),_nfce_xml_value(el,'vPag','0')))
    pay_names={'01':'Dinheiro','02':'Cheque','03':'Cartao credito','04':'Cartao debito','05':'Crediario','15':'Boleto','17':'PIX','99':'Outros'}

    ESC=b'\x1b'; GS=b'\x1d'; out=bytearray()
    out += ESC+b'@' + ESC+b'a'+b'\x01'
    logo_bytes = _escpos_raster_logo()
    if logo_bytes:
        out += logo_bytes + b'\n'
    out += ESC+b'E'+b'\x01'
    for line in _pos5890u_wrap(xNome): out += line.encode('ascii')+b'\n'
    out += ESC+b'E'+b'\x00'
    for line in _pos5890u_wrap(f'CNPJ: {cnpj} IE: {ie}'): out += line.encode('ascii')+b'\n'
    for line in _pos5890u_wrap(endereco): out += line.encode('ascii')+b'\n'
    out += b'-'*32+b'\n'
    out += ESC+b'E'+b'\x01' + b'DANFE NFC-e\n' + ESC+b'E'+b'\x00'
    out += b'Documento Auxiliar da NFC-e\n'
    out += b'NAO PERMITE APROVEITAMENTO DE CREDITO\n'
    out += ESC+b'a'+b'\x00' + b'-'*32+b'\n'
    for d in dets:
        for line in _pos5890u_wrap(d['nome']): out += line.encode('ascii')+b'\n'
        try:
            qtd=_thermal_num(float(d['qtd'])); unit=_thermal_money(float(d['unit'])); tot=_thermal_money(float(d['total'])-float(d['desc'] or 0))
        except Exception:
            qtd=d['qtd']; unit=d['unit']; tot=d['total']
        out += _pos5890u_lr(f'{qtd} x {unit}', tot).encode('ascii')+b'\n'
    out += b'-'*32+b'\n' + ESC+b'E'+b'\x01'
    out += _pos5890u_lr('TOTAL', _thermal_money(float(vNF or 0))).encode('ascii')+b'\n' + ESC+b'E'+b'\x00'
    for code,val in pays:
        try: vtxt=_thermal_money(float(val or 0))
        except Exception: vtxt=val
        out += _pos5890u_lr(pay_names.get(code,code or 'Pagamento'),vtxt).encode('ascii')+b'\n'
    out += b'-'*32+b'\n'
    if consumer_doc or consumer_name:
        for line in _pos5890u_wrap(f'Consumidor: {consumer_name} {consumer_doc}'.strip()): out += line.encode('ascii')+b'\n'
    else:
        out += b'CONSUMIDOR NAO IDENTIFICADO\n'
    out += ESC+b'a'+b'\x01'
    for line in _pos5890u_wrap(f'NFC-e {nNF} Serie {serie}'): out += line.encode('ascii')+b'\n'
    for line in _pos5890u_wrap(f'Emissao: {dhEmi[:19].replace("T"," ")}'): out += line.encode('ascii')+b'\n'
    out += b'CHAVE DE ACESSO\n'
    key_fmt=' '.join(key[i:i+4] for i in range(0,len(key),4))
    for line in _pos5890u_wrap(key_fmt): out += line.encode('ascii')+b'\n'
    if protocolo:
        for line in _pos5890u_wrap(f'Protocolo: {protocolo}'): out += line.encode('ascii')+b'\n'
    if qr:
        out += b'\n' + _escpos_qr(qr,3) + b'\n'
        out += b'Consulte pelo QR Code\n'
    out += ESC+b'a'+b'\x00' + b'\n\n\n'
    return bytes(out)


def build_pos5890u_escpos(venda, itens):
    """Gera bytes ESC/POS para POS-5890U, 58 mm / 384 dots.

    Usa 32 colunas (fonte A) e envia somente os bytes do recibo. Nao existe
    tamanho de pagina, portanto a impressora para logo apos o ultimo line feed,
    evitando o rolo em branco causado pelo driver do Edge/Windows.
    """
    ESC = b"\x1b"
    GS = b"\x1d"
    out = bytearray()
    out += ESC + b"@"                 # inicializa
    out += ESC + b"a" + b"\x01"       # centralizado
    logo_bytes = _escpos_raster_logo()
    if logo_bytes:
        out += logo_bytes + b"\n"
    out += ESC + b"E" + b"\x01"       # negrito
    out += GS + b"!" + b"\x11"        # 2x largura/altura
    out += b"CENTRALVET\n"
    out += GS + b"!" + b"\x00"        # tamanho normal
    out += b"AGROPECUARIA\n"
    out += ESC + b"E" + b"\x00"
    out += _pos5890u_ascii(f"Venda #{venda['id']} - {_thermal_date(venda['data'])}").encode("ascii") + b"\n"
    out += ESC + b"a" + b"\x00"       # esquerda
    out += (b"-" * 32) + b"\n"

    cliente = venda['cliente_nome'] or 'Consumidor'
    for line in _pos5890u_wrap(f"Cliente: {cliente}"):
        out += line.encode("ascii") + b"\n"
    if venda['nf_status'] and venda['nf_status'] != 'Não emitida':
        fiscal = f"Fiscal: {venda['nf_status']} {venda['nf_numero'] or ''}".strip()
        for line in _pos5890u_wrap(fiscal):
            out += line.encode("ascii") + b"\n"
    out += (b"-" * 32) + b"\n"

    for item in itens:
        for line in _pos5890u_wrap(item['produto_nome']):
            out += line.encode("ascii") + b"\n"
        qtd = _thermal_num(item['quantidade'])
        unit = _thermal_money(item['preco_unit'])
        total = _thermal_money(item['total'])
        out += _pos5890u_lr(f"{qtd} x {unit}", total).encode("ascii") + b"\n"

    out += (b"-" * 32) + b"\n"
    out += ESC + b"E" + b"\x01"
    out += GS + b"!" + b"\x01"        # altura dupla, largura normal
    total_txt = _pos5890u_ascii(f"TOTAL {_thermal_money(venda['total'])}")
    out += total_txt.rjust(32).encode("ascii") + b"\n"
    out += GS + b"!" + b"\x00"
    out += ESC + b"E" + b"\x00"
    for line in _pos5890u_wrap(f"Pagamento: {venda['forma_pagamento'] or '-'} - {venda['status'] or '-'}"):
        out += line.encode("ascii") + b"\n"
    if venda['observacao']:
        for line in _pos5890u_wrap(f"Obs.: {venda['observacao']}"):
            out += line.encode("ascii") + b"\n"
    out += (b"-" * 32) + b"\n"
    out += ESC + b"a" + b"\x01"
    out += ESC + b"E" + b"\x01"
    out += b"Obrigado pela preferencia!\n"
    out += ESC + b"E" + b"\x00"
    if not venda['nf_status'] or venda['nf_status'] != 'Autorizada SEFAZ':
        for line in _pos5890u_wrap("Recibo sem valor fiscal quando nao houver documento fiscal autorizado."):
            out += line.encode("ascii") + b"\n"
    out += ESC + b"a" + b"\x00"
    out += b"\n\n\n"                  # avanca so o necessario para destacar
    return bytes(out)


@app.route("/api/vendas/<int:venda_id>/pos5890u")
@login_required
def api_venda_pos5890u(venda_id):
    venda = fetch_one("SELECT * FROM vendas WHERE id=?", (venda_id,))
    if not venda:
        return jsonify({"ok": False, "erro": "Venda nao encontrada."}), 404
    itens = fetch_all("SELECT * FROM venda_itens WHERE venda_id=? ORDER BY id", (venda_id,))
    xml_path = fiscal_safe_file(venda['nf_xml_path']) if venda['nf_status'] == 'Autorizada SEFAZ' else None
    payload = build_nfce_pos5890u_escpos(xml_path) if xml_path else build_pos5890u_escpos(venda, itens)
    return jsonify({
        "ok": True,
        "modelo": "POS-5890U",
        "printer_hint": "POS-58",
        "paper_mm": 58,
        "dots": 384,
        "columns": 32,
        "escpos_base64": base64.b64encode(payload).decode("ascii"),
    })


def build_pos58_pdf(venda, itens):
    page_width = 58 * mm
    margin_x = 4 * mm
    usable = page_width - (2 * margin_x)
    normal_font = "Helvetica"
    bold_font = "Helvetica-Bold"

    # Primeiro montamos uma lista de comandos para saber a altura exata do cupom.
    rows = []
    logo_path = os.path.join(os.path.dirname(__file__), "static", "centralvet_logo.png")
    logo_draw_w = 46 * mm
    logo_draw_h = logo_draw_w * (440 / 1222)
    def add_text(text, size=8.2, bold=False, align="left", gap_after=1.4):
        font = bold_font if bold else normal_font
        for line in _thermal_wrap(text, font, size, usable):
            rows.append(("text", line, size, font, align, size + 2.0))
        if gap_after:
            rows.append(("gap", gap_after))

    def add_rule(gap=3.0):
        rows.append(("rule", gap))

    add_text("CENTRALVET AGROPECUARIA", size=11.5, bold=True, align="center", gap_after=1.5)
    add_text(f"Venda #{venda['id']} - {_thermal_date(venda['data'])}", size=7.8, align="center", gap_after=2.0)
    add_rule()
    add_text(f"Cliente: {venda['cliente_nome'] or 'Consumidor'}", size=7.8, bold=True, gap_after=1.4)
    if venda['nf_status'] and venda['nf_status'] != 'Não emitida':
        fiscal = f"Fiscal: {venda['nf_status']} {venda['nf_numero'] or ''}".strip()
        add_text(fiscal, size=7.2, gap_after=1.4)
    add_rule()

    for item in itens:
        add_text(item['produto_nome'], size=7.8, bold=True, gap_after=0.5)
        detail = f"{_thermal_num(item['quantidade'])} x {_thermal_money(item['preco_unit'])}"
        total = _thermal_money(item['total'])
        # Duas colunas simples, sem tabela HTML/driver.
        rows.append(("columns", detail, total, 7.2, normal_font, 9.0))
        rows.append(("gap", 1.3))

    add_rule()
    add_text(f"TOTAL: {_thermal_money(venda['total'])}", size=11.0, bold=True, align="right", gap_after=2.0)
    add_text(f"Pagamento: {venda['forma_pagamento'] or '-'} - {venda['status'] or '-'}", size=7.6, gap_after=1.5)
    if venda['observacao']:
        add_text(f"Obs.: {venda['observacao']}", size=7.2, gap_after=1.5)
    add_rule()
    add_text("Obrigado pela preferencia.", size=8.0, bold=True, align="center", gap_after=1.5)
    if not venda['nf_status'] or venda['nf_status'] != 'Autorizada SEFAZ':
        add_text("Recibo sem valor fiscal quando nao houver documento fiscal autorizado.", size=6.3, align="center", gap_after=0)

    top_bottom = 5 * mm
    content_height = logo_draw_h + (3 * mm)
    for row in rows:
        if row[0] == "text":
            content_height += row[5]
        elif row[0] == "columns":
            content_height += row[5]
        elif row[0] == "rule":
            content_height += 6.0 + row[1]
        elif row[0] == "gap":
            content_height += row[1]
    page_height = max(45 * mm, content_height + top_bottom)
    # Segurança para vendas muito grandes: ainda é uma única página de bobina.
    page_height = min(page_height, 1500 * mm)

    out = BytesIO()
    pdf = canvas.Canvas(out, pagesize=(page_width, page_height), pageCompression=1)
    y = page_height - 3 * mm
    try:
        pdf.drawImage(ImageReader(logo_path), (page_width - logo_draw_w) / 2, y - logo_draw_h,
                      width=logo_draw_w, height=logo_draw_h, preserveAspectRatio=True, mask='auto')
        y -= logo_draw_h + (3 * mm)
    except Exception:
        pass

    for row in rows:
        kind = row[0]
        if kind == "gap":
            y -= row[1]
            continue
        if kind == "rule":
            y -= 2.0
            pdf.setLineWidth(0.35)
            pdf.line(margin_x, y, page_width - margin_x, y)
            y -= (4.0 + row[1])
            continue
        if kind == "columns":
            _, left, right, size, font, line_h = row
            pdf.setFont(font, size)
            pdf.drawString(margin_x, y, left)
            pdf.drawRightString(page_width - margin_x, y, right)
            y -= line_h
            continue
        _, text, size, font, align, line_h = row
        pdf.setFont(font, size)
        if align == "center":
            pdf.drawCentredString(page_width / 2, y, text)
        elif align == "right":
            pdf.drawRightString(page_width - margin_x, y, text)
        else:
            pdf.drawString(margin_x, y, text)
        y -= line_h

    pdf.showPage()
    pdf.save()
    out.seek(0)
    return out

@app.route("/vendas/<int:venda_id>/cupom.pdf")
@login_required
def cupom_termico_pdf(venda_id):
    venda = fetch_one("SELECT * FROM vendas WHERE id=?", (venda_id,))
    if not venda:
        flash("Venda não encontrada.", "erro")
        return redirect(url_for("vendas"))
    itens = fetch_all("SELECT * FROM venda_itens WHERE venda_id=? ORDER BY id", (venda_id,))
    pdf = build_pos58_pdf(venda, itens)
    response = send_file(
        pdf,
        mimetype="application/pdf",
        as_attachment=False,
        download_name=f"cupom-venda-{venda_id}-pos58.pdf",
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response

@app.route("/vendas/<int:venda_id>/cupom")
@login_required
def cupom_termico(venda_id):
    # A impressao HTML variava conforme o tamanho de papel informado pelo driver POS-58.
    # Agora o cupom abre como PDF com largura 58 mm e altura calculada pelo conteudo.
    return redirect(url_for("cupom_termico_pdf", venda_id=venda_id))

@app.route("/vendas/<int:venda_id>/fiscal", methods=["POST"])
@login_required
def emitir_fiscal(venda_id):
    venda = fetch_one("SELECT * FROM vendas WHERE id=?", (venda_id,))
    if not venda:
        flash("Venda não encontrada.", "erro")
        return redirect(url_for("vendas"))
    nf_tipo = request.form.get("nf_tipo") or "NFC-e"
    if nf_tipo != "NFC-e":
        flash("Para venda de balcão, use NFC-e. A NF-e modelo 55 continua no fluxo de devolução.", "erro")
        return redirect(url_for("recibo_venda", venda_id=venda_id))
    result = emitir_nfce_sefaz_internal(venda_id)
    if result.get('authorized'):
        flash(f"NFC-e autorizada ✅ Nº {fetch_one('SELECT nf_numero FROM vendas WHERE id=?',(venda_id,))['nf_numero']} • cStat {result.get('cStat') or '100'}", 'ok')
    else:
        prefix = 'SEFAZ rejeitou a NFC-e' if result.get('cStat') else 'Falha antes do envio à SEFAZ'
        flash(f"{prefix}: cStat {result.get('cStat') or '-'} - {result.get('xMotivo') or result.get('error') or 'erro desconhecido'}", 'erro')
    return redirect(url_for("recibo_venda", venda_id=venda_id))


@app.route("/vendas/<int:venda_id>/nfce/danfe")
@login_required
def venda_nfce_danfe(venda_id):
    venda = fetch_one("SELECT * FROM vendas WHERE id=?", (venda_id,))
    if not venda or venda['nf_status'] != 'Autorizada SEFAZ':
        flash('Esta venda ainda não possui NFC-e autorizada.', 'erro')
        return redirect(url_for('recibo_venda', venda_id=venda_id))
    path = fiscal_safe_file(venda['nf_danfe_path'])
    if not path:
        flash('DANFE NFC-e não encontrado no armazenamento.', 'erro')
        return redirect(url_for('recibo_venda', venda_id=venda_id))
    return send_file(path, mimetype='application/pdf', as_attachment=False,
                     download_name=f"DANFCE-{venda['nf_numero'] or venda_id}.pdf")


@app.route("/vendas/<int:venda_id>/nfce/xml")
@login_required
def venda_nfce_xml(venda_id):
    venda = fetch_one("SELECT * FROM vendas WHERE id=?", (venda_id,))
    if not venda or venda['nf_status'] != 'Autorizada SEFAZ':
        flash('Esta venda ainda não possui NFC-e autorizada.', 'erro')
        return redirect(url_for('recibo_venda', venda_id=venda_id))
    path = fiscal_safe_file(venda['nf_xml_path'])
    if not path:
        flash('XML autorizado não encontrado no armazenamento.', 'erro')
        return redirect(url_for('recibo_venda', venda_id=venda_id))
    return send_file(path, mimetype='application/xml', as_attachment=True,
                     download_name=f"NFCe-{venda['nf_chave'] or venda_id}.xml")

@app.route("/api/produtos")
@login_required
def api_produtos():
    rows = fetch_all("SELECT id,codigo,nome,estoque,preco_venda,preco_custo,unidade FROM produtos WHERE ativo=1 ORDER BY nome")
    return jsonify([dict(r) for r in rows])


# ---------------- Financeiro ----------------

@app.route("/financeiro", methods=["GET","POST"])
@login_required
def financeiro():
    if request.method == "POST":
        exec_sql("""
            INSERT INTO financeiro (data,tipo,descricao,valor,forma,status,vencimento,pessoa,observacao)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            request.form.get("data") or today_str(),
            request.form.get("tipo","Entrada"),
            request.form.get("descricao","").strip(),
            money_to_float(request.form.get("valor")),
            request.form.get("forma","").strip(),
            request.form.get("status","Pago"),
            request.form.get("vencimento",""),
            request.form.get("pessoa","").strip(),
            request.form.get("observacao","").strip()
        ))
        backup_db("financeiro")
        flash("Lançamento financeiro salvo.", "ok")
        return redirect(url_for("financeiro"))
    q = request.args.get("q","").strip()
    if q:
        rows = fetch_all("SELECT * FROM financeiro WHERE descricao LIKE ? OR pessoa LIKE ? ORDER BY id DESC", (f"%{q}%", f"%{q}%"))
    else:
        rows = fetch_all("SELECT * FROM financeiro ORDER BY id DESC LIMIT 150")
    cards = {
        "entradas": fetch_one("SELECT COALESCE(SUM(valor),0) v FROM financeiro WHERE tipo='Entrada' AND status='Pago'")["v"],
        "saidas": fetch_one("SELECT COALESCE(SUM(valor),0) v FROM financeiro WHERE tipo='Saída' AND status='Pago'")["v"],
        "receber": fetch_one("SELECT COALESCE(SUM(valor),0) v FROM financeiro WHERE tipo='Entrada' AND status!='Pago'")["v"],
        "pagar": fetch_one("SELECT COALESCE(SUM(valor),0) v FROM financeiro WHERE tipo='Saída' AND status!='Pago'")["v"],
    }
    return render_template("financeiro.html", rows=rows, cards=cards, q=q, hoje=today_str())

@app.route("/financeiro/<int:item_id>/pagar", methods=["POST"])
@login_required
def financeiro_pagar(item_id):
    exec_sql("UPDATE financeiro SET status='Pago', data=? WHERE id=?", (today_str(), item_id))
    backup_db("financeiro-pago")
    flash("Marcado como pago.", "ok")
    return redirect(url_for("financeiro"))

@app.route("/financeiro/<int:item_id>/excluir", methods=["POST"])
@login_required
def financeiro_excluir(item_id):
    exec_sql("DELETE FROM financeiro WHERE id=?", (item_id,))
    backup_db("financeiro-excluido")
    flash("Lançamento excluído.", "ok")
    return redirect(url_for("financeiro"))


# ---------------- Fiscal / XML ----------------

@app.route("/fiscal", methods=["GET","POST"])
@login_required
def fiscal():
    if request.method == "POST":
        ambiente = request.form.get("ambiente", "Homologação")
        if ambiente == "Produção":
            temp_cfg = {k: request.form.get(k, "") for k in [
                "cnpj", "inscricao_estadual", "regime", "nf_serie", "nf_numero_inicial", "nfce_serie",
                "nfce_numero_inicial", "csc_token", "cfop_padrao", "cst_csosn_padrao"
            ]}
            class TempCfg(dict):
                def __getitem__(self, key):
                    return self.get(key, "")
            ready, missing = fiscal_prod_ready(TempCfg(temp_cfg), cert_runtime_status())
            if not ready:
                ambiente = "Homologação"
                flash("Mantive em Homologação: ainda falta completar dados fiscais antes de liberar Produção.", "erro")
        exec_sql("""
            UPDATE fiscal_config SET razao_social=?, nome_fantasia=?, cnpj=?, inscricao_estadual=?,
            endereco=?, municipio=?, uf=?, cep=?, telefone=?, email=?, regime=?, ambiente=?, certificado_nome=?,
            certificado_path=?, certificado_senha_env=?, nf_serie=?, nf_numero_inicial=?, nfce_serie=?, nfce_numero_inicial=?,
            csc_id=?, csc_token=?, cfop_padrao=?, cst_csosn_padrao=?, observacao=?
            WHERE id=1
        """, (
            request.form.get("razao_social",""), request.form.get("nome_fantasia",""), request.form.get("cnpj",""),
            request.form.get("inscricao_estadual",""), request.form.get("endereco",""), request.form.get("municipio",""),
            request.form.get("uf",""), request.form.get("cep",""), request.form.get("telefone",""), request.form.get("email",""),
            request.form.get("regime",""), ambiente, request.form.get("certificado_nome",""),
            request.form.get("certificado_path",""), request.form.get("certificado_senha_env","CERTIFICADO_SENHA"),
            request.form.get("nf_serie",""), request.form.get("nf_numero_inicial",""), request.form.get("nfce_serie",""),
            request.form.get("nfce_numero_inicial",""), request.form.get("csc_id",""), request.form.get("csc_token",""),
            request.form.get("cfop_padrao",""), request.form.get("cst_csosn_padrao",""), request.form.get("observacao","")
        ))
        backup_db("fiscal-config")
        flash("Configuração fiscal salva.", "ok")
        return redirect(url_for("fiscal"))
    cfg = fetch_one("SELECT * FROM fiscal_config WHERE id=1")
    notas = fetch_all("SELECT * FROM vendas WHERE nf_status!='Não emitida' ORDER BY id DESC LIMIT 80")
    xmls = fetch_all("SELECT * FROM xml_imports ORDER BY id DESC LIMIT 20")
    recover_devolucao_files()
    devolucoes = fetch_all("SELECT * FROM devolucoes ORDER BY id DESC LIMIT 50")
    cert_status = cert_runtime_status()
    envs = {
        "CERTIFICADO_PATH": os.environ.get("CERTIFICADO_PATH", ""),
        "CERTIFICADO_SENHA": "configurada" if os.environ.get("CERTIFICADO_SENHA") else "não configurada",
        "AMBIENTE_FISCAL": os.environ.get("AMBIENTE_FISCAL", "homologacao"),
        "UF_EMPRESA": os.environ.get("UF_EMPRESA", "MG"),
    }
    checklist = fiscal_checklist(cfg, cert_status)
    prod_ready, prod_missing = fiscal_prod_ready(cfg, cert_status)
    return render_template("fiscal.html", cfg=cfg, notas=notas, xmls=xmls, devolucoes=devolucoes, cert_status=cert_status, envs=envs, checklist=checklist, prod_ready=prod_ready, prod_missing=prod_missing)

@app.route("/fiscal/testar-sefaz", methods=["POST"])
@login_required
def fiscal_testar_sefaz():
    cfg = fetch_one("SELECT * FROM fiscal_config WHERE id=1")
    payload = fiscal_engine_base_payload(cfg)
    payload["action"] = "status"
    result = run_fiscal_engine(payload, timeout=45)
    if result.get("ok") and str(result.get("cStat") or "") == "107":
        flash(f"SEFAZ/MG online ✅ cStat 107 - {result.get('xMotivo') or 'Serviço em operação'}", "ok")
    elif result.get("ok"):
        flash(f"Retorno da SEFAZ: cStat {result.get('cStat') or '-'} - {result.get('xMotivo') or 'sem descrição'}", "erro")
    else:
        flash(f"Não consegui consultar a SEFAZ: {result.get('error') or 'falha desconhecida'}", "erro")
    return redirect(url_for("fiscal"))


@app.route("/fiscal/preencher-padrao", methods=["POST"])
@login_required
def fiscal_preencher_padrao():
    """Restaura campos fiscais base sem mudar o ambiente escolhido."""
    exec_sql("""
        UPDATE fiscal_config SET
            ambiente=COALESCE(NULLIF(ambiente,''), :ambiente),
            uf=COALESCE(NULLIF(uf,''), :uf),
            certificado_path=COALESCE(NULLIF(certificado_path,''), :certificado_path),
            certificado_senha_env=COALESCE(NULLIF(certificado_senha_env,''), :certificado_senha_env),
            regime=COALESCE(NULLIF(regime,''), :regime),
            nf_serie=COALESCE(NULLIF(nf_serie,''), :nf_serie),
            nf_numero_inicial=COALESCE(NULLIF(nf_numero_inicial,''), :nf_numero_inicial),
            nfce_serie=COALESCE(NULLIF(nfce_serie,''), :nfce_serie),
            nfce_numero_inicial=COALESCE(NULLIF(nfce_numero_inicial,''), :nfce_numero_inicial),
            csc_id=COALESCE(NULLIF(csc_id,''), :csc_id),
            csc_token=COALESCE(NULLIF(csc_token,''), :csc_token),
            cfop_padrao=COALESCE(NULLIF(cfop_padrao,''), :cfop_padrao),
            cst_csosn_padrao=COALESCE(NULLIF(cst_csosn_padrao,''), :cst_csosn_padrao),
            observacao=COALESCE(NULLIF(observacao,''), :observacao)
        WHERE id=1
    """, FISCAL_DEFAULTS)
    backup_db("fiscal-padrao")
    flash("Preenchi os dados fiscais base sem alterar o ambiente atual.", "ok")
    return redirect(url_for("fiscal"))

@app.route("/fiscal/aplicar-fixos", methods=["POST"])
@login_required
def fiscal_aplicar_fixos():
    """Força os dados fiscais base já recebidos: empresa, séries, CSC/Token e padrões."""
    exec_sql("""
        UPDATE fiscal_config SET
            razao_social=:razao_social,
            nome_fantasia=:nome_fantasia,
            cnpj=:cnpj,
            inscricao_estadual=:inscricao_estadual,
            endereco=:endereco,
            municipio=:municipio,
            uf=:uf,
            cep=:cep,
            telefone=:telefone,
            email=:email,
            regime=:regime,
            ambiente=:ambiente,
            certificado_nome=:certificado_nome,
            certificado_path=:certificado_path,
            certificado_senha_env=:certificado_senha_env,
            nf_serie=:nf_serie,
            nf_numero_inicial=:nf_numero_inicial,
            nfce_serie=:nfce_serie,
            nfce_numero_inicial=:nfce_numero_inicial,
            csc_id=:csc_id,
            csc_token=:csc_token,
            cfop_padrao=:cfop_padrao,
            cst_csosn_padrao=:cst_csosn_padrao,
            observacao=:observacao
        WHERE id=1
    """, FISCAL_DEFAULTS)
    backup_db("fiscal-dados-fixos")
    flash("Dados fiscais fixos da CENTRALVET aplicados novamente.", "ok")
    return redirect(url_for("fiscal"))

@app.route("/fiscal/preencher-documentos", methods=["POST"])
@login_required
def fiscal_preencher_documentos():
    """Preenche dados cadastrais da empresa com base nos documentos oficiais enviados.
    Não altera ambiente, CSC/Token, séries ou regras fiscais.
    """
    exec_sql("""
        UPDATE fiscal_config SET
            razao_social='CENTRALVET AGROPECUARIA LTDA',
            nome_fantasia='CENTRALVET AGROPECUARIA',
            cnpj='68.690.225/0001-50',
            inscricao_estadual='005626088.00-49',
            endereco='R MANOEL FRANCISCO DE CASTRO, 21, B, CENTRO',
            municipio='ORIZANIA',
            uf='MG',
            cep='36.828-000',
            telefone='(31) 3875-1342',
            email='CONTABILIDADEREIS01@HOTMAIL.COM',
            regime='SIMPLES NACIONAL',
            logradouro='R MANOEL FRANCISCO DE CASTRO', numero='21', complemento='B', bairro='CENTRO', codigo_municipio='3145877', cnae='4683400', crt=1,
            observacao='Dados cadastrais preenchidos pelos documentos enviados. Emissão NF-e direta pela SEFAZ/MG.'
        WHERE id=1
    """)
    backup_db("fiscal-dados-documentos")
    flash("Dados cadastrais da CENTRALVET restaurados sem alterar o ambiente fiscal.", "ok")
    return redirect(url_for("fiscal"))

@app.route("/fiscal/certificado", methods=["GET", "POST"])
@login_required
def enviar_certificado():
    cfg = fetch_one("SELECT * FROM fiscal_config WHERE id=1")
    env_path = os.environ.get("CERTIFICADO_PATH", "").strip()
    cfg_path = cfg["certificado_path"] if cfg and cfg["certificado_path"] else ""
    cert_path = env_path or cfg_path or "/app/certs/centralvet_a1.pfx"
    if not cert_path.lower().endswith((".pfx", ".p12")):
        cert_path = os.path.join(cert_path, "centralvet_a1.pfx")
    cert_path = os.path.abspath(cert_path)
    cert_dir = os.path.dirname(cert_path)
    status = {
        "path": cert_path,
        "dir": cert_dir,
        "exists": os.path.exists(cert_path),
        "has_password": bool(os.environ.get("CERTIFICADO_SENHA", "").strip()),
        "size": os.path.getsize(cert_path) if os.path.exists(cert_path) else 0,
        "updated": datetime.fromtimestamp(os.path.getmtime(cert_path)).strftime("%d/%m/%Y %H:%M") if os.path.exists(cert_path) else ""
    }
    if request.method == "POST":
        arq = request.files.get("certificado")
        if not arq or not arq.filename:
            flash("Selecione o arquivo do certificado A1 (.pfx ou .p12).", "erro")
            return redirect(url_for("enviar_certificado"))
        filename = arq.filename.lower().strip()
        if not filename.endswith((".pfx", ".p12")):
            flash("Arquivo inválido. Envie somente certificado A1 com final .pfx ou .p12.", "erro")
            return redirect(url_for("enviar_certificado"))
        try:
            os.makedirs(cert_dir, exist_ok=True)
            tmp_path = cert_path + ".tmp"
            arq.save(tmp_path)
            if os.path.getsize(tmp_path) <= 0:
                os.remove(tmp_path)
                flash("O arquivo enviado veio vazio. Tente enviar novamente.", "erro")
                return redirect(url_for("enviar_certificado"))
            os.replace(tmp_path, cert_path)
            try:
                os.chmod(cert_path, 0o600)
            except Exception:
                pass
            exec_sql("UPDATE fiscal_config SET certificado_path=?, certificado_nome=? WHERE id=1", (cert_path, arq.filename))
            backup_db("certificado-config")
            flash("Certificado A1 salvo com segurança no servidor.", "ok")
            return redirect(url_for("fiscal"))
        except Exception as e:
            flash(f"Não consegui salvar o certificado em {cert_path}. Detalhe: {e}", "erro")
            return redirect(url_for("enviar_certificado"))
    return render_template("certificado_upload.html", status=status, cfg=cfg)

@app.route("/xml/importar", methods=["GET", "POST"])
@login_required
def importar_xml():
    preview = None
    if request.method == "POST":
        arq = request.files.get("xml_file")
        aplicar_margem = request.form.get("aplicar_margem") == "1"
        margem = money_to_float(request.form.get("margem_lucro"))
        if not arq or not arq.filename:
            flash("Selecione o XML da nota de entrada.", "erro")
            return redirect(url_for("importar_xml"))
        xml_bytes = arq.read()
        xml_hash = hashlib.sha256(xml_bytes).hexdigest()
        try:
            dados = parse_nfe_xml(xml_bytes)
        except Exception as e:
            flash(f"Não consegui ler esse XML. Verifique se é o XML completo da NF-e. Detalhe: {e}", "erro")
            return redirect(url_for("importar_xml"))
        if not dados["itens"]:
            flash("XML lido, mas não encontrei produtos dentro da nota.", "erro")
            return redirect(url_for("importar_xml"))
        chave_unica = dados["chave"] or xml_hash
        ja = fetch_one("SELECT * FROM xml_imports WHERE chave=? OR xml_hash=?", (chave_unica, xml_hash))
        if ja:
            flash(f"Essa nota/XML já foi importada antes. Registro #{ja['id']}.", "erro")
            return redirect(url_for("importar_xml"))
        with get_db() as db:
            fornecedor_id = None
            if dados["fornecedor_nome"] or dados["fornecedor_cnpj"]:
                cnpj_digits = only_digits(dados["fornecedor_cnpj"])
                fornecedor = None
                if cnpj_digits:
                    fornecedor = db.execute("SELECT * FROM fornecedores WHERE cpf_cnpj LIKE ? LIMIT 1", (f"%{cnpj_digits[-8:]}%",)).fetchone()
                if not fornecedor and dados["fornecedor_nome"]:
                    fornecedor = db.execute("SELECT * FROM fornecedores WHERE nome LIKE ? LIMIT 1", (dados["fornecedor_nome"],)).fetchone()
                if fornecedor:
                    fornecedor_id = fornecedor["id"]
                else:
                    cur = db.execute("INSERT INTO fornecedores (nome, cpf_cnpj, observacao) VALUES (?, ?, ?)",
                                     (dados["fornecedor_nome"] or "Fornecedor do XML", dados["fornecedor_cnpj"], "Cadastrado automaticamente pela importação de XML."))
                    fornecedor_id = cur.lastrowid
            novos = atualizados = 0
            data_mov = dados["emissao"] or today_str()
            origem = f"XML NF-e {dados['numero'] or ''}".strip()
            for item in dados["itens"]:
                prod = None
                if item["codigo"]:
                    prod = db.execute("SELECT * FROM produtos WHERE ativo=1 AND codigo=? LIMIT 1", (item["codigo"],)).fetchone()
                if not prod and item["ean"]:
                    prod = db.execute("SELECT * FROM produtos WHERE ativo=1 AND ean=? LIMIT 1", (item["ean"],)).fetchone()
                if not prod:
                    prod = db.execute("SELECT * FROM produtos WHERE ativo=1 AND nome=? LIMIT 1", (item["nome"],)).fetchone()
                qtd = item["quantidade"]
                custo = item["valor_unitario"] or (item["valor_total"] / qtd if qtd else 0)
                if prod:
                    produto_id = prod["id"]
                    novo_preco_venda = prod["preco_venda"] or 0
                    if aplicar_margem and margem > 0:
                        novo_preco_venda = custo * (1 + margem / 100.0)
                    db.execute("""
                        UPDATE produtos SET nome=?, unidade=?, preco_custo=?, preco_venda=?, estoque=estoque+?,
                        ncm=?, cfop=?, cst_csosn=?, ean=?, cest=?, ultima_chave_xml=?, fornecedor_id=?
                        WHERE id=?
                    """, (item["nome"], normalizar_unidade(item["unidade"]), custo, novo_preco_venda, qtd, item["ncm"], item["cfop"],
                          item["cst_csosn"], item["ean"], item["cest"], chave_unica, fornecedor_id, produto_id))
                    atualizados += 1
                else:
                    preco_venda = custo * (1 + margem / 100.0) if aplicar_margem and margem > 0 else 0
                    cur = db.execute("""
                        INSERT INTO produtos (codigo,nome,categoria,unidade,preco_custo,preco_venda,estoque,estoque_minimo,ncm,cfop,cst_csosn,ean,cest,ultima_chave_xml,fornecedor_id)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (item["codigo"], item["nome"], "Importado XML", normalizar_unidade(item["unidade"]), custo, preco_venda, qtd, 0,
                          item["ncm"], item["cfop"], item["cst_csosn"], item["ean"], item["cest"], chave_unica, fornecedor_id))
                    produto_id = cur.lastrowid
                    novos += 1
                db.execute("""
                    INSERT INTO estoque_mov (data, produto_id, tipo, quantidade, custo_unit, valor_total, origem, observacao)
                    VALUES (?, ?, 'Entrada', ?, ?, ?, ?, ?)
                """, (data_mov, produto_id, qtd, custo, item["valor_total"] or qtd*custo, origem, f"Importado pelo XML. Chave: {dados['chave'] or 'sem chave'}"))
            db.execute("""
                INSERT INTO xml_imports (chave, xml_hash, numero, serie, emissao, fornecedor_nome, fornecedor_cnpj, total, itens_qtd, observacao)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (chave_unica, xml_hash, dados["numero"], dados["serie"], dados["emissao"], dados["fornecedor_nome"], dados["fornecedor_cnpj"],
                  dados["total"], len(dados["itens"]), f"{novos} novo(s), {atualizados} atualizado(s)."))
            db.commit()
        backup_db("xml-importado")
        flash(f"XML importado: {novos} produto(s) novo(s), {atualizados} atualizado(s), estoque lançado automaticamente.", "ok")
        return redirect(url_for("produtos"))
    ultimos = fetch_all("SELECT * FROM xml_imports ORDER BY id DESC LIMIT 20")
    cfg = fetch_one("SELECT * FROM fiscal_config WHERE id=1")
    return render_template("importar_xml.html", ultimos=ultimos, cfg=cfg)


# ---------------- Devolução de mercadoria ----------------

def find_product_for_xml_item(db, item):
    prod = None
    if item.get("codigo"):
        prod = db.execute("SELECT * FROM produtos WHERE ativo=1 AND codigo=? LIMIT 1", (item.get("codigo"),)).fetchone()
    if not prod and item.get("ean"):
        prod = db.execute("SELECT * FROM produtos WHERE ativo=1 AND ean=? LIMIT 1", (item.get("ean"),)).fetchone()
    if not prod and item.get("nome"):
        prod = db.execute("SELECT * FROM produtos WHERE ativo=1 AND nome=? LIMIT 1", (item.get("nome"),)).fetchone()
    return prod

@app.route("/devolucoes")
@login_required
def devolucoes():
    recover_devolucao_files()
    q = (request.args.get("q") or "").strip()
    if q:
        like = f"%{q}%"
        rows = fetch_all("""
            SELECT * FROM devolucoes
            WHERE fornecedor_nome LIKE ? OR fornecedor_cnpj LIKE ? OR chave_origem LIKE ? OR numero_origem LIKE ?
            ORDER BY id DESC
        """, (like, like, like, like))
    else:
        rows = fetch_all("SELECT * FROM devolucoes ORDER BY id DESC LIMIT 120")
    return render_template("devolucoes.html", rows=rows, q=q)

@app.route("/devolucoes/nova", methods=["GET", "POST"])
@login_required
def nova_devolucao():
    preview = None
    encoded_xml = ""
    if request.method == "POST" and request.form.get("acao") == "preview":
        arq = request.files.get("xml_file")
        if not arq or not arq.filename:
            flash("Selecione o XML da nota de compra original.", "erro")
            return redirect(url_for("nova_devolucao"))
        xml_bytes = arq.read()
        try:
            dados = parse_nfe_xml(xml_bytes)
        except Exception as e:
            flash(f"Não consegui ler esse XML. Envie o XML completo da NF-e. Detalhe: {e}", "erro")
            return redirect(url_for("nova_devolucao"))
        if not dados["itens"]:
            flash("XML lido, mas não encontrei produtos na nota.", "erro")
            return redirect(url_for("nova_devolucao"))
        xml_hash = hashlib.sha256(xml_bytes).hexdigest()
        encoded_xml = base64.b64encode(xml_bytes).decode("ascii")
        with get_db() as db:
            itens = []
            for i, item in enumerate(dados["itens"], start=1):
                prod = find_product_for_xml_item(db, item)
                d = dict(item)
                d["idx"] = i
                d["produto_id"] = prod["id"] if prod else ""
                d["produto_estoque"] = prod["estoque"] if prod else 0
                d["produto_status"] = "Encontrado no cadastro" if prod else "Ainda não cadastrado"
                itens.append(d)
        empresa_uf = os.environ.get("UF_EMPRESA", FISCAL_DEFAULTS.get("uf", "MG")).strip() or "MG"
        fornecedor_uf = (dados.get("fornecedor_uf") or "").strip()
        tipo_sugerido = "outro_estado" if fornecedor_uf and fornecedor_uf.upper() != empresa_uf.upper() else "mesmo_estado"

        total_com_st = 0
        total_sem_st = 0
        cfops_detectados = []
        for item in itens:
            modo_st = item.get("modo_st") or "sem_st"
            if modo_st == "com_st":
                total_com_st += 1
            else:
                total_sem_st += 1
            item["cfop_devolucao_sugerido"] = sugerir_cfop_devolucao(tipo_sugerido, modo_st)
            cfops_detectados.append(item["cfop_devolucao_sugerido"])

        if total_com_st and total_sem_st:
            status_st_geral = "Misto: há itens com ST e itens sem ST. O sistema preenche por produto."
        elif total_com_st:
            status_st_geral = "Com ST detectado no XML."
        else:
            status_st_geral = "Sem ST / comum detectado no XML."
        substituicao_sugerida = "auto"

        # CFOP padrão usado como fallback. Cada produto já recebe o CFOP detectado individualmente.
        cfop_sugerido = cfops_detectados[0] if cfops_detectados else sugerir_cfop_devolucao(tipo_sugerido, "sem_st")
        preview = {
            "dados": dados, "itens": itens, "xml_hash": xml_hash, "empresa_uf": empresa_uf,
            "tipo_sugerido": tipo_sugerido, "cfop_sugerido": cfop_sugerido,
            "substituicao_sugerida": substituicao_sugerida, "status_st_geral": status_st_geral,
            "total_com_st": total_com_st, "total_sem_st": total_sem_st,
        }
    cfg = fetch_one("SELECT * FROM fiscal_config WHERE id=1")
    return render_template("devolucao_form.html", preview=preview, encoded_xml=encoded_xml, hoje=today_str(), empresa_uf=os.environ.get("UF_EMPRESA", "MG"), cfg=cfg, producao=fiscal_tpamb(cfg)==1)

@app.route("/devolucoes/salvar", methods=["POST"])
@login_required
def salvar_devolucao():
    xml_data = request.form.get("xml_data") or ""
    if not xml_data:
        flash("Importe primeiro o XML da nota original.", "erro")
        return redirect(url_for("nova_devolucao"))
    try:
        xml_bytes = base64.b64decode(xml_data.encode("ascii"))
        dados = parse_nfe_xml(xml_bytes)
    except Exception as e:
        flash(f"Não consegui recuperar os dados do XML. Tente importar novamente. Detalhe: {e}", "erro")
        return redirect(url_for("nova_devolucao"))
    cfg_atual = fetch_one("SELECT * FROM fiscal_config WHERE id=1")
    # Evita duas NF-e diferentes disputando a mesma numeração.
    proximo_nnf = only_digits(cfg_val(cfg_atual, "nf_numero_inicial") or "1") or "1"
    pendente_numero = fetch_one("""
        SELECT id, status, sefaz_cstat, sefaz_motivo FROM devolucoes
        WHERE nf_devolucao_numero=? AND status!='Autorizada SEFAZ'
        ORDER BY id DESC LIMIT 1
    """, (proximo_nnf,))
    if pendente_numero:
        flash(f"Existe uma NF-e de devolução #{pendente_numero['id']} pendente usando o próximo número {proximo_nnf}. Resolva ou exclua essa tentativa antes de criar outra.", "erro")
        return redirect(url_for("ver_devolucao", devolucao_id=pendente_numero["id"]))

    if fiscal_tpamb(cfg_atual) == 1:
        empresa_doc = only_digits(cfg_val(cfg_atual, "cnpj"))
        destinatario_doc = only_digits(dados.get("destinatario_cnpj"))
        if not destinatario_doc or destinatario_doc != empresa_doc:
            flash("Em PRODUÇÃO eu não vou emitir: o XML original não está destinado ao CNPJ da CENTRALVET. Use uma NF-e de compra emitida para a empresa.", "erro")
            return redirect(url_for("nova_devolucao"))
        if str(dados.get("modelo") or "55") != "55":
            flash("Para esta devolução em produção, importe o XML de uma NF-e modelo 55.", "erro")
            return redirect(url_for("nova_devolucao"))
        if len(only_digits(dados.get("chave"))) != 44:
            flash("A chave da NF-e original não tem 44 dígitos. Não vou enviar uma devolução inválida para a SEFAZ.", "erro")
            return redirect(url_for("nova_devolucao"))

    xml_hash = hashlib.sha256(xml_bytes).hexdigest()
    original_path = os.path.join(FISCAL_ORIGINAL_DIR, f"{xml_hash}.xml")
    if not os.path.exists(original_path):
        with open(original_path, "wb") as f:
            f.write(xml_bytes)

    # Mantém o XML de origem visível no histórico fiscal mesmo quando ele foi
    # importado diretamente pela tela de devolução (sem passar por /xml/importar).
    with get_db() as db:
        existente_xml = db.execute("SELECT id FROM xml_imports WHERE chave=? OR xml_hash=? LIMIT 1", (dados.get("chave") or "", xml_hash)).fetchone()
        if not existente_xml:
            db.execute("""INSERT INTO xml_imports
                (chave, xml_hash, numero, serie, emissao, fornecedor_nome, fornecedor_cnpj, total, itens_qtd, observacao, xml_path, tipo)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (dados.get("chave") or xml_hash, xml_hash, dados.get("numero"), dados.get("serie"), dados.get("emissao"),
                 dados.get("fornecedor_nome"), dados.get("fornecedor_cnpj"), dados.get("total") or 0, len(dados.get("itens") or []),
                 "XML usado como origem de nota de devolução.", original_path, "Origem devolução"))
            db.commit()

    motivo = request.form.get("motivo") or "Devolução por avaria"
    baixar_estoque = request.form.get("baixar_estoque") == "1"
    cfop_devolucao_padrao = (request.form.get("cfop_devolucao_padrao") or "").strip()
    tipo_operacao_devolucao = (request.form.get("tipo_operacao_devolucao") or "mesmo_estado").strip()
    substituicao_devolucao = (request.form.get("substituicao_devolucao") or "sem_st").strip()
    if not cfop_devolucao_padrao:
        modo_fallback = "sem_st" if substituicao_devolucao == "auto" else substituicao_devolucao
        cfop_devolucao_padrao = sugerir_cfop_devolucao(tipo_operacao_devolucao, modo_fallback)
    obs = request.form.get("observacao") or ""
    if substituicao_devolucao == "auto":
        obs_info = f"CFOP automático: XML analisado por produto; operação {'outro estado' if tipo_operacao_devolucao == 'outro_estado' else 'mesmo estado'}; fallback {cfop_devolucao_padrao}."
    else:
        obs_info = f"CFOP: {'outro estado' if tipo_operacao_devolucao == 'outro_estado' else 'mesmo estado'} / {'com ST' if substituicao_devolucao == 'com_st' else 'sem ST'} / {cfop_devolucao_padrao}."
    obs = (obs + " | " + obs_info).strip(" |")
    selecionados = 0
    with get_db() as db:
        cur = db.execute("""
            INSERT INTO devolucoes
            (data, fornecedor_nome, fornecedor_cnpj, destinatario_nome, destinatario_cnpj, chave_origem, numero_origem,
             serie_origem, emissao_origem, total_original, motivo, status, baixou_estoque, baixar_estoque_solicitado,
             xml_hash, xml_original_path, observacao)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
        """, (
            request.form.get("data") or today_str(), dados.get("fornecedor_nome"), dados.get("fornecedor_cnpj"),
            dados.get("destinatario_nome"), dados.get("destinatario_cnpj"), dados.get("chave"), dados.get("numero"),
            dados.get("serie"), dados.get("emissao"), dados.get("total"), motivo, "Aguardando autorização SEFAZ",
            1 if baixar_estoque else 0, xml_hash, original_path, obs
        ))
        devolucao_id = cur.lastrowid
        db.execute("UPDATE xml_imports SET devolucao_id=? WHERE xml_hash=? AND (devolucao_id IS NULL OR devolucao_id=0)", (devolucao_id, xml_hash))
        for idx, item in enumerate(dados["itens"], start=1):
            usar = request.form.get(f"usar_{idx}") == "1"
            if not usar:
                continue
            qtd_dev = money_to_float(request.form.get(f"qtd_devolver_{idx}"))
            if qtd_dev <= 0:
                continue
            qtd_original = float(item.get("quantidade") or 0)
            if qtd_original > 0 and qtd_dev > qtd_original:
                qtd_dev = qtd_original
            cfop_dev = (request.form.get(f"cfop_devolucao_{idx}") or "").strip()
            if not cfop_dev:
                cfop_dev = sugerir_cfop_devolucao(tipo_operacao_devolucao, item.get("modo_st") or ("sem_st" if substituicao_devolucao == "auto" else substituicao_devolucao)) or cfop_devolucao_padrao
            prod = find_product_for_xml_item(db, item)
            produto_id = prod["id"] if prod else None
            valor_unit = float(item.get("valor_unitario") or 0)
            valor_total = qtd_dev * valor_unit
            db.execute("""
                INSERT INTO devolucao_itens
                (devolucao_id, produto_id, item_origem_idx, codigo, ean, nome, ncm, cest, cfop_original, cfop_devolucao, cst_csosn,
                 unidade, quantidade_original, quantidade_devolver, valor_unitario, valor_total)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (devolucao_id, produto_id, int(item.get("item_origem_idx") or idx), item.get("codigo"), item.get("ean"), item.get("nome"), item.get("ncm"),
                  item.get("cest"), item.get("cfop"), cfop_dev, item.get("cst_csosn"), item.get("unidade"), qtd_original,
                  qtd_dev, valor_unit, valor_total))
            selecionados += 1
        if selecionados <= 0:
            db.execute("DELETE FROM devolucoes WHERE id=?", (devolucao_id,))
            db.commit()
            flash("Selecione pelo menos um produto e informe a quantidade para devolver.", "erro")
            return redirect(url_for("nova_devolucao"))
        db.commit()
    backup_db("devolucao-criada")
    result = emitir_devolucao_sefaz_internal(devolucao_id)
    if result.get("authorized"):
        flash(f"NF-e autorizada pela SEFAZ ✅ cStat {result.get('cStat')} - {result.get('xMotivo') or 'Autorizada'}", "ok")
        return redirect(url_for("ver_devolucao", devolucao_id=devolucao_id, emitida=1))
    motivo_erro = result.get("xMotivo") or result.get("error") or "Falha na autorização"
    cstat = result.get("cStat")
    if cstat:
        flash(f"NF-e não autorizada pela SEFAZ. cStat {cstat}: {motivo_erro}", "erro")
    else:
        flash(f"Falha antes do envio à SEFAZ: {motivo_erro}", "erro")
    return redirect(url_for("ver_devolucao", devolucao_id=devolucao_id))

@app.route("/devolucoes/<int:devolucao_id>")
@login_required
def ver_devolucao(devolucao_id):
    dev = fetch_one("SELECT * FROM devolucoes WHERE id=?", (devolucao_id,))
    if not dev:
        flash("Devolução não encontrada.", "erro")
        return redirect(url_for("devolucoes"))
    itens = fetch_all("SELECT * FROM devolucao_itens WHERE devolucao_id=? ORDER BY id", (devolucao_id,))
    total = sum(float(i["valor_total"] or 0) for i in itens)
    share_token = make_danfe_share_token(devolucao_id) if dev["status"] == "Autorizada SEFAZ" else None
    return render_template("devolucao_detalhe.html", dev=dev, itens=itens, total=total, share_token=share_token)

@app.route("/devolucoes/<int:devolucao_id>/emitir-sefaz", methods=["POST"])
@login_required
def emitir_devolucao_sefaz(devolucao_id):
    dev_atual = fetch_one("SELECT * FROM devolucoes WHERE id=?", (devolucao_id,))
    if dev_atual and dev_atual["status"] == "Rejeitada SEFAZ":
        exec_sql("UPDATE devolucoes SET emissao_tentativa_em=NULL WHERE id=?", (devolucao_id,))
    result = emitir_devolucao_sefaz_internal(devolucao_id)
    if result.get("authorized"):
        flash(f"NF-e autorizada pela SEFAZ ✅ cStat {result.get('cStat')} - {result.get('xMotivo') or 'Autorizada'}", "ok")
        return redirect(url_for("ver_devolucao", devolucao_id=devolucao_id, emitida=1))
    if result.get("cStat"):
        flash(f"NF-e não autorizada pela SEFAZ. cStat {result.get('cStat')}: {result.get('xMotivo') or result.get('error') or 'Falha desconhecida'}", "erro")
    else:
        flash(f"Falha antes do envio à SEFAZ: {result.get('error') or result.get('xMotivo') or 'Falha desconhecida'}", "erro")
    return redirect(url_for("ver_devolucao", devolucao_id=devolucao_id))


@app.route("/compartilhar/danfe/<token>")
def danfe_devolucao_publico(token):
    try:
        payload = danfe_share_serializer().loads(token, max_age=7 * 24 * 60 * 60)
        devolucao_id = int(payload.get("devolucao_id"))
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return "Link do DANFE inválido ou expirado.", 410

    dev = fetch_one("SELECT * FROM devolucoes WHERE id=?", (devolucao_id,))
    if not dev or dev["status"] != "Autorizada SEFAZ":
        return "DANFE indisponível.", 404
    path = fiscal_safe_file(cfg_val(dev, "danfe_path"))
    if not path:
        return "Arquivo DANFE não encontrado.", 404
    return send_file(path, mimetype="application/pdf", as_attachment=False, download_name=f"DANFE-NFe-{dev['nf_devolucao_numero'] or devolucao_id}.pdf")

@app.route("/devolucoes/<int:devolucao_id>/danfe")
@login_required
def danfe_devolucao(devolucao_id):
    dev = fetch_one("SELECT * FROM devolucoes WHERE id=?", (devolucao_id,))
    if not dev or dev["status"] != "Autorizada SEFAZ":
        flash("O DANFE oficial só fica disponível depois que a SEFAZ autorizar a NF-e.", "erro")
        return redirect(url_for("ver_devolucao", devolucao_id=devolucao_id))
    path = fiscal_safe_file(dev["danfe_path"])
    if not path:
        flash("Arquivo DANFE não encontrado no armazenamento fiscal.", "erro")
        return redirect(url_for("ver_devolucao", devolucao_id=devolucao_id))
    return send_file(path, mimetype="application/pdf", as_attachment=False, download_name=f"DANFE-NFe-{dev['nf_devolucao_numero'] or devolucao_id}.pdf")


@app.route("/devolucoes/<int:devolucao_id>/xml")
@login_required
def xml_devolucao(devolucao_id):
    dev = fetch_one("SELECT * FROM devolucoes WHERE id=?", (devolucao_id,))
    if not dev or dev["status"] != "Autorizada SEFAZ":
        flash("O XML autorizado só fica disponível depois da autorização da SEFAZ.", "erro")
        return redirect(url_for("ver_devolucao", devolucao_id=devolucao_id))
    path = fiscal_safe_file(dev["xml_autorizado_path"])
    if not path:
        flash("XML autorizado não encontrado no armazenamento fiscal.", "erro")
        return redirect(url_for("ver_devolucao", devolucao_id=devolucao_id))
    return send_file(path, mimetype="application/xml", as_attachment=True, download_name=f"NFe-{dev['nf_devolucao_chave'] or devolucao_id}.xml")


@app.route("/devolucoes/<int:devolucao_id>/excluir", methods=["POST"])
@login_required
def excluir_devolucao(devolucao_id):
    dev = fetch_one("SELECT * FROM devolucoes WHERE id=?", (devolucao_id,))
    if not dev:
        flash("Devolução não encontrada.", "erro")
        return redirect(url_for("devolucoes"))
    if dev["status"] == "Autorizada SEFAZ":
        flash("NF-e autorizada não pode ser excluída. Para desfazer uma nota autorizada é necessário usar o evento fiscal adequado (ex.: cancelamento, quando permitido).", "erro")
        return redirect(url_for("ver_devolucao", devolucao_id=devolucao_id))
    if dev["status"] == "Falha na comunicação" and dev["nf_devolucao_numero"]:
        flash("Não vou excluir esta tentativa enquanto o resultado estiver incerto. Tente autorizar novamente para o sistema consultar/recuperar a situação da mesma NF-e.", "erro")
        return redirect(url_for("ver_devolucao", devolucao_id=devolucao_id))
    with get_db() as db:
        if dev["baixou_estoque"]:
            itens = db.execute("SELECT * FROM devolucao_itens WHERE devolucao_id=?", (devolucao_id,)).fetchall()
            for i in itens:
                if i["produto_id"]:
                    db.execute("UPDATE produtos SET estoque=estoque+? WHERE id=?", (i["quantidade_devolver"], i["produto_id"]))
            db.execute("DELETE FROM estoque_mov WHERE origem=?", (f"DEVOLUCAO #{devolucao_id}",))
        db.execute("DELETE FROM devolucao_itens WHERE devolucao_id=?", (devolucao_id,))
        db.execute("DELETE FROM devolucoes WHERE id=?", (devolucao_id,))
        db.commit()
    backup_db("devolucao-excluida")
    flash("Devolução excluída.", "ok")
    return redirect(url_for("devolucoes"))


# ---------------- Relatórios / Backup ----------------

@app.route("/relatorios")
@login_required
def relatorios():
    inicio = request.args.get("inicio") or date.today().replace(day=1).isoformat()
    fim = request.args.get("fim") or today_str()

    def carregar():
        vendas_periodo = fetch_one("SELECT COALESCE(SUM(total),0) total, COALESCE(SUM(lucro),0) lucro, COUNT(*) qtd FROM vendas WHERE data BETWEEN ? AND ?", (inicio, fim))
        entradas = fetch_one("SELECT COALESCE(SUM(valor),0) v FROM financeiro WHERE tipo='Entrada' AND status='Pago' AND data BETWEEN ? AND ?", (inicio, fim))["v"]
        saidas = fetch_one("SELECT COALESCE(SUM(valor),0) v FROM financeiro WHERE tipo='Saída' AND status='Pago' AND data BETWEEN ? AND ?", (inicio, fim))["v"]
        mais_vendidos = fetch_all("""
            SELECT COALESCE(produto_nome,'Produto') produto_nome, COALESCE(SUM(quantidade),0) qtd, COALESCE(SUM(total),0) total
            FROM venda_itens vi JOIN vendas v ON v.id=vi.venda_id
            WHERE v.data BETWEEN ? AND ?
            GROUP BY produto_nome ORDER BY qtd DESC LIMIT 20
        """, (inicio, fim))
        vendas = fetch_all("SELECT * FROM vendas WHERE data BETWEEN ? AND ? ORDER BY data DESC, id DESC", (inicio, fim))
        return vendas_periodo, entradas, saidas, mais_vendidos, vendas

    try:
        vendas_periodo, entradas, saidas, mais_vendidos, vendas = carregar()
    except sqlite3.OperationalError:
        # Banco criado por uma versão antiga: aplica as migrações e tenta uma vez de novo.
        init_db()
        vendas_periodo, entradas, saidas, mais_vendidos, vendas = carregar()

    return render_template("relatorios.html", inicio=inicio, fim=fim, vendas_periodo=vendas_periodo,
                           entradas=entradas, saidas=saidas, mais_vendidos=mais_vendidos, vendas=vendas)

@app.route("/backup")
@login_required
def backup():
    path = backup_db("manual")
    if not path:
        flash("Banco ainda não encontrado.", "erro")
        return redirect(url_for("index"))
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))

@app.route("/backups")
@login_required
def backups():
    files = []
    if os.path.exists(BACKUP_DIR):
        for fn in sorted(os.listdir(BACKUP_DIR), reverse=True):
            if fn.endswith(".db"):
                p = os.path.join(BACKUP_DIR, fn)
                files.append({"name": fn, "size": os.path.getsize(p), "mtime": datetime.fromtimestamp(os.path.getmtime(p)).strftime("%d/%m/%Y %H:%M")})
    return render_template("backups.html", files=files)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
