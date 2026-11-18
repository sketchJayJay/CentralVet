"""Importação e arquivo fiscal com documentos sintéticos; sem conexão à SEFAZ."""
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile

import test_pagamento_fiscal as fixtures
import arquivo_fiscal as af

CNPJ='12345678000195'


def chave(modelo='65',numero=1):
    return '312611'+CNPJ+modelo+'001'+str(numero).zfill(9)+'1'+'12345678'+'0'


def entrada(icms='<ICMS><ICMS00><orig>0</orig><CST>00</CST></ICMS00></ICMS>',namespace=True,codigo='TESTE',ncm='01012100',cfop='6102',quantidade='3',numero=100):
    ns=' xmlns="http://www.portalfiscal.inf.br/nfe"' if namespace else ''
    return f'''<nfeProc{ns}><NFe><infNFe Id="NFe{chave('55',numero)}"><ide><nNF>{numero}</nNF><serie>1</serie><dhEmi>2026-11-18T10:00:00-03:00</dhEmi><mod>55</mod></ide><emit><xNome>Fornecedor teste</xNome><CNPJ>99999999000191</CNPJ></emit><det nItem="1"><prod><cProd>{codigo}</cProd><cEAN>SEM GTIN</cEAN><xProd>Produto {codigo}</xProd><NCM>{ncm}</NCM><CFOP>{cfop}</CFOP><uCom>UN</uCom><qCom>{quantidade}</qCom><vUnCom>12.50</vUnCom><vProd>37.50</vProd></prod><imposto><PIS><PISOutr><CST>49</CST></PISOutr></PIS>{icms}<COFINS><COFINSOutr><CST>99</CST></COFINSOutr></COFINS></imposto></det><total><ICMSTot><vNF>37.50</vNF></ICMSTot></total></infNFe></NFe></nfeProc>'''.encode()


def autorizada(modelo='65',numero=1,data='2026-11-18T10:00:00-03:00',cnpj=CNPJ,ambiente='1',cstat='100',protocolo='123456789012345'):
    k=chave(modelo,numero)
    return f'''<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe"><NFe><infNFe Id="NFe{k}"><ide><mod>{modelo}</mod><nNF>{numero}</nNF><serie>1</serie><dhEmi>{data}</dhEmi><tpAmb>{ambiente}</tpAmb></ide><emit><CNPJ>{cnpj}</CNPJ></emit><dest><xNome>Cliente teste</xNome></dest><total><ICMSTot><vNF>45.00</vNF></ICMSTot></total></infNFe></NFe><protNFe><infProt><cStat>{cstat}</cStat><chNFe>{k}</chNFe><nProt>{protocolo}</nProt></infProt></protNFe></nfeProc>'''.encode()


class Base(unittest.TestCase):
    def setUp(self):
        self.base=fixtures.PagamentoFiscalTests();self.base.setUp();self.addCleanup(self.base.doCleanups)
        self.cv=fixtures.cv;self.client=self.base.client;self.raiz=Path(self.base.temp.name)
        for nome,valor in [('FISCAL_DIR',str(self.raiz)),('FISCAL_ORIGINAL_DIR',str(self.raiz/'originais')),('FISCAL_NFE_DIR',str(self.raiz/'nfe'))]:
            p=patch.object(self.cv,nome,valor);p.start();self.addCleanup(p.stop)
        self.cv.exec_sql('UPDATE fiscal_config SET cnpj=? WHERE id=1',(CNPJ,))

    def produto(self):return dict(self.cv.fetch_one('SELECT * FROM produtos WHERE id=?',(self.base.produto,)))

    def importar(self,xml=None,**campos):
        return self.client.post('/xml/importar',data={'xml_file':(BytesIO(xml or entrada()),'compra.xml'),**campos},follow_redirects=True)

    def fonte(self,venda=None,modelo='65',numero=1,**xmlargs):
        pasta=self.raiz/'fontes'/str(numero);pasta.mkdir(parents=True,exist_ok=True)
        x=pasta/'autorizada.xml';x.write_bytes(autorizada(modelo,numero,**xmlargs))
        p=pasta/'danfe.pdf';p.write_bytes(b'%PDF-1.4\nDANFE SINTETICO PARA TESTE\n%%EOF')
        if modelo=='65':
            venda=venda or self.base.venda()
            self.cv.exec_sql("UPDATE vendas SET nf_status='Autorizada SEFAZ',nf_chave=?,nf_protocolo='123456789012345',nf_numero=?,nf_xml_path=?,nf_danfe_path=?,nf_autorizado_em='2026-11-18T10:01:00-03:00' WHERE id=?",(chave(modelo,numero),str(numero),str(x),str(p),venda))
        else:
            with self.cv.get_db() as db:
                venda=db.execute("INSERT INTO devolucoes(data,status,nf_devolucao_chave,nf_devolucao_numero,protocolo_autorizacao,xml_autorizado_path,danfe_path,emissao_tentativa_em) VALUES ('2026-10-01','Autorizada SEFAZ',?,?,'123456789012345',?,?,'2026-11-18T10:00:00-03:00')",(chave(modelo,numero),str(numero),str(x),str(p))).lastrowid
        return venda,x,p

    def sync(self):return af.sincronizar(self.cv.get_db,str(self.raiz),CNPJ)
    def doc(self,numero=1):return self.cv.fetch_one('SELECT * FROM fiscal_documentos WHERE chave=?',(chave('65',numero),))


