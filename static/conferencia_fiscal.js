(function () {
  const form = document.getElementById('conferenciaSelecao');
  if (!form) return;
  const todas = document.getElementById('confTodas');
  const boxes = Array.from(form.querySelectorAll('input[name="venda_id"]'));
  const moeda = new Intl.NumberFormat('pt-BR', {style: 'currency', currency: 'BRL'});
  function atualizar() {
    const selecionadas = boxes.filter(function (box) { return box.checked; });
    const totais = {};
    let total = 0;
    selecionadas.forEach(function (box) {
      const valor = Number(box.dataset.centavos);
      total += valor;
      totais[box.dataset.forma] = (totais[box.dataset.forma] || 0) + valor;
    });
    document.getElementById('confTotal').textContent = moeda.format(total / 100);
    document.getElementById('confQuantidade').textContent = selecionadas.length + (selecionadas.length === 1 ? ' venda' : ' vendas');
    form.querySelectorAll('[data-total-forma]').forEach(function (item) {
      item.textContent = moeda.format((totais[item.dataset.totalForma] || 0) / 100);
    });
    todas.checked = boxes.length > 0 && selecionadas.length === boxes.length;
    todas.indeterminate = selecionadas.length > 0 && selecionadas.length < boxes.length;
    document.getElementById('confPreparar').disabled = selecionadas.length === 0 || selecionadas.length > 5000;
    document.getElementById('confAjuda').textContent = selecionadas.length > 5000 ? 'Selecione até 5.000 vendas por resumo.' : 'Confira a seleção antes de preparar.';
  }
  todas.addEventListener('change', function () {
    boxes.forEach(function (box) { box.checked = todas.checked; });
    atualizar();
  });
  boxes.forEach(function (box) { box.addEventListener('change', atualizar); });
  atualizar();
}());
