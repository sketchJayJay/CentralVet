"""Controle de tentativas, horário e numeração da NFC-e."""
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from zoneinfo import ZoneInfo

FUSO = ZoneInfo('America/Sao_Paulo')
REJEICOES_ANTIGAS_SEGURAS = {'391','392','437','496','703','704','796','961','963'}
CODIGOS_INCERTOS = {'100','103','104','105','106','108','109','110','150','204','301','302','539','656'}


def agora():
    return datetime.now(FUSO).isoformat(timespec='seconds')


def migrar(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS nfce_tentativas (
            id INTEGER PRIMARY KEY AUTOINCREMENT, venda_id INTEGER NOT NULL,
            lote_id INTEGER, estado TEXT NOT NULL, iniciada_em TEXT NOT NULL,
            concluida_em TEXT, emissao_em TEXT, emissao_anterior_em TEXT, data_venda TEXT,
            serie INTEGER, numero INTEGER, diretorio TEXT, usuario TEXT,
            chave TEXT, protocolo TEXT, cstat TEXT, motivo TEXT,
            xml_assinado_path TEXT, xml_path TEXT, danfe_path TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS nfce_tentativa_aberta
            ON nfce_tentativas(venda_id) WHERE concluida_em IS NULL;
        CREATE TABLE IF NOT EXISTS nfce_lotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, criado_em TEXT NOT NULL,
            usuario TEXT, assinatura TEXT NOT NULL, estado TEXT NOT NULL DEFAULT 'Pronto'
        );
        CREATE TABLE IF NOT EXISTS nfce_lote_itens (
            id INTEGER PRIMARY KEY AUTOINCREMENT, lote_id INTEGER NOT NULL,
            venda_id INTEGER NOT NULL, estado TEXT NOT NULL DEFAULT 'Pendente',
            motivo TEXT, cstat TEXT, tentativa_id INTEGER, iniciada_em TEXT,
            UNIQUE(lote_id,venda_id)
        );
    ''')


def autorizada(venda):
    v=dict(venda)
    return bool(v.get('nf_status')=='Autorizada SEFAZ' or v.get('nf_protocolo') or v.get('nf_chave'))


def bloqueio(db,venda):
    v=dict(venda)
    if autorizada(v):
        return 'Esta venda já tem documento fiscal. Data e emissão estão protegidas.'
    aberta=db.execute('SELECT estado FROM nfce_tentativas WHERE venda_id=? AND concluida_em IS NULL',(v['id'],)).fetchone()
    if aberta:
        return 'Há uma tentativa em andamento ou com retorno incerto. Consulte sua situação antes de reenviar.'
    status=v.get('nf_status') or ''
    if status in ('','Não emitida') and not v.get('nf_numero'):
        return ''
    ultima=db.execute("SELECT estado,cstat FROM nfce_tentativas WHERE venda_id=? AND estado!='Data ajustada' ORDER BY id DESC LIMIT 1",(v['id'],)).fetchone()
    if ultima and ultima['estado']=='Falha local' and status=='Falha local':
        return ''
    cstat=str(v.get('nf_cstat') or '')
    if status=='Rejeitada SEFAZ' and (cstat in REJEICOES_ANTIGAS_SEGURAS or
            (ultima and ultima['estado']=='Rejeitada' and ultima['cstat']==cstat)):
        return ''
    return 'O retorno fiscal anterior precisa ser conferido antes de mudar a data ou reenviar.'


def selecionavel(db,venda):
    v=dict(venda)
    return (v.get('status')=='Pago' and v.get('nf_tipo') in (None,'','NFC-e','Sem nota agora')
            and not bloqueio(db,v))


def ids_validos(valores):
    try:
        if not valores or len(valores)>5000:
            raise ValueError
        ids=sorted(set(int(v) for v in valores))
        if any(v<=0 for v in ids):
            raise ValueError
        return ids
    except (TypeError,ValueError):
        raise ValueError('Selecione de 1 a 5.000 vendas válidas.')


def assinatura(venda,itens):
    return sha256(json.dumps([dict(venda),[dict(i) for i in itens]],sort_keys=True,ensure_ascii=False).encode()).hexdigest()


ITENS_SQL='''SELECT vi.*,p.codigo,p.unidade,p.ncm,p.cfop,p.cst_csosn,p.ean,p.cest
    FROM venda_itens vi LEFT JOIN produtos p ON p.id=vi.produto_id
    WHERE vi.venda_id=? ORDER BY vi.id'''


def reservar(get_db,venda_id,esperada,raiz,lote_id=None,usuario=''):
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        venda=db.execute('SELECT * FROM vendas WHERE id=?',(venda_id,)).fetchone()
        if not venda:
            raise ValueError('Venda não encontrada.')
        motivo=bloqueio(db,venda)
        if motivo:
            raise ValueError(motivo)
        itens=db.execute(ITENS_SQL,(venda_id,)).fetchall()
        if assinatura(venda,itens)!=esperada:
            raise ValueError('A venda mudou durante a preparação. Atualize a página e tente novamente.')
        cfg=db.execute('SELECT * FROM fiscal_config WHERE id=1').fetchone()
        serie=int(venda['nf_serie'] or cfg['nfce_serie'] or 1)
        numero=int(venda['nf_numero'] or 0)
        def ocupado(n):
            return db.execute('''SELECT id FROM vendas WHERE id<>? AND nf_tipo='NFC-e'
                AND CAST(COALESCE(nf_serie,'1') AS INTEGER)=? AND CAST(nf_numero AS INTEGER)=?''',
                (venda_id,serie,n)).fetchone() or db.execute('''SELECT id FROM nfce_tentativas
                WHERE venda_id<>? AND serie=? AND numero=? AND estado!='Data ajustada' ''',
                (venda_id,serie,n)).fetchone()
        if not numero or ocupado(numero):
            max_v=db.execute("SELECT COALESCE(MAX(CAST(nf_numero AS INTEGER)),0) FROM vendas WHERE nf_tipo='NFC-e' AND CAST(COALESCE(nf_serie,'1') AS INTEGER)=?",(serie,)).fetchone()[0]
            max_t=db.execute("SELECT COALESCE(MAX(numero),0) FROM nfce_tentativas WHERE serie=? AND estado!='Data ajustada'",(serie,)).fetchone()[0]
            numero=max(int(cfg['nfce_numero_inicial'] or 1),max_v+1,max_t+1)
        if not 0<numero<=999999999 or not 0<=serie<=999:
            raise ValueError('Confira a série e a numeração da NFC-e na configuração fiscal.')
        momento=agora()
        tid=db.execute('''INSERT INTO nfce_tentativas
            (venda_id,lote_id,estado,iniciada_em,emissao_em,emissao_anterior_em,data_venda,serie,numero,usuario)
            VALUES (?,?,'Em andamento',?,?,?,?,?,?,?)''',
            (venda_id,lote_id,momento,momento,venda['nf_emissao_tentativa_em'],venda['data'],serie,numero,usuario)).lastrowid
        diretorio=str(Path(raiz)/str(venda_id)/f'tentativa-{tid}')
        db.execute('UPDATE nfce_tentativas SET diretorio=? WHERE id=?',(diretorio,tid))
        db.execute("UPDATE vendas SET nf_tipo='NFC-e',nf_status='Em processamento SEFAZ',nf_numero=?,nf_serie=?,nf_emissao_tentativa_em=? WHERE id=?",(str(numero),str(serie),momento,venda_id))
        db.execute('UPDATE fiscal_config SET nfce_numero_inicial=? WHERE id=1',
            (str(max(int(cfg['nfce_numero_inicial'] or 1),numero+1)),))
        return dict(db.execute('SELECT * FROM nfce_tentativas WHERE id=?',(tid,)).fetchone())


def diario(tentativa):
    try:
        dados=json.loads((Path(tentativa['diretorio'])/'transmissao.json').read_text())
        return dados if isinstance(dados,dict) else {}
    except (OSError,ValueError,TypeError):
        return {}


def finalizar(get_db,tentativa,resultado):
    resultado=dict(resultado)
    anterior=diario(tentativa)
    for chave in ('transmission_started','chave','signed_xml_path'):
        if chave not in resultado and chave in anterior:
            resultado[chave]=anterior[chave]
    cstat=str(resultado.get('cStat') or '')
    motivo=str(resultado.get('xMotivo') or resultado.get('error') or 'Retorno fiscal não identificado.')
    autorizou=bool(resultado.get('authorized') and cstat in ('100','150') and resultado.get('protocolo') and resultado.get('chave'))
    rejeitou=(cstat not in CODIGOS_INCERTOS and bool(cstat) and
        (resultado.get('definitive_rejection') is True or cstat in REJEICOES_ANTIGAS_SEGURAS))
    if autorizou:
        estado,status='Autorizada','Autorizada SEFAZ'
    elif rejeitou:
        estado,status='Rejeitada','Rejeitada SEFAZ'
    elif resultado.get('transmission_started') is False:
        estado,status='Falha local','Falha local'
    else:
        estado,status='Incerta','Retorno incerto SEFAZ'
    concluida=agora() if estado!='Incerta' else None
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        atual=db.execute('SELECT * FROM nfce_tentativas WHERE id=?',(tentativa['id'],)).fetchone()
        venda=db.execute('SELECT * FROM vendas WHERE id=?',(tentativa['venda_id'],)).fetchone()
        if atual['concluida_em'] and atual['estado']!='Data ajustada':
            if atual['estado']=='Autorizada':
                return resultado|{'authorized':True,'cStat':atual['cstat'],'chave':atual['chave'],
                    'protocolo':atual['protocolo'],'state':'Autorizada','tentativa_id':atual['id']}
            return resultado|{'state':atual['estado'],'tentativa_id':atual['id']}
        if autorizada(venda) and not autorizou:
            return resultado|{'state':'Autorizada','authorized':True,'tentativa_id':atual['id']}
        db.execute('''UPDATE nfce_tentativas SET estado=?,concluida_em=?,chave=?,protocolo=?,cstat=?,motivo=?,
            xml_assinado_path=?,xml_path=?,danfe_path=? WHERE id=?''',
            (estado,concluida,resultado.get('chave'),resultado.get('protocolo'),cstat,motivo,
             resultado.get('signed_xml_path'),resultado.get('xml_path'),resultado.get('danfe_path'),tentativa['id']))
        db.execute('UPDATE vendas SET nf_status=?,nf_cstat=?,nf_motivo=?,nf_obs=? WHERE id=?',
            (status,cstat,motivo,motivo,tentativa['venda_id']))
        if autorizou:
            db.execute('''UPDATE vendas SET nf_chave=?,nf_protocolo=?,nf_xml_path=?,nf_danfe_path=?,nf_autorizado_em=? WHERE id=?''',
                (resultado['chave'],resultado['protocolo'],resultado.get('xml_path') or '',resultado.get('danfe_path') or '',agora(),tentativa['venda_id']))
    return resultado|{'state':estado,'authorized':autorizou,'tentativa_id':tentativa['id'],'numero':tentativa['numero']}


def atualizar_datas(get_db,valores,usuario=''):
    ids=ids_validos(valores)
    alteradas,ignoradas=[],[]
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        momento=agora()
        for venda_id in ids:
            venda=db.execute('SELECT * FROM vendas WHERE id=?',(venda_id,)).fetchone()
            motivo=bloqueio(db,venda) if venda else 'Venda não encontrada.'
            if not venda or motivo:
                ignoradas.append({'id':venda_id,'motivo':motivo})
                continue
            db.execute('''INSERT INTO nfce_tentativas
                (venda_id,estado,iniciada_em,concluida_em,emissao_em,emissao_anterior_em,data_venda,serie,numero,usuario,motivo)
                VALUES (?,'Data ajustada',?,?,?,?,?,?,?,?,?)''',
                (venda_id,momento,momento,momento,venda['nf_emissao_tentativa_em'],venda['data'],
                 venda['nf_serie'],venda['nf_numero'],usuario,'Data fiscal preparada para hoje.'))
            db.execute('UPDATE vendas SET nf_emissao_tentativa_em=? WHERE id=?',(momento,venda_id))
            alteradas.append(venda_id)
    return alteradas,ignoradas
