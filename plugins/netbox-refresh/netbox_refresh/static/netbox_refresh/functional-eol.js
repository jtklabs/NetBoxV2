(() => {
  const preview = document.getElementById("id_effective_end_of_life");
  const security = document.getElementById("id_end_of_security_support");
  const support = document.getElementById("id_end_of_support");
  if (!preview || !security || !support) return;

  function update() {
    // NetBox's date pickers use YYYY-MM-DD, which sorts chronologically.
    const dates = [security.value, support.value]
      .filter((value) => /^\d{4}-\d{2}-\d{2}$/.test(value))
      .sort();
    preview.value = dates[0] ?? "";
  }

  for (const field of [security, support]) {
    field.addEventListener("input", update);
    field.addEventListener("change", update);
  }
  update();
})();
