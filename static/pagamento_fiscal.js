(function () {
  const formaSelect = document.getElementById('vendaForma');
  const sections = document.querySelectorAll('.fiscal-payment-fields');
  function atualizar() {
    sections.forEach(function (section) {
      const forma = formaSelect ? formaSelect.value : section.dataset.forma;
      const ativo = ['Cartão crédito', 'Cartão débito', 'Pix'].includes(forma);
      const resumo = section.querySelector('summary .cell-sub');
      if (resumo) resumo.textContent = forma;
      section.hidden = !ativo;
      section.querySelectorAll('input, select').forEach(function (input) {
        input.disabled = !ativo;
      });
      const bandeira = section.querySelector('[data-card-brand]');
      if (bandeira) {
        bandeira.hidden = forma === 'Pix';
        bandeira.querySelector('select').disabled = !ativo || forma === 'Pix';
      }
    });
  }
  if (formaSelect) formaSelect.addEventListener('change', atualizar);
  atualizar();
}());
