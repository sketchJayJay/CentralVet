// Fluxos DOM reais do compartilhamento, sem abrir WhatsApp ou transmitir notas.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');
const root = path.resolve(__dirname, '..');
const script = fs.readFileSync(path.join(root, 'static/notas_emitidas.js'), 'utf8');
const html = `<button data-compartilhar="/fiscal/notas/pacote.zip?mes=2026-11" data-titulo="Notas 11/2026">Compartilhar</button>
<p id="arquivo-status"></p><dialog id="arquivo-compartilhar"><p id="arquivo-nome"></p><p id="arquivo-pendencias" hidden></p><p id="arquivo-instrucao"></p><p id="arquivo-dialogo-status"></p><button id="arquivo-enviar" hidden>Enviar</button><a id="arquivo-baixar"></a><a id="arquivo-whatsapp" hidden></a><button id="arquivo-fechar">Fechar</button></dialog>`;
const tick = () => new Promise(resolve => setImmediate(resolve));

function tela({nativo=true,pendencias=0,ok=true,cancelar=false,canShareFalha=false}={}) {
  const dom = new JSDOM(html,{url:'https://centralvet.example/fiscal/notas',runScripts:'outside-only'});
  const w = dom.window;const chamadas={fetch:[],share:[],revogadas:[]};
  w.URL.createObjectURL = () => 'blob:documento-teste';
  w.URL.revokeObjectURL = url => chamadas.revogadas.push(url);
  const dialog=w.document.querySelector('dialog');
  dialog.showModal = () => dialog.setAttribute('open','');
  dialog.close = () => {dialog.removeAttribute('open');dialog.dispatchEvent(new w.Event('close'));};
  w.fetch = async (...args) => {
    chamadas.fetch.push(args);
    return {ok,headers:{get:n=>({'content-type':ok?'application/zip':'text/html','X-Arquivo-Nome':pendencias?'CentralVet-Notas-2026-11-PARCIAL.zip':'CentralVet-Notas-2026-11.zip','X-Fiscal-Pendencias':String(pendencias)})[n]},blob:async()=>new w.Blob(['ZIP sintético'],{type:'application/zip'})};
  };
  if (nativo) {
    w.navigator.canShare = data => {if(canShareFalha)throw new Error('Arquivos bloqueados');return data.files[0].type==='application/zip';};
    w.navigator.share = async data => {chamadas.share.push(data);if(cancelar){const e=new Error('cancelado');e.name='AbortError';throw e;}};
  }
  w.eval(script);
  return {w,dom,dialog,chamadas,get:id=>w.document.getElementById(id),button:w.document.querySelector('[data-compartilhar]')};
}

(async () => {
  {
    const t=tela();t.button.click();await tick();
    assert.equal(t.chamadas.fetch.length,1);assert.equal(t.chamadas.share.length,0,'O preparo não deve tentar compartilhar sem novo toque.');
    assert.equal(t.dialog.hasAttribute('open'),true);assert.equal(t.get('arquivo-enviar').hidden,false);
    assert.equal(t.get('arquivo-whatsapp').hidden,true);
    t.get('arquivo-enviar').click();await tick();
    assert.equal(t.chamadas.share.length,1);assert.equal(t.chamadas.share[0].files[0].name,'CentralVet-Notas-2026-11.zip');
    assert.match(t.get('arquivo-dialogo-status').textContent,/aplicativo escolhido/);
    t.get('arquivo-fechar').click();assert.equal(t.chamadas.revogadas.length,1);t.dom.window.close();
  }
  {
    const t=tela({nativo:false});t.button.click();await tick();
    assert.equal(t.get('arquivo-enviar').hidden,true);assert.equal(t.get('arquivo-whatsapp').hidden,false);
    assert.match(t.get('arquivo-instrucao').textContent,/anexe o arquivo/);
    assert.match(t.get('arquivo-baixar').href,/^blob:/);assert.match(t.get('arquivo-whatsapp').href,/^https:\/\/wa\.me\/\?text=/);
    assert.equal(t.chamadas.share.length,0);t.dom.window.close();
  }
  {
    const t=tela({pendencias:2});t.button.click();await tick();
    assert.equal(t.get('arquivo-pendencias').hidden,false);assert.match(t.get('arquivo-pendencias').textContent,/2 arquivo/);
    assert.match(t.get('arquivo-baixar').download,/PARCIAL/);t.dom.window.close();
  }
  {
    const t=tela({ok:false});t.button.click();await tick();
    assert.match(t.get('arquivo-status').textContent,/Não foi possível/);assert.equal(t.button.disabled,false);
    assert.equal(t.dialog.hasAttribute('open'),false);t.dom.window.close();
  }
  {
    const t=tela({cancelar:true});t.button.click();await tick();t.get('arquivo-enviar').click();await tick();
    assert.match(t.get('arquivo-dialogo-status').textContent,/cancelado/);assert.equal(t.get('arquivo-enviar').disabled,false);t.dom.window.close();
  }
  {
    const t=tela({canShareFalha:true});t.button.click();await tick();
    assert.equal(t.get('arquivo-enviar').hidden,true);assert.equal(t.get('arquivo-whatsapp').hidden,false);
    assert.equal(t.dialog.hasAttribute('open'),true);t.dom.window.close();
  }
  console.log('6 fluxos de compartilhamento aprovados: envio nativo, download/WhatsApp, parcial, erro, cancelamento e compatibilidade.');
})().catch(e=>{console.error(e);process.exitCode=1;});
