from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import test_pagamento_fiscal as fixtures
import nfce_emissao as ne


class NfceLoteTests(unittest.TestCase):
    def setUp(self):
        self.base=fixtures.PagamentoFiscalTests();self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.cv=fixtures.cv;self.client=self.base.client
        p=patch.object(self.cv,'FISCAL_DIR',self.base.temp.name);p.start();self.addCleanup(p.stop)
        p=patch.object(self.cv,'cert_runtime_status',return_value={'exists':True,'has_password':True});p.start();self.addCleanup(p.stop)
        self.client.get('/vendas/nfce-em-lote')
        with self.client.session_transaction() as s:self.token=s['nfce_csrf']

    def venda(self,cstat='704',status='Rejeitada SEFAZ'):
        return self.base.venda(cstat=cstat,status_fiscal=status)

    def registro(self,venda):
        return dict(self.cv.fetch_one('SELECT * FROM vendas WHERE id=?',(venda,)))

    def post(self,url,ids):
        return self.client.post(url,data={'csrf_token':self.token,'venda_id':[str(v) for v in ids]})

    def criar(self,ids):
        r=self.post('/vendas/nfce-em-lote/criar',ids)
        self.assertEqual(r.status_code,302)
        self.assertNotEqual(r.location,'/vendas/nfce-em-lote')
        return r.location

    def passo(self,url):
        r=self.client.post(url+'/passo',headers={'X-CSRF-Token':self.token})
        self.assertEqual(r.status_code,200)
        return r.get_json()

    def aprovar(self,payload,**kwargs):
        return {'ok':True,'authorized':True,'cStat':'100','xMotivo':'Autorização de teste',
            'chave':str(payload.get('nNF',87)).zfill(44),'protocolo':'123456789',
            'transmission_started':True}

    def test_datas_hoje_preservam_financeiro_e_protegem_autorizadas_incertas(self):
        a,b=self.venda('703'),self.venda('704')
        auth=self.venda('100','Autorizada SEFAZ')
        incerta=self.venda('','Falha antes do envio à SEFAZ')
        orig={v:self.registro(v) for v in (a,b,auth,incerta)}
        before=self.base.snapshot()
        with patch.object(ne,'agora',return_value='2026-11-17T22:30:00-03:00'):
            alteradas,ignoradas=ne.atualizar_datas(self.cv.get_db,[a,b,auth,incerta],'Teste')
        self.assertEqual(alteradas,[a,b]);self.assertEqual(len(ignoradas),2)
        for v in (a,b):
            atual=self.registro(v)
            self.assertEqual(atual['nf_emissao_tentativa_em'],'2026-11-17T22:30:00-03:00')
            for k in orig[v].keys()-{'nf_emissao_tentativa_em'}:self.assertEqual(orig[v][k],atual[k],k)
        for v in (auth,incerta):self.assertEqual(orig[v],self.registro(v))
        self.assertEqual(before,self.base.snapshot())
        auditoria=self.cv.fetch_one('SELECT * FROM nfce_tentativas WHERE venda_id=?',(a,))
        self.assertEqual(auditoria['emissao_anterior_em'],orig[a]['nf_emissao_tentativa_em'])

    def test_emissao_usa_hora_fresca_mesmo_se_a_data_foi_preparada_antes(self):
        venda=self.venda()
        with patch.object(ne,'agora',return_value='2026-11-17T08:00:00-03:00'):
            ne.atualizar_datas(self.cv.get_db,[venda])
        with patch.object(ne,'agora',return_value='2026-11-17T23:59:30-03:00'), \
             patch.object(self.cv,'run_fiscal_engine',side_effect=self.aprovar) as engine:
            result=self.cv.emitir_nfce_sefaz_internal(venda)
        self.assertTrue(result['authorized'])
        self.assertEqual(engine.call_args.args[0]['issue_datetime'],'2026-11-17T23:59:30-03:00')
        self.assertEqual(self.registro(venda)['data'],'2026-10-01')

    def test_lote_emite_uma_por_venda_sem_colisao_nem_duplicar_saldos(self):
        ids=[self.venda() for _ in range(3)]
        before=self.base.snapshot()
        with patch.object(self.cv,'run_fiscal_engine',side_effect=self.aprovar) as engine:
            url=self.criar(ids)
            self.assertEqual(engine.call_count,0)
            primeira=self.client.get(url)
            self.assertIn(b'data-iniciar="1"',primeira.data)
            self.assertIn(b'data-iniciar="0"',self.client.get(url).data)
            numeros=[]
            for i in range(3):
                now=f'2026-11-{17+i:02d}T00:00:01-03:00'
                with patch.object(ne,'agora',return_value=now):
                    dados=self.passo(url)
                self.assertEqual(dados['contagens']['Autorizada'],i+1)
                envio=engine.call_args.args[0]
                self.assertEqual(envio['issue_datetime'],now)
                numeros.append(envio['nNF'])
            self.assertEqual(len(set(numeros)),3)
            self.assertEqual(self.passo(url)['estado'],'Concluído')
            self.assertEqual(engine.call_count,3)
            for v in ids:self.assertTrue(self.cv.emitir_nfce_sefaz_internal(v)['authorized'])
            self.assertEqual(engine.call_count,3)
        self.assertEqual(before,self.base.snapshot())
        for v in ids:self.assertEqual(self.registro(v)['data'],'2026-10-01')

    def test_csrf_login_e_get_nao_iniciam_emissao(self):
        venda=self.venda()
        for rota in ('/vendas/nfce-em-lote/criar','/vendas/nfce-em-lote/datas-hoje'):
            self.assertEqual(self.client.post(rota,data={'venda_id':str(venda)}).status_code,403)
        with patch.object(self.cv,'run_fiscal_engine') as engine:
            self.client.get('/vendas/nfce-em-lote')
            self.assertEqual(engine.call_count,0)
            url=self.criar([venda])
            self.client.get(url);self.client.get(url)
            self.assertEqual(engine.call_count,0)
        anonimo=self.cv.app.test_client()
        self.assertEqual(anonimo.get('/vendas/nfce-em-lote').status_code,302)
        self.assertEqual(self.client.get(url+'/passo').status_code,405)

    def test_duplo_clique_reutiliza_lote_e_lotes_sobrepostos_sao_bloqueados(self):
        a,b=self.venda(),self.venda()
        url=self.criar([a,b])
        self.assertEqual(self.criar([b,a,a]),url)
        r=self.post('/vendas/nfce-em-lote/criar',[a])
        self.assertEqual(r.location,'/vendas/nfce-em-lote')
        self.assertEqual(self.cv.fetch_one('SELECT COUNT(*) FROM nfce_lotes')[0],1)

    def test_timeout_pausa_lote_e_bloqueia_nova_data(self):
        a,b=self.venda(),self.venda()
        url=self.criar([a,b])
        resultado={'ok':False,'authorized':False,'error':'Tempo limite','transmission_started':True,'chave':'1'*44}
        with patch.object(self.cv,'run_fiscal_engine',return_value=resultado) as engine:
            dados=self.passo(url)
            self.assertEqual(dados['estado'],'Interrompido')
            self.assertEqual(dados['contagens']['Incerta'],1)
            anterior=self.registro(a)['nf_emissao_tentativa_em']
            self.passo(url)
            self.assertEqual(engine.call_count,1)
            alteradas,ignoradas=ne.atualizar_datas(self.cv.get_db,[a])
            self.assertEqual(alteradas,[]);self.assertEqual(len(ignoradas),1)
            self.assertEqual(self.registro(a)['nf_emissao_tentativa_em'],anterior)

    def test_consulta_recupera_autorizacao_sem_emitir_outra_nota(self):
        a,b=self.venda(),self.venda()
        url=self.criar([a,b]);chave='1'*44
        def engine(payload,**kwargs):
            if payload['action']=='consult_nfce':return {'ok':True,'authorized':True,'cStat':'100','chave':chave,'protocolo':'123'}
            pasta=Path(payload['output_dir']);arquivo=pasta/'assinado.xml';arquivo.write_text('<NFe/>')
            return {'error':'Timeout','transmission_started':True,'chave':chave,'signed_xml_path':str(arquivo)}
        with patch.object(self.cv,'run_fiscal_engine',side_effect=engine) as rpc:
            dados=self.passo(url);tid=dados['item']['tentativa_id']
            self.client.post(f'/vendas/nfce-tentativas/{tid}/consultar',data={'csrf_token':self.token})
            self.assertEqual([c.args[0]['action'] for c in rpc.call_args_list],['emit_nfce','consult_nfce'])
            self.assertEqual(self.registro(a)['nf_status'],'Autorizada SEFAZ')
            self.assertEqual(self.client.get(url+'/status').get_json()['estado'],'Pronto')
        with patch.object(self.cv,'run_fiscal_engine',side_effect=self.aprovar) as rpc:
            self.passo(url);self.assertEqual(rpc.call_count,1)

    def test_consulta_nao_encontrada_ou_chave_divergente_nao_libera_reenvio(self):
        a=self.venda();url=self.criar([a]);chave='1'*44
        def engine(payload,**kwargs):
            arquivo=Path(payload['output_dir'])/'assinado.xml';arquivo.write_text('<NFe/>')
            return {'error':'Timeout','transmission_started':True,'chave':chave,'signed_xml_path':str(arquivo)}
        with patch.object(self.cv,'run_fiscal_engine',side_effect=engine):dados=self.passo(url)
        tid=dados['item']['tentativa_id'];anterior=self.registro(a)
        for resposta in ({'authorized':False,'cStat':'217','xMotivo':'Não encontrada'},
                         {'authorized':True,'cStat':'100','chave':'2'*44,'protocolo':'123'}):
            with patch.object(self.cv,'run_fiscal_engine',return_value=resposta):self.cv.consultar_nfce_tentativa(tid)
            self.assertEqual(self.registro(a),anterior)
        self.assertIsNone(self.cv.fetch_one('SELECT concluida_em FROM nfce_tentativas WHERE id=?',(tid,))[0])

    def test_falha_local_nao_impede_as_outras_vendas(self):
        a,b=self.venda(),self.venda();url=self.criar([a,b])
        with patch.object(self.cv,'run_fiscal_engine',side_effect=[{'error':'XML inválido antes do envio','transmission_started':False},self.aprovar({'nNF':99})]):
            self.assertEqual(self.passo(url)['contagens']['Falha local'],1)
            self.assertEqual(self.passo(url)['contagens']['Autorizada'],1)
        self.assertEqual(self.registro(a)['nf_status'],'Falha local')
        ne.atualizar_datas(self.cv.get_db,[a])

    def test_rejeicao_de_horario_atual_pausa_para_conferir_relogio(self):
        url=self.criar([self.venda(),self.venda()])
        with patch.object(self.cv,'run_fiscal_engine',return_value={'cStat':'703','xMotivo':'Horário futuro','definitive_rejection':True,'transmission_started':True}) as rpc:
            dados=self.passo(url)
        self.assertEqual(rpc.call_count,1)
        self.assertEqual(dados['estado'],'Interrompido')
        self.assertEqual(dados['contagens']['Pendente'],1)

    def test_concorrencia_mesma_venda_tem_so_uma_tentativa_aberta(self):
        venda=self.venda();bloqueados=[]
        def engine(payload,**kwargs):
            bloqueados.append(self.cv.emitir_nfce_sefaz_internal(venda))
            return self.aprovar(payload)
        with patch.object(self.cv,'run_fiscal_engine',side_effect=engine) as rpc:
            self.assertTrue(self.cv.emitir_nfce_sefaz_internal(venda)['authorized'])
        self.assertEqual(rpc.call_count,1)
        self.assertEqual(bloqueados[0]['state'],'Bloqueada')

    def test_concorrencia_vendas_diferentes_reserva_numeros_distintos(self):
        ids=[self.venda(),self.venda()];barreira=threading.Barrier(2)
        def engine(payload,**kwargs):
            barreira.wait(timeout=5)
            return self.aprovar(payload)
        with patch.object(self.cv,'run_fiscal_engine',side_effect=engine):
            with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(self.cv.emitir_nfce_sefaz_internal,ids))
        self.assertTrue(all(r['authorized'] for r in results))
        self.assertNotEqual(self.registro(ids[0])['nf_numero'],self.registro(ids[1])['nf_numero'])

    def test_diario_recupera_resultado_apos_interrupcao_sem_nova_transmissao(self):
        venda=self.venda()
        def engine(payload,**kwargs):
            (Path(payload['output_dir'])/'transmissao.json').write_text(json.dumps(self.aprovar(payload)))
            raise RuntimeError('Processo interrompido após a autorização')
        with patch.object(self.cv,'run_fiscal_engine',side_effect=engine):r=self.cv.emitir_nfce_sefaz_internal(venda)
        self.assertEqual(r['state'],'Incerta')
        with patch.object(self.cv,'run_fiscal_engine') as rpc:r=self.cv.consultar_nfce_tentativa(r['tentativa_id'])
        self.assertTrue(r['authorized']);self.assertEqual(rpc.call_count,0)

    def test_retomada_apos_reiniciar_nao_repete_venda_ja_emitida(self):
        a,b=self.venda(),self.venda();url=self.criar([a,b])
        with patch.object(self.cv,'run_fiscal_engine',side_effect=self.aprovar):self.passo(url)
        self.cv.init_db()
        with patch.object(self.cv,'run_fiscal_engine',side_effect=self.aprovar) as rpc:self.passo(url)
        self.assertEqual(rpc.call_count,1)
        self.assertEqual(self.client.get(url+'/status').get_json()['contagens']['Autorizada'],2)

    def test_etapa_ocupada_e_recuperacao_de_resultado_salvo_apos_queda(self):
        a,b=self.venda(),self.venda();url=self.criar([a,b])
        with self.cv.get_db() as db:
            v=db.execute('SELECT * FROM vendas WHERE id=?',(a,)).fetchone()
            itens=db.execute(ne.ITENS_SQL,(a,)).fetchall()
            lote=db.execute('SELECT id FROM nfce_lotes ORDER BY id DESC LIMIT 1').fetchone()[0]
        t=ne.reservar(self.cv.get_db,a,ne.assinatura(v,itens),self.cv.FISCAL_NFCE_DIR,lote)
        with self.cv.get_db() as db:
            db.execute("UPDATE nfce_lote_itens SET estado='Enviando',iniciada_em=? WHERE lote_id=? AND venda_id=?",(ne.agora(),lote,a))
        with patch.object(self.cv,'run_fiscal_engine') as rpc:
            self.assertTrue(self.passo(url)['ocupado']);self.assertEqual(rpc.call_count,0)
        pasta=Path(t['diretorio']);pasta.mkdir(parents=True)
        (pasta/'transmissao.json').write_text(json.dumps(self.aprovar({'nNF':t['numero']})))
        antiga=(datetime.fromisoformat(ne.agora())-timedelta(minutes=5)).isoformat()
        self.cv.exec_sql('UPDATE nfce_lote_itens SET iniciada_em=? WHERE lote_id=? AND venda_id=?',(antiga,lote,a))
        with patch.object(self.cv,'run_fiscal_engine') as rpc:
            dados=self.passo(url)
            self.assertEqual(dados['contagens']['Autorizada'],1);self.assertEqual(rpc.call_count,0)
        with patch.object(self.cv,'run_fiscal_engine',side_effect=self.aprovar) as rpc:self.passo(url)
        self.assertEqual(rpc.call_count,1)

    def test_venda_autorizada_fora_do_lote_e_pulada_sem_reemitir(self):
        a,b=self.venda(),self.venda();url=self.criar([a,b])
        self.cv.exec_sql("UPDATE vendas SET nf_status='Autorizada SEFAZ',nf_protocolo='123' WHERE id=?",(a,))
        with patch.object(self.cv,'run_fiscal_engine',side_effect=self.aprovar) as rpc:
            self.assertEqual(self.passo(url)['contagens']['Pulada'],1)
            self.passo(url)
            self.assertEqual(rpc.call_count,1)

    def test_resultado_tardio_nao_rebaixa_autorizacao_ja_confirmada(self):
        venda=self.venda()
        with patch.object(self.cv,'run_fiscal_engine',side_effect=self.aprovar):r=self.cv.emitir_nfce_sefaz_internal(venda)
        t=dict(self.cv.fetch_one('SELECT * FROM nfce_tentativas WHERE id=?',(r['tentativa_id'],)))
        anterior=self.registro(venda)
        recuperado=ne.finalizar(self.cv.get_db,t,{'authorized':False,'error':'Retorno tardio indisponível'})
        self.assertTrue(recuperado['authorized'])
        self.assertEqual(self.registro(venda),anterior)


if __name__=='__main__':unittest.main()
