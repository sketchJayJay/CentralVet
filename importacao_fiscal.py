"""Códigos originais de ICMS da nota e importação sem duplicar entradas."""
from datetime import datetime
from pathlib import Path
import math
import os
import tempfile
from zoneinfo import ZoneInfo

CAMPOS_XML = ('ncm_xml','cfop_xml','cst_csosn_xml','tipo_icms_xml','origem_icms_xml','chave_fiscal_xml','fiscal_xml_atualizado_em')


def migrar(db,add_column):
    for campo in CAMPOS_XML:
        add_column(db,'produtos',campo,'TEXT')


def local(tag):
    return tag.split('}')[-1]


def icms_do_item(det):
    imposto=next((n for n in list(det) if local(n.tag)=='imposto'),None)
    return next((n for n in list(imposto) if local(n.tag)=='ICMS'),None) if imposto is not None else None


def dados_icms(det):
    icms=icms_do_item(det)
    def ler(nome):
        return next(((n.text or '').strip() for n in icms.iter() if local(n.tag)==nome),'') if icms is not None else ''
    csosn,cst=ler('CSOSN'),ler('CST')
    return dict(codigo=csosn or cst,tipo='CSOSN' if csosn else ('CST' if cst else ''),origem=ler('orig'))


def guardar_xml(raiz,conteudo,hash_xml):
    pasta=Path(raiz);pasta.mkdir(parents=True,exist_ok=True)
    destino=pasta/f'entrada-{hash_xml}.xml'
    if destino.is_file():return str(destino)
    temporario=None
    try:
        with tempfile.NamedTemporaryFile(dir=pasta,prefix='.xml-',delete=False) as f:
            temporario=f.name;f.write(conteudo);f.flush();os.fsync(f.fileno())
        os.replace(temporario,destino)
    finally:
        if temporario and os.path.exists(temporario):os.unlink(temporario)
    return str(destino)


def encontrar_produto(db,item):
    prod=None
    if item['codigo']:prod=db.execute('SELECT * FROM produtos WHERE ativo=1 AND codigo=? LIMIT 1',(item['codigo'],)).fetchone()
    if not prod and item['ean'] and item['ean']!='SEM GTIN':prod=db.execute('SELECT * FROM produtos WHERE ativo=1 AND ean=? LIMIT 1',(item['ean'],)).fetchone()
    if not prod:prod=db.execute('SELECT * FROM produtos WHERE ativo=1 AND nome=? LIMIT 1',(item['nome'],)).fetchone()
    return prod


