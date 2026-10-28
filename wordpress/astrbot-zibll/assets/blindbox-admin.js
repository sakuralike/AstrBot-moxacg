(() => {
  const list = document.getElementById('az-blindbox-prizes');
  const template = document.getElementById('az-blindbox-prize-template');
  const add = document.getElementById('az-blindbox-add');
  const total = document.getElementById('az-blindbox-total');
  if (!list || !template || !add || !total) return;

  const updateRow = (row) => {
    const type = row.querySelector('.az-blindbox-type');
    const points = row.querySelector('.az-blindbox-points');
    const text = row.querySelector('.az-blindbox-text');
    const isPoints = type && type.value === 'points';
    if (points) points.hidden = !isPoints;
    if (text) text.hidden = isPoints;
  };

  const updateTotal = () => {
    let sum = 0;
    list.querySelectorAll('input[name$="[probability]"]').forEach((field) => {
      const value = Number.parseFloat(field.value);
      if (Number.isFinite(value)) sum += value;
    });
    total.textContent = `${sum.toFixed(2)}%`;
    total.classList.toggle('az-blindbox-invalid', Math.abs(sum - 100) > 0.001);
  };

  list.addEventListener('change', (event) => {
    const row = event.target.closest('.az-blindbox-prize');
    if (row && event.target.matches('.az-blindbox-type')) updateRow(row);
    updateTotal();
  });
  list.addEventListener('input', updateTotal);
  list.addEventListener('click', (event) => {
    if (!event.target.matches('.az-blindbox-remove')) return;
    const row = event.target.closest('.az-blindbox-prize');
    if (row) row.remove();
    updateTotal();
  });
  add.addEventListener('click', () => {
    const index = `${Date.now()}_${Math.random().toString(16).slice(2)}`;
    const fragment = template.content.cloneNode(true);
    fragment.querySelectorAll('[name]').forEach((field) => {
      field.name = field.name.replaceAll('__INDEX__', index);
    });
    const row = fragment.querySelector('.az-blindbox-prize');
    if (row) updateRow(row);
    list.appendChild(fragment);
    updateTotal();
  });
  list.querySelectorAll('.az-blindbox-prize').forEach(updateRow);
  updateTotal();
})();
