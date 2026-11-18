"""Arquivo mensal de XMLs autorizados e DANFEs já emitidos pela aplicação."""
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
import csv
import io
import os
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from zoneinfo import ZoneInfo

FUSO=ZoneInfo('America/Sao_Paulo')


def migrar(db):
    db.execute('''CREATE TABLE IF NOT EXISTS fiscal_documentos (
        id INTEGER PRIMARY KEY AUTOINCREMENT, origem TEXT NOT NULL, origem_id INTEGER NOT NULL,
        modelo TEXT, numero TEXT, serie TEXT, chave TEXT, protocolo TEXT, destinatario TEXT, ambiente TEXT,
        emitido_em TEXT, mes TEXT, valor_centavos INTEGER DEFAULT 0,
        xml_path TEXT, pdf_path TEXT, xml_sha256 TEXT, pdf_sha256 TEXT,
        aviso TEXT, registrado_em TEXT NOT NULL, UNIQUE(origem,origem_id)
    )''')
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS fiscal_documento_chave ON fiscal_documentos(chave) WHERE chave IS NOT NULL AND chave!=''")
    db.execute('CREATE INDEX IF NOT EXISTS fiscal_documentos_mes ON fiscal_documentos(mes,emitido_em)')


def agora():
    return datetime.now(FUSO).isoformat(timespec='seconds')


def digitos(valor):
    return ''.join(c for c in str(valor or '') if c.isdigit())


def caminho_seguro(raiz,path):
    if not path:return None
    try:
        raiz=Path(raiz).resolve();p=Path(path).resolve()
        return p if p.is_relative_to(raiz) and p.is_file() else None
    except (ValueError,OSError):return None


def mes_valido(mes):
    if not re.fullmatch(r'\d{4}-\d{2}',mes or ''):raise ValueError('Selecione um mês válido.')
    try:datetime.strptime(mes,'%Y-%m')
    except ValueError:raise ValueError('Selecione um mês válido.')
    return mes


def _data(valor):
    if not valor:return None,None
    try:
        d=datetime.fromisoformat(str(valor).replace('Z','+00:00'))
        if d.tzinfo is None:d=d.replace(tzinfo=FUSO)
        d=d.astimezone(FUSO)
        return d.isoformat(timespec='seconds'),d.strftime('%Y-%m')
    except (ValueError,TypeError):return None,None


def _centavos(valor):
    try:
        v=Decimal(str(valor or 0))
        return int((v*100).quantize(Decimal('1'))) if v.is_finite() and v>=0 else 0
    except (InvalidOperation,ValueError,OverflowError):return 0


def _primeiro(node,nome):
    return next((n for n in node.iter() if n.tag.split('}')[-1]==nome),None) if node is not None else None


def _texto(node,nome):
    n=_primeiro(node,nome)
    return (n.text or '').strip() if n is not None else ''


def ler_autorizado(path,cnpj_esperado):
    root=ET.parse(path).getroot()
    inf=_primeiro(root,'infNFe');prot=_primeiro(_primeiro(root,'protNFe'),'infProt')
    if inf is None or prot is None or _texto(prot,'cStat') not in ('100','150'):
        raise ValueError('O arquivo não contém XML autorizado com protocolo da SEFAZ.')
    chave=(inf.attrib.get('Id') or '').removeprefix('NFe')
    if not re.fullmatch(r'\d{44}',chave) or _texto(prot,'chNFe')!=chave or not _texto(prot,'nProt'):
        raise ValueError('A chave e o protocolo do XML não conferem.')
    emit=_primeiro(inf,'emit')
    if digitos(_texto(emit,'CNPJ'))!=digitos(cnpj_esperado) or not digitos(cnpj_esperado):
        raise ValueError('O emitente do XML não corresponde à empresa configurada.')
    ide=_primeiro(inf,'ide');modelo=_texto(ide,'mod')
    if modelo not in ('55','65'):raise ValueError('Modelo fiscal não suportado no arquivo de NF-e/NFC-e.')
    ambiente=_texto(ide,'tpAmb')
    if ambiente not in ('1','2'):raise ValueError('Ambiente de emissão ausente ou inválido no XML.')
    emitido,mes=_data(_texto(ide,'dhEmi') or _texto(ide,'dEmi'))
    if not mes:raise ValueError('O XML está sem data de emissão válida.')
    dest=_primeiro(inf,'dest')
    return dict(modelo=modelo,ambiente=ambiente,numero=_texto(ide,'nNF'),serie=_texto(ide,'serie'),chave=chave,protocolo=_texto(prot,'nProt'),
        destinatario=_texto(dest,'xNome') or 'Consumidor',emitido_em=emitido,mes=mes,
        valor_centavos=_centavos(_texto(_primeiro(inf,'ICMSTot'),'vNF')))