def aplicar(db,dados,modo,substituir,aplicar_margem,margem,hoje,xml_hash,xml_path,normalizar_unidade):
    if modo not in ('completa','fiscal'):raise ValueError('Selecione um modo de importação válido.')
    if modo=='completa':
        for item in dados['itens']:
            valores=[item['quantidade'],item['valor_unitario'],item['valor_total']]
            if not all(math.isfinite(v) and v>=0 for v in valores) or item['quantidade']<=0:
                raise ValueError('O XML contém quantidade ou valor inválido em um produto. Nenhuma entrada foi lançada.')
        if aplicar_margem and (not math.isfinite(margem) or margem<0):raise ValueError('Informe uma margem válida.')
    chave=dados['chave'] or xml_hash
    anterior=db.execute('SELECT * FROM xml_imports WHERE chave=? OR xml_hash=?',(chave,xml_hash)).fetchone()
    if anterior and modo=='completa':
        raise ValueError(f"Essa nota já foi importada, no registro #{anterior['id']}. Para corrigir os códigos, escolha Somente dados fiscais, sem estoque.")
    fornecedor_id=None
    if modo=='completa' and (dados['fornecedor_nome'] or dados['fornecedor_cnpj']):
        digits=''.join(c for c in dados['fornecedor_cnpj'] if c.isdigit())
        fornecedor=db.execute('SELECT * FROM fornecedores WHERE cpf_cnpj LIKE ? LIMIT 1',(f'%{digits[-8:]}%',)).fetchone() if digits else None
        if not fornecedor and dados['fornecedor_nome']:
            fornecedor=db.execute('SELECT * FROM fornecedores WHERE nome LIKE ? LIMIT 1',(dados['fornecedor_nome'],)).fetchone()
        fornecedor_id=fornecedor['id'] if fornecedor else db.execute('INSERT INTO fornecedores(nome,cpf_cnpj,observacao) VALUES (?,?,?)',
            (dados['fornecedor_nome'] or 'Fornecedor do XML',dados['fornecedor_cnpj'],'Cadastrado pela importação de XML.')).lastrowid
    novos=atualizados=nao_encontrados=0
    momento=datetime.now(ZoneInfo('America/Sao_Paulo')).isoformat(timespec='seconds')
    for item in dados['itens']:
        prod=encontrar_produto(db,item)
        if modo=='fiscal' and not prod:
            nao_encontrados+=1;continue
        cfop=item['cfop'] if not prod or substituir else (prod['cfop'] or item['cfop'])
        cst=item['cst_csosn'] if not prod or substituir else (prod['cst_csosn'] or item['cst_csosn'])
        if prod:
            cfop=cfop or prod['cfop'];cst=cst or prod['cst_csosn']
            pid=prod['id'];ncm=item['ncm'] or prod['ncm']
        else:
            ncm=item['ncm']
        if modo=='completa':
            qtd=item['quantidade'];custo=item['valor_unitario'] or (item['valor_total']/qtd if qtd else 0)
            preco=custo*(1+margem/100) if aplicar_margem and margem>0 else (prod['preco_venda'] or 0 if prod else 0)
            margem_atual=margem if aplicar_margem and margem>0 else (prod['margem_lucro'] or 0 if prod else 0)
            if prod:
                db.execute('''UPDATE produtos SET nome=?,unidade=?,preco_custo=?,preco_venda=?,margem_lucro=?,estoque=estoque+?,
                    ncm=?,cfop=?,cst_csosn=?,ean=?,cest=?,ultima_chave_xml=?,fornecedor_id=? WHERE id=?''',
                    (item['nome'],normalizar_unidade(item['unidade']),custo,preco,margem_atual,qtd,ncm,cfop,cst,item['ean'],item['cest'],chave,fornecedor_id,pid))
            else:
                pid=db.execute('''INSERT INTO produtos(codigo,nome,categoria,unidade,preco_custo,preco_venda,margem_lucro,estoque,
                    estoque_minimo,ncm,cfop,cst_csosn,ean,cest,ultima_chave_xml,fornecedor_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (item['codigo'],item['nome'],'Importado XML',normalizar_unidade(item['unidade']),custo,preco,margem_atual,qtd,0,
                     ncm,cfop,cst,item['ean'],item['cest'],chave,fornecedor_id)).lastrowid
                novos+=1
            db.execute('''INSERT INTO estoque_mov(data,produto_id,tipo,quantidade,custo_unit,valor_total,origem,observacao)
                VALUES (?,?,'Entrada',?,?,?,?,?)''',(dados['emissao'] or hoje,pid,qtd,custo,item['valor_total'] or qtd*custo,
                f"XML NF-e {dados['numero'] or ''}".strip(),f'Importado pelo XML. Chave: {chave}'))
        else:
            db.execute('UPDATE produtos SET ncm=?,cfop=?,cst_csosn=? WHERE id=?',(ncm,cfop,cst,pid))
        db.execute('''UPDATE produtos SET ncm_xml=?,cfop_xml=?,cst_csosn_xml=?,tipo_icms_xml=?,origem_icms_xml=?,
            chave_fiscal_xml=?,fiscal_xml_atualizado_em=? WHERE id=?''',
            (item['ncm'],item['cfop'],item['cst_csosn'],item.get('tipo_icms',''),item.get('origem_icms',''),chave,momento,pid))
        if prod:atualizados+=1
    if modo=='completa':
        db.execute('''INSERT INTO xml_imports(chave,xml_hash,numero,serie,emissao,fornecedor_nome,fornecedor_cnpj,total,itens_qtd,observacao,xml_path)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)''',(chave,xml_hash,dados['numero'],dados['serie'],dados['emissao'],dados['fornecedor_nome'],dados['fornecedor_cnpj'],
            dados['total'],len(dados['itens']),f'{novos} novo(s), {atualizados} atualizado(s). NCM, CFOP e CST/CSOSN do ICMS capturados.',xml_path))
    elif anterior:
        db.execute('UPDATE xml_imports SET xml_path=? WHERE id=?',(xml_path,anterior['id']))
    return dict(novos=novos,atualizados=atualizados,nao_encontrados=nao_encontrados)
