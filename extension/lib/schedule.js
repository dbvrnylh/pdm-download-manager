// Plage horaire des téléchargements programmés : même règle que in_window()
// dans engine/pdm_engine/engine.py. Elle peut passer minuit (23:00 → 06:00).

export function inWindow(date, start, end) {
  const minutes = date.getHours() * 60 + date.getMinutes();
  const [first, last] = [start, end].map((time) => {
    const [hours, mins] = time.split(':').map(Number);
    return hours * 60 + mins;
  });
  return first < last ? minutes >= first && minutes < last : minutes >= first || minutes < last;
}
