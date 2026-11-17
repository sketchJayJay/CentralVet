(function () {
  const selecao = document.getElementById('nfceSelecao');
  if (selecao) {
    const boxes = Array.from(selecao.querySelectorAll('[name="venda_id"]'));
    const todas = document.getElementById('nfceTodas');
    function atualizar() {
      const qtd = boxes.filter(function (b) { return b.checked; }).length;
      todas.checked = boxes.length > 0 && qtd === boxes.length;
      todas.indeterminate = qtd > 0 && qtd < boxes.length;
      document.getElementById('nfceSelecionadas').textContent = qtd + ' venda(s) selecionada(s)';
      selecao.querySelectorAll('button[type="submit"]').forEach(function (b) { b.disabled = qtd === 0 || qtd > 5000; });
      document.getElementById('nfceSelecaoAjuda').textContent = qtd > 5000 ? 'Selecione até 5.000 vendas por lote.' : 'A data original das vendas e os recebimentos são preservados.';
    }
    todas.addEventListener('change', function () { boxes.forEach(function (b) { b.checked = todas.checked; }); atualizar(); });
    boxes.forEach(function (b) { b.addEventListener('change', atualizar); });
    selecao.addEventListener('submit', function (event) {
      const qtd = boxes.filter(function (b) { return b.checked; }).length;
      const emitir = event.submitter && event.submitter.dataset.operacao === 'emitir';
      const pergunta = emitir ? 'Emitir uma NFC-e para cada uma das ' + qtd + ' vendas selecionadas, com data e hora de hoje? As notas serão enviadas à SEFAZ.' : 'Preparar a data fiscal de hoje para as ' + qtd + ' vendas selecionadas?';
      if (!qtd || qtd > 5000 || !window.confirm(pergunta)) event.preventDefault();
    });
    atualizar();
  }
  const painel = document.getElementById('nfceExecucao');
  if (!painel) return;
  const continuar = document.getElementById('nfceContinuar');
  const pausar = document.getElementById('nfcePausar');
  let rodando = false, parar = false;
  function mostrar(dados) {
    document.getElementById('nfceEstado').textContent = dados.estado;
    const c = dados.contagens || {};
    document.getElementById('nfceContagem').textContent = (c.Autorizada || 0) + ' autorizada(s), ' + (c.Rejeitada || 0) + ' rejeitada(s), ' + (c['Falha local'] || 0) + ' falha(s) local(is), ' + (c.Incerta || 0) + ' retorno(s) incerto(s). Total: ' + dados.total + '.';
    document.getElementById('nfceProgresso').value = dados.total - (c.Pendente || 0) - (c.Enviando || 0);
    if (dados.aviso) document.getElementById('nfceAviso').textContent = dados.aviso;
    const item = dados.item;
    if (!item) return;
    const row = document.getElementById('nfceItem-' + item.id);
    if (!row) return;
    row.querySelector('[data-numero]').textContent = item.nf_numero || '—';
    row.querySelector('[data-estado]').textContent = item.estado;
    const retorno = row.querySelector('[data-retorno]');
    retorno.textContent = (item.motivo || '') + (item.cstat ? ' · cStat ' + item.cstat : '');
    if (item.estado === 'Incerta' && item.tentativa_id) {
      const form = document.createElement('form');
      form.method = 'post';
      form.action = painel.dataset.consulta.replace('/0/', '/' + item.tentativa_id + '/');
      const token = document.createElement('input');
      token.type = 'hidden'; token.name = 'csrf_token'; token.value = painel.dataset.csrf;
      const button = document.createElement('button');
      button.className = 'secondary'; button.textContent = 'Consultar situação na SEFAZ';
      form.append(token, button); retorno.append(form);
    }
  }
  async function iniciar() {
    if (rodando) return;
    rodando = true; parar = false; continuar.disabled = true; pausar.disabled = false;
    let concluido = false;
    try {
      while (!parar) {
        document.getElementById('nfceAviso').textContent = 'Processando a próxima venda. Aguarde o retorno da SEFAZ.';
        const resposta = await fetch(painel.dataset.passo, {method: 'POST', headers: {'X-CSRF-Token': painel.dataset.csrf, 'Accept': 'application/json'}, credentials: 'same-origin'});
        if (!resposta.ok || !(resposta.headers.get('content-type') || '').includes('application/json')) throw new Error('A conexão ou a sessão foi interrompida. Atualize a lista para conferir o resultado antes de continuar.');
        const dados = await resposta.json(); mostrar(dados);
        if (dados.estado === 'Concluído') { concluido = true; document.getElementById('nfceAviso').textContent = 'Processamento concluído. Confira o resultado de cada venda abaixo.'; break; }
        if (dados.estado === 'Interrompido' || dados.aviso) { document.getElementById('nfceAviso').textContent = dados.aviso || 'O lote foi pausado. Confira o retorno da venda; se houver horário divergente, confira o relógio do servidor. Retornos incertos devem ser consultados antes de continuar.'; break; }
        await new Promise(function (resolve) { setTimeout(resolve, dados.ocupado ? 2000 : 400); });
      }
      if (parar) document.getElementById('nfceAviso').textContent = 'Pausado após o retorno da última nota. O progresso está salvo.';
    } catch (erro) {
      document.getElementById('nfceAviso').textContent = erro.message;
    } finally {
      rodando = false; continuar.disabled = concluido; pausar.disabled = true;
    }
  }
  continuar.addEventListener('click', iniciar);
  pausar.addEventListener('click', function () { parar = true; pausar.disabled = true; });
  if (painel.dataset.iniciar === '1') iniciar();
}());