class ImportacaoFiscalTests(Base):
    def test_icms_cst_nao_confunde_com_pis_cofins_e_preserva_zeros(self):
        for namespace in (True,False):
            item=self.cv.parse_nfe_xml(entrada(namespace=namespace))['itens'][0]
            self.assertEqual((item['ncm'],item['cfop'],item['cst_csosn'],item['tipo_icms'],item['origem_icms']),('01012100','6102','00','CST','0'))

    def test_csosn_e_ausencia_icms(self):
        for imposto,esperado,tipo in [('<ICMS><ICMSSN102><orig>1</orig><CSOSN>102</CSOSN></ICMSSN102></ICMS>','102','CSOSN'),('','','')]:
            item=self.cv.parse_nfe_xml(entrada(icms=imposto))['itens'][0]
            self.assertEqual(item['cst_csosn'],esperado);self.assertEqual(item['tipo_icms'],tipo)
            if not imposto:self.assertFalse(item['tem_st'])

    def test_importacao_completa_guarda_codigos_e_original(self):
        r=self.importar();self.assertEqual(r.status_code,200)
        p=self.produto()
        self.assertEqual(p['estoque'],21);self.assertEqual(p['ncm'],'01012100')
        self.assertEqual((p['ncm_xml'],p['cfop_xml'],p['cst_csosn_xml'],p['tipo_icms_xml']),('01012100','6102','00','CST'))
        self.assertEqual(p['cst_csosn'],'102')
        row=self.cv.fetch_one('SELECT * FROM xml_imports')
        self.assertEqual(Path(row['xml_path']).read_bytes(),entrada())
        self.assertEqual(row['chave'],chave('55',100))
        self.assertIn(b'NCM 01012100',r.data)

    def test_novo_produto_recebe_tres_codigos(self):
        self.importar(entrada(codigo='NOVO'))
        p=self.cv.fetch_one("SELECT * FROM produtos WHERE codigo='NOVO'")
        self.assertEqual((p['ncm'],p['cfop'],p['cst_csosn'],p['estoque']),('01012100','6102','00',3))

    def test_mesma_nota_nao_duplica_entrada(self):
        self.importar();antes=self.base.snapshot()
        r=self.importar()
        self.assertIn('já foi importada'.encode(),r.data)
        self.assertEqual(antes,self.base.snapshot())
        self.assertEqual(self.cv.fetch_one('SELECT COUNT(*) FROM xml_imports')[0],1)

    def test_fiscal_reimporta_sem_estoque_preco_custo_fornecedor_ou_financeiro(self):
        self.importar();self.cv.exec_sql("UPDATE produtos SET cfop='5405',cst_csosn='500',ncm='' WHERE id=?",(self.base.produto,))
        p=self.produto();antes=self.base.snapshot()
        fornecedores=self.cv.fetch_one('SELECT COUNT(*) FROM fornecedores')[0]
        for _ in range(2):
            r=self.importar(modo_importacao='fiscal',aplicar_margem='1',margem_lucro='900')
            self.assertIn(b'sem movimentar estoque',r.data)
        novo=self.produto()
        for c in ('estoque','preco_custo','preco_venda','margem_lucro','nome','fornecedor_id','ultima_chave_xml'):self.assertEqual(novo[c],p[c],c)
        self.assertEqual((novo['ncm'],novo['cfop'],novo['cst_csosn']),('01012100','5405','500'))
        for t in ('estoque_mov','financeiro','financeiro_baixas','venda_itens'):self.assertEqual(antes[t],self.base.snapshot()[t])
        self.assertEqual(self.cv.fetch_one('SELECT COUNT(*) FROM fornecedores')[0],fornecedores)
        self.assertEqual(self.cv.fetch_one('SELECT COUNT(*) FROM xml_imports')[0],1)

    def test_substituir_explicito_e_codigos_ausentes_nao_apagam_configuracao(self):
        self.cv.exec_sql("UPDATE produtos SET cfop='5102',cst_csosn='102' WHERE id=?",(self.base.produto,))
        self.importar(modo_importacao='fiscal',substituir_codigos='1')
        self.assertEqual((self.produto()['cfop'],self.produto()['cst_csosn']),('6102','00'))
        self.importar(entrada(icms='',cfop='',ncm=''),modo_importacao='fiscal',substituir_codigos='1')
        self.assertEqual((self.produto()['cfop'],self.produto()['cst_csosn'],self.produto()['ncm']),('6102','00','01012100'))

    def test_fiscal_sem_importacao_anterior_nao_impede_entrada_posterior(self):
        self.importar(modo_importacao='fiscal')
        self.assertEqual(self.cv.fetch_one('SELECT COUNT(*) FROM xml_imports')[0],0)
        self.importar();self.assertEqual(self.produto()['estoque'],21)

    def test_fiscal_ignora_produto_nao_encontrado_sem_criar_fornecedor(self):
        r=self.importar(entrada(codigo='DESCONHECIDO'),modo_importacao='fiscal')
        self.assertIn('não encontrados no cadastro'.encode(),r.data)
        self.assertEqual(self.cv.fetch_one('SELECT COUNT(*) FROM produtos')[0],1)
        self.assertEqual(self.cv.fetch_one('SELECT COUNT(*) FROM fornecedores')[0],0)

    def test_xml_invalido_e_quantidade_negativa_nao_modificam_cadastro(self):
        antes=self.base.snapshot()
        for xml in (b'<NFe>',entrada(quantidade='-5'),entrada(quantidade='NaN')):
            r=self.importar(xml)
            self.assertIn('Não foi possível importar'.encode(),r.data)
            self.assertEqual(antes,self.base.snapshot())

    def test_dados_originais_aparecem_no_cadastro(self):
        self.importar(modo_importacao='fiscal')
        r=self.client.get(f'/produtos/{self.base.produto}/editar')
        self.assertEqual(r.status_code,200)
        self.assertIn(b'CST do ICMS no XML',r.data);self.assertIn(b'value="00"',r.data)


