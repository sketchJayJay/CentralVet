(() => {
  'use strict';
  const dialog = document.getElementById('arquivo-compartilhar');
  if (!dialog) return;
  const byId = id => document.getElementById(id);
  const status = byId('arquivo-status'), localStatus = byId('arquivo-dialogo-status');
  const enviar = byId('arquivo-enviar'), baixar = byId('arquivo-baixar'), whatsapp = byId('arquivo-whatsapp');
  let arquivo = null, objectUrl = null, titulo = '', preparando = false;
  function limpar() {
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    objectUrl = null; arquivo = null;
  }
  byId('arquivo-fechar').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', limpar);
  document.querySelectorAll('[data-compartilhar]').forEach(button => {
    button.addEventListener('click', async () => {
      if (preparando) return;
      preparando = true;
      button.disabled = true; status.textContent = 'Preparando os documentos…';
      try {
        const response = await fetch(button.dataset.compartilhar, {credentials: 'same-origin', headers: {'Accept':'application/zip'}});
        if (!response.ok || !response.headers.get('content-type')?.includes('application/zip')) throw new Error('Não foi possível preparar os documentos. Atualize a página e confira as notas.');
        const blob = await response.blob();
        limpar();
        const nome = response.headers.get('X-Arquivo-Nome') || 'CentralVet-Notas.zip';
        titulo = button.dataset.titulo || 'Documentos fiscais CentralVet';
        arquivo = new File([blob], nome, {type:'application/zip'});
        objectUrl = URL.createObjectURL(arquivo);
        baixar.href = objectUrl; baixar.download = nome;
        const n = Number(response.headers.get('X-Fiscal-Pendencias') || 0);
        byId('arquivo-nome').textContent = nome;
        byId('arquivo-pendencias').hidden = !n;
        byId('arquivo-pendencias').textContent = n ? `Pacote parcial: ${n} arquivo(s) indisponível(is). Confira PENDENCIAS.txt dentro do ZIP.` : '';
        let nativo = false;
        try { nativo = typeof navigator.share === 'function' && typeof navigator.canShare === 'function' && navigator.canShare({files:[arquivo]}); } catch (_) { /* O download continua disponível. */ }
        enviar.hidden = !nativo; whatsapp.hidden = nativo;
        whatsapp.href = 'https://wa.me/?text=' + encodeURIComponent(titulo + '. Vou anexar o arquivo ' + nome + '.');
        byId('arquivo-instrucao').textContent = nativo
          ? 'Toque no botão abaixo e escolha o WhatsApp ou outro aplicativo para anexar o ZIP.'
          : 'Baixe o ZIP, abra o WhatsApp e anexe o arquivo à conversa. O botão Abrir WhatsApp prepara apenas a mensagem.';
        localStatus.textContent = ''; status.textContent = '';
        dialog.showModal();
      } catch (error) { status.textContent = error.message || 'Não foi possível preparar o arquivo.'; }
      finally { button.disabled = false; preparando = false; }
    });
  });
  enviar.addEventListener('click', async () => {
    if (!arquivo) return;
    enviar.disabled = true; localStatus.textContent = '';
    try {
      await navigator.share({files:[arquivo],title:titulo,text:titulo});
      localStatus.textContent = 'Arquivo entregue ao aplicativo escolhido.';
    } catch (error) {
      localStatus.textContent = error.name === 'AbortError' ? 'Compartilhamento cancelado. Você pode tentar novamente ou baixar o arquivo.' : 'O aplicativo não recebeu o arquivo. Use Baixar arquivo e anexe o ZIP manualmente.';
    } finally { enviar.disabled = false; }
  });
})();
