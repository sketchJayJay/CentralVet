const MPTII_STORAGE = 'centralvet_mptii_usb_printer';
let centralvetMptiiPrinter = '';
let mptiiBusy = false;
try { centralvetMptiiPrinter = localStorage.getItem(MPTII_STORAGE) || ''; } catch (_) {}

function posStatus(msg, ok) {
  const el = document.getElementById('pos5890-status');
  if (el) { el.textContent = msg; el.style.color = ok === true ? '#087f5b' : ok === false ? '#b42318' : 'inherit'; }
}
function mostrarAjudaPOS() {
  const el = document.getElementById('pos5890-ajuda');
  if (el) el.style.display = 'block';
}
function selecionarMPTII(nome) {
  centralvetMptiiPrinter = nome;
  try { localStorage.setItem(MPTII_STORAGE, nome); } catch (_) {}
  posStatus(nome ? 'Selecionada: ' + nome + '. Use Imprimir teste para conferir.' : 'Selecione a impressora MPT-II na lista.');
}
async function qzConectar() {
  if (typeof qz === 'undefined') throw new Error('QZ Tray não carregou. Confira a internet e deixe o QZ Tray aberto no notebook.');
  if (!qz.websocket.isActive()) await qz.websocket.connect({ retries: 2, delay: 1 });
}
async function localizarMPTII() {
  await qzConectar();
  const printers = await qz.printers.find();
  if (!Array.isArray(printers) || !printers.length) throw new Error('Nenhuma impressora instalada no Windows. Instale o driver da MPT-II e confira o cabo USB.');
  // A preferência da antiga POS-5890U não é reutilizada na impressora nova.
  const matches = printers.filter(p => /mpt[\s_-]*(?:ii|2)\b|mobile[\s_-]*printer/i.test(p));
  if (!printers.includes(centralvetMptiiPrinter)) {
    centralvetMptiiPrinter = matches.length === 1 ? matches[0] : '';
    try { localStorage.setItem(MPTII_STORAGE, centralvetMptiiPrinter); } catch (_) {}
  }
  const select = document.getElementById('impressora-termica');
  if (select) {
    select.replaceChildren();
    const placeholder = document.createElement('option');
    placeholder.value = ''; placeholder.textContent = 'Selecione a fila da MPT-II / MobilePrinter';
    select.appendChild(placeholder);
    for (const name of printers) {
      const option = document.createElement('option');
      option.value = name; option.textContent = name; select.appendChild(option);
    }
    select.value = centralvetMptiiPrinter;
  }
  if (!centralvetMptiiPrinter) throw new Error('Selecione a fila da MPT-II na lista acima. Ela também pode aparecer como POS-58 ou POS58.');
  return centralvetMptiiPrinter;
}
async function conectarMPTII() {
  try {
    const name = await localizarMPTII();
    posStatus('Selecionada: ' + name + '. Clique em Imprimir teste para conferir.', true);
  } catch (e) { mostrarAjudaPOS(); posStatus(e.message || String(e), false); }
}
async function enviarMPTII(vendaId) {
  if (mptiiBusy) return;
  mptiiBusy = true;
  const buttons = ['btn-mptii', 'btn-teste-termica'].map(id => document.getElementById(id)).filter(Boolean);
  buttons.forEach(b => { b.disabled = true; });
  try {
    const printer = await localizarMPTII();
    let data;
    if (vendaId === null) {
      const test = '\x1b@\x1ba\x01CENTRALVET\nTESTE MPT-II USB\n\x1ba\x00' + '-'.repeat(32) + '\n12345678901234567890123456789012\nPapel 58mm - 32 colunas\nSe leu tudo, teste concluido.\n\n\n';
      data = btoa(test);
    } else {
      const resp = await fetch(`/api/vendas/${vendaId}/termica`, { credentials: 'same-origin', cache: 'no-store' });
      if (!resp.ok) throw new Error('Não foi possível obter o cupom. Confira se o login está ativo e tente novamente.');
      const payload = await resp.json();
      if (!payload.ok || !payload.escpos_base64) throw new Error(payload.erro || 'Cupom indisponível.');
      data = payload.escpos_base64;
    }
    const config = qz.configs.create(printer, { jobName: vendaId === null ? 'CENTRALVET Teste MPT-II' : `CENTRALVET Venda ${vendaId}`, copies: 1 });
    await qz.print(config, [{ type: 'raw', format: 'command', flavor: 'base64', data }]);
    posStatus('Enviado para ' + printer + '. Confira o papel na impressora.', true);
  } catch (e) { mostrarAjudaPOS(); posStatus(e.message || String(e), false); }
  finally { mptiiBusy = false; buttons.forEach(b => { b.disabled = false; }); }
}
function imprimirMPTII(vendaId) { return enviarMPTII(vendaId); }
function testarMPTII() { return enviarMPTII(null); }
window.addEventListener('load', () => {
  posStatus(centralvetMptiiPrinter ? 'Impressora salva neste notebook: ' + centralvetMptiiPrinter : 'Clique em Conectar / atualizar impressoras e selecione a MPT-II.');
});