class ArquivoFiscalTests(Base):
    def test_recupera_anteriores_sem_duplicar_e_preserva_financeiro(self):
        self.fonte(numero=1);self.fonte(modelo='55',numero=2)
        antes=self.base.snapshot()
        for _ in range(2):
            r=self.client.get('/fiscal/notas?mes=2026-11')
            self.assertEqual(r.status_code,200)
            self.assertIn(b'NFC-e n',r.data);self.assertIn(b'NF-e n',r.data)
        self.assertEqual(self.cv.fetch_one('SELECT COUNT(*) FROM fiscal_documentos')[0],2)
        self.assertEqual(self.doc()['mes'],'2026-11')
        self.assertEqual(antes,self.base.snapshot())

    def test_mes_usa_emissao_xml_no_fuso_de_brasilia(self):
        self.fonte(numero=1,data='2026-12-01T01:30:00Z')
        self.fonte(numero=2,data='2026-12-01T03:30:00Z')
        self.sync()
        self.assertEqual(self.doc(1)['mes'],'2026-11');self.assertEqual(self.doc(2)['mes'],'2026-12')
        r=self.client.get('/fiscal/notas/pacote.zip?mes=2026-11')
        with zipfile.ZipFile(BytesIO(r.data)) as z:
            self.assertTrue(any(chave('65',1) in f for f in z.namelist()))
            self.assertFalse(any(chave('65',2) in f for f in z.namelist()))
        r.close()

    def test_pacote_contem_xml_pdf_reais_e_relacao_dos_dois_modelos(self):
        self.fonte(numero=1);self.fonte(modelo='55',numero=2)
        r=self.client.get('/fiscal/notas/pacote.zip?mes=2026-11')
        self.assertEqual(r.status_code,200);self.assertEqual(r.headers['X-Fiscal-Pendencias'],'0')
        with zipfile.ZipFile(BytesIO(r.data)) as z:
            nomes=z.namelist()
            self.assertEqual(sum(n.endswith('.xml') for n in nomes),2)
            self.assertEqual(sum(n.endswith('.pdf') for n in nomes),2)
            self.assertEqual(z.read('Producao/XML/NFCe-'+chave()+'.xml'),autorizada())
            self.assertIn(b'45,',z.read('RELACAO_NOTAS.csv'))
            self.assertNotIn('PENDENCIAS.txt',nomes)
            self.assertFalse(any(n.endswith(('.db','.pfx','.json')) for n in nomes))
        r.close()

    def test_xml_somente_e_homologacao_identificada(self):
        self.fonte(ambiente='2')
        r=self.client.get('/fiscal/notas/pacote.zip?mes=2026-11&modo=xml')
        with zipfile.ZipFile(BytesIO(r.data)) as z:
            self.assertIn('Homologacao/XML/NFCe-'+chave()+'.xml',z.namelist())
            self.assertFalse(any(n.endswith('.pdf') for n in z.namelist()))
        r.close()
        html=self.client.get('/fiscal/notas?mes=2026-11').data
        self.assertIn('Homologação'.encode(),html)

    def test_copia_arquivada_continua_disponivel_sem_fonte_original(self):
        _,x,p=self.fonte();self.sync();x.unlink();p.unlink()
        self.client.get('/fiscal/notas?mes=2026-11')
        d=self.doc()
        for tipo in ('xml','pdf'):
            r=self.client.get(f"/fiscal/notas/{d['id']}/{tipo}")
            self.assertEqual(r.status_code,200);self.assertIn('no-store',r.headers['Cache-Control']);r.close()

    def test_pdf_ausente_e_exportacao_parcial_explicita(self):
        _,x,p=self.fonte();p.unlink()
        r=self.client.get('/fiscal/notas/pacote.zip?mes=2026-11')
        self.assertIn('PARCIAL',r.headers['X-Arquivo-Nome']);self.assertEqual(r.headers['X-Fiscal-Pendencias'],'1')
        with zipfile.ZipFile(BytesIO(r.data)) as z:self.assertIn(b'PDF',z.read('PENDENCIAS.txt'))
        r.close()
        self.assertIn(b'PDF pendente',self.client.get('/fiscal/notas?mes=2026-11').data)

    def test_xml_de_terceiro_rejeitado_ou_chave_divergente_nao_entra_no_pacote(self):
        for numero,kwargs in [(1,{'cnpj':'99999999000191'}),(2,{'cstat':'204'}),(3,{'protocolo':''})]:self.fonte(numero=numero,**kwargs)
        _,x,_=self.fonte(numero=4);x.write_bytes(autorizada(numero=99))
        self.sync()
        with self.cv.get_db() as db:
            for d in db.execute('SELECT * FROM fiscal_documentos'):self.assertFalse(d['xml_path']);self.assertTrue(d['aviso'])
        r=self.client.get('/fiscal/notas/pacote.zip?mes=2026-10')
        with zipfile.ZipFile(BytesIO(r.data)) as z:self.assertFalse(any(n.endswith('.xml') for n in z.namelist()))
        r.close()

    def test_integridade_e_caminho_fora_da_raiz_bloqueiam_download(self):
        self.fonte();self.sync();d=self.doc();Path(d['xml_path']).write_bytes(b'alterado')
        r=self.client.get(f"/fiscal/notas/{d['id']}/xml");self.assertEqual(r.status_code,404)
        self.cv.exec_sql('UPDATE fiscal_documentos SET xml_path=?,xml_sha256=? WHERE id=?',('/etc/hostname',sha256(Path('/etc/hostname').read_bytes()).hexdigest(),d['id']))
        self.assertEqual(self.client.get(f"/fiscal/notas/{d['id']}/xml").status_code,404)
        self.assertEqual(self.client.get(f"/fiscal/notas/{d['id']}/pfx").status_code,404)

    def test_arquivo_repetido_mesma_chave_nao_gera_duas_notas(self):
        self.fonte();self.fonte(numero=1);self.sync()
        self.assertEqual(self.cv.fetch_one('SELECT COUNT(*) FROM fiscal_documentos')[0],1)

    def test_rotas_exigem_login_e_validam_mes(self):
        anon=self.cv.app.test_client()
        for url in ('/fiscal/notas','/fiscal/notas/pacote.zip?mes=2026-11','/fiscal/notas/1/xml','/fiscal/notas/1/pacote.zip'):
            self.assertEqual(anon.get(url).status_code,302,url)
        for mes in ('2026-13','../','invalido'):
            self.assertEqual(self.client.get('/fiscal/notas?mes='+mes).status_code,400)
        self.assertEqual(self.client.get('/fiscal/notas/pacote.zip?mes=2026-11').status_code,404)

    def test_nova_emissao_arquiva_automaticamente_sem_alterar_saldos(self):
        venda=self.base.venda();antes=self.base.snapshot()
        def engine(payload,**kwargs):
            pasta=Path(payload['output_dir']);pasta.mkdir(parents=True,exist_ok=True)
            numero=payload['nNF'];x=pasta/'autorizada.xml';x.write_bytes(autorizada(numero=numero,data=payload['issue_datetime']))
            p=pasta/'danfe.pdf';p.write_bytes(b'%PDF-1.4\nteste')
            return dict(ok=True,authorized=True,cStat='100',chave=chave(numero=numero),protocolo='123456789012345',xml_path=str(x),danfe_path=str(p))
        with patch.object(self.cv,'cert_runtime_status',return_value={'exists':True,'has_password':True}),patch.object(self.cv,'run_fiscal_engine',side_effect=engine):
            result=self.cv.emitir_nfce_sefaz_internal(venda)
        self.assertTrue(result['authorized']);self.assertNotIn('archive_warning',result)
        d=self.cv.fetch_one('SELECT * FROM fiscal_documentos WHERE origem_id=?',(venda,))
        self.assertTrue(af.arquivo_documento(str(self.raiz),d,'xml'));self.assertTrue(af.arquivo_documento(str(self.raiz),d,'pdf'))
        self.assertEqual(antes,self.base.snapshot())

    def test_falha_de_arquivo_nao_rebaixa_nota_autorizada(self):
        venda=self.base.venda(status_fiscal='Autorizada SEFAZ',cstat='100')
        with patch.object(af,'sincronizar',side_effect=OSError('Disco indisponível')),self.assertLogs(self.cv.app.logger,level='ERROR'):
            r=self.cv.arquivar_documento_autorizado('Venda',venda,{'authorized':True})
        self.assertTrue(r['authorized']);self.assertIn('archive_warning',r)
        self.assertEqual(self.cv.fetch_one('SELECT nf_status FROM vendas WHERE id=?',(venda,))[0],'Autorizada SEFAZ')

    def test_emissao_devolucao_tambem_cria_arquivo_mensal(self):
        pasta=self.raiz/'originais';pasta.mkdir()
        original=pasta/'compra.xml';original.write_bytes(entrada())
        with self.cv.get_db() as db:
            dev=db.execute("INSERT INTO devolucoes(data,status,xml_original_path,baixar_estoque_solicitado) VALUES ('2026-10-01','Aguardando autorização SEFAZ',?,0)",(str(original),)).lastrowid
            db.execute("INSERT INTO devolucao_itens(devolucao_id,item_origem_idx,nome,quantidade_original,quantidade_devolver,valor_unitario,valor_total) VALUES (?,1,'Produto teste',3,1,45,45)",(dev,))
        def engine(payload,**kwargs):
            p=Path(payload['output_dir']);p.mkdir(parents=True,exist_ok=True)
            x=p/'procNFe.xml';x.write_bytes(autorizada(modelo='55',numero=payload['nNF'],data=payload['issue_datetime']))
            pdf=p/'danfe.pdf';pdf.write_bytes(b'%PDF-1.4\nteste')
            return dict(authorized=True,ok=True,cStat='100',chave=chave('55',payload['nNF']),protocolo='123456789012345',xml_path=str(x),danfe_path=str(pdf))
        with patch.object(self.cv,'cert_runtime_status',return_value={'exists':True,'has_password':True}),patch.object(self.cv,'run_fiscal_engine',side_effect=engine):r=self.cv.emitir_devolucao_sefaz_internal(dev)
        self.assertTrue(r['authorized']);self.assertNotIn('archive_warning',r)
        d=self.cv.fetch_one("SELECT * FROM fiscal_documentos WHERE origem='Devolução' AND origem_id=?",(dev,))
        self.assertEqual(d['modelo'],'55');self.assertTrue(d['mes'])
        self.assertTrue(af.arquivo_documento(str(self.raiz),d,'xml'))

    def test_faltas_de_data_sao_visiveis_e_exportaveis_para_conferencia(self):
        venda=self.base.venda(status_fiscal='Autorizada SEFAZ',cstat='100')
        self.cv.exec_sql('UPDATE vendas SET nf_emissao_tentativa_em=NULL,nf_autorizado_em=NULL WHERE id=?',(venda,))
        r=self.client.get('/fiscal/notas?mes=2026-11')
        self.assertIn(b'Data a conferir',r.data)
        r=self.client.get('/fiscal/notas/pacote.zip?mes=sem-data')
        self.assertEqual(r.status_code,200);self.assertIn('PARCIAL',r.headers['X-Arquivo-Nome']);r.close()


if __name__=='__main__':unittest.main()