def _copiar(origem,destino):
    destino.parent.mkdir(parents=True,exist_ok=True)
    conteudo=origem.read_bytes();digest=sha256(conteudo).hexdigest()
    if destino.is_file():
        if sha256(destino.read_bytes()).hexdigest()!=digest:raise ValueError('Há outro conteúdo salvo com a mesma chave; o arquivo existente foi preservado.')
        return str(destino),digest
    nome=None
    try:
        with tempfile.NamedTemporaryFile(dir=destino.parent,prefix='.arquivo-',delete=False) as temp:
            nome=temp.name
            temp.write(conteudo)
            temp.flush();os.fsync(temp.fileno())
        try:os.link(nome,destino)
        except FileExistsError:
            if sha256(destino.read_bytes()).hexdigest()!=digest:raise ValueError('Há outro conteúdo salvo com a mesma chave; o arquivo existente foi preservado.')
    finally:
        if nome and os.path.exists(nome):os.unlink(nome)
    return str(destino),digest


def origens(db,origem=None,item_id=None):
    rows=[]
    if origem in (None,'Venda'):
        sql="""SELECT id origem_id,'Venda' origem,'65' modelo,nf_numero numero,nf_serie serie,nf_chave chave,nf_protocolo protocolo,
            cliente_nome destinatario,nf_emissao_tentativa_em emitido_em,nf_autorizado_em autorizado_em,total,
            nf_xml_path xml_origem,nf_danfe_path pdf_origem FROM vendas WHERE nf_status='Autorizada SEFAZ'"""
        rows += [dict(r) for r in db.execute(sql+(' AND id=?' if item_id is not None else ''),(item_id,) if item_id is not None else ())]
    if origem in (None,'Devolução'):
        sql="""SELECT id origem_id,'Devolução' origem,'55' modelo,nf_devolucao_numero numero,substr(nf_devolucao_chave,23,3) serie,
            nf_devolucao_chave chave,protocolo_autorizacao protocolo,fornecedor_nome destinatario,
            emissao_tentativa_em emitido_em,autorizado_em,
            (SELECT COALESCE(SUM(valor_total),0) FROM devolucao_itens WHERE devolucao_id=devolucoes.id) total,
            xml_autorizado_path xml_origem,danfe_path pdf_origem FROM devolucoes WHERE status='Autorizada SEFAZ'"""
        rows += [dict(r) for r in db.execute(sql+(' AND id=?' if item_id is not None else ''),(item_id,) if item_id is not None else ())]
    return rows


def registrar(get_db,raiz,registro,cnpj):
    r=dict(registro)
    chave=r.get('chave') if re.fullmatch(r'\d{44}',str(r.get('chave') or '')) else None
    with get_db() as db:
        anterior=db.execute('SELECT * FROM fiscal_documentos WHERE (origem=? AND origem_id=?) OR (chave IS NOT NULL AND chave=?) ORDER BY id LIMIT 1',
            (r['origem'],r['origem_id'],chave)).fetchone()
    if anterior and not anterior['aviso'] and arquivo_documento(raiz,anterior,'xml') and arquivo_documento(raiz,anterior,'pdf'):
        return anterior['id'],''
    data,mes=_data(r.get('emitido_em'))
    if not mes:data,mes=_data(r.get('autorizado_em'))
    dados=dict(modelo=str(r.get('modelo') or ''),ambiente='',numero=str(r.get('numero') or ''),serie=str(r.get('serie') or ''),
        chave=chave,protocolo=r.get('protocolo') or '',destinatario=r.get('destinatario') or 'Consumidor',
        emitido_em=data,mes=mes,valor_centavos=_centavos(r.get('total')))
    arquivos={k:(anterior[k] if anterior else None) for k in ('xml_path','pdf_path','xml_sha256','pdf_sha256')}
    avisos=[]
    original=(arquivo_documento(raiz,anterior,'xml') if anterior else None) or caminho_seguro(raiz,r.get('xml_origem'))
    try:
        if not original:raise ValueError('XML autorizado não encontrado no armazenamento.')
        parsed=ler_autorizado(original,cnpj)
        if chave and parsed['chave']!=chave:raise ValueError('O XML não corresponde à chave registrada na venda/devolução.')
        if parsed['modelo']!=dados['modelo']:raise ValueError('O modelo do XML não corresponde ao registro de origem.')
        if r.get('protocolo') and parsed['protocolo']!=str(r['protocolo']):raise ValueError('O protocolo do XML diverge do registro de origem.')
        dados.update(parsed)
        pasta=Path(raiz)/'emitidas'/parsed['mes']/('producao' if parsed['ambiente']=='1' else 'homologacao')/('NFCe' if parsed['modelo']=='65' else 'NFe')
        if not pasta.resolve().is_relative_to(Path(raiz).resolve()):raise ValueError('Pasta fiscal fora do armazenamento permitido.')
        base=('NFCe' if parsed['modelo']=='65' else 'NFe')+'-'+parsed['chave']
        arquivos['xml_path'],arquivos['xml_sha256']=_copiar(original,pasta/(base+'.xml'))
        pdf=caminho_seguro(raiz,r.get('pdf_origem'))
        cabecalho=b''
        if pdf:
            with pdf.open('rb') as f:cabecalho=f.read(5)
        if pdf and cabecalho==b'%PDF-':
            arquivos['pdf_path'],arquivos['pdf_sha256']=_copiar(pdf,pasta/(base+'-DANFE.pdf'))
        elif not (anterior and arquivo_documento(raiz,anterior,'pdf')):avisos.append('DANFE PDF não encontrado ou inválido.')
    except (OSError,ValueError,ET.ParseError) as exc:
        avisos.append(str(exc))
    dados.update(arquivos);dados['aviso']=' '.join(avisos)
    colunas=list(dados)
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        existente=db.execute('SELECT id FROM fiscal_documentos WHERE (origem=? AND origem_id=?) OR (chave IS NOT NULL AND chave=?) ORDER BY id LIMIT 1',
            (r['origem'],r['origem_id'],dados['chave'])).fetchone()
        if existente:
            numero=existente['id']
            db.execute('UPDATE fiscal_documentos SET '+','.join(k+'=?' for k in colunas)+' WHERE id=?',tuple(dados.values())+(numero,))
        else:
            numero=db.execute('INSERT INTO fiscal_documentos(origem,origem_id,registrado_em,'+','.join(colunas)+') VALUES ('+','.join('?' for _ in range(len(colunas)+3))+')',
                (r['origem'],r['origem_id'],agora(),*dados.values())).lastrowid
    return numero,dados['aviso']


def sincronizar(get_db,raiz,cnpj,origem=None,item_id=None):
    with get_db() as db:rows=origens(db,origem,item_id)
    return [registrar(get_db,raiz,r,cnpj) for r in rows]


def arquivo_documento(raiz,doc,tipo):
    campo='xml' if tipo=='xml' else 'pdf'
    caminho=caminho_seguro(raiz,doc[campo+'_path'])
    digest=doc[campo+'_sha256']
    if not caminho or not digest:
        return None
    try:
        if sha256(caminho.read_bytes()).hexdigest()!=digest:return None
    except OSError:return None
    return caminho


def montar_zip(raiz,docs,modo='completo'):
    arquivo=tempfile.SpooledTemporaryFile(max_size=8*1024*1024,mode='w+b')
    faltas=[];nomes=set();linhas=[]
    with zipfile.ZipFile(arquivo,'w',zipfile.ZIP_DEFLATED) as z:
        for d in docs:
            chave=d['chave'] or f"registro-{d['id']}"
            base=('NFCe' if d['modelo']=='65' else 'NFe')+'-'+chave
            for tipo,pasta,sufixo in [('xml','XML','.xml')]+([('pdf','DANFE','-DANFE.pdf')] if modo=='completo' else []):
                path=arquivo_documento(raiz,d,tipo)
                if not path:
                    faltas.append(f"Registro #{d['id']} | {d['modelo']} série {d['serie']} nº {d['numero']} | {chave}: {tipo.upper()} indisponível ou com integridade divergente.")
                    continue
                ambiente='Producao' if d['ambiente']=='1' else ('Homologacao' if d['ambiente']=='2' else 'Conferir')
                nome=ambiente+'/'+pasta+'/'+base+sufixo
                if nome not in nomes:z.write(path,nome);nomes.add(nome)
            linhas.append(['Produção' if d['ambiente']=='1' else ('Homologação' if d['ambiente']=='2' else 'A conferir'),d['modelo'],d['serie'],d['numero'],d['chave'] or '',d['emitido_em'] or '',d['protocolo'] or '',d['destinatario'] or '',format(Decimal(d['valor_centavos'])/100,'.2f').replace('.',',')])
        texto=io.StringIO(newline='');writer=csv.writer(texto,delimiter=';')
        writer.writerow(['Ambiente','Modelo','Série','Número','Chave','Emissão','Protocolo','Destinatário','Valor (R$)'])
        for linha in linhas:writer.writerow(["'"+str(v) if str(v).lstrip().startswith(('=','+','-','@')) else v for v in linha])
        z.writestr('RELACAO_NOTAS.csv',texto.getvalue().encode('utf-8-sig'))
        if faltas:z.writestr('PENDENCIAS.txt','\n'.join(faltas).encode('utf-8'))
    arquivo.seek(0)
    return arquivo,faltas
